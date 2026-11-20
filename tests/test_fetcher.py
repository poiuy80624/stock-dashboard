from datetime import date, datetime, timedelta

import pytest
import requests

import fetcher
from core.db import last_date, open_db, upsert_prices
from fetcher import FetchError, QuotaExceededError, TZ, run

CFG = {
    "stocks": ["2330"],
    "retry": {"interval_minutes": 15, "deadline": "19:00"},
    "market_close": "13:30",
    "default_backfill_days": 120,
}


class Clock:
    """假時鐘：sleep 會把時間往前推。"""

    def __init__(self, start: datetime):
        self.t = start
        self.sleeps = 0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps += 1
        self.t += timedelta(seconds=seconds)


def _row(sid, d, close=100.0):
    return {"stock_id": sid, "date": d, "open": close, "max": close, "min": close,
            "close": close, "spread": 0.0, "Trading_Volume": 1, "Trading_money": 1}


def _clock(h, m, day=(2026, 11, 20)):
    return Clock(datetime(*day, h, m, tzinfo=TZ))   # 2026-11-20 是星期五


def test_空資料觸發重試_之後取得資料即停止(tmp_path):
    clk = _clock(17, 30)
    calls = []

    def fetch(sid, s, e):
        calls.append((s, e))
        return [] if len(calls) < 3 else [_row(sid, "2026-11-20")]

    with open_db(tmp_path / "t.db") as conn:
        upsert_prices(conn, [_row("2330", "2026-11-19")])
        code = run(CFG, conn, fetch_fn=fetch, trading_day_fn=lambda d: True,
                   now_fn=clk.now, sleep_fn=clk.sleep)
        assert code == 0
        assert clk.sleeps == 2
        assert last_date(conn, "2330") == "2026-11-20"
        assert calls[0] == ("2026-11-20", "2026-11-20")   # 自動補洞：從最後一天 + 1 開始


def test_超過_19點停止重試並記錄(tmp_path):
    clk = _clock(17, 30)
    with open_db(tmp_path / "t.db") as conn:
        upsert_prices(conn, [_row("2330", "2026-11-19")])
        code = run(CFG, conn, fetch_fn=lambda *a: [], trading_day_fn=lambda d: True,
                   now_fn=clk.now, sleep_fn=clk.sleep)
        assert code == 0
        assert clk.sleeps == 6                      # 17:30 → 19:00 共 6 次等待
        assert clk.t.hour == 19 and clk.t.minute == 0   # 不會超過 19:00
        status = [r["status"] for r in conn.execute("SELECT status FROM fetch_log")]
        assert "missing" in status


def test_已過截止時間補跑只抓一次不等待(tmp_path):
    clk = _clock(21, 0)
    with open_db(tmp_path / "t.db") as conn:
        upsert_prices(conn, [_row("2330", "2026-11-17")])
        run(CFG, conn, fetch_fn=lambda sid, s, e: [_row(sid, "2026-11-18"), _row(sid, "2026-11-19")],
            trading_day_fn=lambda d: True, now_fn=clk.now, sleep_fn=clk.sleep)
        assert clk.sleeps == 0
        assert last_date(conn, "2330") == "2026-11-19"


def test_非交易日與週末不等待(tmp_path):
    with open_db(tmp_path / "t.db") as conn:
        upsert_prices(conn, [_row("2330", "2026-11-19")])
        clk = _clock(17, 30)
        run(CFG, conn, fetch_fn=lambda *a: [], trading_day_fn=lambda d: False,
            now_fn=clk.now, sleep_fn=clk.sleep)
        assert clk.sleeps == 0
        clk2 = _clock(17, 30, day=(2026, 11, 21))   # 星期六
        run(CFG, conn, fetch_fn=lambda *a: [], trading_day_fn=lambda d: True,
            now_fn=clk2.now, sleep_fn=clk2.sleep)
        assert clk2.sleeps == 0


def test_no_wait_只抓一次(tmp_path):
    clk = _clock(17, 30)
    with open_db(tmp_path / "t.db") as conn:
        upsert_prices(conn, [_row("2330", "2026-11-19")])
        run(CFG, conn, wait=False, fetch_fn=lambda *a: [], trading_day_fn=lambda d: True,
            now_fn=clk.now, sleep_fn=clk.sleep)
        assert clk.sleeps == 0


def test_單檔失敗不影響其他檔且寫入_log(tmp_path):
    cfg = {**CFG, "stocks": ["2330", "2317"]}
    clk = _clock(17, 30)

    def fetch(sid, s, e):
        if sid == "2330":
            raise FetchError("測試錯誤")
        return [_row(sid, "2026-11-20")]

    with open_db(tmp_path / "t.db") as conn:
        upsert_prices(conn, [_row("2330", "2026-11-19"), _row("2317", "2026-11-19")])
        code = run(cfg, conn, wait=False, fetch_fn=fetch, trading_day_fn=lambda d: True,
                   now_fn=clk.now, sleep_fn=clk.sleep)
        assert code == 1
        assert last_date(conn, "2317") == "2026-11-20"
        rows = conn.execute("SELECT stock_id, status, message FROM fetch_log WHERE status='error'").fetchall()
        assert any(r["stock_id"] == "2330" and "測試錯誤" in r["message"] for r in rows)


def test_網路例外視為暫時性錯誤(tmp_path):
    clk = _clock(17, 30)

    def fetch(*a):
        raise requests.ConnectionError("連線失敗")

    with open_db(tmp_path / "t.db") as conn:
        upsert_prices(conn, [_row("2330", "2026-11-19")])
        assert run(CFG, conn, wait=False, fetch_fn=fetch, now_fn=clk.now, sleep_fn=clk.sleep) == 1


def test_代號無資料_寫入錯誤且不重試(tmp_path):
    clk = _clock(17, 30)
    cfg = {**CFG, "stocks": ["9999"]}
    with open_db(tmp_path / "t.db") as conn:
        code = run(cfg, conn, fetch_fn=lambda *a: [], trading_day_fn=lambda d: True,
                   now_fn=clk.now, sleep_fn=clk.sleep)
        assert code == 1
        assert clk.sleeps == 0
        msg = conn.execute("SELECT message FROM fetch_log WHERE stock_id='9999'").fetchone()["message"]
        assert "代號" in msg


def test_額度用完中止並寫入_log(tmp_path):
    cfg = {**CFG, "stocks": ["2330", "2317"]}
    clk = _clock(17, 30)

    def fetch(*a):
        raise QuotaExceededError("額度用完")

    with open_db(tmp_path / "t.db") as conn:
        upsert_prices(conn, [_row("2330", "2026-11-19")])
        code = run(cfg, conn, fetch_fn=fetch, trading_day_fn=lambda d: True,
                   now_fn=clk.now, sleep_fn=clk.sleep)
        assert code == 1
        assert clk.sleeps == 0
        assert conn.execute("SELECT COUNT(*) FROM fetch_log WHERE message LIKE '%額度%'").fetchone()[0] == 1


def test_首次執行回補預設_120_天(tmp_path):
    clk = _clock(17, 30)
    seen = []
    with open_db(tmp_path / "t.db") as conn:
        run(CFG, conn, wait=False, fetch_fn=lambda sid, s, e: seen.append(s) or [_row(sid, "2026-11-20")],
            now_fn=clk.now, sleep_fn=clk.sleep)
    assert seen[0] == (date(2026, 11, 20) - timedelta(days=120)).isoformat()


class _Resp:
    def __init__(self, status_code, body=None, reason="x"):
        self.status_code, self._b, self.reason = status_code, body, reason

    def json(self):
        if self._b is None:
            raise ValueError
        return self._b


class _Sess:
    def __init__(self, resp):
        self.resp, self.kwargs = resp, None

    def get(self, url, **kw):
        self.kwargs = kw
        return self.resp


def test_fetch_dataset_錯誤分類與_bearer_標頭():
    ok = _Sess(_Resp(200, {"status": 200, "msg": "success", "data": [{"a": 1}]}))
    assert fetcher.fetch_dataset("TaiwanStockPrice", {}, "TOK", ok) == [{"a": 1}]
    assert ok.kwargs["headers"]["Authorization"] == "Bearer TOK"
    with pytest.raises(QuotaExceededError):
        fetcher.fetch_dataset("x", {}, "t", _Sess(_Resp(402, {"status": 402, "msg": "limit"})))
    with pytest.raises(QuotaExceededError):
        fetcher.fetch_dataset("x", {}, "t", _Sess(_Resp(200, {"status": 402, "msg": "limit"})))
    with pytest.raises(FetchError):
        fetcher.fetch_dataset("x", {}, "t", _Sess(_Resp(500, None)))
    with pytest.raises(FetchError):
        fetcher.fetch_dataset("x", {}, "t", _Sess(_Resp(200, {"status": 400, "msg": "bad token"})))


def test_缺少_token(monkeypatch):
    monkeypatch.delenv("FINMIND_TOKEN", raising=False)
    with pytest.raises(fetcher.MissingTokenError):
        fetcher.get_token()
