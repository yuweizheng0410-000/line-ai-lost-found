import os
import torch
import open_clip
import psycopg2
from PIL import Image
from dotenv import load_dotenv

load_dotenv()

# ---------- 1. 載入 CLIP 模型（第一次執行會自動下載模型檔） ----------
device = "cuda" if torch.cuda.is_available() else "cpu"
model, _, preprocess = open_clip.create_model_and_transforms(
    "ViT-B-32", pretrained="openai"
)
model.eval().to(device)


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
    cur.execute(
        """
        SELECT id, image_url, category, location, description,
               1 - (embedding <=> %s::vector) AS similarity
        FROM items
        WHERE type = %s AND status = 'open'
        ORDER BY embedding <=> %s::vector
        LIMIT %s;
        """,
        (embedding, opposite_type, embedding, top_n),
    )
    results = cur.fetchall()
    cur.close()
    conn.close()
    return results


# ---------- 測試用 ----------
if __name__ == "__main__":
    # 先塞幾筆「拾獲物」進資料庫
    insert_item("test_photos/found_bottle.jpg", "found", "水壺", "圖書館", "藍色水壺")
    insert_item("test_photos/found_umbrella.jpg", "found", "雨傘", "教室", "黑色雨傘")

    # 模擬有人來協尋，拿一張「遺失物」照片去找相似的拾獲物
    matches = find_matches("test_photos/lost_bottle.jpg", opposite_type="found", top_n=3)

    print("\n找到的相似物品：")
    for item_id, image_url, category, location, description, similarity in matches:
        print(f"id={item_id} | {category} | {location} | {description} | 相似度={similarity:.4f}")