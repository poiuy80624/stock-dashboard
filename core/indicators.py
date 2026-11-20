"""指標計算：MA5、MA20、日漲跌幅（%）。"""
from __future__ import annotations

import sqlite3

import pandas as pd

from core.db import upsert_indicators


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """df 需含 date、close 欄位。回傳含 ma5、ma20、change_pct 的新 DataFrame（依日期升冪）。"""
    out = df.sort_values("date").reset_index(drop=True).copy()
    out["ma5"] = out["close"].rolling(5, min_periods=5).mean()
    out["ma20"] = out["close"].rolling(20, min_periods=20).mean()
    out["change_pct"] = out["close"].pct_change() * 100
    return out


def _none(v: float) -> float | None:
    return None if pd.isna(v) else round(float(v), 4)


def update_indicators(conn: sqlite3.Connection, stock_id: str) -> int:
    """以資料庫中該股全部有效價格（收盤 > 0）重算指標並 upsert，回傳筆數。"""
    rows = conn.execute(
        "SELECT date, close FROM prices WHERE stock_id = ? AND close > 0 ORDER BY date", (stock_id,)
    ).fetchall()
    if not rows:
        return 0
    df = pd.DataFrame([dict(r) for r in rows])
    res = compute_indicators(df)
    upsert_indicators(
        conn,
        [
            (stock_id, r.date, _none(r.ma5), _none(r.ma20), _none(r.change_pct))
            for r in res.itertuples()
        ],
    )
    return len(res)
