# 專案規則

- Python 3.12（由 uv 固定；3.15 缺少 pyyaml 等套件的預編譯 wheel），套件管理用 uv
- FinMind endpoint: https://api.finmindtrade.com/api/v4/data，dataset: TaiwanStockPrice
- token 只從環境變數 `FINMIND_TOKEN` 取得（本機開發由 `.env` 經 python-dotenv 載入；雲端由 GitHub Secrets 提供），以 `Authorization: Bearer` 傳送；禁止寫進程式碼或 commit
- 時區 Asia/Taipei；SQLite 路徑 `data/stock.db`（此檔進 git，由 GitHub Actions 平日 17:30 更新後 commit）
- 每天只抓一次（平日 17:30），不做盤中排程；fetcher 每次執行會自動補齊缺漏的交易日
- 本機與雲端不要同時跑 fetcher，避免 git 衝突；本機取得資料用 `git pull`
- stock_mcp 與網頁只讀 SQLite（唯讀連線），不直接呼叫 FinMind
- 趨勢判斷只實作在 `core/trend.py`，其他地方 import 使用
- `TaiwanStockPrice` 為未還原股價，除權息日 MA 與漲跌幅會失真
- MCP SDK 為 2.x：`from mcp.server.mcpserver import MCPServer`；stdio 模式不可 print 到 stdout，log 走 stderr
- 所有檔案讀寫指定 `encoding="utf-8"`；路徑以 `Path(__file__)` 為基準
- 網站只有一份範本 `web/templates/index.html`：公開網站（GitHub Pages）由 `build_site.py` 產生純靜態頁面；本機版 `web/app.py` 啟動時用同一份資料建站，並多一個「AI 對話分析」分頁（`chat_enabled=True`）
- **對話功能只放本機版**：`ac_chat/` 以 Claude 的工具呼叫查詢 SQLite（只讀），`ANTHROPIC_API_KEY` 只能放本機 `.env`，絕不可進 repo、公開網站或前端程式碼；公開網站的建置不可包含對話分頁
- 對話預設模型 `claude-opus-5-5`（`AC_CHAT_MODEL` 可改），只用資料庫內的資料，沒有新聞；系統提示規定不得編造原因、不給買賣建議
- 程式碼註解與 log 訊息用繁體中文
- 測試：`uv run pytest -q`
