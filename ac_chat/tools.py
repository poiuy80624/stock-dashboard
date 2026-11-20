"""對話 AI 可呼叫的查詢工具：全部唯讀，只讀 SQLite，不呼叫任何外部服務。

回傳值皆為精簡的 dict（數字已四捨五入、長序列已抽樣），避免把整年資料灌進對話而浪費 token。
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from core.db import (
    get_foreign_holding, get_history, get_index_series, get_institutional, get_month_revenue,
    get_total_institutional, get_valuation, open_db,
)
from core.extras import INDEX_SPECS, group_parties
from core.view import with_states

MAX_RESULT_CHARS = 14_000      # 單一工具結果的字數上限，超過就要求縮小範圍
LOAD_ALL = 100_000


class ToolInputError(Exception):
    """工具參數有誤；訊息會原樣回給 AI，讓它自行修正參數。"""


def _r(v, n=2):
    return None if v is None else round(float(v), n)


def _pct(new, old):
    return None if not old or new is None else round((new / old - 1) * 100, 2)


def _downsample(rows: list, n: int) -> list:
    """均勻抽樣到最多 n 筆（保留第一筆與最後一筆）。"""
    if len(rows) <= n:
        return rows
    step = (len(rows) - 1) / (n - 1)
    idx = sorted({round(i * step) for i in range(n)} | {len(rows) - 1})
    return [rows[i] for i in idx]


def _iso(value: Any, label: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ToolInputError(f"{label} 格式需為 YYYY-MM-DD（收到：{value!r}）") from None


class Toolbox:
    """綁定一個資料庫與追蹤清單；每個 tool 對應一個 public 方法（名稱以 t_ 開頭）。"""

    def __init__(self, db_path: Path | str, stocks: list[str]):
        self.db_path = Path(db_path)
        self.stocks = [str(s) for s in stocks]

    # ---------- 共用 ----------
    def _conn(self):
        return open_db(self.db_path, readonly=True)

    def _sid(self, stock_id: Any) -> str:
        sid = str(stock_id or "").strip()
        if sid not in self.stocks:
            raise ToolInputError(f"{sid!r} 不在追蹤清單內。可查詢的股票代號：{', '.join(self.stocks)}")
        return sid

    @staticmethod
    def _latest(conn: sqlite3.Connection) -> date:
        row = conn.execute("SELECT MAX(date) AS d FROM prices").fetchone()
        if not row or not row["d"]:
            raise ToolInputError("資料庫裡沒有價格資料")
        return date.fromisoformat(row["d"])

    def _range(self, conn, args: dict, default_days: int) -> tuple[str, str]:
        end = _iso(args.get("end_date"), "end_date") or self._latest(conn)
        start = _iso(args.get("start_date"), "start_date") or (end - timedelta(days=default_days))
        if start > end:
            start, end = end, start
        return start.isoformat(), end.isoformat()

    # ---------- tools ----------
    def t_list_stocks(self, args: dict) -> dict:
        with self._conn() as conn:
            names = {
                r["stock_id"]: r["stock_name"]
                for r in conn.execute(
                    "SELECT stock_id, stock_name FROM foreign_holding WHERE stock_name IS NOT NULL GROUP BY stock_id"
                )
            }
            out = []
            for sid in self.stocks:
                row = conn.execute("SELECT MAX(date) AS d FROM prices WHERE stock_id = ?", (sid,)).fetchone()
                out.append({"stock_id": sid, "name": names.get(sid, ""), "latest_date": row["d"]})
            return {"stocks": out, "latest_data_date": self._latest(conn).isoformat()}

    def t_get_stock_snapshot(self, args: dict) -> dict:
        """一次取得單一股票目前的整體狀況（建議分析個股時第一個呼叫）。"""
        sid = self._sid(args.get("stock_id"))
        with self._conn() as conn:
            rows = get_history(conn, sid, 300)
            if not rows:
                raise ToolInputError(f"{sid} 沒有價格資料")
            last = rows[-1]
            st = with_states(rows[-2:])[-1]
            closes = [r["close"] for r in rows]

            def ret(n):
                return _pct(closes[-1], closes[-1 - n]) if len(closes) > n else None

            vols = [r["trading_volume"] for r in rows[-21:-1] if r["trading_volume"]]
            vol_ratio = round(last["trading_volume"] / statistics.mean(vols), 2) if vols and last["trading_volume"] else None
            window = rows[-250:]
            hi = max(window, key=lambda r: r["high"])
            lo = min(window, key=lambda r: r["low"])

            snapshot: dict[str, Any] = {
                "stock_id": sid,
                "date": last["date"],
                "close": last["close"],
                "change": last["spread"],
                "change_pct": _r(last["change_pct"]),
                "ma5": _r(last["ma5"]), "ma20": _r(last["ma20"]),
                "close_vs_ma20_pct": _pct(last["close"], last["ma20"]),
                "ma_trend": st["trend"],
                "macd": {"dif": _r(last["macd"]), "signal": _r(last["macd_signal"]),
                         "hist": _r(last["macd_hist"]), "state": st["macd_state"]},
                "return_pct": {"5d": ret(5), "20d": ret(20), "60d": ret(60), "120d": ret(120), "250d": ret(250)},
                "volume_vs_20d_avg": vol_ratio,
                "high_250d": {"price": hi["high"], "date": hi["date"], "from_high_pct": _pct(last["close"], hi["high"])},
                "low_250d": {"price": lo["low"], "date": lo["date"], "from_low_pct": _pct(last["close"], lo["low"])},
            }

            val = get_valuation(conn, sid)
            if val:
                pers = [v["per"] for v in val if v["per"] and v["per"] > 0]
                latest = val[-1]
                snapshot["valuation"] = {
                    "date": latest["date"], "per": latest["per"], "pbr": latest["pbr"], "dividend_yield_pct": latest["dividend_yield"],
                    "per_percentile_in_history": round(sum(1 for p in pers if p <= latest["per"]) / len(pers) * 100)
                    if pers and latest["per"] and latest["per"] > 0 else None,
                    "history_days": len(val),
                }

            fh = get_foreign_holding(conn, sid, 40)
            if fh:
                ref = fh[-21] if len(fh) > 20 else fh[0]
                snapshot["foreign_holding"] = {
                    "date": fh[-1]["date"], "ratio_pct": fh[-1]["foreign_ratio"],
                    "change_vs_prev_day_pct_pt": _r(fh[-1]["foreign_ratio"] - fh[-2]["foreign_ratio"]) if len(fh) > 1 else None,
                    "change_20d_pct_pt": _r(fh[-1]["foreign_ratio"] - ref["foreign_ratio"]) if ref is not fh[-1] else None,
                }

            inst = group_parties(get_institutional(conn, sid, 25), divisor=1000)
            if inst:
                def net(party, n):
                    return round(sum(d[party]["net"] for d in inst[-n:]))
                snapshot["institutional_net_lots"] = {
                    "unit": "張", "latest_date": inst[-1]["date"],
                    "last_5d": {p: net(p, 5) for p in ("foreign", "trust", "dealer")},
                    "last_20d": {p: net(p, 20) for p in ("foreign", "trust", "dealer")},
                }
            return snapshot

    def t_get_price_history(self, args: dict) -> dict:
        sid = self._sid(args.get("stock_id"))
        max_rows = max(5, min(int(args.get("max_rows") or 60), 120))
        with self._conn() as conn:
            start, end = self._range(conn, args, 90)
            rows = [r for r in get_history(conn, sid, LOAD_ALL) if start <= r["date"] <= end]
            if not rows:
                raise ToolInputError(f"{sid} 在 {start} ~ {end} 沒有交易資料")
            hi = max(rows, key=lambda r: r["high"])
            lo = min(rows, key=lambda r: r["low"])
            picked = _downsample(rows, max_rows)
            return {
                "stock_id": sid, "start": rows[0]["date"], "end": rows[-1]["date"], "trading_days": len(rows),
                "start_close": rows[0]["close"], "end_close": rows[-1]["close"],
                "period_return_pct": _pct(rows[-1]["close"], rows[0]["close"]),
                "high": {"price": hi["high"], "date": hi["date"]}, "low": {"price": lo["low"], "date": lo["date"]},
                "downsampled": len(picked) < len(rows),
                "columns": ["date", "open", "high", "low", "close", "volume", "ma5", "ma20", "macd_hist"],
                "rows": [[r["date"], r["open"], r["high"], r["low"], r["close"], r["trading_volume"],
                          _r(r["ma5"]), _r(r["ma20"]), _r(r["macd_hist"])] for r in picked],
            }

    def t_get_institutional_flow(self, args: dict) -> dict:
        sid = self._sid(args.get("stock_id"))
        with self._conn() as conn:
            start, end = self._range(conn, args, 30)
            days = [d for d in group_parties(get_institutional(conn, sid, LOAD_ALL), divisor=1000)
                    if start <= d["date"] <= end]
            if not days:
                raise ToolInputError(f"{sid} 在 {start} ~ {end} 沒有三大法人資料")

            def streak(party):
                sign = 1 if days[-1][party]["net"] > 0 else -1 if days[-1][party]["net"] < 0 else 0
                n = 0
                for d in reversed(days):
                    s = 1 if d[party]["net"] > 0 else -1 if d[party]["net"] < 0 else 0
                    if s != sign or s == 0:
                        break
                    n += 1
                return {"direction": {1: "連續買超", -1: "連續賣超", 0: "持平"}[sign], "days": n if sign else 0}

            return {
                "stock_id": sid, "unit": "張", "start": days[0]["date"], "end": days[-1]["date"], "trading_days": len(days),
                "period_net": {p: round(sum(d[p]["net"] for d in days)) for p in ("foreign", "trust", "dealer", "total")},
                "latest_streak": {p: streak(p) for p in ("foreign", "trust", "dealer")},
                "columns": ["date", "foreign_net", "trust_net", "dealer_net", "total_net"],
                "rows": [[d["date"], round(d["foreign"]["net"]), round(d["trust"]["net"]), round(d["dealer"]["net"]),
                          round(d["total"]["net"])] for d in _downsample(days, 40)],
                "downsampled": len(days) > 40,
            }

    def t_get_foreign_holding(self, args: dict) -> dict:
        sid = self._sid(args.get("stock_id"))
        with self._conn() as conn:
            start, end = self._range(conn, args, 90)
            rows = [r for r in get_foreign_holding(conn, sid, LOAD_ALL) if start <= r["date"] <= end]
            if not rows:
                raise ToolInputError(f"{sid} 在 {start} ~ {end} 沒有外資持股資料")
            first, last = rows[0], rows[-1]
            return {
                "stock_id": sid, "name": last.get("stock_name"), "start": first["date"], "end": last["date"],
                "start_ratio_pct": first["foreign_ratio"], "end_ratio_pct": last["foreign_ratio"],
                "change_pct_pt": _r(last["foreign_ratio"] - first["foreign_ratio"]),
                "end_shares_lots": None if last["foreign_shares"] is None else round(last["foreign_shares"] / 1000),
                "remain_ratio_pct": last["remain_ratio"], "upper_limit_pct": last["upper_limit_ratio"],
                "columns": ["date", "ratio_pct"],
                "rows": [[r["date"], r["foreign_ratio"]] for r in _downsample(rows, 30)],
            }

    def t_get_valuation(self, args: dict) -> dict:
        sid = self._sid(args.get("stock_id"))
        with self._conn() as conn:
            allv = get_valuation(conn, sid)
            if not allv:
                raise ToolInputError(f"{sid} 沒有本益比資料（可能尚未回補）")
            start, end = self._range(conn, args, 180)
            rows = [v for v in allv if start <= v["date"] <= end] or allv[-1:]
            pers = [v["per"] for v in allv if v["per"] and v["per"] > 0]
            pbrs = [v["pbr"] for v in allv if v["pbr"] and v["pbr"] > 0]
            latest = allv[-1]
            return {
                "stock_id": sid, "latest": latest, "history_days": len(allv),
                "per_stats": {"min": _r(min(pers)), "median": _r(statistics.median(pers)), "max": _r(max(pers)),
                              "latest_percentile": round(sum(1 for p in pers if p <= latest["per"]) / len(pers) * 100)
                              if pers and latest["per"] and latest["per"] > 0 else None} if pers else None,
                "pbr_stats": {"min": _r(min(pbrs)), "median": _r(statistics.median(pbrs)), "max": _r(max(pbrs))} if pbrs else None,
                "columns": ["date", "per", "pbr", "dividend_yield_pct"],
                "rows": [[v["date"], v["per"], v["pbr"], v["dividend_yield"]] for v in _downsample(rows, 30)],
            }

    def t_get_month_revenue(self, args: dict) -> dict:
        sid = self._sid(args.get("stock_id"))
        months = max(3, min(int(args.get("months") or 12), 36))
        with self._conn() as conn:
            rows = get_month_revenue(conn, sid)
        if not rows:
            raise ToolInputError(f"{sid} 沒有月營收資料（可能尚未回補）")
        by_ym = {(r["revenue_year"], r["revenue_month"]): r["revenue"] for r in rows}
        out = []
        for r in rows[-months:]:
            y, m = r["revenue_year"], r["revenue_month"]
            prev_y, prev_m = (y, m - 1) if m > 1 else (y - 1, 12)
            out.append([f"{y}-{m:02d}", _r(r["revenue"] / 1e8), _pct(r["revenue"], by_ym.get((prev_y, prev_m))),
                        _pct(r["revenue"], by_ym.get((y - 1, m)))])
        return {"stock_id": sid, "unit": "億元", "columns": ["year_month", "revenue", "mom_pct", "yoy_pct"], "rows": out}

    def t_get_index_performance(self, args: dict) -> dict:
        with self._conn() as conn:
            start, end = self._range(conn, args, 90)
            result = []
            for spec in INDEX_SPECS:
                rows = [r for r in get_index_series(conn, spec["symbol"], LOAD_ALL) if start <= r["date"] <= end]
                if not rows:
                    result.append({"name": spec["name"], "symbol": spec["symbol"], "note": "此期間無資料"})
                    continue
                hi = max(rows, key=lambda r: r["high"] or 0)
                lo = min(rows, key=lambda r: r["low"] if r["low"] else 1e18)
                result.append({
                    "name": spec["name"], "symbol": spec["symbol"],
                    "start": rows[0]["date"], "end": rows[-1]["date"],
                    "start_close": _r(rows[0]["close"]), "end_close": _r(rows[-1]["close"]),
                    "period_return_pct": _pct(rows[-1]["close"], rows[0]["close"]),
                    "last_day_change_pct": _pct(rows[-1]["close"], rows[-2]["close"]) if len(rows) > 1 else None,
                    "high": {"price": _r(hi["high"]), "date": hi["date"]}, "low": {"price": _r(lo["low"]), "date": lo["date"]},
                })
            return {"requested": {"start": start, "end": end},
                    "note": "美股日期為美國交易日，與台股並不對齊；台指期為日盤近月（換月日漲跌幅含價差）",
                    "indices": result}

    def t_get_market_institutional(self, args: dict) -> dict:
        with self._conn() as conn:
            start, end = self._range(conn, args, 30)
            days = [d for d in group_parties(get_total_institutional(conn, LOAD_ALL), divisor=1e8)
                    if start <= d["date"] <= end]
        if not days:
            raise ToolInputError(f"{start} ~ {end} 沒有整體三大法人資料")
        return {
            "unit": "億元", "start": days[0]["date"], "end": days[-1]["date"], "trading_days": len(days),
            "period_net": {p: _r(sum(d[p]["net"] for d in days)) for p in ("foreign", "trust", "dealer", "total")},
            "columns": ["date", "foreign_net", "trust_net", "dealer_net", "total_net"],
            "rows": [[d["date"], _r(d["foreign"]["net"]), _r(d["trust"]["net"]), _r(d["dealer"]["net"]), _r(d["total"]["net"])]
                     for d in _downsample(days, 30)],
        }

    # ---------- 對外 ----------
    def run(self, name: str, args: dict) -> tuple[str, bool]:
        """執行工具，回傳 (給 AI 的文字, 是否為錯誤)。任何例外都轉成訊息，不會讓對話中斷。"""
        fn: Callable[[dict], dict] | None = getattr(self, f"t_{name}", None)
        if fn is None:
            return f"未知的工具：{name}", True
        try:
            text = json.dumps(fn(args or {}), ensure_ascii=False, separators=(",", ":"))
        except ToolInputError as e:
            return str(e), True
        except (ValueError, TypeError) as e:
            return f"參數有誤：{e}", True
        except sqlite3.Error:
            return "讀取資料庫失敗，請稍後再試", True
        if len(text) > MAX_RESULT_CHARS:
            return f"結果過大（{len(text)} 字），請縮小日期範圍或減少 max_rows 後再查", True
        return text, False


_DATE = {"type": "string", "description": "YYYY-MM-DD；省略則依各工具的預設期間"}
_SID = {"type": "string", "description": "股票代號字串，例如 '2330'（不是公司名稱）；必須在追蹤清單內"}


def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


TOOL_SCHEMAS: list[dict] = [
    {"name": "list_stocks", "description": "列出可查詢的追蹤股票（代號、名稱、最新資料日），並回傳資料庫最新資料日期。不確定使用者說的是哪一檔時先呼叫。",
     "input_schema": _schema({})},
    {"name": "get_stock_snapshot",
     "description": "取得單一股票目前的整體狀況：最新收盤、均線與趨勢、MACD 狀態、5/20/60/120/250 日報酬、量能相對 20 日均量、250 日高低點、本益比與歷史百分位、外資持股變化、近 5/20 日三大法人買賣超（張）。分析個股時請先呼叫這個。",
     "input_schema": _schema({"stock_id": _SID}, ["stock_id"])},
    {"name": "get_price_history",
     "description": "取得單一股票在指定期間的日 K（開高低收、成交量、MA5、MA20、MACD 柱）與期間摘要（報酬、最高最低）。預設最近約 90 天；超過 max_rows 會均勻抽樣。用來說明某段漲跌的過程與轉折。",
     "input_schema": _schema({"stock_id": _SID, "start_date": _DATE, "end_date": _DATE,
                              "max_rows": {"type": "integer", "description": "最多回傳幾列，5~120，預設 60"}}, ["stock_id"])},
    {"name": "get_institutional_flow",
     "description": "取得單一股票的三大法人（外資、投信、自營商）買賣超（單位：張）：期間合計、目前連續買賣超天數、每日明細。預設最近 30 天。用來判斷籌碼面是否支持這段走勢。",
     "input_schema": _schema({"stock_id": _SID, "start_date": _DATE, "end_date": _DATE}, ["stock_id"])},
    {"name": "get_foreign_holding",
     "description": "取得單一股票的外資持股比例走勢與期間變動（百分點）。預設最近 90 天。",
     "input_schema": _schema({"stock_id": _SID, "start_date": _DATE, "end_date": _DATE}, ["stock_id"])},
    {"name": "get_valuation",
     "description": "取得單一股票的本益比、股價淨值比、殖利率，以及目前本益比在歷史資料中的百分位（約五年）。預設最近 180 天明細。用來判斷估值相對自身歷史偏貴或偏便宜。",
     "input_schema": _schema({"stock_id": _SID, "start_date": _DATE, "end_date": _DATE}, ["stock_id"])},
    {"name": "get_month_revenue",
     "description": "取得單一股票最近幾個月的月營收（億元）、月增率與年增率。用來判斷基本面動能。",
     "input_schema": _schema({"stock_id": _SID, "months": {"type": "integer", "description": "回傳最近幾個月，3~36，預設 12"}}, ["stock_id"])},
    {"name": "get_index_performance",
     "description": "取得台指期、台股加權指數、道瓊工業指數、費城半導體指數、那斯達克（綜合）指數在指定期間的漲跌幅、最高最低與最後一日漲跌。預設最近 90 天。用來比較個股與大盤、美股的走勢。",
     "input_schema": _schema({"start_date": _DATE, "end_date": _DATE})},
    {"name": "get_market_institutional",
     "description": "取得整體市場三大法人買賣超（單位：億元）：期間合計與每日明細。預設最近 30 天。用來判斷市場資金面。",
     "input_schema": _schema({"start_date": _DATE, "end_date": _DATE})},
]

# 顯示在畫面上的查詢說明
TOOL_LABELS = {
    "list_stocks": lambda a: "查詢追蹤清單",
    "get_stock_snapshot": lambda a: f"查詢 {a.get('stock_id', '')} 目前整體狀況",
    "get_price_history": lambda a: f"查詢 {a.get('stock_id', '')} 股價走勢" + _span(a),
    "get_institutional_flow": lambda a: f"查詢 {a.get('stock_id', '')} 三大法人買賣" + _span(a),
    "get_foreign_holding": lambda a: f"查詢 {a.get('stock_id', '')} 外資持股" + _span(a),
    "get_valuation": lambda a: f"查詢 {a.get('stock_id', '')} 本益比與估值",
    "get_month_revenue": lambda a: f"查詢 {a.get('stock_id', '')} 月營收",
    "get_index_performance": lambda a: "查詢各指數表現" + _span(a),
    "get_market_institutional": lambda a: "查詢整體三大法人" + _span(a),
}


def _span(a: dict) -> str:
    s, e = a.get("start_date"), a.get("end_date")
    return f"（{s or '…'} ~ {e or '最新'}）" if (s or e) else ""
