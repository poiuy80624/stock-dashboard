"""對話功能的 HTTP 介面（只掛在本機網頁，公開網站不包含）。

安全：
- 只接受同源的 JSON POST（檢查 Origin），避免你瀏覽其他網站時被偷偷呼叫而消耗 API 額度。
- 限制訊息數量與長度、工具往返輪數與單次回答長度。
- 金鑰只在伺服器端讀取，不會傳給瀏覽器。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Callable, Iterator, Literal
from urllib.parse import urlparse

import anthropic
import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ac_chat.engine import DEFAULT_MODEL, run_chat
from ac_chat.tools import Toolbox

log = logging.getLogger("ac_chat")
MAX_MESSAGES = 20       # 每次最多帶入最近 20 則對話
MAX_CHARS = 2000        # 單則訊息長度上限


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_CHARS * 4)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=60)
    model: str | None = Field(default=None, max_length=100)     # 畫面上的模型切換；省略則用預設


# 畫面上可切換的模型（白名單：前端傳來的模型名稱必須在這裡，避免被拿去呼叫任意模型）。
# 費用為本專案以「2330 最近趨勢如何？為什麼會這樣？」實測一題的約略值，僅供參考。
MODELS = [
    {"id": "claude-sonnet-5-5", "label": "Sonnet 5.5", "hint": "較快、較省，實測一題約 $0.04"},
    {"id": "claude-opus-5-5", "label": "Opus 5.5", "hint": "分析較深入，實測一題約 $0.11"},
]


def default_model() -> str:
    return os.environ.get("AC_CHAT_MODEL") or DEFAULT_MODEL


def available_models() -> list[dict]:
    """白名單；若用環境變數 AC_CHAT_MODEL 指定了清單以外的模型，也一併列入（視為使用者自己的選擇）。"""
    models = list(MODELS)
    if default_model() not in {m["id"] for m in models}:
        models.append({"id": default_model(), "label": default_model(), "hint": "由環境變數 AC_CHAT_MODEL 指定"})
    return models


def default_client() -> anthropic.Anthropic | None:
    """有設定 ANTHROPIC_API_KEY 才建立 client；沒有則回傳 None。"""
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return None
    return anthropic.Anthropic()


def make_chat_router(
    *, db_path: Path, config_path: Path, client_factory: Callable[[], anthropic.Anthropic | None] = default_client
) -> APIRouter:
    router = APIRouter()

    def toolbox() -> Toolbox:
        with open(config_path, encoding="utf-8") as f:
            stocks = [str(s) for s in yaml.safe_load(f)["stocks"]]
        return Toolbox(db_path, stocks)

    @router.get("/api/chat/status")
    def status():
        return {"configured": client_factory() is not None, "model": default_model(), "models": available_models()}

    @router.post("/api/chat")
    def chat(req: ChatRequest, request: Request):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "不允許跨網站呼叫")
        model = req.model or default_model()
        if model not in {m["id"] for m in available_models()}:
            raise HTTPException(400, f"不支援的模型：{model}")
        client = client_factory()
        if client is None:
            raise HTTPException(503, "尚未設定 ANTHROPIC_API_KEY（請寫在專案的 .env 或使用者環境變數，重新啟動後生效）")
        msgs = [m.model_dump() for m in req.messages][-MAX_MESSAGES:]
        if msgs[-1]["role"] != "user":
            raise HTTPException(400, "最後一則訊息必須是使用者的提問")
        for m in msgs:
            m["content"] = m["content"][:MAX_CHARS * 4 if m["role"] == "assistant" else MAX_CHARS]
        tb = toolbox()

        def stream() -> Iterator[str]:
            try:
                for event in run_chat(client, msgs, tb, model=model):
                    yield json.dumps(event, ensure_ascii=False) + "\n"
            except Exception:       # 保底：任何例外都轉成錯誤事件，不外洩細節
                log.exception("對話串流失敗")
                yield json.dumps({"type": "error", "message": "發生未預期的錯誤，請查看本機伺服器的日誌。"}, ensure_ascii=False) + "\n"

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    return router
