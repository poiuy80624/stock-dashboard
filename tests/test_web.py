import pytest
from fastapi.testclient import TestClient

from core.db import open_db, upsert_prices
from core.indicators import update_indicators
from web.app import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    p = tmp_path / "t.db"
    rows = [
        {"stock_id": "2330", "date": f"2026-10-{i:02d}", "open": float(i), "max": float(i), "min": float(i),
         "close": float(i), "spread": 1.0, "Trading_Volume": 1000, "Trading_money": 1}
        for i in range(1, 26)
    ]
    with open_db(p) as conn:
        upsert_prices(conn, rows)
        update_indicators(conn, "2330")
    monkeypatch.setenv("STOCK_DB", str(p))
    return TestClient(app)


def test_首頁含股票下拉(client):
    r = client.get("/")
    assert r.status_code == 200 and "2330" in r.text and "plotly" in r.text


def test_history_與_quote(client):
    h = client.get("/api/history/2330?days=10").json()
    assert h["updated"] == "2026-10-25" and len(h["rows"]) == 10
    q = client.get("/api/quote/2330").json()
    assert q["trend"] == "多頭" and q["ma5"] == 23.0


def test_錯誤情況(client):
    assert client.get("/api/quote/9999").status_code == 404
    assert client.get("/api/quote/bad!id").status_code in (400, 404)
    assert client.get("/api/history/2330?days=9999").status_code == 422
