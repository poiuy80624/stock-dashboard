"""網頁與靜態網站共用的資料整形。"""
from __future__ import annotations

from core.trend import classify_macd, classify_trend


def quote_dict(stock_id: str, row: dict, prev_row: dict | None = None) -> dict:
    """由 get_history 的一列資料（及前一列）組成最新報價，含趨勢與 MACD 判斷。"""
    prev_hist = prev_row.get("macd_hist") if prev_row else None
    return {
        "stock_id": stock_id,
        "date": row["date"],
        "close": row["close"],
        "change": row["spread"],
        "change_pct": row["change_pct"],
        "ma5": row["ma5"],
        "ma20": row["ma20"],
        "trend": classify_trend(row["close"], row["ma5"], row["ma20"]),
        "macd": row.get("macd"),
        "macd_signal": row.get("macd_signal"),
        "macd_hist": row.get("macd_hist"),
        "macd_state": classify_macd(row.get("macd_hist"), prev_hist),
    }
