"""趨勢判斷：全專案唯一實作，fetcher、MCP、網頁皆由此 import。"""
from __future__ import annotations

import math


def _valid(x: float | None) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def classify_trend(close: float | None, ma5: float | None, ma20: float | None) -> str:
    """close > MA5 > MA20 為多頭；close < MA5 < MA20 為空頭；其餘盤整；缺值為資料不足。"""
    if not (_valid(close) and _valid(ma5) and _valid(ma20)):
        return "資料不足"
    if close > ma5 > ma20:
        return "多頭"
    if close < ma5 < ma20:
        return "空頭"
    return "盤整"


def classify_macd(hist: float | None, prev_hist: float | None = None) -> str:
    """依 MACD 柱狀體判斷：由負轉正為黃金交叉、由正轉負為死亡交叉，其餘為偏多／偏空；缺值為資料不足。"""
    if not _valid(hist):
        return "資料不足"
    if _valid(prev_hist):
        if prev_hist <= 0 < hist:
            return "黃金交叉"
        if prev_hist >= 0 > hist:
            return "死亡交叉"
    if hist > 0:
        return "偏多"
    if hist < 0:
        return "偏空"
    return "持平"
