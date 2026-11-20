"""擴充資料集（指數、三大法人、外資持股）：FinMind 原始列 → 資料表列，以及網頁用的彙整。"""
from __future__ import annotations

# 指數頁的五個項目。那斯達克使用 ^IXIC（綜合指數）；FinMind 沒有「那斯達克工業指數」。
INDEX_SPECS = [
    {"symbol": "TX", "name": "台指期", "dataset": "TaiwanFuturesDaily", "data_id": "TX"},
    {"symbol": "TAIEX", "name": "台股加權指數", "dataset": "TaiwanStockPrice", "data_id": "TAIEX"},
    {"symbol": "^DJI", "name": "道瓊工業指數", "dataset": "USStockPrice", "data_id": "^DJI"},
    {"symbol": "^SOX", "name": "費城半導體指數", "dataset": "USStockPrice", "data_id": "^SOX"},
    {"symbol": "^IXIC", "name": "那斯達克指數", "dataset": "USStockPrice", "data_id": "^IXIC"},
]

# FinMind 法人名稱 → 三大法人分類
PARTY_OF = {
    "Foreign_Investor": "foreign",
    "Foreign_Dealer_Self": "foreign",
    "Investment_Trust": "trust",
    "Dealer_self": "dealer",
    "Dealer_Hedging": "dealer",
    "Dealer": "dealer",
}
PARTIES = ("foreign", "trust", "dealer")


def normalize_index(spec: dict, rows: list[dict]) -> list[tuple]:
    """→ (symbol, date, open, high, low, close, volume, open_interest, note)"""
    sym = spec["symbol"]
    if spec["dataset"] == "TaiwanFuturesDaily":
        # 只取日盤（position）、單一合約（排除跨月價差），每天選成交量最大的合約（通常是近月）
        best: dict[str, dict] = {}
        for r in rows:
            if r.get("trading_session") != "position" or "/" in str(r.get("contract_date", "")):
                continue
            if not r.get("close"):
                continue
            cur = best.get(r["date"])
            if cur is None or (r.get("volume") or 0) > (cur.get("volume") or 0):
                best[r["date"]] = r
        return [
            (sym, d, r["open"], r["max"], r["min"], r["close"], r.get("volume"),
             r.get("open_interest"), str(r.get("contract_date")))
            for d, r in sorted(best.items())
        ]
    if spec["dataset"] == "TaiwanStockPrice":
        return [
            (sym, r["date"], r["open"], r["max"], r["min"], r["close"], r.get("Trading_Volume"), None, None)
            for r in rows if r.get("close")
        ]
    # USStockPrice
    return [
        (sym, r["date"], r.get("Open"), r.get("High"), r.get("Low"), r.get("Close"), r.get("Volume"), None, None)
        for r in rows if r.get("Close")
    ]


def normalize_institutional(rows: list[dict]) -> list[tuple]:
    """→ (stock_id, date, name, buy, sell)"""
    return [(str(r["stock_id"]), r["date"], r["name"], int(r.get("buy") or 0), int(r.get("sell") or 0)) for r in rows]


def normalize_foreign(rows: list[dict]) -> list[tuple]:
    """→ (stock_id, date, stock_name, foreign_shares, foreign_ratio, remain_shares, remain_ratio,
    issued_shares, upper_limit_ratio)"""
    return [
        (
            str(r["stock_id"]), r["date"], r.get("stock_name"),
            r.get("ForeignInvestmentShares"), r.get("ForeignInvestmentSharesRatio"),
            r.get("ForeignInvestmentRemainingShares"), r.get("ForeignInvestmentRemainRatio"),
            r.get("NumberOfSharesIssued"), r.get("ForeignInvestmentUpperLimitRatio"),
        )
        for r in rows
    ]


def normalize_revenue(rows: list[dict]) -> list[tuple]:
    """→ (stock_id, date, revenue, revenue_month, revenue_year)"""
    return [
        (str(r["stock_id"]), r["date"], r.get("revenue"), r.get("revenue_month"), r.get("revenue_year"))
        for r in rows
    ]


def normalize_valuation(rows: list[dict]) -> list[tuple]:
    """→ (stock_id, date, dividend_yield, per, pbr)"""
    return [(str(r["stock_id"]), r["date"], r.get("dividend_yield"), r.get("PER"), r.get("PBR")) for r in rows]


def normalize_total(rows: list[dict]) -> list[tuple]:
    """→ (date, name, buy, sell)"""
    return [(r["date"], r["name"], int(r.get("buy") or 0), int(r.get("sell") or 0)) for r in rows]


def group_parties(rows: list[dict], divisor: float = 1.0) -> list[dict]:
    """把「每個法人身分一列」彙整成每日 外資／投信／自營商／合計 的 買進、賣出、買賣超（除以 divisor）。

    外資 = 外資及陸資 + 外資自營商；自營商 = 自行買賣 + 避險。若同日已有自行買賣或避險，就不重複計入彙總的 Dealer。
    """
    by_date: dict[str, dict[str, dict[str, float]]] = {}
    names_by_date: dict[str, set[str]] = {}
    for r in rows:
        names_by_date.setdefault(r["date"], set()).add(r["name"])
    for r in rows:
        party = PARTY_OF.get(r["name"])
        if party is None:
            continue
        names = names_by_date[r["date"]]
        if r["name"] == "Dealer" and ({"Dealer_self", "Dealer_Hedging"} & names):
            continue
        d = by_date.setdefault(r["date"], {p: {"buy": 0.0, "sell": 0.0} for p in PARTIES})
        d[party]["buy"] += r.get("buy") or 0
        d[party]["sell"] += r.get("sell") or 0

    out = []
    for date in sorted(by_date):
        item: dict = {"date": date}
        tot_buy = tot_sell = 0.0
        for p in PARTIES:
            buy, sell = by_date[date][p]["buy"], by_date[date][p]["sell"]
            item[p] = {"buy": buy / divisor, "sell": sell / divisor, "net": (buy - sell) / divisor}
            tot_buy += buy
            tot_sell += sell
        item["total"] = {"buy": tot_buy / divisor, "sell": tot_sell / divisor, "net": (tot_buy - tot_sell) / divisor}
        out.append(item)
    return out
