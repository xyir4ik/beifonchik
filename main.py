import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from app_config import build_notifier_from_env, read_config
from bot_client import ScheduledDiscordBot
from process_lock import SingleInstanceLock
from telegram_client import TelegramNotifier


load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("scheduled-discord-bot")


async def notify_runtime_error(notifier: TelegramNotifier, exc: BaseException) -> None:
    if str(exc) == "Another bot process is already running":
        await notifier.send(
            "Вторая копия бота остановлена\n\n"
            "Причина: другая копия уже запущена.\n"
            "Сообщения по расписанию не будут дублироваться."
        )
        return

    await notifier.send(
        "Бот аварийно завершился\n\n"
        f"Ошибка:\n{type(exc).__name__}: {exc}"
    )


async def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable is required")

    config = read_config()
    notifier = TelegramNotifier(
        config.telegram_bot_token,
        config.telegram_chat_id,
        admin_user_id=config.telegram_admin_user_id,
    )

    bot = ScheduledDiscordBot(config=config, notifier=notifier)
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    lock_file = Path(os.getenv("LOCK_FILE", ".bot.lock"))
    startup_notifier = build_notifier_from_env()

    try:
        with SingleInstanceLock(lock_file):
            asyncio.run(main())
    except RuntimeError as exc:
        logger.error("%s", exc)
        asyncio.run(notify_runtime_error(startup_notifier, exc))
        sys.exit(1)
    except Exception as exc:
        logger.exception("Bot crashed")
        asyncio.run(notify_runtime_error(startup_notifier, exc))
        sys.exit(1)
