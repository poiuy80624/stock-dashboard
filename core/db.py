"""SQLite 存取層。日期一律存 TEXT（YYYY-MM-DD），stock_id 存 TEXT。

注意：資料庫檔會進 git，因此不使用 WAL（避免產生 -wal/-shm 附屬檔）。
"""
from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "stock.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    stock_id TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, spread REAL,
    trading_volume INTEGER, trading_money INTEGER,
    PRIMARY KEY (stock_id, date)
);
CREATE TABLE IF NOT EXISTS indicators (
    stock_id TEXT NOT NULL,
    date TEXT NOT NULL,
    ma5 REAL, ma20 REAL, change_pct REAL,
    PRIMARY KEY (stock_id, date)
);
CREATE TABLE IF NOT EXISTS fetch_log (
    run_at TEXT NOT NULL,
    stock_id TEXT,
    status TEXT NOT NULL,
    message TEXT
);
"""


@contextmanager
def open_db(path: Path | str | None = None, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    """開啟連線（結束時一定關閉並 commit）。readonly=True 為唯讀連線，供 MCP 與網頁使用。"""
    p = Path(path) if path else DEFAULT_DB
    if readonly:
        conn = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    try:
        if not readonly:
            conn.executescript(SCHEMA)
        yield conn
        if not readonly:
            conn.commit()
    finally:
        conn.close()


def _to_int(v) -> int | None:
    return None if v is None else int(v)


def upsert_prices(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """寫入 FinMind 原始列（max/min 改名為 high/low），重複的 (stock_id, date) 會覆蓋。"""
    data = [
        (
            str(r["stock_id"]), str(r["date"]),
            r.get("open"), r.get("max"), r.get("min"), r.get("close"), r.get("spread"),
            _to_int(r.get("Trading_Volume")), _to_int(r.get("Trading_money")),
        )
        for r in rows
    ]
    conn.executemany(
        "INSERT INTO prices (stock_id, date, open, high, low, close, spread, trading_volume, trading_money) "
        "VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(stock_id, date) DO UPDATE SET "
        "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, "
        "spread=excluded.spread, trading_volume=excluded.trading_volume, trading_money=excluded.trading_money",
        data,
    )
    return len(data)


def upsert_indicators(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    """rows: (stock_id, date, ma5, ma20, change_pct)"""
    conn.executemany(
        "INSERT INTO indicators (stock_id, date, ma5, ma20, change_pct) VALUES (?,?,?,?,?) "
        "ON CONFLICT(stock_id, date) DO UPDATE SET "
        "ma5=excluded.ma5, ma20=excluded.ma20, change_pct=excluded.change_pct",
        rows,
    )


def get_history(conn: sqlite3.Connection, stock_id: str, days: int = 60) -> list[dict]:
    """最近 days 筆（排除收盤 0 的無成交日），依日期升冪，含 MA 與漲跌幅。"""
    rows = conn.execute(
        "SELECT p.date, p.open, p.high, p.low, p.close, p.spread, p.trading_volume, p.trading_money, "
        "i.ma5, i.ma20, i.change_pct "
        "FROM prices p LEFT JOIN indicators i ON i.stock_id = p.stock_id AND i.date = p.date "
        "WHERE p.stock_id = ? AND p.close > 0 ORDER BY p.date DESC LIMIT ?",
        (stock_id, days),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def get_latest(conn: sqlite3.Connection, stock_id: str) -> dict | None:
    rows = get_history(conn, stock_id, 1)
    return rows[0] if rows else None


def has_date(conn: sqlite3.Connection, stock_id: str, date: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM prices WHERE stock_id = ? AND date = ?", (stock_id, date)
    ).fetchone() is not None


def last_date(conn: sqlite3.Connection, stock_id: str) -> str | None:
    row = conn.execute("SELECT MAX(date) AS d FROM prices WHERE stock_id = ?", (stock_id,)).fetchone()
    return row["d"] if row else None


def log_fetch(conn: sqlite3.Connection, stock_id: str | None, status: str, message: str, now: datetime | None = None) -> None:
    ts = (now or datetime.now()).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO fetch_log (run_at, stock_id, status, message) VALUES (?,?,?,?)",
        (ts, stock_id, status, message),
    )


__all__ = [
    "open_db", "upsert_prices", "upsert_indicators", "get_history", "get_latest",
    "has_date", "last_date", "log_fetch", "ROOT", "DEFAULT_DB", "closing",
]
