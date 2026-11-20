import json
from datetime import date, timedelta

from build_site import build, main
from core.db import (
    open_db, upsert_foreign_holding, upsert_index_prices, upsert_institutional, upsert_prices,
    upsert_total_institutional,
)
from core.indicators import update_indicators


def _seed(path):
    # 45 個交易日，最後一天 2026-10-28（足夠算出 MACD 訊號線，需 34 筆）
    dates = [(date(2026, 10, 28) - timedelta(days=44 - i)).isoformat() for i in range(45)]
    rows = [
        {"stock_id": "2330", "date": d, "open": float(i + 1), "max": float(i + 1), "min": float(i + 1),
         "close": float(i + 1), "spread": 1.0, "Trading_Volume": 1000, "Trading_money": 1}
        for i, d in enumerate(dates)
    ]
    with open_db(path) as conn:
        upsert_prices(conn, rows)
        update_indicators(conn, "2330")
        upsert_institutional(conn, [
            ("2330", "2026-10-27", "Foreign_Investor", 5_000_000, 2_000_000),
            ("2330", "2026-10-27", "Investment_Trust", 100_000, 400_000),
            ("2330", "2026-10-27", "Dealer_self", 0, 0),
            ("2330", "2026-10-28", "Foreign_Investor", 1_000_000, 3_000_000),
        ])
        upsert_foreign_holding(conn, [
            ("2330", "2026-10-27", "台積電", 17_000_000_000, 66.5, 8_000_000_000, 33.5, 25_000_000_000, 100.0),
            ("2330", "2026-10-28", "台積電", 17_100_000_000, 66.9, 7_900_000_000, 33.1, 25_000_000_000, 100.0),
        ])
        upsert_total_institutional(conn, [
            ("2026-10-28", "Foreign_Investor", 300_000_000_000, 250_000_000_000),
            ("2026-10-28", "total", 1, 1),
        ])
        upsert_index_prices(conn, [
            ("TAIEX", "2026-10-27", 100.0, 110.0, 90.0, 100.0, 1000, None, None),
            ("TAIEX", "2026-10-28", 100.0, 120.0, 95.0, 110.0, 1200, None, None),
            ("TX", "2026-10-28", 1, 2, 0.5, 1.5, 10, 5, "202611"),
        ])


def _cfg(tmp_path, stocks):
    p = tmp_path / "config.yaml"
    p.write_text("stocks: " + json.dumps(stocks), encoding="utf-8")
    return p


def test_產生靜態網站(tmp_path):
    db = tmp_path / "t.db"
    _seed(db)
    out = tmp_path / "site"
    stocks = build(out, db, _cfg(tmp_path, ["2330", "9999"]))
    assert stocks == ["2330"]                       # 沒資料的股票略過

    d = json.loads((out / "data" / "2330.json").read_text(encoding="utf-8"))
    assert d["updated"] == "2026-10-28" and len(d["rows"]) == 45        # 輸出全部歷史，由前端依日期區間篩選
    assert "quote" not in d
    first, last = d["rows"][0], d["rows"][-1]
    assert first["trend"] == "資料不足" and first["macd_state"] == "資料不足"
    assert last["trend"] == "多頭" and last["macd"] is not None and last["macd_state"] in ("偏多", "黃金交叉")

    html = (out / "index.html").read_text(encoding="utf-8")
    assert "<title>AC台股分析</title>" in html and "個股分析" in html and "整體三大法人買賣表" in html
    assert '<option value="2330">2330 台積電</option>' in html and "9999" not in html
    # 日期選擇器的可選範圍 = 價格資料的第一天與最後一天
    assert 'data-min="2026-09-14"' in html and 'data-max="2026-10-28"' in html
    assert 'id="r-start"' in html and 'id="r-end"' in html and 'data-p="all"' in html
    assert (out / ".nojekyll").exists()
    # 公開網站是純靜態頁面，不含對話分頁與 /api/chat 呼叫（金鑰不可能放在公開網頁）
    assert "page-chat" not in html and "AI 對話分析" not in html and "/api/chat" not in html

    local = tmp_path / "site_local"
    build(local, db, _cfg(tmp_path, ["2330"]), chat_enabled=True)
    local_html = (local / "index.html").read_text(encoding="utf-8")
    assert 'id="page-chat"' in local_html and 'data-page="chat"' in local_html and "/api/chat" in local_html

    inst =json.loads((out / "data" / "inst" / "2330.json").read_text(encoding="utf-8"))
    day = inst["rows"][0]                            # 2026-10-27，單位換算成張
    assert day["date"] == "2026-10-27" and day["foreign"]["net"] == 3000.0 and day["trust"]["net"] == -300.0
    assert day["total"]["net"] == 2700.0

    fo = json.loads((out / "data" / "foreign.json").read_text(encoding="utf-8"))
    assert fo["stocks"][0]["series"][-1]["shares"] == 17_100_000.0 and fo["stocks"][0]["name"] == "台積電"

    ix = {i["symbol"]: i for i in json.loads((out / "data" / "indices.json").read_text(encoding="utf-8"))["indices"]}
    assert [i for i in ix] == ["TX", "TAIEX", "^DJI", "^SOX", "^IXIC"]
    assert ix["TAIEX"]["rows"][-1]["change"] == 10.0 and round(ix["TAIEX"]["rows"][-1]["change_pct"], 2) == 10.0
    assert ix["^DJI"]["rows"] == [] and ix["TX"]["note"] == "202611"

    tot = json.loads((out / "data" / "total_inst.json").read_text(encoding="utf-8"))
    assert tot["rows"][0]["foreign"]["net"] == 500.0   # 億元


def test_沒有任何資料時回傳失敗(tmp_path):
    db = tmp_path / "t.db"
    _seed(db)
    code = main(["--out", str(tmp_path / "s"), "--db", str(db), "--config", str(_cfg(tmp_path, ["0000"]))])
    assert code == 1
