"""對話引擎：以 Claude 的工具呼叫（手動迴圈）查詢本機資料庫，再用繁體中文分析。

- 預設模型 claude-opus-5-5，可用環境變數 AC_CHAT_MODEL 改成 claude-sonnet-5-5 等較省錢的模型。
- 只使用 tools.py 的唯讀查詢工具；不上網、沒有新聞，所以系統提示要求不得編造事件。
- 金鑰只從環境變數 ANTHROPIC_API_KEY 讀取（本機放 .env），絕不進 repo。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Iterator

import anthropic

from ac_chat.tools import TOOL_LABELS, TOOL_SCHEMAS, Toolbox

log = logging.getLogger("ac_chat")

DEFAULT_MODEL = "claude-opus-5-5"
MAX_ITERATIONS = 8          # 單次提問最多往返幾輪工具呼叫
MAX_TOKENS = 16000          # 非串流請求的建議上限，避免 HTTP 逾時


def system_prompt(latest_date: str, stocks_text: str) -> str:
    return f"""你是「AC台股分析」網站內建的台股資料分析助理，服務對象是一般投資人。請一律使用繁體中文回答。

【你能用的資料】
只能透過工具查詢本站資料庫（資料來源 FinMind，截至 {latest_date}）：追蹤清單內個股的日 K、MA、MACD、三大法人買賣、外資持股、本益比、月營收，以及台指期、台股加權、道瓊、費城半導體、那斯達克指數與整體三大法人。
追蹤清單：{stocks_text}
股價為「未還原股價」，除權息日會出現跳空，不代表真實漲跌。你沒有新聞、公告、法說會、產業消息或即時行情，也不能上網。

【回答規則】
1. 先查資料再回答，不要憑記憶說股價或數字。引用具體的日期與數字；單位要寫清楚（張、億元、%、百分點）。可一次平行呼叫多個工具。
2. 使用者問「為什麼漲／跌」時，只從資料找「可以支持的線索」：技術面（均線、MACD、量能、高低點）、籌碼面（三大法人、外資持股）、跨市場（加權、台指期、費半、那斯達克、道瓊）、估值與營收動能。要分清楚「資料顯示的事實」與「你的推論」，推論要用「可能」「傾向」等字眼，並說明依據。
3. 絕對不要編造新聞、事件、財報內容或原因。若資料無法解釋（例如單日大跌常與消息面有關），要直接說明資料看不出原因，並建議使用者查詢公司公告、法說會或財經新聞。
4. 相關不等於因果；樣本短或訊號互相矛盾時，要指出不確定性，不要硬給單一結論。
5. 不提供買進、賣出、加碼、停損的指示，也不給目標價或保證性預測。可以描述條件與風險（例如「若收盤跌破 MA20，短線轉弱的訊號會增加」）。回答最後用一句話提醒「以上僅為資料分析，非投資建議」。
6. 使用者問到不在追蹤清單的股票，直接說明目前只能查詢清單內的股票。若使用者只說公司名稱，先用 list_stocks 對照代號。
7. 回答格式：先給 2～3 句結論，再用條列或小表格列依據。台股慣例紅漲綠跌，說明時以「上漲／下跌」文字表達即可，不需要提顏色。保持精簡，不要重複工具回傳的原始數字清單。"""


_ERRORS: list[tuple[type, str]] = [
    (anthropic.AuthenticationError, "API 金鑰無效或已被停用，請檢查 .env 裡的 ANTHROPIC_API_KEY。"),
    (anthropic.PermissionDeniedError, "這把 API 金鑰沒有權限使用此模型，請改用其他模型（環境變數 AC_CHAT_MODEL）或檢查帳號權限。"),
    (anthropic.NotFoundError, "找不到指定的模型，請檢查環境變數 AC_CHAT_MODEL 是否拼錯。"),
    (anthropic.RateLimitError, "請求太頻繁或已達用量上限，請稍後再試。"),
    (anthropic.BadRequestError, "請求被 API 拒絕（可能是帳戶額度不足、或模型不支援某個參數），請到 Anthropic Console 檢查。"),
    (anthropic.APIConnectionError, "無法連線到 Anthropic，請檢查網路。"),
]


def _friendly_error(exc: Exception) -> str:
    for cls, msg in _ERRORS:
        if isinstance(exc, cls):
            return msg
    if isinstance(exc, anthropic.APIStatusError):
        return f"Anthropic 服務暫時發生錯誤（HTTP {exc.status_code}），請稍後再試。"
    return "發生未預期的錯誤，請查看本機伺服器的日誌。"


def _text_of(blocks: list[Any]) -> str:
    return "".join(b.text for b in blocks if getattr(b, "type", None) == "text").strip()


def run_chat(
    client: anthropic.Anthropic,
    messages: list[dict],
    toolbox: Toolbox,
    *,
    model: str | None = None,
    effort: str | None = None,
) -> Iterator[dict]:
    """執行一次提問，逐步產生事件：tool（正在查詢）、text（最終回答）、error、done（含 token 用量）。

    messages 為先前對話（只含 user/assistant 的純文字）；工具往返只存在於這一次提問之內。
    """
    model = model or os.environ.get("AC_CHAT_MODEL") or DEFAULT_MODEL
    effort = effort or os.environ.get("AC_CHAT_EFFORT") or "medium"

    latest = toolbox.t_list_stocks({})["latest_data_date"]
    system = system_prompt(latest, "、".join(toolbox.stocks))
    api_messages: list[dict] = [{"role": m["role"], "content": m["content"]} for m in messages]
    used_in = used_out = 0

    for _ in range(MAX_ITERATIONS):
        try:
            response = client.messages.create(
                model=model, max_tokens=MAX_TOKENS, system=system, tools=TOOL_SCHEMAS,
                messages=api_messages, output_config={"effort": effort},
            )
        except anthropic.APIError as e:
            log.error("Anthropic API 錯誤：%s %s", type(e).__name__, getattr(e, "message", ""))
            yield {"type": "error", "message": _friendly_error(e)}
            return
        except Exception:
            log.exception("對話發生未預期錯誤")
            yield {"type": "error", "message": _friendly_error(Exception())}
            return

        used_in += response.usage.input_tokens
        used_out += response.usage.output_tokens

        if response.stop_reason == "refusal":
            yield {"type": "error", "message": "這個問題被模型的安全機制拒絕回答，請換個方式提問。"}
            return

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason == "pause_turn":
            api_messages.append({"role": "assistant", "content": response.content})
            continue
        if not tool_uses:
            text = _text_of(response.content)
            if response.stop_reason == "max_tokens":
                text += "\n\n（回答過長被截斷，請縮小問題範圍後再問一次。）"
            yield {"type": "text", "text": text or "（沒有產生回答，請換個方式提問。）"}
            yield {"type": "done", "usage": {"input_tokens": used_in, "output_tokens": used_out}, "model": model}
            return

        # 思考區塊與工具呼叫要原樣放回，下一輪才能延續推理
        api_messages.append({"role": "assistant", "content": response.content})
        results = []
        for tu in tool_uses:
            args = tu.input if isinstance(tu.input, dict) else {}
            label = TOOL_LABELS.get(tu.name, lambda a: f"呼叫 {tu.name}")(args)
            yield {"type": "tool", "name": tu.name, "label": label}
            content, is_error = toolbox.run(tu.name, args)
            item = {"type": "tool_result", "tool_use_id": tu.id, "content": content}
            if is_error:
                item["is_error"] = True
            results.append(item)
        api_messages.append({"role": "user", "content": results})

    yield {"type": "error", "message": "查詢次數過多仍無法完成，請把問題拆小一點再問。"}
