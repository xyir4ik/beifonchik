from dataclasses import dataclass
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class ScheduledEvent:
    name: str
    channel_id: int
    text: str
    reaction: str
    cron: str
    allowed_mentions: str = "none"
    enabled: bool = True


@dataclass(frozen=True)
class BotConfig:
    events: list[ScheduledEvent]
    timezone: ZoneInfo
    event_store: object
    guild_id: int | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_admin_user_id: str | None = None
    enable_discord_commands: bool = False
