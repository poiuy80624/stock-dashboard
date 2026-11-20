"""網頁與靜態網站共用的資料整形。"""
from __future__ import annotations

from core.trend import classify_macd, classify_trend


def with_states(rows: list[dict]) -> list[dict]:
    """為每一列加上均線趨勢（trend）與 MACD 狀態（macd_state）。

    前端依使用者選的日期區間取最後一列顯示，所以每一列都要有判斷結果；
    判斷規則只在 core/trend.py 實作一份。rows 需為日期升冪、含 get_history 的欄位。
    """
    out, prev = [], None
    for r in rows:
        item = dict(r)
        item["trend"] = classify_trend(r["close"], r["ma5"], r["ma20"])
        item["macd_state"] = classify_macd(r.get("macd_hist"), prev.get("macd_hist") if prev else None)
        out.append(item)
        prev = r
    return out
