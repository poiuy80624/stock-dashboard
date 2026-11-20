"""指標計算：MA5、MA20、日漲跌幅（%）、MACD（12, 26, 9）。"""
from __future__ import annotations

import sqlite3

import pandas as pd

from core.db import upsert_indicators


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """df 需含 date、close 欄位。回傳含 ma5、ma20、change_pct、macd、macd_signal、macd_hist 的新 DataFrame
    （依日期升冪）。

    MACD 採常用定義：DIF = EMA12 − EMA26；訊號線 = DIF 的 9 日 EMA；柱狀體 = DIF − 訊號線。
    EMA 以 adjust=False 遞迴計算，前 25 筆（不足 26 日）不輸出，訊號線再晚 8 筆。
    """
    out = df.sort_values("date").reset_index(drop=True).copy()
    close = out["close"]
    out["ma5"] = close.rolling(5, min_periods=5).mean()
    out["ma20"] = close.rolling(20, min_periods=20).mean()
    out["change_pct"] = close.pct_change() * 100

    dif = (close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean())
    dif.iloc[:25] = float("nan")                   # 不足 26 日不輸出
    valid = dif.dropna()
    signal = pd.Series(float("nan"), index=dif.index)
    if len(valid):
        signal.loc[valid.index] = valid.ewm(span=9, adjust=False, min_periods=9).mean()
    out["macd"] = dif
    out["macd_signal"] = signal
    out["macd_hist"] = dif - signal
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
            (stock_id, r.date, _none(r.ma5), _none(r.ma20), _none(r.change_pct),
             _none(r.macd), _none(r.macd_signal), _none(r.macd_hist))
            for r in res.itertuples()
        ],
    )
    return len(res)
