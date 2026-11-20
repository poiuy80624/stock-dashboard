import json

from build_site import build, main
from core.db import open_db, upsert_prices
from core.indicators import update_indicators


def _seed(path):
    rows = [
        {"stock_id": "2330", "date": f"2026-10-{i:02d}", "open": float(i), "max": float(i), "min": float(i),
         "close": float(i), "spread": 1.0, "Trading_Volume": 1000, "Trading_money": 1}
        for i in range(1, 26)
    ]
    with open_db(path) as conn:
        upsert_prices(conn, rows)
        update_indicators(conn, "2330")


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
    assert d["updated"] == "2026-10-25" and d["quote"]["trend"] == "多頭" and len(d["rows"]) == 25
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "const STATIC = true" in html and '<option value="2330">' in html and "9999" not in html
    assert (out / ".nojekyll").exists()


def test_沒有任何資料時回傳失敗(tmp_path):
    db = tmp_path / "t.db"
    _seed(db)
    code = main(["--out", str(tmp_path / "s"), "--db", str(db), "--config", str(_cfg(tmp_path, ["0000"]))])
    assert code == 1
