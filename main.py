import os
<<<<<<< HEAD
=======
import io
>>>>>>> 1796586 (連結 GitHub 儲存庫並同步檔案)
import torch
import open_clip
import psycopg2
from PIL import Image
from dotenv import load_dotenv
<<<<<<< HEAD

load_dotenv()

# ---------- 1. 載入 CLIP 模型（第一次執行會自動下載模型檔） ----------
device = "cuda" if torch.cuda.is_available() else "cpu"
=======
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional

load_dotenv()

# ---------- 1. 初始化 FastAPI 與 CORS (解決 Failed to fetch 問題) ----------
app = FastAPI(title="校園失物招領 API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允許前端 cross-origin 存取
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- 2. 載入 CLIP 模型 ----------
device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"目前使用的運算裝置: {device}")
>>>>>>> 1796586 (連結 GitHub 儲存庫並同步檔案)
model, _, preprocess = open_clip.create_model_and_transforms(
    "ViT-B-32", pretrained="openai"
)
model.eval().to(device)


<<<<<<< HEAD
def get_image_embedding(image_path: str) -> list[float]:
    """把一張圖片轉成 512 維向量"""
    image = preprocess(Image.open(image_path).convert("RGB")).unsqueeze(0).to(device)
    with torch.no_grad():
        embedding = model.encode_image(image)
        embedding /= embedding.norm(dim=-1, keepdim=True)  # 正規化，之後算 cosine similarity 更準
    return embedding.squeeze().cpu().numpy().tolist()


# ---------- 2. 連線 Supabase ----------
def get_connection():
    return psycopg2.connect(os.getenv("DB_CONNECTION_STRING"))


# ---------- 3. 寫入一筆物品資料 ----------
def insert_item(image_path: str, item_type: str, category: str, location: str, description: str = ""):
    embedding = get_image_embedding(image_path)

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO items (type, image_url, embedding, category, location, description)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id;
        """,
        (item_type, image_path, embedding, category, location, description),
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    print(f"已寫入，item id = {new_id}")
    return new_id


# ---------- 4. 找出最相似的前 N 筆（限定對向類型） ----------
def find_matches(image_path: str, opposite_type: str, top_n: int = 3):
    embedding = get_image_embedding(image_path)

    conn = get_connection()
    cur = conn.cursor()
    # <=> 是 pgvector 的 cosine distance 運算子，數字越小代表越相似
=======
def get_image_embedding_from_bytes(image_bytes: bytes) -> list[float]:
    """將上傳的照片 Byte 轉成 512 維向量"""
    image = preprocess(Image.open(io.BytesIO(image_bytes)).convert("RGB")).unsqueeze(0).to(device)
    with torch.no_grad():
        embedding = model.encode_image(image)
        embedding /= embedding.norm(dim=-1, keepdim=True)  # 正規化
    return embedding.squeeze().cpu().numpy().tolist()


# ---------- 3. 連線 Supabase ----------
def get_connection():
    db_url = os.getenv("DB_CONNECTION_STRING")
    if not db_url:
        raise RuntimeError("未設定 DB_CONNECTION_STRING 環境變數")
    return psycopg2.connect(db_url)


# ---------- 4. API 路由設定 ----------

@app.get("/")
def read_root():
    return {"status": "ok", "message": "失物招領 API 伺服器運作中"}


# 【API 1】取得物品列表 (後端與前端瀏覽清單共用)
@app.get("/items")
def get_items(
    type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    category: Optional[str] = Query(None)
):
    conn = get_connection()
    cur = conn.cursor()
    
    query = "SELECT id, type, image_url, category, location, description, status, created_at FROM items WHERE 1=1"
    params = []
    
    if type:
        query += " AND type = %s"
        params.append(type)
    if status:
        query += " AND status = %s"
        params.append(status)
    if category:
        query += " AND category = %s"
        params.append(category)
        
    query += " ORDER BY created_at DESC;"
    
    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    
    items = []
    for r in rows:
        items.append({
            "id": str(r[0]),
            "type": r[1],
            "image_url": r[2],
            "category": r[3],
            "location": r[4],
            "description": r[5],
            "status": r[6],
            "created_at": r[7].isoformat() if r[7] else None
        })
        
    return {"items": items}


# 【API 2】使用者新增失物/拾獲通報
@app.post("/items")
async def create_item(
    file: UploadFile = File(...),
    type: str = Form(...),        # "found" 或 "lost"
    category: str = Form(...),
    location: str = Form(...),
    description: str = Form("")
):
    try:
        contents = await file.read()
        embedding = get_image_embedding_from_bytes(contents)
        
        # 提示：實務上可在此將圖片上傳至 Supabase Storage 取得公共 URL
        # 這裡先使用檔名作為示意 URL
        image_url = f"uploads/{file.filename}"

        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO items (type, image_url, embedding, category, location, description, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'pending')
            RETURNING id;
            """,
            (type, image_url, embedding, category, location, description),
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()

        return {"status": "success", "id": str(new_id)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# 【API 3】查詢與該物品相似的配對項目 (多筆比對)
@app.get("/items/{item_id}/matches")
def get_item_matches(item_id: str, top_n: int = 5):
    conn = get_connection()
    cur = conn.cursor()
    
    # 先拿目標物品的向量與類型
    cur.execute("SELECT embedding, type FROM items WHERE id = %s", (item_id,))
    target = cur.fetchone()
    
    if not target:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail="找不到該物品")
        
    target_embedding, target_type = target[0], target[1]
    opposite_type = "lost" if target_type == "found" else "found"
    
    # 進行向量餘弦相似度查詢 (1 - cosine_distance)
>>>>>>> 1796586 (連結 GitHub 儲存庫並同步檔案)
    cur.execute(
        """
        SELECT id, image_url, category, location, description,
               1 - (embedding <=> %s::vector) AS similarity
        FROM items
        WHERE type = %s AND status = 'open'
        ORDER BY embedding <=> %s::vector
        LIMIT %s;
        """,
<<<<<<< HEAD
        (embedding, opposite_type, embedding, top_n),
    )
    results = cur.fetchall()
    cur.close()
    conn.close()
    return results


# ---------- 測試用 ----------
if __name__ == "__main__":
    # 先塞一筆「拾獲物」進資料庫
    insert_item("test_photos/doll_1.jpg", "found", "玩偶", "宿舍", "測試用玩偶")

    # 模擬有人來協尋，拿另一張照片去找相似的拾獲物
    matches = find_matches("test_photos/doll_2.jpg", opposite_type="found", top_n=3)

    print("\n找到的相似物品：")
    for item_id, image_url, category, location, description, similarity in matches:
        print(f"id={item_id} | {category} | {location} | {description} | 相似度={similarity:.4f}")
=======
        (target_embedding, opposite_type, target_embedding, top_n),
    )
    matches = cur.fetchall()
    cur.close()
    conn.close()
    
    results = []
    for m in matches:
        results.append({
            "id": str(m[0]),
            "image_url": m[1],
            "category": m[2],
            "location": m[3],
            "description": m[4],
            "similarity": float(m[5])
        })
        
    return {"matches": results}


# 【API 4】管理員審核通過 (將狀態改為 open 以進行比對與公開)
@app.post("/items/{item_id}/approve")
def approve_item(item_id: str):
    return update_item_status(item_id, "open")


# 【API 5】管理員審核拒絕
@app.post("/items/{item_id}/reject")
def reject_item(item_id: str):
    return update_item_status(item_id, "rejected")


# 【API 6】更新物品狀態 (例如結案)
@app.patch("/items/{item_id}")
def patch_item_status(item_id: str, status: str = Form(...)):
    return update_item_status(item_id, status)


def update_item_status(item_id: str, new_status: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE items SET status = %s WHERE id = %s;", (new_status, item_id))
    conn.commit()
    cur.close()
    conn.close()
    return {"status": "success", "item_id": item_id, "new_status": new_status}
>>>>>>> 1796586 (連結 GitHub 儲存庫並同步檔案)
