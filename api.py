import os
import uuid
import torch
import open_clip
import psycopg2
from PIL import Image
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

app = FastAPI(title="校園失物招領 API")

# 開放跨來源請求(之後 LIFF 前端要呼叫這支 API 會需要)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- 載入 CLIP 模型(伺服器啟動時載入一次,不要每次請求都重載) ----------
device = "cuda" if torch.cuda.is_available() else "cpu"
model, _, preprocess = open_clip.create_model_and_transforms(
    "ViT-B-32", pretrained="openai"
)
model.eval().to(device)

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


def get_image_embedding(image_path: str) -> list[float]:
    image = preprocess(Image.open(image_path).convert("RGB")).unsqueeze(0).to(device)
    with torch.no_grad():
        embedding = model.encode_image(image)
        embedding /= embedding.norm(dim=-1, keepdim=True)
    return embedding.squeeze().cpu().numpy().tolist()


def get_connection():
    return psycopg2.connect(os.getenv("DB_CONNECTION_STRING"))


# ---------- API 1:上傳物品(拾獲通報 or 遺失協尋)----------
# 新提交的物品一律先進入「待審核」狀態,不會出現在配對結果裡,
# 要等後台審核通過(status 改成 open)才會開放搜尋比對。
@app.post("/items")
async def create_item(
    file: UploadFile = File(...),
    type: str = Form(...),        # "found" 或 "lost"
    category: str = Form(...),
    location: str = Form(...),
    description: str = Form(""),
):
    # 存檔到本機
    ext = file.filename.split(".")[-1]
    saved_filename = f"{uuid.uuid4()}.{ext}"
    saved_path = os.path.join(UPLOAD_DIR, saved_filename)
    with open(saved_path, "wb") as f:
        f.write(await file.read())

    # 算 embedding
    embedding = get_image_embedding(saved_path)

    # 寫入資料庫,狀態固定寫 pending(待審核)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO items (type, image_url, embedding, category, location, description, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id;
        """,
        (type, saved_path, embedding, category, location, description, "pending"),
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()

    return {"id": new_id, "status": "pending", "message": "上傳成功,待後台審核後才會公開"}


# ---------- API 2:查詢某物品的相似配對(只比對已審核通過的 open 物品)----------
@app.get("/items/{item_id}/matches")
def get_matches(item_id: str, top_n: int = 3):
    conn = get_connection()
    cur = conn.cursor()

    # 先查這筆物品本身的 embedding 和 type
    cur.execute("SELECT embedding, type FROM items WHERE id = %s;", (item_id,))
    row = cur.fetchone()
    if row is None:
        cur.close()
        conn.close()
        return {"error": "找不到這筆物品"}

    embedding, item_type = row
    opposite_type = "found" if item_type == "lost" else "lost"

    # 查對向池裡最相似的,只找 status = open(已審核通過)的物品
    cur.execute(
        """
        SELECT id, image_url, category, location, description,
               1 - (embedding <=> %s::vector) AS similarity
        FROM items
        WHERE type = %s AND status = 'open' AND id != %s
        ORDER BY embedding <=> %s::vector
        LIMIT %s;
        """,
        (embedding, opposite_type, item_id, embedding, top_n),
    )
    results = cur.fetchall()
    cur.close()
    conn.close()

    matches = [
        {
            "id": str(r[0]),
            "image_url": r[1],
            "category": r[2],
            "location": r[3],
            "description": r[4],
            "similarity": round(r[5], 4),
        }
        for r in results
    ]
    return {"matches": matches}


# ---------- API 3:更新物品狀態(標記已配對/已結案)----------
@app.patch("/items/{item_id}")
def update_status(item_id: str, status: str = Form(...)):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE items SET status = %s WHERE id = %s;", (status, item_id))
    conn.commit()
    cur.close()
    conn.close()
    return {"message": "狀態已更新", "id": item_id, "status": status}


# ---------- API 4:列出所有物品(給後台管理用,可用 status/type 篩選)----------
@app.get("/items")
def list_items(status: str = None, type: str = None):
    conn = get_connection()
    cur = conn.cursor()

    query = "SELECT id, type, image_url, category, location, description, status, created_at FROM items WHERE 1=1"
    params = []

    if status:
        query += " AND status = %s"
        params.append(status)
    if type:
        query += " AND type = %s"
        params.append(type)

    query += " ORDER BY created_at DESC"

    cur.execute(query, params)
    results = cur.fetchall()
    cur.close()
    conn.close()

    items = [
        {
            "id": str(r[0]),
            "type": r[1],
            "image_url": r[2],
            "category": r[3],
            "location": r[4],
            "description": r[5],
            "status": r[6],
            "created_at": str(r[7]),
        }
        for r in results
    ]
    return {"items": items}


# ---------- API 5:審核通過,讓物品正式公開參與配對(pending -> open) ----------
@app.post("/items/{item_id}/approve")
def approve_item(item_id: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE items SET status = 'open' WHERE id = %s AND status = 'pending';",
        (item_id,),
    )
    if cur.rowcount == 0:
        cur.close()
        conn.close()
        return {"error": "找不到待審核的物品,或此物品已審核過"}
    conn.commit()
    cur.close()
    conn.close()
    return {"message": "審核通過,已公開", "id": item_id, "status": "open"}


# ---------- API 6:審核拒絕(pending -> rejected) ----------
@app.post("/items/{item_id}/reject")
def reject_item(item_id: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE items SET status = 'rejected' WHERE id = %s AND status = 'pending';",
        (item_id,),
    )
    if cur.rowcount == 0:
        cur.close()
        conn.close()
        return {"error": "找不到待審核的物品,或此物品已審核過"}
    conn.commit()
    cur.close()
    conn.close()
    return {"message": "已拒絕", "id": item_id, "status": "rejected"}