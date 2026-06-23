import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType

import discord
from discord import app_commands
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


@dataclass(frozen=True)
class BotConfig:
    events: list[ScheduledEvent]
    timezone: ZoneInfo
    guild_id: int | None = None


class SingleInstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.file = None

    def __enter__(self) -> "SingleInstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+", encoding="utf-8")

        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError("Another bot process is already running") from exc

        self.file.seek(0)
        self.file.truncate()
        self.file.write(str(os.getpid()))
        self.file.flush()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.file is None:
            return

        try:
            if os.name == "nt":
                import msvcrt

                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()


def read_events() -> list[ScheduledEvent]:
    raw_events = os.getenv("EVENTS_JSON")

    if raw_events:
        source = "EVENTS_JSON"
        data = json.loads(raw_events)
    else:
        events_file = Path(os.getenv("EVENTS_FILE", "events.json"))
        source = str(events_file)
        data = json.loads(events_file.read_text(encoding="utf-8"))

    if isinstance(data, dict):
        raw_events_list = data.get("events")
        defaults = data
    elif isinstance(data, list):
        raw_events_list = data
        defaults = {}
    else:
        raise ValueError(f"{source} must contain a JSON object or array of events")

    events: list[ScheduledEvent] = []
    event_names: set[str] = set()
    if not isinstance(raw_events_list, list):
        raise ValueError(f"{source} must contain an events array")

    default_channel_id = defaults.get("channel_id")
    default_role_id = defaults.get("role_id")
    default_reaction = defaults.get("reaction")
    default_allowed_mentions = defaults.get("allowed_mentions", "none")

    for index, item in enumerate(raw_events_list, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Event #{index} must be a JSON object")

        try:
            text = str(item["text"])
            role_id = item.get("role_id", default_role_id)
            if role_id and not text.startswith("<@&"):
                text = f"<@&{int(role_id)}> {text}"

            channel_id = item.get("channel_id", default_channel_id)
            reaction = item.get("reaction", default_reaction)
            if channel_id is None:
                raise ValueError("channel_id")
            if reaction is None:
                raise ValueError("reaction")

            event = ScheduledEvent(
                name=str(item["name"]),
                channel_id=int(channel_id),
                text=text,
                reaction=str(reaction),
                cron=str(item["cron"]),
                allowed_mentions=str(item.get("allowed_mentions", default_allowed_mentions)),
            )
        except KeyError as exc:
            raise ValueError(f"Event #{index} is missing required field: {exc.args[0]}") from exc
        except ValueError as exc:
            if exc.args and exc.args[0] in {"channel_id", "reaction"}:
                raise ValueError(f"Event #{index} is missing required field or default: {exc.args[0]}") from exc
            raise
        except TypeError as exc:
            raise ValueError(f"Event #{index} is missing channel_id, reaction, or another required default") from exc

        if event.name in event_names:
            raise ValueError(f"Event name must be unique: {event.name}")

        CronTrigger.from_crontab(event.cron)
        events.append(event)
        event_names.add(event.name)

    if not events:
        raise ValueError(f"{source} must contain at least one event")

    return events


def read_config() -> BotConfig:
    timezone_name = os.getenv("TIMEZONE", "Europe/Moscow")
    guild_id = int(os.getenv("DISCORD_GUILD_ID", "0") or "0") or None

    return BotConfig(
        events=read_events(),
        timezone=ZoneInfo(timezone_name),
        guild_id=guild_id,
    )


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
    def __init__(self, events: list[ScheduledEvent], timezone: ZoneInfo, guild_id: int | None) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)

        self.events = events
        self.scheduler = AsyncIOScheduler(timezone=timezone)
        self.timezone = timezone
        self.guild_id = guild_id
        self.tree = app_commands.CommandTree(self)
        self._scheduled = False
        self._commands_synced = False

        self.tree.command(
            name="send_now",
            description="Send one scheduled message by name, or all messages when event_name is empty.",
        )(self.send_now_command)

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")

        await self.sync_commands()

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
        self.log_next_runs()

    async def sync_commands(self) -> None:
        if self._commands_synced:
            return

        try:
            if self.guild_id is not None:
                guild = discord.Object(id=self.guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                logger.info("Synced %s slash command(s) to guild %s", len(synced), self.guild_id)
            else:
                synced = await self.tree.sync()
                logger.info("Synced %s global slash command(s)", len(synced))
        except discord.Forbidden:
            logger.exception(
                "Could not sync slash commands. Check DISCORD_GUILD_ID and invite the bot with applications.commands scope. Scheduled messages will still run."
            )
        except discord.HTTPException:
            logger.exception("Discord API error while syncing slash commands. Scheduled messages will still run.")
        finally:
            self._commands_synced = True

    def log_next_runs(self) -> None:
        for event in self.events:
            job = self.scheduler.get_job(event.name)
            if job is None or job.next_run_time is None:
                logger.warning("No next run time for %s", event.name)
                continue

            next_run = job.next_run_time.astimezone(self.timezone)
            logger.info("Next %s: %s", event.name, next_run.strftime("%Y-%m-%d %H:%M %Z"))

    async def send_scheduled_message(self, event: ScheduledEvent) -> bool:
        try:
            channel = self.get_channel(event.channel_id)
            if channel is None:
                channel = await self.fetch_channel(event.channel_id)

            send = getattr(channel, "send", None)
            if not callable(send):
                logger.error("Channel %s for event %s is not messageable", event.channel_id, event.name)
                return False

            message = await send(
                event.text,
                allowed_mentions=build_allowed_mentions(event.allowed_mentions),
            )
            await message.add_reaction(event.reaction)
            logger.info("Sent scheduled message for event %s", event.name)
            return True
        except discord.Forbidden:
            logger.exception("Missing Discord permissions for event %s", event.name)
        except discord.HTTPException:
            logger.exception("Discord API error while sending event %s", event.name)
        except Exception:
            logger.exception("Unexpected error while sending event %s", event.name)
        return False

    async def send_now_command(self, interaction: discord.Interaction, event_name: str = "") -> None:
        if not self.can_use_manual_command(interaction):
            await interaction.response.send_message(
                "Эта команда доступна только администраторам или пользователям с правом Manage Server.",
                ephemeral=True,
            )
            return

        selected_events = self.events
        if event_name:
            selected_events = [event for event in self.events if event.name == event_name]

        if not selected_events:
            available = ", ".join(event.name for event in self.events)
            await interaction.response.send_message(
                f"Событие не найдено. Доступные имена: {available}",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        sent_count = 0
        for event in selected_events:
            if await self.send_scheduled_message(event):
                sent_count += 1

        await interaction.followup.send(
            f"Готово: отправлено {sent_count} из {len(selected_events)} сообщений.",
            ephemeral=True,
        )

    @staticmethod
    def can_use_manual_command(interaction: discord.Interaction) -> bool:
        permissions = getattr(interaction.user, "guild_permissions", None)
        return bool(
            permissions
            and (permissions.administrator or permissions.manage_guild)
        )


async def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable is required")

    config = read_config()

    bot = ScheduledDiscordBot(
        events=config.events,
        timezone=config.timezone,
        guild_id=config.guild_id,
    )
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    lock_file = Path(os.getenv("LOCK_FILE", ".bot.lock"))
    try:
        with SingleInstanceLock(lock_file):
            asyncio.run(main())
    except RuntimeError as exc:
        logger.error("%s", exc)
        sys.exit(1)
