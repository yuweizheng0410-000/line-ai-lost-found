# 校園失物招領（AI 圖像比對｜MySQL 版）

用 OpenCLIP 把照片／文字轉成向量，存進 **MySQL**，在 Python 做 cosine similarity 配對。照片存本機 `uploads/`。

> 目前是 Web API + 靜態前端；尚未整合 LINE Bot。

## 架構

```
瀏覽器 (user.html / admin.html)
        ↓
   FastAPI (api.py)
   ├─ CLIP 算向量
   ├─ MySQL 存資料（embedding 用 JSON）
   └─ uploads/ 存照片
```

## 環境需求

- Python 3.10+
- MySQL 8.0+
- （建議）NVIDIA GPU；無 GPU 會用 CPU

## 安裝

```bash
python -m venv .venv
.venv\Scripts\activate

pip install -r requirements.txt
```

## 設定 MySQL

1. 啟動 MySQL，用 root（或你的帳號）執行 `schema.sql`。
2. 複製環境變數：

```bash
copy .env.example .env
```

3. 編輯 `.env`，例如：

```env
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=你的MySQL密碼
MYSQL_DATABASE=lost_found
ADMIN_API_KEY=自己設一組長密碼
PUBLIC_BASE_URL=http://127.0.0.1:8000
```

| 變數 | 填什麼 |
|------|--------|
| `MYSQL_HOST` | 本機通常 `127.0.0.1` |
| `MYSQL_PORT` | 預設 `3306` |
| `MYSQL_USER` | 例如 `root` |
| `MYSQL_PASSWORD` | 你的 MySQL 密碼（沒設就留空） |
| `MYSQL_DATABASE` | `lost_found`（跟 schema 一致） |
| `ADMIN_API_KEY` | **自己隨便設**，後台登入用同一組 |
| `PUBLIC_BASE_URL` | 對外網址，用來組照片 URL |

**不再需要** `SUPABASE_URL` / `SUPABASE_KEY` / `DB_CONNECTION_STRING`。

## 啟動

```bash
uvicorn api:app --reload --host 0.0.0.0 --port 8000
```

- 使用者：http://127.0.0.1:8000/
- 後台：http://127.0.0.1:8000/admin
- 健康檢查：http://127.0.0.1:8000/health

## 與舊版（Supabase）差異

| | 舊版 | 現在 |
|--|------|------|
| 資料庫 | PostgreSQL + pgvector | MySQL + JSON 向量 |
| 比對 | SQL `<=>` | Python cosine |
| 照片 | Supabase Storage | 本機 `uploads/` |
| 連線設定 | 一條 connection string | `MYSQL_*` 分開填 |

校園規模資料量通常沒問題；若之後上千筆以上，可再考慮加向量套件或改回 pgvector。
