import math
from datetime import datetime

import pandas as pd

from core.db import last_date_in, open_db
from core.extras import INDEX_SPECS, group_parties, normalize_foreign, normalize_index
from core.indicators import compute_indicators
from core.trend import classify_macd
from fetcher import FetchError, TZ, run_extras

CFG = {"stocks": ["2330"], "default_backfill_days": 30}
NOW = lambda: datetime(2026, 11, 20, 17, 30, tzinfo=TZ)   # noqa: E731


def test_macd_起算位置與數值():
    df = pd.DataFrame({"date": [f"d{i:03d}" for i in range(60)], "close": [float(i) for i in range(1, 61)]})
    res = compute_indicators(df)
    assert math.isnan(res.loc[24, "macd"]) and not math.isnan(res.loc[25, "macd"])
    assert math.isnan(res.loc[32, "macd_signal"]) and not math.isnan(res.loc[33, "macd_signal"])
    close = df["close"]
    dif = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    assert abs(res.loc[40, "macd"] - dif[40]) < 1e-9 and res.loc[40, "macd"] > 0
    assert abs(res.loc[50, "macd_hist"] - (res.loc[50, "macd"] - res.loc[50, "macd_signal"])) < 1e-12


def test_macd_判斷():
    assert classify_macd(1.0, -0.5) == "黃金交叉"
    assert classify_macd(-1.0, 0.5) == "死亡交叉"
    assert classify_macd(1.0, 0.5) == "偏多" and classify_macd(-1.0, -0.5) == "偏空"
    assert classify_macd(None) == "資料不足" and classify_macd(float("nan"), 1) == "資料不足"


def test_台指期只取日盤單一合約且選成交量最大者():
    spec = INDEX_SPECS[0]
    rows = [
        {"date": "2026-11-20", "contract_date": "202612", "trading_session": "position", "open": 1, "max": 2, "min": 0.5, "close": 1.5, "volume": 100, "open_interest": 9},
        {"date": "2026-11-20", "contract_date": "202701", "trading_session": "position", "open": 9, "max": 9, "min": 9, "close": 9, "volume": 5, "open_interest": 1},
        {"date": "2026-11-20", "contract_date": "202612", "trading_session": "after_market", "open": 7, "max": 7, "min": 7, "close": 7, "volume": 999, "open_interest": 0},
        {"date": "2026-11-20", "contract_date": "202702/202703", "trading_session": "position", "open": 8, "max": 8, "min": 8, "close": 8, "volume": 888, "open_interest": 0},
        {"date": "2026-11-19", "contract_date": "202612", "trading_session": "position", "open": 0, "max": 0, "min": 0, "close": 0, "volume": 0, "open_interest": 0},
    ]
    out = normalize_index(spec, rows)
    assert out == [("TX", "2026-11-20", 1, 2, 0.5, 1.5, 100, 9, "202612")]


def test_美股與加權欄位對應():
    us = normalize_index(INDEX_SPECS[2], [{"date": "2026-11-19", "Open": 1, "High": 3, "Low": 0.5, "Close": 2, "Volume": 7}])
    assert us == [("^DJI", "2026-11-19", 1, 3, 0.5, 2, 7, None, None)]
    tw = normalize_index(INDEX_SPECS[1], [{"date": "2026-11-20", "open": 1, "max": 3, "min": 0.5, "close": 2, "Trading_Volume": 7}])
    assert tw == [("TAIEX", "2026-11-20", 1, 3, 0.5, 2, 7, None, None)]


def test_法人彙整_不重複計入彙總自營商():
    rows = [
        {"date": "d1", "name": "Foreign_Investor", "buy": 1000, "sell": 400},
        {"date": "d1", "name": "Foreign_Dealer_Self", "buy": 100, "sell": 0},
        {"date": "d1", "name": "Investment_Trust", "buy": 0, "sell": 300},
        {"date": "d1", "name": "Dealer_self", "buy": 50, "sell": 10},
        {"date": "d1", "name": "Dealer_Hedging", "buy": 20, "sell": 0},
        {"date": "d1", "name": "Dealer", "buy": 70, "sell": 10},      # 已有自行買賣／避險，不可重複
        {"date": "d1", "name": "total", "buy": 12345, "sell": 1},      # 官方合計列忽略，改自行加總
    ]
    (day,) = group_parties(rows, divisor=10)
    assert day["foreign"] == {"buy": 110.0, "sell": 40.0, "net": 70.0}
    assert day["trust"]["net"] == -30.0
    assert day["dealer"] == {"buy": 7.0, "sell": 1.0, "net": 6.0}
    assert day["total"]["net"] == 46.0


def test_外資持股欄位對應():
    out = normalize_foreign([{
        "stock_id": "2330", "date": "d", "stock_name": "台積電", "ForeignInvestmentShares": 10,
        "ForeignInvestmentSharesRatio": 66.5, "ForeignInvestmentRemainingShares": 5,
        "ForeignInvestmentRemainRatio": 33.5, "NumberOfSharesIssued": 15, "ForeignInvestmentUpperLimitRatio": 100.0}])
    assert out == [("2330", "d", "台積電", 10, 66.5, 5, 33.5, 15, 100.0)]


def _fake_fetch(calls, fail_for=None):
    def fetch(dataset, params):
        calls.append((dataset, dict(params)))
        if fail_for and dataset == fail_for:
            raise FetchError("測試錯誤")
        if dataset == "TaiwanStockInstitutionalInvestorsBuySell":
            return [{"stock_id": "2330", "date": "2026-11-20", "name": "Foreign_Investor", "buy": 5, "sell": 2}]
        if dataset == "TaiwanStockShareholding":
            return [{"stock_id": "2330", "date": "2026-11-20", "stock_name": "台積電", "ForeignInvestmentShares": 1,
                     "ForeignInvestmentSharesRatio": 66.0}]
        if dataset == "TaiwanStockTotalInstitutionalInvestors":
            return [{"date": "2026-11-20", "name": "Foreign_Investor", "buy": 9, "sell": 4}]
        if dataset == "USStockPrice":
            return [{"date": "2026-11-19", "Open": 1, "High": 2, "Low": 0.5, "Close": 1.5, "Volume": 3}]
        return []
    return fetch


def test_extras_寫入各資料表並自動補洞(tmp_path):
    calls = []
    with open_db(tmp_path / "t.db") as conn:
        assert run_extras(CFG, conn, fetch_fn=_fake_fetch(calls), now_fn=NOW) == 0
        assert last_date_in(conn, "institutional", "2330") == "2026-11-20"
        assert last_date_in(conn, "foreign_holding", "2330") == "2026-11-20"
        assert last_date_in(conn, "total_institutional") == "2026-11-20"
        assert last_date_in(conn, "index_prices", "^DJI") == "2026-11-19"
        assert last_date_in(conn, "index_prices", "TX") is None            # 該資料集沒回傳資料
        first = [p for ds, p in calls if ds == "TaiwanStockInstitutionalInvestorsBuySell"][0]
        assert first["start_date"] == "2026-10-21"                          # 首次回補預設 30 天
        calls.clear()
        run_extras(CFG, conn, fetch_fn=_fake_fetch(calls), now_fn=NOW)       # 同一天再跑：已是最新，不再呼叫 API
        assert not [p for ds, p in calls if ds == "TaiwanStockInstitutionalInvestorsBuySell"]
        monday = lambda: datetime(2026, 11, 23, 17, 30, tzinfo=TZ)           # noqa: E731
        run_extras(CFG, conn, fetch_fn=_fake_fetch(calls), now_fn=monday)
        second = [p for ds, p in calls if ds == "TaiwanStockInstitutionalInvestorsBuySell"][0]
        assert second["start_date"] == "2026-11-21"                          # 從最後一天 + 1 補


def test_extras_單項失敗不影響其他(tmp_path):
    calls = []
    with open_db(tmp_path / "t.db") as conn:
        failures = run_extras(CFG, conn, fetch_fn=_fake_fetch(calls, fail_for="TaiwanStockShareholding"), now_fn=NOW)
        assert failures == 1
        assert last_date_in(conn, "institutional", "2330") == "2026-11-20"
        assert last_date_in(conn, "foreign_holding", "2330") is None
        msg = conn.execute("SELECT message FROM fetch_log WHERE status='error'").fetchone()["message"]
        assert "外資持股" in msg and "測試錯誤" in msg
