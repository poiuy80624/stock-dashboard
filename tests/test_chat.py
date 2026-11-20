import importlib
import json
from datetime import date, timedelta
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

from ac_chat import engine
from ac_chat.tools import TOOL_SCHEMAS, Toolbox
from core.db import (
    open_db, upsert_foreign_holding, upsert_index_prices, upsert_institutional, upsert_month_revenue,
    upsert_prices, upsert_total_institutional, upsert_valuation,
)
from core.indicators import update_indicators

LAST = date(2026, 10, 28)
DATES = [(LAST - timedelta(days=44 - i)).isoformat() for i in range(45)]


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "t.db"
    with open_db(p) as conn:
        upsert_prices(conn, [
            {"stock_id": "2330", "date": d, "open": i + 1.0, "max": i + 2.0, "min": i + 0.5, "close": i + 1.0,
             "spread": 1.0, "Trading_Volume": 1000 + i, "Trading_money": 1}
            for i, d in enumerate(DATES)
        ])
        update_indicators(conn, "2330")
        upsert_valuation(conn, [("2330", d, 1.5, 10.0 + i * 0.1, 2.0) for i, d in enumerate(DATES)])
        upsert_foreign_holding(conn, [("2330", d, "台積電", 17_000_000_000 + i, 66.0 + i * 0.01, 8e9, 33.0, 25e9, 100.0)
                                      for i, d in enumerate(DATES)])
        upsert_institutional(conn, [
            ("2330", d, n, b, s) for d in DATES
            for n, b, s in (("Foreign_Investor", 5_000_000, 2_000_000), ("Investment_Trust", 100_000, 400_000),
                            ("Dealer_self", 10_000, 0))
        ])
        upsert_total_institutional(conn, [(d, "Foreign_Investor", 300_000_000_000, 250_000_000_000) for d in DATES])
        upsert_index_prices(conn, [("TAIEX", d, 100.0, 110.0, 90.0, 100.0 + i, 1, None, None) for i, d in enumerate(DATES)])
        upsert_month_revenue(conn, [
            ("2330", "2025-09-01", 100e8, 8, 2025), ("2330", "2025-10-01", 110e8, 9, 2025),
            ("2330", "2026-09-01", 130e8, 8, 2026), ("2330", "2026-10-01", 143e8, 9, 2026),
        ])
    return p


@pytest.fixture
def tb(db):
    return Toolbox(db, ["2330", "2317"])


def call(tb, name, **args):
    text, is_error = tb.run(name, args)
    return (text if is_error else json.loads(text)), is_error


# ---------- 工具 ----------
def test_工具定義都有對應實作與必要欄位(tb):
    for t in TOOL_SCHEMAS:
        assert hasattr(tb, f"t_{t['name']}") and t["description"] and t["input_schema"]["type"] == "object"


def test_snapshot(tb):
    d, err = call(tb, "get_stock_snapshot", stock_id="2330")
    assert not err
    assert d["date"] == LAST.isoformat() and d["ma_trend"] == "多頭" and d["macd"]["state"] in ("偏多", "黃金交叉")
    assert d["return_pct"]["5d"] is not None and d["return_pct"]["250d"] is None       # 只有 45 日資料
    assert d["valuation"]["per_percentile_in_history"] == 100                          # PER 持續上升，目前最高
    assert d["institutional_net_lots"]["last_5d"]["foreign"] == 15000                  # (500萬-200萬股)/1000 × 5 天
    assert d["foreign_holding"]["change_20d_pct_pt"] > 0


def test_price_history_期間與抽樣(tb):
    d, err = call(tb, "get_price_history", stock_id="2330", start_date="2026-09-14", end_date="2026-10-28", max_rows=10)
    assert not err and d["trading_days"] == 45 and len(d["rows"]) <= 11 and d["downsampled"]
    assert d["rows"][-1][0] == "2026-10-28" and d["period_return_pct"] == round((45 / 1 - 1) * 100, 2)
    d, _ = call(tb, "get_price_history", stock_id="2330", start_date="2026-10-28", end_date="2026-09-14")   # 顛倒會自動對調
    assert d["trading_days"] == 45


def test_法人_營收_估值_指數_整體(tb):
    f, _ = call(tb, "get_institutional_flow", stock_id="2330", start_date="2026-10-20")
    assert f["unit"] == "張" and f["latest_streak"]["foreign"]["direction"] == "連續買超"
    assert f["latest_streak"]["trust"]["direction"] == "連續賣超" and f["period_net"]["foreign"] == 3000 * f["trading_days"]
    r, _ = call(tb, "get_month_revenue", stock_id="2330", months=12)
    assert r["rows"][-1] == ["2026-09", 143.0, round((143 / 130 - 1) * 100, 2), round((143 / 110 - 1) * 100, 2)]
    v, _ = call(tb, "get_valuation", stock_id="2330")
    assert v["per_stats"]["max"] == v["latest"]["per"] and v["per_stats"]["latest_percentile"] == 100
    h, _ = call(tb, "get_foreign_holding", stock_id="2330")
    assert h["change_pct_pt"] > 0 and h["name"] == "台積電"
    ix, _ = call(tb, "get_index_performance")
    names = {i["name"]: i for i in ix["indices"]}
    assert names["台股加權指數"]["period_return_pct"] is not None and names["道瓊工業指數"]["note"] == "此期間無資料"
    m, _ = call(tb, "get_market_institutional")
    assert m["unit"] == "億元" and m["period_net"]["foreign"] == 500.0 * m["trading_days"]
    s, _ = call(tb, "list_stocks")
    assert s["latest_data_date"] == LAST.isoformat() and s["stocks"][0]["name"] == "台積電"


def test_工具錯誤會變成給_AI_的訊息而不是例外(tb):
    text, err = tb.run("get_stock_snapshot", {"stock_id": "9999"})
    assert err and "不在追蹤清單" in text and "2330" in text
    text, err = tb.run("get_stock_snapshot", {"stock_id": "台積電"})
    assert err and "代號" in text
    text, err = tb.run("get_price_history", {"stock_id": "2330", "start_date": "昨天"})
    assert err and "YYYY-MM-DD" in text
    text, err = tb.run("get_price_history", {"stock_id": "2330", "max_rows": "很多"})
    assert err and "參數有誤" in text
    text, err = tb.run("no_such_tool", {})
    assert err and "未知" in text
    text, err = tb.run("get_month_revenue", {"stock_id": "2317"})            # 在清單內但沒有資料
    assert err and "沒有月營收" in text
    text, err = tb.run("get_price_history", {"stock_id": "2330", "start_date": "2000-01-01", "end_date": "2000-02-01"})
    assert err and "沒有交易資料" in text


def test_工具只讀(tb, db):
    tb.run("get_stock_snapshot", {"stock_id": "2330"})
    with open_db(db, readonly=True) as conn:
        with pytest.raises(Exception):
            conn.execute("DELETE FROM prices")


# ---------- 引擎（假的 Claude 客戶端） ----------
def _resp(content, stop="end_turn", i=100, o=20):
    return SimpleNamespace(content=content, stop_reason=stop, usage=SimpleNamespace(input_tokens=i, output_tokens=o))


def _tool_use(name, args, id="tu_1"):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=args)


def _text(t):
    return SimpleNamespace(type="text", text=t)


class FakeClient:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _api_error(cls, status):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("x", response=httpx2.Response(status, request=req), body=None)


def test_引擎_工具往返後產生最終回答(tb):
    fake = FakeClient([
        _resp([_text("我先查一下"), _tool_use("get_stock_snapshot", {"stock_id": "2330"})], "tool_use"),
        _resp([_text("結論：偏多。")], "end_turn", 300, 50),
    ])
    events = list(engine.run_chat(fake, [{"role": "user", "content": "2330 趨勢？"}], tb))
    assert [e["type"] for e in events] == ["tool", "text", "done"]
    assert events[0]["label"] == "查詢 2330 目前整體狀況" and events[1]["text"] == "結論：偏多。"
    assert events[2]["usage"] == {"input_tokens": 400, "output_tokens": 70} and events[2]["model"] == "claude-sonnet-5-5"

    first, second = fake.calls
    assert first["model"] == "claude-sonnet-5-5" and first["output_config"] == {"effort": "medium"}
    assert len(first["tools"]) == len(TOOL_SCHEMAS)
    assert "絕對不要編造" in first["system"] and "非投資建議" in first["system"] and "未還原股價" in first["system"]
    assert LAST.isoformat() in first["system"] and "2330、2317" in first["system"]
    # 第二輪要把 assistant 的 tool_use 與對應的 tool_result 原樣帶回
    assert second["messages"][-2]["role"] == "assistant" and second["messages"][-1]["role"] == "user"
    result = second["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "tu_1" and "is_error" not in result
    assert json.loads(result["content"])["stock_id"] == "2330"


def test_引擎_工具錯誤會標記_is_error_並讓模型自行修正(tb):
    fake = FakeClient([
        _resp([_tool_use("get_stock_snapshot", {"stock_id": "9999"})], "tool_use"),
        _resp([_text("該股票不在清單內。")]),
    ])
    events = list(engine.run_chat(fake, [{"role": "user", "content": "9999 呢？"}], tb))
    assert events[-2]["type"] == "text"
    result = fake.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "不在追蹤清單" in result["content"]


def test_引擎_可用環境變數換模型與強度(tb, monkeypatch):
    monkeypatch.setenv("AC_CHAT_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("AC_CHAT_EFFORT", "high")
    fake = FakeClient([_resp([_text("ok")])])
    list(engine.run_chat(fake, [{"role": "user", "content": "hi"}], tb))
    assert fake.calls[0]["model"] == "claude-opus-5-5" and fake.calls[0]["output_config"] == {"effort": "high"}


@pytest.mark.parametrize("cls,status,keyword", [
    (anthropic.AuthenticationError, 401, "金鑰"), (anthropic.RateLimitError, 429, "頻繁"),
    (anthropic.NotFoundError, 404, "模型"), (anthropic.BadRequestError, 400, "額度"),
])
def test_引擎_API_錯誤轉成中文訊息(tb, cls, status, keyword):
    fake = FakeClient([_api_error(cls, status)])
    events = list(engine.run_chat(fake, [{"role": "user", "content": "hi"}], tb))
    assert events == [{"type": "error", "message": events[0]["message"]}] and keyword in events[0]["message"]


def test_引擎_拒絕_截斷_與迴圈上限(tb):
    ev = list(engine.run_chat(FakeClient([_resp([], "refusal")]), [{"role": "user", "content": "x"}], tb))
    assert ev[-1]["type"] == "error" and "安全機制" in ev[-1]["message"]
    ev = list(engine.run_chat(FakeClient([_resp([_text("很長…")], "max_tokens")]), [{"role": "user", "content": "x"}], tb))
    assert "截斷" in ev[0]["text"]
    loop = FakeClient([_resp([_tool_use("list_stocks", {}, f"t{i}")], "tool_use") for i in range(engine.MAX_ITERATIONS)])
    ev = list(engine.run_chat(loop, [{"role": "user", "content": "x"}], tb))
    assert ev[-1]["type"] == "error" and "查詢次數過多" in ev[-1]["message"] and len(loop.calls) == engine.MAX_ITERATIONS


# ---------- HTTP 介面 ----------
@pytest.fixture
def make_client(tmp_path, monkeypatch, db):
    monkeypatch.setenv("STOCK_DB", str(db))
    monkeypatch.setenv("STOCK_SITE_DIR", str(tmp_path / "site"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text('stocks: ["2330", "2317"]', encoding="utf-8")

    def make(fake):
        from ac_chat.api import make_chat_router
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(make_chat_router(db_path=db, config_path=cfg, client_factory=lambda: fake))
        return TestClient(app)
    return make


def test_api_串流事件與狀態(make_client):
    fake = FakeClient([_resp([_tool_use("list_stocks", {})], "tool_use"), _resp([_text("好的")])])
    c = make_client(fake)
    st = c.get("/api/chat/status").json()
    assert st["configured"] is True and st["model"] == "claude-sonnet-5-5"
    assert [m["id"] for m in st["models"]] == ["claude-sonnet-5-5", "claude-opus-5-5"] and all(m["label"] and m["hint"] for m in st["models"])
    r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "有哪些股票？"}]})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in r.text.strip().split("\n")]
    assert [e["type"] for e in events] == ["tool", "text", "done"] and events[1]["text"] == "好的"


def test_api_安全檢查(make_client):
    c = make_client(FakeClient([]))
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert c.post("/api/chat", json=body, headers={"Origin": "https://evil.example"}).status_code == 403   # 跨站呼叫
    assert c.post("/api/chat", json={"messages": [{"role": "assistant", "content": "x"}]}).status_code == 400
    assert c.post("/api/chat", json={"messages": []}).status_code == 422
    assert c.post("/api/chat", json={"messages": [{"role": "system", "content": "x"}]}).status_code == 422
    assert c.post("/api/chat", json={"messages": [{"role": "user", "content": "a" * 9000}]}).status_code == 422
    nokey = make_client(None)
    assert nokey.get("/api/chat/status").json()["configured"] is False
    r = nokey.post("/api/chat", json=body)
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_api_模型切換與白名單(make_client, monkeypatch):
    fake = FakeClient([_resp([_text("a")]), _resp([_text("b")]), _resp([_text("c")])])
    c = make_client(fake)
    msg = [{"role": "user", "content": "hi"}]
    c.post("/api/chat", json={"messages": msg})                                         # 省略 → 預設
    c.post("/api/chat", json={"messages": msg, "model": "claude-opus-5-5"})              # 畫面選了 Opus
    c.post("/api/chat", json={"messages": msg, "model": None})
    assert [k["model"] for k in fake.calls] == ["claude-sonnet-5-5", "claude-opus-5-5", "claude-sonnet-5-5"]
    r = c.post("/api/chat", json={"messages": msg, "model": "claude-not-allowed"})      # 不在白名單 → 拒絕，且不會呼叫 API
    assert r.status_code == 400 and "不支援" in r.json()["detail"] and len(fake.calls) == 3
    # 用環境變數指定清單外的模型時，會成為預設並出現在可選清單
    monkeypatch.setenv("AC_CHAT_MODEL", "claude-custom-x")
    st = c.get("/api/chat/status").json()
    assert st["model"] == "claude-custom-x" and st["models"][-1]["id"] == "claude-custom-x"


def test_api_只送最近的訊息(make_client):
    fake = FakeClient([_resp([_text("ok")])])
    c = make_client(fake)
    msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(31)]   # 以 user 結尾
    c.post("/api/chat", json={"messages": msgs})
    sent = fake.calls[0]["messages"]
    assert len(sent) == 20 and sent[-1]["content"] == "m30"
