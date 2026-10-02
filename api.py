"""
校園失物招領 API（MySQL + LINE + Web）
啟動：uvicorn api:app --reload --host 0.0.0.0 --port 80
"""
from __future__ import annotations

import os
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from linebot.v3.exceptions import InvalidSignatureError

load_dotenv()

# 先載入服務層（會初始化 CLIP）
import services as svc
import line_bot

REQUIRED_ENV = ("MYSQL_HOST", "MYSQL_USER", "MYSQL_DATABASE", "ADMIN_API_KEY")
CORS_ORIGINS = [
    o.strip()
    for o in os.getenv("CORS_ORIGINS", "http://127.0.0.1:80,http://localhost:80").split(",")
    if o.strip()
]


def _require_env() -> None:
    missing = [k for k in REQUIRED_ENV if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"缺少必要環境變數: {', '.join(missing)}")


_require_env()
line_bot.init_line()

app = FastAPI(title="校園失物招領 API", version="4.0.0-line-mysql")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS if "*" not in CORS_ORIGINS else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/uploads", StaticFiles(directory=str(svc.UPLOAD_DIR)), name="uploads")

api_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)


def require_admin(x_admin_key: Optional[str] = Depends(api_key_header)) -> None:
    if not svc.ADMIN_API_KEY:
        raise HTTPException(status_code=503, detail="伺服器未設定 ADMIN_API_KEY，管理功能已停用")
    if not x_admin_key or x_admin_key != svc.ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="未授權：請提供正確的 X-Admin-Key")


def _user_page():
    index = svc.STATIC_DIR / "user.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail="找不到 user.html")
    return FileResponse(index)


@app.get("/")
def root():
    """首頁：導向「我撿到東西」"""
    return RedirectResponse(url="/pick", status_code=302)


@app.get("/pick")
def page_pick():
    """我撿到東西（拾獲通報）→ http://127.0.0.1/pick"""
    return _user_page()


@app.get("/lost")
def page_lost():
    """我遺失東西（協尋申請）→ http://127.0.0.1/lost"""
    return _user_page()


@app.get("/browse")
def page_browse():
    """瀏覽招領清單 → http://127.0.0.1/browse"""
    return _user_page()


@app.get("/admin")
def admin_page():
    path = svc.STATIC_DIR / "admin.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="找不到 admin.html")
    return FileResponse(path)


@app.get("/health")
def health():
    try:
        with svc.db_cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        db_ok = True
        db_error = None
    except Exception as exc:
        db_ok = False
        db_error = str(exc)
    payload = {
        "status": "ok" if db_ok else "degraded",
        "device": svc.device,
        "db": "ok" if db_ok else "error",
        "storage": "local",
        "line_bot": line_bot.line_enabled(),
        "admin_auth_configured": bool(svc.ADMIN_API_KEY),
    }
    if db_error:
        payload["db_error"] = db_error
    return payload


# ---------- LINE Webhook ----------
@app.post("/callback")
async def line_callback(
    request: Request,
    x_line_signature: str = Header(..., alias="X-Line-Signature"),
):
    if not line_bot.line_enabled():
        raise HTTPException(status_code=503, detail="未設定 LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN")
    body = (await request.body()).decode("utf-8")
    try:
        line_bot.handle_webhook(body, x_line_signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "OK"


# ---------- 公開 API ----------
@app.post("/items")
async def create_item(
    file: Optional[UploadFile] = File(None),
    type: str = Form(...),
    category: str = Form(...),
    location: str = Form(...),
    description: str = Form(""),
    line_user_id: Optional[str] = Form(None),  # 👉 1. 新增這行：接收從前端傳來的 ID
    student_id: Optional[str] = Form(""), # 👉 新增這行接收學號
    custodian: Optional[str] = Form(""),  # 👉 新增接收保管地點
):
    image_bytes = None
    filename = "upload.jpg"
    content_type = "image/jpeg"
    if file is not None and file.filename:
        image_bytes = await file.read()
        filename = file.filename
        content_type = file.content_type or "image/jpeg"
    try:
        return svc.create_item_record(
            item_type=type,
            category=category,
            location=location,
            description=description,
            image_bytes=image_bytes,
            image_filename=filename,
            content_type=content_type,
            source="web",
            line_user_id=line_user_id,  # 👉 2. 新增這行：把 ID 傳進資料庫
            student_id=student_id, # 👉 新增這行傳給 services
            custodian=custodian,  # 👉 新增：傳遞給 services
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"資料庫寫入失敗: {exc}") from exc


@app.get("/items")
def list_public_items(
    type: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
):
    if status and status != "open":
        raise HTTPException(status_code=403, detail="公開 API 僅能查詢 status=open，管理請使用 /admin/items")
    if type and type not in svc.ITEM_TYPES:
        raise HTTPException(status_code=400, detail="無效的 type")
    return {"items": svc.list_items(status="open", item_type=type, category=category)}


@app.get("/items/{item_id}")
def get_item(item_id: str):
    item = svc.get_item_by_id(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="找不到該物品")
    return item


@app.get("/items/{item_id}/matches")
def get_matches(item_id: str, top_n: int = Query(3, ge=1, le=20)):
    try:
        return svc.find_matches(item_id, top_n=top_n)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="此筆物品的向量資料損壞") from exc

@app.post("/items/{item_id}/reject", dependencies=[Depends(require_admin)])
def reject_item(item_id: str):
    if not svc.update_item_status(item_id, "rejected", only_from="pending"):
        raise HTTPException(status_code=404, detail="找不到待審核的物品，或此物品已審核過")
    
    try:
        line_user_id = svc.get_line_user_id(item_id)
        if line_user_id:
            line_bot.send_push_notification(line_user_id, "❌ 您的通報未通過審核，如有疑問請聯繫管理單位。")
    except Exception as e:
        print(f"推播失敗: {e}")

    return {"message": "已拒絕", "id": item_id, "status": "rejected"}
# ---------- 管理端 ----------
@app.get("/admin/items", dependencies=[Depends(require_admin)])
def list_admin_items(
    type: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
):
    if status and status not in svc.ITEM_STATUSES:
        raise HTTPException(status_code=400, detail="無效的 status")
    if type and type not in svc.ITEM_TYPES:
        raise HTTPException(status_code=400, detail="無效的 type")
    return {"items": svc.list_items(status=status, item_type=type, category=category)}

@app.post("/items/{item_id}/close")
def close_item_by_user(
    item_id: str, 
    matched_item_id: Optional[str] = Form(None)  # 👉 新增接收對方 ID
):
    # 1. 關閉自己原本的通報
    if not svc.update_item_status(item_id, "closed"):
        raise HTTPException(status_code=404, detail="找不到該物品，或狀態無法更新")
    
    # 2. 如果有選擇配對成功的物品，一併將對方結案
    if matched_item_id:
        svc.update_item_status(matched_item_id, "closed")
        
    return {"message": "已成功結案", "id": item_id, "status": "closed"}

@app.patch("/items/{item_id}", dependencies=[Depends(require_admin)])
def update_status(item_id: str, status: str = Form(...)):
    try:
        ok = svc.update_item_status(item_id, status.strip().lower())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="找不到該物品")
    return {"message": "狀態已更新", "id": item_id, "status": status.strip().lower()}

@app.post("/items/{item_id}/approve", dependencies=[Depends(require_admin)])
def approve_item(item_id: str):
    if not svc.update_item_status(item_id, "open", only_from="pending"):
        raise HTTPException(status_code=404, detail="找不到待審核的物品，或此物品已審核過")
    
    try:
        # 1. 取得新審核通過的通報者 LINE ID、物品詳情與配對結果
        line_user_id = svc.get_line_user_id(item_id)
        new_item = svc.get_item_by_id(item_id) 
        matches_data = svc.find_matches(item_id, top_n=3)
        matches = matches_data.get("matches", [])
        
        # 2. 推播給「剛被審核通過」的通報者
        if line_user_id:
            if matches:
                msg = svc.format_matches_text(matches_data)
                line_bot.send_push_notification(line_user_id, f"✅ 您的通報已審核通過！\n\n{msg}")
            else:
                line_bot.send_push_notification(line_user_id, "✅ 您的通報已審核公開！目前尚無相似度高的配對，系統將持續為您留意。")

        # 3. 雙向推播：通知「配對清單上另一方」的使用者
        if matches and new_item:
            for match in matches:
                other_user_id = svc.get_line_user_id(match["id"])
                
                # 如果對方當初有留下 LINE ID，就發送通知
                if other_user_id:
                    match_pct = round(match["similarity"] * 100, 1)
                    new_type_str = "拾獲" if new_item["type"] == "found" else "遺失"
                    
                    other_msg = (
                        f"🔔 系統自動配對通知\n\n"
                        f"剛剛有人通報了一筆新的「{new_type_str}」物品（{new_item['category']}），與您的紀錄有 {match_pct}% 相似，可能就是您在找的東西 / 失主！\n\n"
                        f"【新通報資訊】\n"
                        f"地點：{new_item['location']}\n"
                        f"描述：{new_item['description'] or '無'}\n\n"
                        f"請複製以下編號，回到系統查詢詳情：\n"
                        f"{new_item['id']}"
                    )
                    line_bot.send_push_notification(other_user_id, other_msg)

    except Exception as e:
        print(f"配對或推播失敗: {e}")

    return {"message": "審核通過，已公開", "id": item_id, "status": "open"}