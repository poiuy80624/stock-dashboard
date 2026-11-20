"""產生可放上 GitHub Pages 的靜態網站：site/index.html + site/data/*.json。

只讀 SQLite（唯讀連線），不呼叫 FinMind、不需要 token。

資料檔：
  data/{代號}.json        個股日 K、MA、MACD、最新報價
  data/inst/{代號}.json   個股三大法人買賣（單位：張）
  data/foreign.json       各股外資持股比例走勢
  data/indices.json       台指期、加權、道瓊、費半、那斯達克
  data/total_inst.json    整體三大法人買賣（單位：億元）
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

from core.db import (
    ROOT, get_foreign_holding, get_history, get_index_series, get_institutional,
    get_total_institutional, open_db,
)
from core.extras import INDEX_SPECS, group_parties
from core.view import with_states

log = logging.getLogger("build_site")
ALL = 100_000        # 輸出資料庫內全部歷史，由前端依使用者選的日期區間篩選
SITE_TITLE = "AC台股分析"


def _dump(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _with_change(rows: list[dict]) -> list[dict]:
    """為指數列加上相對前一日收盤的漲跌與漲跌幅（%）。"""
    out, prev = [], None
    for r in rows:
        item = dict(r)
        if prev:
            item["change"] = round(r["close"] - prev, 4)
            item["change_pct"] = round((r["close"] / prev - 1) * 100, 4)
        else:
            item["change"] = item["change_pct"] = None
        prev = r["close"]
        out.append(item)
    return out


def build(out_dir: Path, db_path: Path, config_path: Path) -> list[str]:
    """產生靜態網站，回傳有資料的股票代號清單。"""
    with open(config_path, encoding="utf-8") as f:
        wanted = [str(s) for s in yaml.safe_load(f)["stocks"]]

    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "data").mkdir(parents=True)

    stocks: list[dict] = []
    foreign_stocks: list[dict] = []
    first_dates: list[str] = []
    last_dates: list[str] = []
    with open_db(db_path, readonly=True) as conn:
        for sid in wanted:
            rows = get_history(conn, sid, ALL)
            if not rows:
                log.warning("%s 沒有價格資料，略過", sid)
                continue
            first_dates.append(rows[0]["date"])
            last_dates.append(rows[-1]["date"])
            _dump(out_dir / "data" / f"{sid}.json", {
                "stock_id": sid,
                "updated": rows[-1]["date"],
                "rows": with_states(rows),
            })

            fh = get_foreign_holding(conn, sid, ALL)
            name = next((r["stock_name"] for r in reversed(fh) if r.get("stock_name")), "")
            stocks.append({"id": sid, "name": name})
            if fh:
                foreign_stocks.append({
                    "stock_id": sid,
                    "name": name,
                    "series": [
                        {
                            "date": r["date"],
                            "ratio": r["foreign_ratio"],
                            "shares": None if r["foreign_shares"] is None else r["foreign_shares"] / 1000,  # 張
                            "remain_ratio": r["remain_ratio"],
                            "upper_limit": r["upper_limit_ratio"],
                            "issued": None if r["issued_shares"] is None else r["issued_shares"] / 1000,
                        }
                        for r in fh
                    ],
                })

            _dump(out_dir / "data" / "inst" / f"{sid}.json", {
                "stock_id": sid,
                "unit": "張",
                "rows": group_parties(get_institutional(conn, sid, ALL), divisor=1000),
            })

        _dump(out_dir / "data" / "foreign.json", {"unit": "張", "stocks": foreign_stocks})

        indices = []
        for spec in INDEX_SPECS:
            series = get_index_series(conn, spec["symbol"], ALL)
            indices.append({
                "symbol": spec["symbol"],
                "name": spec["name"],
                "note": series[-1]["note"] if series and spec["symbol"] == "TX" else None,
                "rows": _with_change(series),
            })
        _dump(out_dir / "data" / "indices.json", {"indices": indices})

        _dump(out_dir / "data" / "total_inst.json", {
            "unit": "億元",
            "rows": group_parties(get_total_institutional(conn, ALL), divisor=1e8),
        })

    env = Environment(
        loader=FileSystemLoader(str(ROOT / "web" / "templates")),
        autoescape=select_autoescape(["html"]),
    )
    html = env.get_template("index.html").render(
        title=SITE_TITLE,
        stocks=stocks,
        min_date=min(first_dates) if first_dates else "",     # 日期選擇器可選範圍
        max_date=max(last_dates) if last_dates else "",
        built_at=datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M（台北時間）"),
    )
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")   # 不要讓 GitHub 用 Jekyll 處理
    return [s["id"] for s in stocks]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="產生靜態網站")
    ap.add_argument("--out", default=str(ROOT / "site"))
    ap.add_argument("--db", default=str(ROOT / "data" / "stock.db"))
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    args = ap.parse_args(argv)

    stocks = build(Path(args.out), Path(args.db), Path(args.config))
    if not stocks:
        log.error("所有股票都沒有資料，不產生網站")
        return 1
    log.info("已產生 %d 檔股票的靜態網站：%s", len(stocks), args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
