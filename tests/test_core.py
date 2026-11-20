import math

import pandas as pd

from core.db import get_history, get_latest, has_date, last_date, open_db, upsert_prices
from core.indicators import compute_indicators, update_indicators
from core.trend import classify_trend


def _row(sid, d, close, **kw):
    base = {"stock_id": sid, "date": d, "open": close, "max": close, "min": close,
            "close": close, "spread": 0.0, "Trading_Volume": 1000, "Trading_money": 100000}
    base.update(kw)
    return base


def test_upsert_重複寫入不會產生兩列(tmp_path):
    with open_db(tmp_path / "t.db") as conn:
        upsert_prices(conn, [_row("2330", "2026-11-19", 100.0)])
        upsert_prices(conn, [_row("2330", "2026-11-19", 101.0)])
        n = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
        assert n == 1
        assert get_latest(conn, "2330")["close"] == 101.0
        assert has_date(conn, "2330", "2026-11-19")
        assert last_date(conn, "2330") == "2026-11-19"
        assert last_date(conn, "9999") is None


def test_history_升冪且排除無成交日(tmp_path):
    with open_db(tmp_path / "t.db") as conn:
        upsert_prices(conn, [
            _row("2330", "2026-11-17", 10.0),
            _row("2330", "2026-11-18", 0.0),   # 無成交
            _row("2330", "2026-11-19", 12.0),
        ])
        hist = get_history(conn, "2330", 10)
        assert [h["date"] for h in hist] == ["2026-11-17", "2026-11-19"]
        assert get_history(conn, "2330", 1)[0]["date"] == "2026-11-19"


def test_唯讀連線不能寫入(tmp_path):
    p = tmp_path / "t.db"
    with open_db(p) as conn:
        upsert_prices(conn, [_row("2330", "2026-11-19", 1.0)])
    with open_db(p, readonly=True) as conn:
        try:
            conn.execute("DELETE FROM prices")
            raised = False
        except Exception:
            raised = True
    assert raised


def test_ma_數值():
    df = pd.DataFrame({"date": [f"d{i:02d}" for i in range(25)], "close": [float(i) for i in range(1, 26)]})
    res = compute_indicators(df)
    assert math.isnan(res.loc[3, "ma5"])
    assert res.loc[4, "ma5"] == 3.0            # (1+2+3+4+5)/5
    assert math.isnan(res.loc[18, "ma20"])
    assert res.loc[19, "ma20"] == 10.5          # 1..20 平均
    assert res.loc[24, "ma5"] == 23.0           # 21..25 平均
    assert abs(res.loc[1, "change_pct"] - 100.0) < 1e-9   # 1 → 2
    assert math.isnan(res.loc[0, "change_pct"])


def test_update_indicators_寫入並可由_history_取回(tmp_path):
    with open_db(tmp_path / "t.db") as conn:
        upsert_prices(conn, [_row("2330", f"2026-10-{i:02d}", float(i)) for i in range(1, 26)])
        assert update_indicators(conn, "2330") == 25
        last = get_latest(conn, "2330")
        assert last["ma5"] == 23.0 and last["ma20"] == 15.5


def test_趨勢判斷():
    assert classify_trend(10, 9, 8) == "多頭"
    assert classify_trend(7, 8, 9) == "空頭"
    assert classify_trend(9, 10, 8) == "盤整"
    assert classify_trend(10, None, 8) == "資料不足"
    assert classify_trend(10, float("nan"), 8) == "資料不足"
