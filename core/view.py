"""網頁與靜態網站共用的資料整形。"""
from __future__ import annotations

from core.trend import classify_trend


def quote_dict(stock_id: str, row: dict) -> dict:
    """由 get_history 的一列資料組成最新報價（含趨勢判斷）。"""
    return {
        "stock_id": stock_id,
        "date": row["date"],
        "close": row["close"],
        "change": row["spread"],
        "change_pct": row["change_pct"],
        "ma5": row["ma5"],
        "ma20": row["ma20"],
        "trend": classify_trend(row["close"], row["ma5"], row["ma20"]),
    }
