"""本機網頁：啟動時用資料庫產生與 GitHub Pages 相同的靜態網站，再以 FastAPI 提供檢視。

啟動：uv run uvicorn web.app:app
資料庫路徑可用環境變數 STOCK_DB 覆寫（測試用）。重新啟動即會依最新資料重建。
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from build_site import build
from core.db import DEFAULT_DB, ROOT

SITE_DIR = Path(os.environ.get("STOCK_SITE_DIR", ROOT / "site"))
DB_PATH = Path(os.environ.get("STOCK_DB", DEFAULT_DB))

if not DB_PATH.exists():
    raise RuntimeError(f"找不到資料庫：{DB_PATH}。請先執行 git pull 取得 data/stock.db，或執行 fetcher.py")

if not build(SITE_DIR, DB_PATH, ROOT / "config.yaml"):
    raise RuntimeError("資料庫裡沒有任何股票資料，無法產生網頁")

app = FastAPI(title="AC台股分析", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/", StaticFiles(directory=str(SITE_DIR), html=True), name="site")
