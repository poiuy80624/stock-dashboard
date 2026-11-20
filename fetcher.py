"""抓取 FinMind 台股日成交資料並寫入 SQLite。

特色：
- 自動補洞：每次執行從各股「最後一天 + 1」抓到今天，漏跑幾天下次自動補齊。
- 重試：收盤後若當日資料尚未出現，整批每 N 分鐘重試，超過截止時間停止；
  非交易日（週末或 FinMind 交易日曆無該日）不等待。
- 只有此程式讀取 token（環境變數 FINMIND_TOKEN，本機可放 .env）。
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Callable
from zoneinfo import ZoneInfo

import requests
import yaml
from dotenv import load_dotenv

from core.db import ROOT, last_date, log_fetch, open_db, upsert_prices
from core.indicators import update_indicators

URL = "https://api.finmindtrade.com/api/v4/data"
TZ = ZoneInfo("Asia/Taipei")
log = logging.getLogger("fetcher")


class FetchError(Exception):
    """FinMind 回應錯誤。"""


class MissingTokenError(FetchError):
    """未設定 FINMIND_TOKEN。"""


class QuotaExceededError(FetchError):
    """API 額度用完。"""


def get_token() -> str:
    token = os.environ.get("FINMIND_TOKEN", "").strip()
    if not token:
        raise MissingTokenError("未設定環境變數 FINMIND_TOKEN（本機請在 .env 設定，雲端請設 GitHub Secret）")
    return token


def fetch_dataset(dataset: str, params: dict, token: str, session=None) -> list[dict]:
    """呼叫 FinMind v4 API，回傳 data 陣列；錯誤轉成明確的例外。"""
    http = session or requests
    resp = http.get(
        URL,
        headers={"Authorization": f"Bearer {token}"},
        params={"dataset": dataset, **params},
        timeout=30,
    )
    try:
        body = resp.json()
    except ValueError:
        body = None
    body_status = body.get("status") if isinstance(body, dict) else None
    msg = body.get("msg") if isinstance(body, dict) else None
    if resp.status_code == 402 or body_status == 402:
        raise QuotaExceededError(f"FinMind 額度已用完（{msg or 'HTTP 402'}），請稍後再試或升級方案")
    if resp.status_code >= 400:
        raise FetchError(f"HTTP {resp.status_code}：{msg or resp.reason}")
    if body_status != 200:
        raise FetchError(f"FinMind 錯誤：{msg or '回應格式異常'}（status={body_status}）")
    return body.get("data", [])


def fetch_price(stock_id: str, start_date: str, end_date: str, token: str, session=None) -> list[dict]:
    return fetch_dataset(
        "TaiwanStockPrice",
        {"data_id": stock_id, "start_date": start_date, "end_date": end_date},
        token,
        session,
    )


def is_trading_day(day: date, token: str, session=None) -> bool | None:
    """查 FinMind 交易日曆；查詢失敗回傳 None（視為未知，照常重試）。"""
    try:
        rows = fetch_dataset(
            "TaiwanStockTradingDate",
            {"start_date": day.isoformat(), "end_date": day.isoformat()},
            token,
            session,
        )
        return len(rows) > 0
    except (FetchError, requests.RequestException) as e:
        log.warning("無法查詢交易日曆，視為未知：%s", e)
        return None


def _hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def run(
    cfg: dict,
    conn: sqlite3.Connection,
    *,
    token: str = "",
    backfill: int | None = None,
    wait: bool = True,
    fetch_fn: Callable[[str, str, str], list[dict]] | None = None,
    trading_day_fn: Callable[[date], bool | None] | None = None,
    now_fn: Callable[[], datetime] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> int:
    """執行一次抓取流程，回傳結束碼（0 成功、1 有錯誤）。時鐘與 API 皆可注入以利測試。"""
    fetch_fn = fetch_fn or (lambda sid, s, e: fetch_price(sid, s, e, token))
    trading_day_fn = trading_day_fn or (lambda d: is_trading_day(d, token))
    now_fn = now_fn or (lambda: datetime.now(TZ))

    stocks = [str(s) for s in cfg["stocks"]]
    retry = cfg.get("retry", {})
    interval = int(retry.get("interval_minutes", 15)) * 60
    deadline = _hhmm(retry.get("deadline", "19:00"))
    market_close = _hhmm(cfg.get("market_close", "13:30"))
    default_back = int(cfg.get("default_backfill_days", 120))

    permanent: dict[str, str] = {}   # 代號無資料等不必重試的錯誤
    transient: dict[str, str] = {}   # 網路等暫時性錯誤，下一輪會重試
    changed: set[str] = set()
    trading_today: bool | None = None
    bf = backfill
    aborted = False

    try:
        while True:
            now = now_fn()
            today = now.date()
            for sid in stocks:
                if sid in permanent:
                    continue
                try:
                    last = last_date(conn, sid)
                    if bf:
                        start = today - timedelta(days=bf)
                    elif last:
                        start = date.fromisoformat(last) + timedelta(days=1)
                    else:
                        start = today - timedelta(days=default_back)
                    if start > today:
                        transient.pop(sid, None)
                        continue  # 已是最新，不必呼叫 API
                    rows = fetch_fn(sid, start.isoformat(), today.isoformat())
                    if not rows and last is None:
                        permanent[sid] = "查無資料，請確認股票代號是否正確"
                        log.error("%s：%s", sid, permanent[sid])
                        log_fetch(conn, sid, "error", permanent[sid], now)
                        conn.commit()
                        continue
                    n = upsert_prices(conn, rows)
                    conn.commit()
                    transient.pop(sid, None)
                    if n:
                        changed.add(sid)
                        log.info("%s：寫入 %d 筆（%s ~ %s）", sid, n, start, today)
                        log_fetch(conn, sid, "ok", f"寫入 {n} 筆（{start} ~ {today}）", now)
                        conn.commit()
                except QuotaExceededError:
                    raise
                except (FetchError, requests.RequestException) as e:
                    transient[sid] = str(e)
                    log.error("%s：抓取失敗：%s", sid, e)
            bf = None  # 回補只做第一輪，重試輪不再重複

            now = now_fn()
            today = now.date()
            pending = [
                s for s in stocks
                if s not in permanent and (last_date(conn, s) or "") < today.isoformat()
            ]
            if not pending or not wait:
                break
            if today.weekday() >= 5 or now.time() < market_close:
                break
            if (now + timedelta(seconds=interval)).time() > deadline or now.time() >= deadline:
                break
            if trading_today is None:
                trading_today = trading_day_fn(today)
            if trading_today is False:
                log.info("%s 非交易日，不等待當日資料", today)
                break
            log.info("尚缺 %s 當日資料：%s；%d 分鐘後重試", today, ",".join(pending), interval // 60)
            sleep_fn(interval)
    except QuotaExceededError as e:
        aborted = True
        log.error("%s", e)
        log_fetch(conn, None, "error", str(e), now_fn())
        conn.commit()
    finally:
        for sid in sorted(changed):
            update_indicators(conn, sid)
        conn.commit()

    now = now_fn()
    today = now.date()
    for sid in stocks:
        if sid in transient:
            log_fetch(conn, sid, "error", transient[sid], now)
    missing = [
        s for s in stocks
        if s not in permanent and (last_date(conn, s) or "") < today.isoformat()
    ]
    incomplete = False
    if missing and today.weekday() < 5 and now.time() >= market_close and trading_today is not False:
        msg = f"截止時仍未取得 {today} 當日資料：{','.join(missing)}（下次執行會自動補齊）"
        log.warning(msg)
        log_fetch(conn, None, "missing", msg, now)
        # 已確認是交易日、且有等待過卻仍缺資料 → 視為失敗，讓排程（GitHub Actions）發出失敗通知
        incomplete = wait and trading_today is True
    conn.commit()
    return 1 if (permanent or transient or aborted or incomplete) else 0


def setup_logging() -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers = [
        logging.StreamHandler(sys.stderr),
        logging.FileHandler(ROOT / "logs" / "fetch.log", encoding="utf-8"),
    ]
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="抓取 FinMind 台股日成交資料")
    ap.add_argument("--backfill", type=int, metavar="N", help="回補近 N 天歷史資料")
    ap.add_argument("--no-wait", action="store_true", help="不重試，只抓一次")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    args = ap.parse_args(argv)

    setup_logging()
    load_dotenv(ROOT / ".env")
    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    try:
        token = get_token()
    except MissingTokenError as e:
        log.error("%s", e)
        return 2

    with open_db(ROOT / cfg["db_path"]) as conn:
        return run(cfg, conn, token=token, backfill=args.backfill, wait=not args.no_wait)


if __name__ == "__main__":
    sys.exit(main())
