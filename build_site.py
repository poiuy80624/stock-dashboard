"""產生可放上 GitHub Pages 的靜態網站：site/index.html + site/data/{代號}.json。

只讀 SQLite（唯讀連線），不呼叫 FinMind、不需要 token。
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

from core.db import ROOT, get_history, open_db
from core.view import quote_dict

log = logging.getLogger("build_site")
KEEP_DAYS = 250   # 網頁最長可選 250 日


def build(out_dir: Path, db_path: Path, config_path: Path) -> list[str]:
    """產生靜態網站，回傳有資料的股票代號清單。"""
    with open(config_path, encoding="utf-8") as f:
        wanted = [str(s) for s in yaml.safe_load(f)["stocks"]]

    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "data").mkdir(parents=True)

    stocks: list[str] = []
    with open_db(db_path, readonly=True) as conn:
        for sid in wanted:
            rows = get_history(conn, sid, KEEP_DAYS)
            if not rows:
                log.warning("%s 沒有資料，略過", sid)
                continue
            payload = {
                "stock_id": sid,
                "updated": rows[-1]["date"],
                "quote": quote_dict(sid, rows[-1]),
                "rows": rows,
            }
            (out_dir / "data" / f"{sid}.json").write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
            )
            stocks.append(sid)

    env = Environment(
        loader=FileSystemLoader(str(ROOT / "web" / "templates")),
        autoescape=select_autoescape(["html"]),
    )
    html = env.get_template("index.html").render(
        stocks=stocks,
        static=True,
        built_at=datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M（台北時間）"),
    )
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")   # 不要讓 GitHub 用 Jekyll 處理
    return stocks


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
