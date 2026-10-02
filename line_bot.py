"""
LINE Bot：拾獲 / 遺失通報、查詢狀態、AI 配對回覆
Webhook：POST /callback
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage,
    QuickReply,
    QuickReplyItem,
    MessageAction,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, ImageMessageContent

import services as svc

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()

handler: Optional[WebhookHandler] = None
configuration: Optional[Configuration] = None

# 簡易對話狀態（重啟會清空；正式環境可改存 Redis/MySQL）
_sessions: dict[str, dict[str, Any]] = {}

UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def line_enabled() -> bool:
    return bool(LINE_CHANNEL_SECRET and LINE_CHANNEL_ACCESS_TOKEN)


def init_line() -> bool:
    global handler, configuration
    if not line_enabled():
        print("[LINE] 未設定 CHANNEL_SECRET / ACCESS_TOKEN，LINE Bot 停用（網頁仍可用）")
        return False
    configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
    handler = WebhookHandler(LINE_CHANNEL_SECRET)
    handler.add(MessageEvent, message=TextMessageContent)(on_text)
    handler.add(MessageEvent, message=ImageMessageContent)(on_image)
    print("[LINE] Bot webhook 已啟用：POST /callback")
    return True


def handle_webhook(body: str, signature: str) -> None:
    if not handler:
        raise RuntimeError("LINE Bot 未啟用")
    handler.handle(body, signature)


def _reply(reply_token: str, texts: list[str], quick: Optional[list[tuple[str, str]]] = None) -> None:
    if not configuration:
        return
    messages = []
    for i, text in enumerate(texts):
        kwargs: dict[str, Any] = {"text": text[:4900]}
        if quick and i == len(texts) - 1:
            kwargs["quick_reply"] = QuickReply(
                items=[
                    QuickReplyItem(action=MessageAction(label=label[:20], text=text_val[:300]))
                    for label, text_val in quick[:13]
                ]
            )
        messages.append(TextMessage(**kwargs))

    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        api.reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=messages)
        )


def _download_image(message_id: str) -> bytes:
    with ApiClient(configuration) as api_client:
        blob_api = MessagingApiBlob(api_client)
        return blob_api.get_message_content(message_id)


def _menu_quick() -> list[tuple[str, str]]:
    return [
        ("我撿到東西", "拾獲"),
        ("我遺失東西", "遺失"),
        ("查詢說明", "查詢"),
        ("取消", "取消"),
    ]


def _category_quick() -> list[tuple[str, str]]:
    return [(c[:20], c) for c in svc.CATEGORIES]


def _help_text() -> str:
    return (
        "校園失物招領 Bot\n"
        "————————\n"
        "輸入「拾獲」：通報撿到的物品\n"
        "輸入「遺失」：通報遺失的物品\n"
        "輸入「查詢 編號」：查狀態與 AI 配對\n"
        "輸入「取消」：中止目前流程\n"
        "————————\n"
        "網頁也能用同一系統："
        f"{svc.PUBLIC_BASE_URL}/"
    )


def _get_session(user_id: str) -> dict[str, Any]:
    return _sessions.setdefault(user_id, {"step": "idle"})


def _clear_session(user_id: str) -> None:
    _sessions.pop(user_id, None)


def _submit(user_id: str, sess: dict[str, Any], image_bytes: Optional[bytes] = None) -> str:
    result = svc.create_item_record(
        item_type=sess["type"],
        category=sess["category"],
        location=sess["location"],
        description=sess.get("description", ""),
        image_bytes=image_bytes,
        image_filename="line.jpg",
        content_type="image/jpeg",
        source="line",
        line_user_id=user_id,
    )
    _clear_session(user_id)
    type_label = "拾獲" if sess["type"] == "found" else "遺失"
    return (
        f"已收到你的{type_label}通報，進入學校審核。\n\n"
        f"請保存編號：\n{result['id']}\n\n"
        f"之後可輸入：\n查詢 {result['id']}"
    )


def on_text(event: MessageEvent) -> None:
    user_id = event.source.user_id
    text = (event.message.text or "").strip()
    reply_token = event.reply_token
    sess = _get_session(user_id)
    low = text.lower()

    if text in ("取消", "cancel"):
        _clear_session(user_id)
        _reply(reply_token, ["已取消。需要時再輸入「拾獲」或「遺失」。"], _menu_quick())
        return

    if text in ("說明", "選單", "幫助", "help", "開始", "你好", "嗨"):
        _reply(reply_token, [_help_text()], _menu_quick())
        return

    if text.startswith("查詢"):
        m = UUID_RE.search(text)
        if not m:
            _reply(
                reply_token,
                ["請輸入：查詢 <你的編號>\n例如：查詢 123e4567-e89b-12d3-a456-426614174000"],
                _menu_quick(),
            )
            return
        item = svc.get_item_by_id(m.group(0))
        if not item:
            _reply(reply_token, ["找不到這個編號，請確認後再試。"], _menu_quick())
            return
        msgs = [svc.format_item_status_text(item)]
        if item["status"] == "open":
            try:
                msgs.append(svc.format_matches_text(svc.find_matches(item["id"])))
            except Exception:
                msgs.append("配對查詢暫時失敗，請稍後再試。")
        elif item["status"] == "pending":
            msgs.append("學校審核通過後，會開始 AI 圖像配對。")
        _reply(reply_token, msgs, _menu_quick())
        return

    # ---- 對話流程 ----
    step = sess.get("step", "idle")

    if text in ("拾獲", "我撿到", "我撿到東西", "found"):
        sess.clear()
        sess.update({"step": "category", "type": "found"})
        _reply(reply_token, ["請選擇物品分類："], _category_quick())
        return

    if text in ("遺失", "我遺失", "我遺失東西", "lost"):
        sess.clear()
        sess.update({"step": "category", "type": "lost"})
        _reply(reply_token, ["請選擇物品分類："], _category_quick())
        return

    if step == "category":
        if text not in svc.CATEGORIES:
            _reply(reply_token, ["請點下方分類按鈕，或輸入正確分類名稱。"], _category_quick())
            return
        sess["category"] = text
        sess["step"] = "location"
        place = "拾獲地點" if sess["type"] == "found" else "遺失地點"
        _reply(reply_token, [f"請輸入{place}（例如：圖書館三樓）"])
        return

    if step == "location":
        if len(text) > 200:
            _reply(reply_token, ["地點太長，請再簡短一點。"])
            return
        sess["location"] = text
        sess["step"] = "description"
        _reply(
            reply_token,
            ["請輸入物品描述（顏色、品牌、特徵）。\n若沒有可輸入「無」。"],
        )
        return

    if step == "description":
        sess["description"] = "" if text in ("無", "沒有", "-", "skip") else text
        sess["step"] = "photo"
        _reply(
            reply_token,
            [
                "請傳送物品照片。\n"
                "若沒有照片，輸入「略過」改用文字比對。"
            ],
            [("略過（無照片）", "略過")],
        )
        return

    if step == "photo":
        if text in ("略過", "跳過", "無照片", "skip"):
            try:
                msg = _submit(user_id, sess, image_bytes=None)
                _reply(reply_token, [msg], _menu_quick())
            except Exception as exc:
                _reply(reply_token, [f"送出失敗：{exc}"], _menu_quick())
            return
        _reply(reply_token, ["請直接傳送照片，或輸入「略過」。"], [("略過（無照片）", "略過")])
        return

    # idle 或其他
    _reply(reply_token, [_help_text()], _menu_quick())


def on_image(event: MessageEvent) -> None:
    user_id = event.source.user_id
    reply_token = event.reply_token
    sess = _get_session(user_id)

    if sess.get("step") != "photo":
        _reply(
            reply_token,
            ["收到照片了。請先輸入「拾獲」或「遺失」開始通報流程，再於最後一步傳照片。"],
            _menu_quick(),
        )
        return

    try:
        image_bytes = _download_image(event.message.id)
        msg = _submit(user_id, sess, image_bytes=image_bytes)
        _reply(reply_token, [msg], _menu_quick())
    except Exception as exc:
        _reply(reply_token, [f"處理照片失敗：{exc}\n可輸入「略過」改用不帶照片送出。"])

from linebot.v3.messaging import PushMessageRequest, TextMessage

def send_push_notification(user_id: str, text: str) -> None:
    """主動推播訊息給指定使用者"""
    if not configuration or not user_id:
        return
    try:
        with ApiClient(configuration) as api_client:
            api = MessagingApi(api_client)
            api.push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=[TextMessage(text=text[:4900])]
                )
            )
    except Exception as e:
        print(f"[LINE Push Error] {e}")