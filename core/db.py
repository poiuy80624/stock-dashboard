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
    macd REAL, macd_signal REAL, macd_hist REAL,
    PRIMARY KEY (stock_id, date)
);
CREATE TABLE IF NOT EXISTS fetch_log (
    run_at TEXT NOT NULL,
    stock_id TEXT,
    status TEXT NOT NULL,
    message TEXT
);
-- 指數類：台指期（近月、日盤）、加權指數、道瓊、費城半導體、那斯達克
CREATE TABLE IF NOT EXISTS index_prices (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL, open_interest REAL, note TEXT,
    PRIMARY KEY (symbol, date)
);
-- 個股三大法人買賣（每個法人身分一列，單位：股）
CREATE TABLE IF NOT EXISTS institutional (
    stock_id TEXT NOT NULL,
    date TEXT NOT NULL,
    name TEXT NOT NULL,
    buy INTEGER, sell INTEGER,
    PRIMARY KEY (stock_id, date, name)
);
-- 外資持股
CREATE TABLE IF NOT EXISTS foreign_holding (
    stock_id TEXT NOT NULL,
    date TEXT NOT NULL,
    stock_name TEXT,
    foreign_shares INTEGER, foreign_ratio REAL,
    remain_shares INTEGER, remain_ratio REAL,
    issued_shares INTEGER, upper_limit_ratio REAL,
    PRIMARY KEY (stock_id, date)
);
-- 整體三大法人買賣（每個法人身分一列，單位：元）
CREATE TABLE IF NOT EXISTS total_institutional (
    date TEXT NOT NULL,
    name TEXT NOT NULL,
    buy INTEGER, sell INTEGER,
    PRIMARY KEY (date, name)
);
-- 月營收（date 為 FinMind 回傳的日期，revenue_year/revenue_month 為營收所屬年月，單位：元）
CREATE TABLE IF NOT EXISTS month_revenue (
    stock_id TEXT NOT NULL,
    date TEXT NOT NULL,
    revenue REAL, revenue_month INTEGER, revenue_year INTEGER,
    PRIMARY KEY (stock_id, date)
);
-- 每日本益比、股價淨值比、殖利率（%）
CREATE TABLE IF NOT EXISTS valuation (
    stock_id TEXT NOT NULL,
    date TEXT NOT NULL,
    dividend_yield REAL, per REAL, pbr REAL,
    PRIMARY KEY (stock_id, date)
);
"""

# 資料表 → 以哪個欄位區分「對象」（None 代表整張表只有一組）
_KEY_COLUMN = {
    "prices": "stock_id",
    "index_prices": "symbol",
    "institutional": "stock_id",
    "foreign_holding": "stock_id",
    "total_institutional": None,
    "month_revenue": "stock_id",
    "valuation": "stock_id",
}


def _migrate(conn: sqlite3.Connection) -> None:
    """舊版資料庫補欄位（indicators 新增 MACD 三欄）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(indicators)")}
    for c in ("macd", "macd_signal", "macd_hist"):
        if c not in cols:
            conn.execute(f"ALTER TABLE indicators ADD COLUMN {c} REAL")


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
            _migrate(conn)
        yield conn
        if not readonly:
            conn.commit()
    finally:
        conn.close()


def _to_int(v) -> int | None:
    return None if v is None else int(v)


def _upsert(conn: sqlite3.Connection, table: str, cols: list[str], pk: list[str], rows: list[tuple]) -> int:
    """通用 upsert。table/cols 皆為程式內常數，不接受外部輸入。"""
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in pk)
    sql = (
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
        f"ON CONFLICT({','.join(pk)}) DO UPDATE SET {updates}"
    )
    conn.executemany(sql, rows)
    return len(rows)


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
    return _upsert(
        conn, "prices",
        ["stock_id", "date", "open", "high", "low", "close", "spread", "trading_volume", "trading_money"],
        ["stock_id", "date"], data,
    )


def upsert_indicators(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    """rows: (stock_id, date, ma5, ma20, change_pct, macd, macd_signal, macd_hist)"""
    _upsert(
        conn, "indicators",
        ["stock_id", "date", "ma5", "ma20", "change_pct", "macd", "macd_signal", "macd_hist"],
        ["stock_id", "date"], rows,
    )


def upsert_index_prices(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """rows: (symbol, date, open, high, low, close, volume, open_interest, note)"""
    return _upsert(
        conn, "index_prices",
        ["symbol", "date", "open", "high", "low", "close", "volume", "open_interest", "note"],
        ["symbol", "date"], rows,
    )


def upsert_institutional(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """rows: (stock_id, date, name, buy, sell)"""
    return _upsert(conn, "institutional", ["stock_id", "date", "name", "buy", "sell"],
                   ["stock_id", "date", "name"], rows)


def upsert_foreign_holding(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """rows: (stock_id, date, stock_name, foreign_shares, foreign_ratio, remain_shares, remain_ratio,
    issued_shares, upper_limit_ratio)"""
    return _upsert(
        conn, "foreign_holding",
        ["stock_id", "date", "stock_name", "foreign_shares", "foreign_ratio", "remain_shares",
         "remain_ratio", "issued_shares", "upper_limit_ratio"],
        ["stock_id", "date"], rows,
    )


def upsert_total_institutional(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """rows: (date, name, buy, sell)"""
    return _upsert(conn, "total_institutional", ["date", "name", "buy", "sell"], ["date", "name"], rows)


def upsert_month_revenue(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """rows: (stock_id, date, revenue, revenue_month, revenue_year)"""
    return _upsert(conn, "month_revenue", ["stock_id", "date", "revenue", "revenue_month", "revenue_year"],
                   ["stock_id", "date"], rows)


def upsert_valuation(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """rows: (stock_id, date, dividend_yield, per, pbr)"""
    return _upsert(conn, "valuation", ["stock_id", "date", "dividend_yield", "per", "pbr"],
                   ["stock_id", "date"], rows)


def get_month_revenue(conn: sqlite3.Connection, stock_id: str) -> list[dict]:
    """全部月營收，依營收所屬年月升冪。"""
    rows = conn.execute(
        "SELECT date, revenue, revenue_year, revenue_month FROM month_revenue WHERE stock_id = ? "
        "ORDER BY revenue_year, revenue_month",
        (stock_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_valuation(conn: sqlite3.Connection, stock_id: str, days: int = 100_000) -> list[dict]:
    rows = conn.execute(
        "SELECT date, dividend_yield, per, pbr FROM valuation WHERE stock_id = ? ORDER BY date DESC LIMIT ?",
        (stock_id, days),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def _macd_select(conn: sqlite3.Connection) -> str:
    """舊版資料庫（尚未有 MACD 欄位）時以 NULL 代替，讓唯讀端不會出錯。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(indicators)")}
    if {"macd", "macd_signal", "macd_hist"} <= cols:
        return "i.macd, i.macd_signal, i.macd_hist"
    return "NULL AS macd, NULL AS macd_signal, NULL AS macd_hist"


def get_history(conn: sqlite3.Connection, stock_id: str, days: int = 60) -> list[dict]:
    """最近 days 筆（排除收盤 0 的無成交日），依日期升冪，含 MA、MACD 與漲跌幅。"""
    rows = conn.execute(
        "SELECT p.date, p.open, p.high, p.low, p.close, p.spread, p.trading_volume, p.trading_money, "
        f"i.ma5, i.ma20, i.change_pct, {_macd_select(conn)} "
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
    return last_date_in(conn, "prices", stock_id)


def last_date_in(conn: sqlite3.Connection, table: str, key: str | None = None) -> str | None:
    """指定資料表（需在白名單內）某個對象的最後一天。"""
    col = _KEY_COLUMN[table]
    if col:
        row = conn.execute(f"SELECT MAX(date) AS d FROM {table} WHERE {col} = ?", (key,)).fetchone()
    else:
        row = conn.execute(f"SELECT MAX(date) AS d FROM {table}").fetchone()
    return row["d"] if row else None


def get_index_series(conn: sqlite3.Connection, symbol: str, days: int = 250) -> list[dict]:
    rows = conn.execute(
        "SELECT date, open, high, low, close, volume, open_interest, note FROM index_prices "
        "WHERE symbol = ? AND close > 0 ORDER BY date DESC LIMIT ?",
        (symbol, days),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def get_institutional(conn: sqlite3.Connection, stock_id: str, days: int = 120) -> list[dict]:
    """最近 days 個交易日的個股三大法人原始列（每個法人身分一列）。"""
    rows = conn.execute(
        "SELECT date, name, buy, sell FROM institutional WHERE stock_id = ? AND date IN "
        "(SELECT DISTINCT date FROM institutional WHERE stock_id = ? ORDER BY date DESC LIMIT ?) "
        "ORDER BY date, name",
        (stock_id, stock_id, days),
    ).fetchall()
    return [dict(r) for r in rows]


def get_foreign_holding(conn: sqlite3.Connection, stock_id: str, days: int = 250) -> list[dict]:
    rows = conn.execute(
        "SELECT date, stock_name, foreign_shares, foreign_ratio, remain_shares, remain_ratio, "
        "issued_shares, upper_limit_ratio FROM foreign_holding WHERE stock_id = ? "
        "ORDER BY date DESC LIMIT ?",
        (stock_id, days),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def get_total_institutional(conn: sqlite3.Connection, days: int = 120) -> list[dict]:
    rows = conn.execute(
        "SELECT date, name, buy, sell FROM total_institutional WHERE date IN "
        "(SELECT DISTINCT date FROM total_institutional ORDER BY date DESC LIMIT ?) ORDER BY date, name",
        (days,),
    ).fetchall()
    return [dict(r) for r in rows]


def log_fetch(conn: sqlite3.Connection, stock_id: str | None, status: str, message: str, now: datetime | None = None) -> None:
    ts = (now or datetime.now()).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO fetch_log (run_at, stock_id, status, message) VALUES (?,?,?,?)",
        (ts, stock_id, status, message),
    )


__all__ = [
    "open_db", "upsert_prices", "upsert_indicators", "upsert_index_prices", "upsert_institutional",
    "upsert_foreign_holding", "upsert_total_institutional", "get_history", "get_latest", "has_date",
    "last_date", "last_date_in", "get_index_series", "get_institutional", "get_foreign_holding",
    "get_total_institutional", "upsert_month_revenue", "upsert_valuation", "get_month_revenue",
    "get_valuation", "log_fetch", "ROOT", "DEFAULT_DB", "closing",
]
