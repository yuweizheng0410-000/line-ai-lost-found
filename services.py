"""
共用業務邏輯：MySQL + CLIP + 本機上傳
網頁 API 與 LINE Bot 都走這裡。
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import urljoin

import numpy as np
import open_clip
import pymysql
import torch
from PIL import Image, UnidentifiedImageError
from pymysql.cursors import Cursor

ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "").strip()
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "5")) * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
ALLOWED_EXT = {"jpg", "jpeg", "png", "webp", "gif"}
ITEM_TYPES = {"found", "lost"}
ITEM_STATUSES = {"pending", "open", "closed", "rejected"}
CATEGORIES = ["3C產品", "證件", "衣物", "文具", "水壺/餐具", "鑰匙", "其他"]
SAME_CATEGORY_THRESHOLD = float(os.getenv("SAME_CATEGORY_THRESHOLD", "0.75"))
CROSS_CATEGORY_THRESHOLD = float(os.getenv("CROSS_CATEGORY_THRESHOLD", "0.88"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:80").rstrip("/")

STATIC_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = STATIC_DIR / os.getenv("UPLOAD_DIR", "uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

device = "cuda" if torch.cuda.is_available() else "cpu"
model, _, preprocess = open_clip.create_model_and_transforms("ViT-B-32", pretrained="openai")
model.eval().to(device)
tokenizer = open_clip.get_tokenizer("ViT-B-32")
print(f"[CLIP] device={device}")


def get_connection():
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DATABASE"),
        charset="utf8mb4",
        cursorclass=Cursor,
        autocommit=False,
    )


@contextmanager
def db_cursor() -> Iterator[Cursor]:
    conn = get_connection()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def row_to_item(r) -> dict:
    return {
        "id": str(r[0]),
        "type": r[1],
        "image_url": r[2],
        "category": r[3],
        "location": r[4],
        "description": r[5] or "",
        "status": r[6],
        "created_at": r[7].isoformat() if r[7] else None,
        "line_user_id": r[8] if len(r) > 8 and r[8] else "網頁通報/無ID",
        "student_id": r[9] if len(r) > 9 and r[9] else "未提供",
        "custodian": r[10] if len(r) > 10 and r[10] else "未提供"
    }


def parse_embedding(raw) -> np.ndarray:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        raw = json.loads(raw)
    vec = np.asarray(raw, dtype=np.float32)
    if vec.ndim != 1 or vec.size == 0:
        raise ValueError("invalid embedding")
    return vec


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def get_image_embedding_from_bytes(image_bytes: bytes) -> list[float]:
    try:
        image = Image.open(BytesIO(image_bytes)).convert("RGB")
    except UnidentifiedImageError as exc:
        raise ValueError("無法辨識的圖片格式") from exc

    tensor = preprocess(image).unsqueeze(0).to(device)
    with torch.no_grad():
        embedding = model.encode_image(tensor)
        embedding /= embedding.norm(dim=-1, keepdim=True)
    return embedding.squeeze().cpu().numpy().tolist()


def get_text_embedding(text: str) -> list[float]:
    tokens = tokenizer([text]).to(device)
    with torch.no_grad():
        embedding = model.encode_text(tokens)
        embedding /= embedding.norm(dim=-1, keepdim=True)
    return embedding.squeeze().cpu().numpy().tolist()


def validate_image_bytes(data: bytes, content_type: str = "", filename: str = "upload.jpg") -> tuple[bytes, str]:
    content_type = (content_type or "").lower()
    if content_type and content_type not in ALLOWED_IMAGE_TYPES:
        raise ValueError(f"不支援的圖片類型: {content_type}")
    if not data:
        raise ValueError("上傳的檔案是空的")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"圖片過大，上限 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    try:
        Image.open(BytesIO(data)).verify()
    except Exception as exc:
        raise ValueError("檔案內容不是有效圖片") from exc
    raw_ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    ext = raw_ext if raw_ext in ALLOWED_EXT else "jpg"
    return data, ext


def save_upload(file_bytes: bytes, ext: str) -> str:
    filename = f"{uuid.uuid4()}.{ext}"
    path = UPLOAD_DIR / filename
    path.write_bytes(file_bytes)
    return urljoin(PUBLIC_BASE_URL + "/", f"uploads/{filename}")


def create_item_record(
    *,
    item_type: str,
    category: str,
    location: str,
    description: str = "",
    image_bytes: Optional[bytes] = None,
    image_filename: str = "upload.jpg",
    content_type: str = "image/jpeg",
    source: str = "web",
    line_user_id: Optional[str] = None,
    student_id: str = "", # 👉 新增這行
    custodian: str = "",
) -> dict:
    item_type = item_type.strip().lower()
    category = category.strip()
    location = location.strip()
    description = (description or "").strip()

    if item_type not in ITEM_TYPES:
        raise ValueError("type 必須是 found 或 lost")
    if not category or not location:
        raise ValueError("category 與 location 為必填")
    if len(category) > 50 or len(location) > 200 or len(description) > 1000:
        raise ValueError("欄位長度超出限制")

    public_url: Optional[str] = None
    if image_bytes:
        data, ext = validate_image_bytes(image_bytes, content_type, image_filename)
        embedding = get_image_embedding_from_bytes(data)
        public_url = save_upload(data, ext)
    else:
        text_for_embedding = f"{category} {description}".strip() or category
        embedding = get_text_embedding(text_for_embedding)

    item_id = str(uuid.uuid4())
    with db_cursor() as cur:
        # 相容：若尚未加 source / line_user_id 欄位，退回舊 INSERT
        try:
            cur.execute(
                """
                INSERT INTO items
                    (id, type, image_url, embedding, category, location, description, status, source, line_user_id, student_id, custodian)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s)
                """,
                (
                    item_id,
                    item_type,
                    public_url,
                    json.dumps(embedding),
                    category,
                    location,
                    description,
                    source,
                    line_user_id,
                    student_id,
                    custodian
                ),
            )
        except pymysql.err.OperationalError as e:
            print(f"⚠️ 觸發備用寫入，真正的 SQL 錯誤是：{e}")
            cur.execute(
                """
                INSERT INTO items
                    (id, type, image_url, embedding, category, location, description, status)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, 'pending')
                """,
                (
                    item_id,
                    item_type,
                    public_url,
                    json.dumps(embedding),
                    category,
                    location,
                    description,
                ),
            )

    return {
        "id": item_id,
        "image_url": public_url,
        "status": "pending",
        "message": "上傳成功，待後台審核後才會公開",
    }


def get_item_by_id(item_id: str) -> Optional[dict]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT id, type, image_url, category, location, description, status, created_at, line_user_id, student_id, custodian
            FROM items WHERE id = %s
            """,
            (item_id,),
        )
        row = cur.fetchone()
    return row_to_item(row) if row else None


def list_items(
    *,
    status: Optional[str] = None,
    item_type: Optional[str] = None,
    category: Optional[str] = None,
) -> list[dict]:
    filters = ["1=1"]
    params: list = []
    if status:
        filters.append("status = %s")
        params.append(status)
    if item_type:
        filters.append("type = %s")
        params.append(item_type)
    if category:
        filters.append("category = %s")
        params.append(category)
    where = " AND ".join(filters)
    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT id, type, image_url, category, location, description, status, created_at, line_user_id, student_id, custodian
            FROM items
            WHERE {where}
            ORDER BY created_at DESC
            """,
            params,
        )
        rows = cur.fetchall()
    return [row_to_item(r) for r in rows]


def build_match_result(scored: list[tuple], source: str) -> list[dict]:
    return [
        {
            "id": item["id"],
            "image_url": item["image_url"],
            "category": item["category"],
            "location": item["location"],
            "description": item["description"],
            "similarity": round(score, 4),
            "confidence": "高" if score >= 0.85 else "中",
            "match_source": source,
        }
        for item, score in scored
    ]


def rank_candidates(query_vec: np.ndarray, rows: list, base_threshold: float, top_n: int, is_query_text: bool = False) -> list[tuple]:
    scored = []
    for r in rows:
        try:
            cand = parse_embedding(r[5])
        except Exception:
            continue
        score = cosine_similarity(query_vec, cand)
        
        # 判斷資料庫裡的候選物品是否也是純文字 (r[1] 是 image_url)
        is_cand_text = not bool(r[1]) 
        
        # 👉 智慧動態門檻：根據是否為「同分類」來決定嚴格度
        if is_query_text or is_cand_text:
            if base_threshold < 0.8:  
                # 這是同分類搜尋 (SAME_CATEGORY_THRESHOLD = 0.75)
                # 有分類保護，門檻放寬到 0.20，確保能抓到同分類的「藍筆」
                current_threshold = 0.20
            else:
                # 這是跨分類搜尋 (CROSS_CATEGORY_THRESHOLD = 0.88)
                # 無分類保護，門檻提高到 0.30，嚴格擋掉「食物」等雜訊
                current_threshold = 0.30
        else:
            current_threshold = base_threshold
            
        if score >= current_threshold:
            scored.append(
                (
                    {
                        "id": str(r[0]),
                        "image_url": r[1],
                        "category": r[2],
                        "location": r[3],
                        "description": r[4] or "",
                    },
                    score,
                )
            )
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_n]


def find_matches(item_id: str, top_n: int = 3) -> dict:
    with db_cursor() as cur:
        cur.execute("SELECT embedding, type, category, image_url FROM items WHERE id = %s", (item_id,))
        row = cur.fetchone()
        if row is None:
            raise LookupError("找不到這筆物品")

        query_vec = parse_embedding(row[0])
        item_type, item_category, item_image_url= row[1], row[2], row[3]
        is_query_text = not bool(item_image_url)
        opposite_type = "found" if item_type == "lost" else "lost"

        cur.execute(
            """
            SELECT id, image_url, category, location, description, embedding
            FROM items
            WHERE type = %s AND status = 'open' AND category = %s AND id != %s
            """,
            (opposite_type, item_category, item_id),
        )
        same_scored = rank_candidates(query_vec, cur.fetchall(), SAME_CATEGORY_THRESHOLD, top_n, is_query_text)
        if same_scored:
            return {
                "matches": build_match_result(same_scored, "same_category"),
                "strategy": "same_category",
            }

        cur.execute(
            """
            SELECT id, image_url, category, location, description, embedding
            FROM items
            WHERE type = %s AND status = 'open' AND id != %s
            """,
            (opposite_type, item_id),
        )
        cross_scored = rank_candidates(query_vec, cur.fetchall(), CROSS_CATEGORY_THRESHOLD, top_n, is_query_text)

    return {
        "matches": build_match_result(cross_scored, "cross_category"),
        "strategy": "cross_category" if cross_scored else "no_match",
    }


def update_item_status(item_id: str, new_status: str, *, only_from: Optional[str] = None) -> bool:
    if new_status not in ITEM_STATUSES:
        raise ValueError(f"status 必須是以下之一: {', '.join(sorted(ITEM_STATUSES))}")
    with db_cursor() as cur:
        if only_from:
            cur.execute(
                "UPDATE items SET status = %s WHERE id = %s AND status = %s",
                (new_status, item_id, only_from),
            )
        else:
            cur.execute("UPDATE items SET status = %s WHERE id = %s", (new_status, item_id))
        return cur.rowcount > 0


def format_item_status_text(item: dict) -> str:
    status_label = {
        "pending": "審核中",
        "open": "已公開比對中",
        "closed": "已結案",
        "rejected": "審核未通過",
    }.get(item["status"], item["status"])
    type_label = "拾獲通報" if item["type"] == "found" else "協尋申請"
    return (
        f"{type_label}\n"
        f"狀態：{status_label}\n"
        f"分類：{item['category']}\n"
        f"地點：{item['location']}\n"
        f"描述：{item['description'] or '（無）'}\n"
        f"編號：{item['id']}"
    )


def format_matches_text(match_data: dict) -> str:
    matches = match_data.get("matches") or []
    if not matches:
        return "目前尚未找到相似的物品，審核通過後系統會持續比對。"
    lines = [f"AI 比對到 {len(matches)} 筆可能相符："]
    for i, m in enumerate(matches, 1):
        pct = round(m["similarity"] * 100, 1)
        lines.append(
            f"{i}. {m['category']}｜{m['location']}｜相似度 {pct}%\n"
            f"   {m['description'] or '無描述'}"
        )
    return "\n".join(lines)

def get_line_user_id(item_id: str) -> Optional[str]:
    with db_cursor() as cur:
        try:
            cur.execute("SELECT line_user_id FROM items WHERE id = %s", (item_id,))
            row = cur.fetchone()
            return row[0] if row else None
        except Exception:
            return None