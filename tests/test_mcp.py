import json

import pytest
from mcp import Client

from core.db import open_db, upsert_prices
from core.indicators import update_indicators
from stock_mcp.server import create_server


def _seed(path):
    rows = [
        {"stock_id": "2330", "date": f"2026-10-{i:02d}", "open": float(i), "max": float(i), "min": float(i),
         "close": float(i), "spread": 1.0, "Trading_Volume": 1000, "Trading_money": 1}
        for i in range(1, 26)
    ]
    with open_db(path) as conn:
        upsert_prices(conn, rows)
        update_indicators(conn, "2330")


async def _call(name, args):
    async with Client(create_server()) as c:
        return await c.call_tool(name, args)


def _data(result):
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "t.db"
    _seed(p)
    monkeypatch.setenv("STOCK_DB", str(p))
    return p


@pytest.mark.anyio
async def test_三個_tool_正常(db):
    q = _data(await _call("get_quote", {"stock_id": "2330"}))
    assert q["date"] == "2026-10-25" and q["close"] == 25.0
    t = _data(await _call("get_trend", {"stock_id": "2330"}))
    assert t["trend"] == "多頭" and t["ma5"] == 23.0
    assert t["macd_state"] == "資料不足"          # 只有 25 筆，不足以算出 MACD 訊號線
    h = _data(await _call("get_history", {"stock_id": "2330", "days": 10}))
    assert h["count"] == 10 and h["rows"][0]["date"] < h["rows"][-1]["date"]


@pytest.mark.anyio
async def test_days_上限與下限(db):
    h = _data(await _call("get_history", {"stock_id": "2330", "days": 9999}))
    assert h["count"] == 25
    h = _data(await _call("get_history", {"stock_id": "2330", "days": 0}))
    assert h["count"] == 1


@pytest.mark.anyio
async def test_錯誤代號得到清楚訊息(db):
    r = await _call("get_quote", {"stock_id": "9999"})
    assert r.is_error and "NO_DATA" in r.content[0].text and "建議" in r.content[0].text
    r = await _call("get_quote", {"stock_id": "台積電"})
    assert r.is_error and "INVALID_STOCK_ID" in r.content[0].text


@pytest.mark.anyio
async def test_資料庫不存在(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_DB", str(tmp_path / "none.db"))
    r = await _call("get_trend", {"stock_id": "2330"})
    assert r.is_error and "DB_NOT_FOUND" in r.content[0].text
