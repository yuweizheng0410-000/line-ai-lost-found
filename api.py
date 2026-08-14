import os
import uuid
import torch
import open_clip
import psycopg2
from PIL import Image
from io import BytesIO
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client

load_dotenv()

app = FastAPI(title="校園失物招領 API")

# 開放跨來源請求(前端頁面要呼叫這支 API 會需要)
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

# ---------- 連線 Supabase Storage(照片存這裡,不落地存本機) ----------
supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY"),
)
BUCKET_NAME = "item-photos"

# ---------- 比對邏輯的門檻設定 ----------
SAME_CATEGORY_THRESHOLD = 0.75   # 同分類比對時,只要達到這個分數就算有效配對
CROSS_CATEGORY_THRESHOLD = 0.88  # 跨分類比對時,要更高分才算數(因為誤判風險較高)


def get_image_embedding_from_bytes(image_bytes: bytes) -> list[float]:
    image = preprocess(Image.open(BytesIO(image_bytes)).convert("RGB")).unsqueeze(0).to(device)
    with torch.no_grad():
        embedding = model.encode_image(image)
        embedding /= embedding.norm(dim=-1, keepdim=True)
    return embedding.squeeze().cpu().numpy().tolist()


def get_connection():
    return psycopg2.connect(os.getenv("DB_CONNECTION_STRING"))


def build_match_result(rows, source):
    """把資料庫查詢結果轉成回傳格式,source 標記這筆是同分類還是跨分類找到的"""
    return [
        {
            "id": str(r[0]),
            "image_url": r[1],
            "category": r[2],
            "location": r[3],
            "description": r[4],
            "similarity": round(r[5], 4),
            "confidence": "高" if r[5] >= 0.85 else "中",
            "match_source": source,  # "same_category" 或 "cross_category"
        }
        for r in rows
    ]


# ---------- API 1:上傳物品(拾獲通報 or 遺失協尋)----------
# 照片直接上傳到 Supabase Storage(雲端),不寫進本機硬碟。
# 新提交的物品一律先進入「待審核」狀態,審核通過後才會出現在配對結果裡。
@app.post("/items")
async def create_item(
    file: UploadFile = File(...),
    type: str = Form(...),        # "found" 或 "lost"
    category: str = Form(...),
    location: str = Form(...),
    description: str = Form(""),
):
    file_bytes = await file.read()
    embedding = get_image_embedding_from_bytes(file_bytes)

    ext = file.filename.split(".")[-1]
    storage_filename = f"{uuid.uuid4()}.{ext}"
    supabase.storage.from_(BUCKET_NAME).upload(
        storage_filename,
        file_bytes,
        {"content-type": file.content_type},
    )
    public_url = supabase.storage.from_(BUCKET_NAME).get_public_url(storage_filename)

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO items (type, image_url, embedding, category, location, description, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id;
        """,
        (type, public_url, embedding, category, location, description, "pending"),
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()

    return {"id": new_id, "image_url": public_url, "status": "pending", "message": "上傳成功,待後台審核後才會公開"}


# ---------- API 2:查詢某物品的相似配對(混合邏輯)----------
# 階段一:先在「同分類」裡找,門檻較寬鬆(0.75),因為同分類誤判機率較低
# 階段二:如果同分類找不到達標的結果,才放寬到「全部分類」,但要求更高分(0.88)才算數
@app.get("/items/{item_id}/matches")
def get_matches(item_id: str, top_n: int = 3):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT embedding, type, category FROM items WHERE id = %s;", (item_id,))
    row = cur.fetchone()
    if row is None:
        cur.close()
        conn.close()
        return {"error": "找不到這筆物品"}

    embedding, item_type, item_category = row
    opposite_type = "found" if item_type == "lost" else "lost"

    # ---- 階段一:同分類比對 ----
    cur.execute(
        """
        SELECT id, image_url, category, location, description,
               1 - (embedding <=> %s::vector) AS similarity
        FROM items
        WHERE type = %s AND status = 'open' AND category = %s AND id != %s
          AND 1 - (embedding <=> %s::vector) >= %s
        ORDER BY embedding <=> %s::vector
        LIMIT %s;
        """,
        (embedding, opposite_type, item_category, item_id,
         embedding, SAME_CATEGORY_THRESHOLD, embedding, top_n),
    )
    same_category_rows = cur.fetchall()

    if same_category_rows:
        # 同分類就找到達標結果,直接回傳,不需要再查跨分類
        cur.close()
        conn.close()
        return {
            "matches": build_match_result(same_category_rows, "same_category"),
            "strategy": "same_category",
        }

    # ---- 階段二:同分類沒有達標結果,放寬到全部分類,但要更高分才算數 ----
    cur.execute(
        """
        SELECT id, image_url, category, location, description,
               1 - (embedding <=> %s::vector) AS similarity
        FROM items
        WHERE type = %s AND status = 'open' AND id != %s
          AND 1 - (embedding <=> %s::vector) >= %s
        ORDER BY embedding <=> %s::vector
        LIMIT %s;
        """,
        (embedding, opposite_type, item_id,
         embedding, CROSS_CATEGORY_THRESHOLD, embedding, top_n),
    )
    cross_category_rows = cur.fetchall()
    cur.close()
    conn.close()

    return {
        "matches": build_match_result(cross_category_rows, "cross_category"),
        "strategy": "cross_category" if cross_category_rows else "no_match",
    }


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