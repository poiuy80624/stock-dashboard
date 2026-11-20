"""本機網頁：啟動時用資料庫產生與 GitHub Pages 相同的靜態網站，再以 FastAPI 提供檢視。

和公開網站的差別：本機版多一個「AI技術分析」分頁（/api/chat），由本機伺服器用你的 ANTHROPIC_API_KEY
呼叫 Claude；金鑰只在伺服器端，不會傳到瀏覽器，也不會出現在公開網站。

啟動：uv run uvicorn web.app:app
資料庫路徑可用環境變數 STOCK_DB 覆寫（測試用）。重新啟動即會依最新資料重建。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ac_chat.api import default_client, make_chat_router
from build_site import build
from core.db import DEFAULT_DB, ROOT

load_dotenv(ROOT / ".env")      # ANTHROPIC_API_KEY 等設定（.env 已被 .gitignore 忽略）


def create_app(client_factory: Callable = default_client) -> FastAPI:
    site_dir = Path(os.environ.get("STOCK_SITE_DIR", ROOT / "site"))
    db_path = Path(os.environ.get("STOCK_DB", DEFAULT_DB))
    config_path = ROOT / "config.yaml"

    if not db_path.exists():
        raise RuntimeError(f"找不到資料庫：{db_path}。請先執行 git pull 取得 data/stock.db，或執行 fetcher.py")
    if not build(site_dir, db_path, config_path, chat_enabled=True):
        raise RuntimeError("資料庫裡沒有任何股票資料，無法產生網頁")

    app = FastAPI(title="AC台股分析", docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(make_chat_router(db_path=db_path, config_path=config_path, client_factory=client_factory))
    app.mount("/", StaticFiles(directory=str(site_dir), html=True), name="site")   # 必須放在 API 路由之後
    return app


app = create_app()
