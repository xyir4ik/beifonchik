import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType

import aiohttp
import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from discord import app_commands
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
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None


class TelegramNotifier:
    def __init__(self, bot_token: str | None, chat_id: str | None) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)

    async def send(self, text: str) -> None:
        if not self.enabled:
            return

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text[:3900],
            "disable_web_page_preview": True,
        }

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as response:
                    if response.status >= 400:
                        body = await response.text()
                        logger.error("Telegram notification failed: HTTP %s %s", response.status, body)
        except Exception:
            logger.exception("Telegram notification failed")


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

    if not isinstance(raw_events_list, list):
        raise ValueError(f"{source} must contain an events array")

    default_channel_id = defaults.get("channel_id")
    default_role_id = defaults.get("role_id")
    default_reaction = defaults.get("reaction")
    default_allowed_mentions = defaults.get("allowed_mentions", "none")

    events: list[ScheduledEvent] = []
    event_names: set[str] = set()
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
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
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


def build_notifier_from_env() -> TelegramNotifier:
    return TelegramNotifier(
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
    )


async def notify_runtime_error(notifier: TelegramNotifier, exc: BaseException) -> None:
    if str(exc) == "Another bot process is already running":
        await notifier.send(
            "⚠️ Вторая копия бота остановлена\n\n"
            "Причина: другая копия уже запущена.\n"
            "Сообщения по расписанию не будут дублироваться."
        )
        return

    await notifier.send(
        "💥 Бот аварийно завершился\n\n"
        f"Ошибка:\n{type(exc).__name__}: {exc}"
    )


class ScheduledDiscordBot(discord.Client):
    def __init__(self, config: BotConfig, notifier: TelegramNotifier) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)

        self.events = config.events
        self.scheduler = AsyncIOScheduler(timezone=config.timezone)
        self.timezone = config.timezone
        self.guild_id = config.guild_id
        self.notifier = notifier
        self.tree = app_commands.CommandTree(self)
        self._scheduled = False
        self._commands_synced = False

        self.tree.command(
            name="send_now",
            description="Send one scheduled message by name, or all messages when event_name is empty.",
        )(self.send_now_command)
        self.tree.command(
            name="next_events",
            description="Show upcoming scheduled messages.",
        )(self.next_events_command)

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
        await self.notify_started()

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
            await self.notifier.send(
                "⚠️ Slash-команды не подключились\n\n"
                "Команды: /send_now и /next_events\n"
                "Ошибка: Discord вернул Missing Access\n\n"
                "Проверьте:\n"
                "• бот приглашен на сервер\n"
                "• при приглашении выбран applications.commands\n"
                "• DISCORD_GUILD_ID — это ID сервера, а не канала или роли\n\n"
                "Расписание продолжит работать."
            )
        except discord.HTTPException as exc:
            logger.exception("Discord API error while syncing slash commands. Scheduled messages will still run.")
            await self.notifier.send(
                "⚠️ Slash-команды не подключились\n\n"
                "Команды: /send_now и /next_events\n"
                f"Ошибка Discord API: {exc}\n\n"
                "Расписание продолжит работать."
            )
        finally:
            self._commands_synced = True

    def get_next_runs(self) -> list[tuple[ScheduledEvent, datetime]]:
        next_runs: list[tuple[ScheduledEvent, datetime]] = []
        for event in self.events:
            job = self.scheduler.get_job(event.name)
            if job is None or job.next_run_time is None:
                continue
            next_runs.append((event, job.next_run_time.astimezone(self.timezone)))

        return sorted(next_runs, key=lambda item: item[1])

    def log_next_runs(self) -> None:
        logged_events = set()
        for event, next_run in self.get_next_runs():
            logged_events.add(event.name)
            logger.info("Next %s: %s", event.name, next_run.strftime("%Y-%m-%d %H:%M %Z"))

        for event in self.events:
            if event.name not in logged_events:
                logger.warning("No next run time for %s", event.name)

    def format_next_events(self, limit: int = 5) -> str:
        next_runs = self.get_next_runs()[:limit]
        if not next_runs:
            return "Ближайшие события не найдены."

        lines = ["Ближайшие события:"]
        for event, next_run in next_runs:
            lines.append(f"• {event.name} — {next_run.strftime('%d.%m.%Y %H:%M МСК')}")
        return "\n".join(lines)

    async def notify_started(self) -> None:
        user = f"{self.user}" if self.user else "неизвестно"
        await self.notifier.send(
            "✅ Бот запущен\n\n"
            f"Аккаунт: {user}\n"
            f"Событий в расписании: {len(self.events)}\n\n"
            f"{self.format_next_events(limit=5)}"
        )

    async def send_scheduled_message(self, event: ScheduledEvent) -> bool:
        try:
            channel = self.get_channel(event.channel_id)
            if channel is None:
                channel = await self.fetch_channel(event.channel_id)

            send = getattr(channel, "send", None)
            if not callable(send):
                logger.error("Channel %s for event %s is not messageable", event.channel_id, event.name)
                await self.notifier.send(
                    "❌ Ошибка отправки сообщения\n\n"
                    f"Событие: {event.name}\n"
                    f"Канал: {event.channel_id}\n"
                    f"Текст: {event.text}\n\n"
                    "Ошибка: канал не поддерживает отправку сообщений"
                )
                return False

            message = await send(
                event.text,
                allowed_mentions=build_allowed_mentions(event.allowed_mentions),
            )

            try:
                await message.add_reaction(event.reaction)
            except discord.Forbidden:
                logger.exception("Missing Discord permissions to add reaction for event %s", event.name)
                await self.notifier.send(
                    "⚠️ Сообщение отправлено, но реакция не поставилась\n\n"
                    f"Событие: {event.name}\n"
                    f"Канал: {event.channel_id}\n"
                    f"Реакция: {event.reaction}\n\n"
                    "Ошибка: нет прав Discord на добавление реакции"
                )
                return False
            except discord.HTTPException as exc:
                logger.exception("Discord API error while adding reaction for event %s", event.name)
                await self.notifier.send(
                    "⚠️ Сообщение отправлено, но реакция не поставилась\n\n"
                    f"Событие: {event.name}\n"
                    f"Канал: {event.channel_id}\n"
                    f"Реакция: {event.reaction}\n\n"
                    f"Ошибка Discord API: {exc}"
                )
                return False

            logger.info("Sent scheduled message for event %s", event.name)
            return True
        except discord.Forbidden as exc:
            logger.exception("Missing Discord permissions for event %s", event.name)
            await self.notifier.send(
                "❌ Ошибка отправки сообщения\n\n"
                f"Событие: {event.name}\n"
                f"Канал: {event.channel_id}\n"
                f"Текст: {event.text}\n\n"
                f"Ошибка: нет прав Discord на отправку сообщения\n{exc}"
            )
        except discord.HTTPException as exc:
            logger.exception("Discord API error while sending event %s", event.name)
            await self.notifier.send(
                "❌ Ошибка отправки сообщения\n\n"
                f"Событие: {event.name}\n"
                f"Канал: {event.channel_id}\n"
                f"Текст: {event.text}\n\n"
                f"Ошибка Discord API: {exc}"
            )
        except Exception as exc:
            logger.exception("Unexpected error while sending event %s", event.name)
            await self.notifier.send(
                "❌ Неожиданная ошибка отправки сообщения\n\n"
                f"Событие: {event.name}\n"
                f"Канал: {event.channel_id}\n"
                f"Текст: {event.text}\n\n"
                f"Ошибка: {type(exc).__name__}: {exc}"
            )
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
        await self.notifier.send(
            "ℹ️ Ручная отправка через /send_now\n\n"
            f"Пользователь: {interaction.user}\n"
            f"Событий отправлено: {sent_count} из {len(selected_events)}"
        )

    async def next_events_command(self, interaction: discord.Interaction, limit: int = 5) -> None:
        if not self.can_use_manual_command(interaction):
            await interaction.response.send_message(
                "Эта команда доступна только администраторам или пользователям с правом Manage Server.",
                ephemeral=True,
            )
            return

        limit = max(1, min(limit, 10))
        await interaction.response.send_message(self.format_next_events(limit=limit), ephemeral=True)

    @staticmethod
    def can_use_manual_command(interaction: discord.Interaction) -> bool:
        permissions = getattr(interaction.user, "guild_permissions", None)
        return bool(permissions and (permissions.administrator or permissions.manage_guild))


async def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable is required")

    config = read_config()
    notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)

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
