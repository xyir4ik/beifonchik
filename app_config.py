import os
from pathlib import Path
from zoneinfo import ZoneInfo

from models import BotConfig
from stores import EventStore
from telegram_client import TelegramNotifier


def get_env_first(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def read_config() -> BotConfig:
    timezone_name = os.getenv("TIMEZONE", "Europe/Moscow")
    guild_id = int(os.getenv("DISCORD_GUILD_ID", "0") or "0") or None
    raw_events_json = os.getenv("EVENTS_JSON") or None
    event_store_path = resolve_event_store_path()

    event_store = EventStore(
        path=event_store_path,
        raw_events_json=raw_events_json,
    )

    return BotConfig(
        events=event_store.load(),
        timezone=ZoneInfo(timezone_name),
        event_store=event_store,
        guild_id=guild_id,
        telegram_bot_token=get_env_first("TG_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=get_env_first("TG_CHAT_ID", "TELEGRAM_CHAT_ID"),
        telegram_admin_user_id=get_env_first("TG_ADMIN_USER_ID", "TELEGRAM_ADMIN_USER_ID"),
        enable_discord_commands=parse_bool(os.getenv("ENABLE_DISCORD_COMMANDS"), default=False),
    )


def resolve_event_store_path() -> Path:
    schedule_file = os.getenv("SCHEDULE_FILE")
    seed_file = Path(os.getenv("EVENTS_FILE", "events.json"))

    if schedule_file:
        target = Path(schedule_file)
    else:
        data_dir = os.getenv("DATA_DIR")
        target = Path(data_dir).joinpath("schedule.json") if data_dir else seed_file

    if target != seed_file and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(seed_file.read_text(encoding="utf-8"), encoding="utf-8")

    return target


def build_notifier_from_env() -> TelegramNotifier:
    return TelegramNotifier(
        bot_token=get_env_first("TG_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"),
        chat_id=get_env_first("TG_CHAT_ID", "TELEGRAM_CHAT_ID"),
        admin_user_id=get_env_first("TG_ADMIN_USER_ID", "TELEGRAM_ADMIN_USER_ID"),
    )


def data_file(name: str) -> Path:
    return Path(os.getenv("DATA_DIR", ".")).joinpath(name)
