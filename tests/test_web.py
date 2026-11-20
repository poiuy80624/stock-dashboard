import importlib
import json

import pytest
from fastapi.testclient import TestClient

from core.db import open_db, upsert_prices
from core.indicators import update_indicators


@pytest.fixture
def client(tmp_path, monkeypatch):
    p = tmp_path / "t.db"
    rows = [
        {"stock_id": "2330", "date": f"2026-{(i - 1) // 28 + 9:02d}-{(i - 1) % 28 + 1:02d}", "open": float(i),
         "max": float(i), "min": float(i), "close": float(i), "spread": 1.0, "Trading_Volume": 1000, "Trading_money": 1}
        for i in range(1, 41)
    ]
    with open_db(p) as conn:
        upsert_prices(conn, rows)
        update_indicators(conn, "2330")
    monkeypatch.setenv("STOCK_DB", str(p))
    monkeypatch.setenv("STOCK_SITE_DIR", str(tmp_path / "site"))
    import web.app as webapp
    importlib.reload(webapp)          # 模組載入時才讀環境變數並建置網站
    return TestClient(webapp.app)


def test_本機網頁提供與靜態網站相同的內容(client):
    r = client.get("/")
    assert r.status_code == 200 and "AC台股分析" in r.text and "個股三大法人買賣表" in r.text
    d = client.get("/data/2330.json").json()
    assert d["rows"][-1]["trend"] == "多頭" and d["rows"][-1]["macd_state"] != "資料不足"
    assert client.get("/data/foreign.json").status_code == 200
    assert json.loads(client.get("/data/indices.json").text)["indices"][0]["symbol"] == "TX"
    assert client.get("/data/9999.json").status_code == 404
