import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from zoneinfo import ZoneInfo


load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("scheduled-discord-bot")


@dataclass(frozen=True)
class ScheduledEvent:
    name: str
    channel_id: int
    text: str
    reaction: str
    cron: str
    allowed_mentions: str = "none"


def read_events() -> list[ScheduledEvent]:
    raw_events = os.getenv("EVENTS_JSON")

    if raw_events:
        source = "EVENTS_JSON"
        data = json.loads(raw_events)
    else:
        events_file = Path(os.getenv("EVENTS_FILE", "events.json"))
        source = str(events_file)
        data = json.loads(events_file.read_text(encoding="utf-8"))

    if not isinstance(data, list):
        raise ValueError(f"{source} must contain a JSON array of events")

    events: list[ScheduledEvent] = []
    event_names: set[str] = set()
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Event #{index} must be a JSON object")

        try:
            event = ScheduledEvent(
                name=str(item["name"]),
                channel_id=int(item["channel_id"]),
                text=str(item["text"]),
                reaction=str(item["reaction"]),
                cron=str(item["cron"]),
                allowed_mentions=str(item.get("allowed_mentions", "none")),
            )
        except KeyError as exc:
            raise ValueError(f"Event #{index} is missing required field: {exc.args[0]}") from exc

        if event.name in event_names:
            raise ValueError(f"Event name must be unique: {event.name}")

        CronTrigger.from_crontab(event.cron)
        events.append(event)
        event_names.add(event.name)

    if not events:
        raise ValueError(f"{source} must contain at least one event")

    return events


def build_allowed_mentions(mode: str) -> discord.AllowedMentions:
    normalized = mode.strip().lower()

    if normalized == "none":
        return discord.AllowedMentions.none()
    if normalized == "users":
        return discord.AllowedMentions(users=True)
    if normalized == "roles":
        return discord.AllowedMentions(roles=True)
    if normalized == "everyone":
        return discord.AllowedMentions(everyone=True)
    if normalized == "all":
        return discord.AllowedMentions.all()

    logger.warning("Unknown allowed_mentions=%s, falling back to none", mode)
    return discord.AllowedMentions.none()


class ScheduledDiscordBot(discord.Client):
    def __init__(self, events: list[ScheduledEvent], timezone: ZoneInfo) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)

        self.events = events
        self.scheduler = AsyncIOScheduler(timezone=timezone)
        self._scheduled = False

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")

        if self._scheduled:
            return

        for event in self.events:
            trigger = CronTrigger.from_crontab(event.cron, timezone=self.scheduler.timezone)
            self.scheduler.add_job(
                self.send_scheduled_message,
                trigger=trigger,
                args=[event],
                id=event.name,
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("Scheduled %s with cron '%s'", event.name, event.cron)

        self.scheduler.start()
        self._scheduled = True

    async def send_scheduled_message(self, event: ScheduledEvent) -> None:
        try:
            channel = self.get_channel(event.channel_id)
            if channel is None:
                channel = await self.fetch_channel(event.channel_id)

            send = getattr(channel, "send", None)
            if not callable(send):
                logger.error("Channel %s for event %s is not messageable", event.channel_id, event.name)
                return

            message = await send(
                event.text,
                allowed_mentions=build_allowed_mentions(event.allowed_mentions),
            )
            await message.add_reaction(event.reaction)
            logger.info("Sent scheduled message for event %s", event.name)
        except discord.Forbidden:
            logger.exception("Missing Discord permissions for event %s", event.name)
        except discord.HTTPException:
            logger.exception("Discord API error while sending event %s", event.name)
        except Exception:
            logger.exception("Unexpected error while sending event %s", event.name)


async def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable is required")

    timezone_name = os.getenv("TIMEZONE", "Europe/Moscow")
    timezone = ZoneInfo(timezone_name)
    events = read_events()

    bot = ScheduledDiscordBot(events=events, timezone=timezone)
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
