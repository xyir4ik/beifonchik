import logging
from typing import Any

import aiohttp


logger = logging.getLogger("scheduled-discord-bot")


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str | None,
        chat_id: str | None,
        admin_user_id: str | None = None,
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = str(chat_id) if chat_id else None
        self.admin_user_id = str(admin_user_id) if admin_user_id else None
        self.enabled = bool(bot_token and chat_id)

    async def api_request(self, method: str, payload: dict[str, Any], timeout_seconds: int = 10) -> Any | None:
        if not self.bot_token:
            return None

        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"

        try:
            timeout = aiohttp.ClientTimeout(total=timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as response:
                    body = await response.json(content_type=None)
                    if response.status >= 400 or not body.get("ok"):
                        logger.error("Telegram API %s failed: HTTP %s %s", method, response.status, body)
                        return None
                    return body.get("result")
        except Exception:
            logger.exception("Telegram API %s failed", method)
            return None

    async def send(self, text: str, reply_markup: dict[str, Any] | None = None) -> None:
        if not self.enabled or not self.chat_id:
            return

        await self.send_to_chat(self.chat_id, text, reply_markup=reply_markup)

    async def send_to_chat(
        self,
        chat_id: str | int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        if not self.bot_token:
            return

        payload: dict[str, Any] = {
            "chat_id": str(chat_id),
            "text": text[:3900],
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        await self.api_request("sendMessage", payload)

    async def answer_callback_query(self, callback_query_id: str, text: str) -> None:
        await self.api_request(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_query_id,
                "text": text[:200],
            },
        )

    async def edit_message_text(
        self,
        chat_id: str | int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": str(chat_id),
            "message_id": message_id,
            "text": text[:3900],
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        await self.api_request("editMessageText", payload)

    async def get_updates(self, offset: int | None, timeout_seconds: int = 30) -> list[dict[str, Any]] | None:
        if not self.bot_token:
            return []

        payload: dict[str, Any] = {
            "timeout": timeout_seconds,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset

        result = await self.api_request("getUpdates", payload, timeout_seconds=timeout_seconds + 5)
        return result if isinstance(result, list) else None
