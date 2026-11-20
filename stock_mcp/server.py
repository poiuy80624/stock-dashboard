"""stock_mcp：台股查詢 MCP Server（stdio）。

只讀 SQLite（唯讀連線），不呼叫 FinMind、不接觸 token。
stdio 模式的 stdout 是協定通道，禁止 print；log 走 stderr。
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# 讓 `python stock_mcp/server.py` 直接執行時也找得到 core 套件
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from core.db import DEFAULT_DB, get_history as db_history, open_db  # noqa: E402
from core.trend import classify_trend  # noqa: E402

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stock_mcp")

INSTRUCTIONS = (
    "台股日線資料查詢（資料來源 FinMind，每個交易日傍晚更新一次，未還原股價）。"
    "stock_id 必須是股票代號字串（例如 '2330'、'0050'），不是公司名稱；"
    "使用者給公司名稱時，請先換成代號，不確定就先向使用者確認。"
    "回答時請註明資料日期。"
)


def _db_path() -> Path:
    return Path(os.environ.get("STOCK_DB", DEFAULT_DB))


def _load(stock_id: str, days: int) -> list[dict]:
    stock_id = str(stock_id).strip()
    if not (stock_id.isascii() and stock_id.isalnum()):
        raise ToolError(f"[INVALID_STOCK_ID] 股票代號格式不正確：{stock_id!r}｜建議：請提供代號字串，例如 '2330'")
    path = _db_path()
    if not path.exists():
        raise ToolError("[DB_NOT_FOUND] 找不到資料庫｜建議：請先執行 git pull 取得最新 data/stock.db，或執行 fetcher.py")
    try:
        with open_db(path, readonly=True) as conn:
            rows = db_history(conn, stock_id, days)
    except Exception:
        log.exception("讀取資料庫失敗")
        raise ToolError("[DB_ERROR] 讀取資料庫失敗｜建議：確認 data/stock.db 完整，必要時重新 git pull") from None
    if not rows:
        raise ToolError(
            f"[NO_DATA] 查無 {stock_id} 的資料｜建議：確認代號是否正確，或該股票是否在 config.yaml 的追蹤清單中"
        )
    return rows


def create_server() -> MCPServer:
    mcp = MCPServer("stock_mcp", instructions=INSTRUCTIONS)

    @mcp.tool()
    def get_quote(stock_id: str) -> dict:
        """取得單一台股最新一個交易日的收盤價、漲跌幅（%）與資料日期。
        stock_id 為股票代號字串，例如 '2330'（不是公司名稱）。"""
        r = _load(stock_id, 1)[0]
        return {
            "stock_id": stock_id,
            "date": r["date"],
            "close": r["close"],
            "change": r["spread"],
            "change_pct": r["change_pct"],
            "volume": r["trading_volume"],
        }

    @mcp.tool()
    def get_history(stock_id: str, days: int = 60) -> dict:
        """取得台股日 K 資料（日期由舊到新，含開高低收、成交量、MA5、MA20、漲跌幅%）。
        stock_id 為股票代號字串，例如 '2330'（不是公司名稱）；days 為最近幾個交易日，1~365，預設 60。"""
        days = max(1, min(int(days), 365))
        rows = _load(stock_id, days)
        return {"stock_id": stock_id, "count": len(rows), "rows": rows}

    @mcp.tool()
    def get_trend(stock_id: str) -> dict:
        """取得單一台股最新的 MA5、MA20 與趨勢判斷。
        規則：收盤 > MA5 > MA20 為多頭；收盤 < MA5 < MA20 為空頭；其餘為盤整；均線資料不足時為「資料不足」。
        stock_id 為股票代號字串，例如 '2330'（不是公司名稱）。"""
        r = _load(stock_id, 1)[0]
        return {
            "stock_id": stock_id,
            "date": r["date"],
            "close": r["close"],
            "ma5": r["ma5"],
            "ma20": r["ma20"],
            "trend": classify_trend(r["close"], r["ma5"], r["ma20"]),
        }

    return mcp


if __name__ == "__main__":
    create_server().run(transport="stdio")
