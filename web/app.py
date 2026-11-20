"""股票分析網頁：FastAPI，只讀 SQLite（唯讀連線），不呼叫 FinMind。"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from core.db import DEFAULT_DB, ROOT, get_history, open_db
from core.trend import classify_trend

app = FastAPI(title="股票分析")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
STOCK_ID = re.compile(r"^[A-Za-z0-9]{1,10}$")


def _stocks() -> list[str]:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return [str(s) for s in yaml.safe_load(f)["stocks"]]


def _rows(stock_id: str, days: int) -> list[dict]:
    if not STOCK_ID.match(stock_id):
        raise HTTPException(400, "股票代號格式不正確，請使用代號（例如 2330）")
    path = Path(os.environ.get("STOCK_DB", DEFAULT_DB))
    if not path.exists():
        raise HTTPException(503, "找不到資料庫，請先 git pull 取得 data/stock.db")
    with open_db(path, readonly=True) as conn:
        rows = get_history(conn, stock_id, days)
    if not rows:
        raise HTTPException(404, f"查無 {stock_id} 的資料")
    return rows


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"stocks": _stocks()})


@app.get("/api/history/{stock_id}")
def api_history(stock_id: str, days: int = Query(120, ge=1, le=365)):
    rows = _rows(stock_id, days)
    return {"stock_id": stock_id, "updated": rows[-1]["date"], "rows": rows}


@app.get("/api/quote/{stock_id}")
def api_quote(stock_id: str):
    r = _rows(stock_id, 1)[0]
    return {
        "stock_id": stock_id,
        "date": r["date"],
        "close": r["close"],
        "change": r["spread"],
        "change_pct": r["change_pct"],
        "ma5": r["ma5"],
        "ma20": r["ma20"],
        "trend": classify_trend(r["close"], r["ma5"], r["ma20"]),
    }
