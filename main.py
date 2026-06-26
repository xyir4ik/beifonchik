import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any

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

TEST_ROLE_ID = 1203431256419995668
DUPLICATE_WINDOW_SECONDS = 300


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
    event_store: "EventStore"
    guild_id: int | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    enable_discord_commands: bool = False


class EventStore:
    def __init__(self, path: Path, raw_events_json: str | None = None) -> None:
        self.path = path
        self.raw_events_json = raw_events_json
        self.readonly = raw_events_json is not None
        self.data: dict[str, Any] = {}

    def load(self) -> list[ScheduledEvent]:
        if self.raw_events_json:
            source = "EVENTS_JSON"
            loaded = json.loads(self.raw_events_json)
        else:
            source = str(self.path)
            loaded = json.loads(self.path.read_text(encoding="utf-8"))

        if isinstance(loaded, list):
            loaded = {"events": loaded}
        if not isinstance(loaded, dict):
            raise ValueError(f"{source} must contain a JSON object or array of events")

        events_raw = loaded.get("events")
        if not isinstance(events_raw, list):
            raise ValueError(f"{source} must contain an events array")

        self.data = loaded
        return self.parse_events(self.data)

    def save(self) -> None:
        if self.readonly:
            raise RuntimeError("Cannot save schedule while EVENTS_JSON is used")

        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def parse_events(self, data: dict[str, Any]) -> list[ScheduledEvent]:
        default_channel_id = data.get("channel_id")
        default_role_id = data.get("role_id")
        default_reaction = data.get("reaction")
        default_allowed_mentions = data.get("allowed_mentions", "none")
        raw_events = data.get("events")

        if not isinstance(raw_events, list):
            raise ValueError("events.json must contain an events array")

        events: list[ScheduledEvent] = []
        event_names: set[str] = set()
        for index, item in enumerate(raw_events, start=1):
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
                    enabled=bool(item.get("enabled", True)),
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
            raise ValueError("events.json must contain at least one event")

        return events

    def events_raw(self) -> list[dict[str, Any]]:
        events = self.data.setdefault("events", [])
        if not isinstance(events, list):
            raise ValueError("events must be a list")
        return events

    def set_enabled(self, index: int, enabled: bool) -> None:
        events = self.events_raw()
        events[index]["enabled"] = enabled
        self.save()

    def delete_event(self, index: int) -> None:
        events = self.events_raw()
        del events[index]
        self.save()

    def add_event(self, name: str, cron: str, text: str) -> None:
        CronTrigger.from_crontab(cron)

        events = self.events_raw()
        if any(str(event.get("name")) == name for event in events if isinstance(event, dict)):
            raise ValueError(f"Событие с именем {name} уже существует")

        events.append(
            {
                "name": name,
                "text": text,
                "cron": cron,
                "enabled": True,
            }
        )
        self.save()


class LastSentStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, float] = {}
        self.load()

    def load(self) -> None:
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.data = {str(key): float(value) for key, value in raw.items()}
        except Exception:
            logger.exception("Could not read last sent file %s", self.path)
            self.data = {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        except Exception:
            logger.exception("Could not write last sent file %s", self.path)

    def recently_sent(self, event_name: str, window_seconds: int = DUPLICATE_WINDOW_SECONDS) -> bool:
        last_sent_at = self.data.get(event_name)
        if last_sent_at is None:
            return False
        return datetime.now(timezone.utc).timestamp() - last_sent_at < window_seconds

    def mark_sent(self, event_name: str) -> None:
        self.data[event_name] = datetime.now(timezone.utc).timestamp()
        self.save()


class TelegramNotifier:
    def __init__(self, bot_token: str | None, chat_id: str | None) -> None:
        self.bot_token = bot_token
        self.chat_id = str(chat_id) if chat_id else None
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
        logger.info("Created persistent schedule file %s from %s", target, seed_file)

    return target


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
        bot_token=get_env_first("TG_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"),
        chat_id=get_env_first("TG_CHAT_ID", "TELEGRAM_CHAT_ID"),
    )


def data_file(name: str) -> Path:
    return Path(os.getenv("DATA_DIR", ".")).joinpath(name)


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


class ScheduledDiscordBot(discord.Client):
    def __init__(self, config: BotConfig, notifier: TelegramNotifier) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)

        self.events = config.events
        self.event_store = config.event_store
        self.scheduler = AsyncIOScheduler(timezone=config.timezone)
        self.timezone = config.timezone
        self.guild_id = config.guild_id
        self.enable_discord_commands = config.enable_discord_commands
        self.notifier = notifier
        self.last_sent_store = LastSentStore(data_file("last_sent.json"))
        self.pending_actions: dict[str, str] = {}
        self.tree: app_commands.CommandTree | None = None
        self._scheduled = False
        self._commands_synced = False
        self._telegram_task: asyncio.Task[None] | None = None

        if self.enable_discord_commands:
            self.tree = app_commands.CommandTree(self)
            self.tree.command(
                name="send_now",
                description="Send one scheduled message by name, or all messages when event_name is empty.",
            )(self.discord_send_now_command)
            self.tree.command(
                name="next_events",
                description="Show upcoming scheduled messages.",
            )(self.discord_next_events_command)

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")

        await self.sync_discord_commands()

        if self._scheduled:
            return

        self.rebuild_schedule()
        self.scheduler.start()
        self._scheduled = True
        self.log_next_runs()
        await self.notify_started()
        self.start_telegram_commands()

    async def close(self) -> None:
        if self._telegram_task:
            self._telegram_task.cancel()
        await super().close()

    def rebuild_schedule(self) -> None:
        self.scheduler.remove_all_jobs()
        for event in self.events:
            if not event.enabled:
                logger.info("Skipped disabled event %s", event.name)
                continue

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

    async def reload_events(self) -> None:
        self.events = self.event_store.load()
        self.rebuild_schedule()
        self.log_next_runs()

    async def sync_discord_commands(self) -> None:
        if not self.enable_discord_commands or self.tree is None or self._commands_synced:
            return

        try:
            if self.guild_id is not None:
                guild = discord.Object(id=self.guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                logger.info("Synced %s Discord slash command(s) to guild %s", len(synced), self.guild_id)
            else:
                synced = await self.tree.sync()
                logger.info("Synced %s global Discord slash command(s)", len(synced))
        except discord.Forbidden:
            logger.exception("Could not sync Discord slash commands")
            await self.notifier.send(
                "Discord slash-команды не подключились\n\n"
                "Расписание и Telegram-команды продолжат работать."
            )
        except discord.HTTPException as exc:
            logger.exception("Discord API error while syncing slash commands")
            await self.notifier.send(
                "Discord slash-команды не подключились\n\n"
                f"Ошибка Discord API: {exc}\n\n"
                "Расписание и Telegram-команды продолжат работать."
            )
        finally:
            self._commands_synced = True

    def start_telegram_commands(self) -> None:
        if not self.notifier.enabled:
            logger.info(
                "Telegram commands are disabled because TG_BOT_TOKEN/TELEGRAM_BOT_TOKEN or TG_CHAT_ID/TELEGRAM_CHAT_ID is not set"
            )
            return
        if self._telegram_task and not self._telegram_task.done():
            return

        self._telegram_task = asyncio.create_task(self.telegram_polling_loop())
        logger.info("Telegram commands polling started")

    async def telegram_polling_loop(self) -> None:
        offset = await self.get_initial_telegram_offset()

        while not self.is_closed():
            updates = await self.notifier.get_updates(offset=offset, timeout_seconds=30)
            if updates is None:
                logger.error(
                    "Telegram getUpdates failed. Check TELEGRAM_BOT_TOKEN or TG_BOT_TOKEN. Retrying in 60 seconds."
                )
                await asyncio.sleep(60)
                continue

            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                await self.handle_telegram_update(update)

            await asyncio.sleep(0.2)

    async def get_initial_telegram_offset(self) -> int | None:
        updates = await self.notifier.get_updates(offset=None, timeout_seconds=1)
        if updates is None:
            logger.error("Telegram getUpdates failed during startup. Check TELEGRAM_BOT_TOKEN or TG_BOT_TOKEN.")
            return None

        update_ids = [update.get("update_id") for update in updates if isinstance(update.get("update_id"), int)]
        if not update_ids:
            return None
        return max(update_ids) + 1

    async def handle_telegram_update(self, update: dict[str, Any]) -> None:
        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            await self.handle_telegram_callback(callback_query)
            return

        message = update.get("message")
        if not isinstance(message, dict):
            return

        chat = message.get("chat")
        if not isinstance(chat, dict):
            return

        chat_id = str(chat.get("id"))
        if chat_id != self.notifier.chat_id:
            logger.warning("Ignoring Telegram command from unauthorized chat_id=%s", chat_id)
            return

        text = str(message.get("text") or "").strip()
        if not text:
            return

        pending_action = self.pending_actions.get(chat_id)
        if pending_action == "add_event" and not text.startswith("/"):
            await self.handle_add_event_input(text)
            return

        if not text.startswith("/"):
            return

        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        argument = argument.strip()

        if command in {"/start", "/help"}:
            await self.notifier.send(self.telegram_help_text())
        elif command == "/status":
            await self.handle_status()
        elif command == "/reload":
            await self.handle_reload()
        elif command == "/schedule":
            await self.handle_schedule_menu()
        elif command == "/next_events":
            await self.handle_telegram_next_events(argument)
        elif command == "/send_now":
            await self.handle_telegram_send_now(argument)
        elif command == "/test":
            await self.handle_telegram_test()
        elif command == "/cancel":
            self.pending_actions.pop(chat_id, None)
            await self.notifier.send("Действие отменено.")
        else:
            await self.notifier.send("Неизвестная команда. Напишите /help.")

    def telegram_help_text(self) -> str:
        return (
            "Команды админа:\n\n"
            "/status - статус бота\n"
            "/schedule - управление расписанием кнопками\n"
            "/reload - перечитать events.json\n"
            "/next_events - показать 5 ближайших событий\n"
            "/next_events 10 - показать до 10 событий\n"
            "/send_now - выбрать ручную отправку кнопкой\n"
            "/send_now weekly_25x25_common_monday - отправить одно событие\n"
            "/test - выбрать тестовое событие кнопкой\n"
            "/cancel - отменить ввод"
        )

    async def handle_status(self) -> None:
        active_count = sum(1 for event in self.events if event.enabled)
        disabled_count = len(self.events) - active_count
        await self.notifier.send(
            "Статус бота\n\n"
            f"Discord: онлайн как {self.user}\n"
            f"Часовой пояс: {self.timezone.key}\n"
            f"Событий всего: {len(self.events)}\n"
            f"Включено: {active_count}\n"
            f"Отключено: {disabled_count}\n"
            f"Telegram-команды: {'включены' if self.notifier.enabled else 'выключены'}\n\n"
            f"{self.format_next_events(limit=5)}"
        )

    async def handle_reload(self) -> None:
        try:
            await self.reload_events()
            await self.notifier.send(
                "Расписание перечитано\n\n"
                f"Событий: {len(self.events)}\n\n"
                f"{self.format_next_events(limit=5)}"
            )
        except Exception as exc:
            logger.exception("Could not reload schedule")
            await self.notifier.send(f"Не удалось перечитать расписание: {type(exc).__name__}: {exc}")

    async def handle_schedule_menu(self) -> None:
        await self.notifier.send(
            "Управление расписанием",
            reply_markup=self.schedule_menu_keyboard(),
        )

    async def handle_telegram_test(self) -> None:
        await self.notifier.send(
            "Выберите тестовое уведомление для отправки в Discord.\n\n"
            f"В тесте будет тегаться роль: {TEST_ROLE_ID}",
            reply_markup=self.event_keyboard("test"),
        )

    async def handle_telegram_send_now(self, event_name: str) -> None:
        if not event_name:
            await self.notifier.send(
                "Выберите событие для ручной отправки:",
                reply_markup=self.event_keyboard("send"),
            )
            return

        selected_events = self.find_events(event_name)
        if not selected_events:
            await self.notifier.send(
                "Событие не найдено.\n\n"
                f"Доступные имена:\n{self.format_event_names()}"
            )
            return

        await self.send_events_manually(selected_events)

    async def handle_telegram_next_events(self, argument: str) -> None:
        limit = 5
        if argument:
            try:
                limit = int(argument)
            except ValueError:
                await self.notifier.send("Лимит должен быть числом, например: /next_events 10")
                return

        limit = max(1, min(limit, 10))
        await self.notifier.send(self.format_next_events(limit=limit))

    async def handle_add_event_input(self, text: str) -> None:
        try:
            name, cron, event_text = [part.strip() for part in text.split("|", 2)]
            if not name or not cron or not event_text:
                raise ValueError("empty field")
        except ValueError:
            await self.notifier.send(
                "Не понял формат.\n\n"
                "Отправьте так:\n"
                "name | cron | text\n\n"
                "Пример:\n"
                "weekly_test | 0 19 * * fri | реаки тест"
            )
            return

        try:
            self.event_store.add_event(name=name, cron=cron, text=event_text)
            await self.reload_events()
            self.pending_actions.pop(self.notifier.chat_id or "", None)
            await self.notifier.send(f"Событие добавлено и включено:\n{name}")
        except Exception as exc:
            logger.exception("Could not add event")
            await self.notifier.send(f"Не удалось добавить событие: {type(exc).__name__}: {exc}")

    async def handle_telegram_callback(self, callback_query: dict[str, Any]) -> None:
        callback_query_id = str(callback_query.get("id") or "")
        data = str(callback_query.get("data") or "")

        message = callback_query.get("message")
        if not isinstance(message, dict):
            return

        chat = message.get("chat")
        if not isinstance(chat, dict):
            return

        chat_id = str(chat.get("id"))
        if chat_id != self.notifier.chat_id:
            logger.warning("Ignoring Telegram callback from unauthorized chat_id=%s", chat_id)
            if callback_query_id:
                await self.notifier.answer_callback_query(callback_query_id, "Нет доступа")
            return

        if callback_query_id:
            await self.notifier.answer_callback_query(callback_query_id, "Принято")

        if data == "menu:main":
            await self.edit_callback_message(message, "Управление расписанием", self.schedule_menu_keyboard())
        elif data == "menu:list":
            await self.edit_callback_message(message, self.format_events_list(), self.schedule_menu_keyboard())
        elif data == "menu:toggle":
            await self.edit_callback_message(message, "Выберите событие для включения/отключения:", self.event_keyboard("toggle"))
        elif data == "menu:delete":
            await self.edit_callback_message(message, "Выберите событие для удаления:", self.event_keyboard("delete"))
        elif data == "menu:add":
            self.pending_actions[chat_id] = "add_event"
            await self.edit_callback_message(
                message,
                "Добавление события\n\n"
                "Отправьте следующим сообщением:\n"
                "name | cron | text\n\n"
                "Пример:\n"
                "weekly_test | 0 19 * * fri | реаки тест\n\n"
                "Для отмены: /cancel",
            )
        elif data == "menu:reload":
            await self.handle_reload()
        elif data.startswith("toggle:"):
            await self.handle_toggle_callback(message, data)
        elif data.startswith("delete:"):
            await self.handle_delete_callback(message, data)
        elif data.startswith("confirm_delete:"):
            await self.handle_confirm_delete_callback(message, data)
        elif data == "send_all":
            await self.edit_callback_message(message, "Отправляю все включенные события...")
            await self.send_events_manually([event for event in self.events if event.enabled])
        elif data.startswith("send:"):
            await self.handle_send_callback(message, data)
        elif data.startswith("test:"):
            await self.handle_test_callback(message, data)

    async def edit_callback_message(
        self,
        message: dict[str, Any],
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        chat = message.get("chat")
        message_id = message.get("message_id")
        if isinstance(chat, dict) and isinstance(message_id, int):
            await self.notifier.edit_message_text(str(chat.get("id")), message_id, text, reply_markup=reply_markup)

    async def handle_toggle_callback(self, message: dict[str, Any], data: str) -> None:
        index = self.callback_index(data)
        event = self.event_at(index)
        if index is None or event is None:
            await self.edit_callback_message(message, "Событие не найдено. Откройте меню заново.", self.schedule_menu_keyboard())
            return
        self.event_store.set_enabled(index, not event.enabled)
        await self.reload_events()
        updated = self.events[index]
        await self.edit_callback_message(
            message,
            f"Событие обновлено:\n{self.format_event_line(updated)}",
            self.event_keyboard("toggle"),
        )

    async def handle_delete_callback(self, message: dict[str, Any], data: str) -> None:
        index = self.callback_index(data)
        event = self.event_at(index)
        if index is None or event is None:
            await self.edit_callback_message(message, "Событие не найдено. Откройте меню заново.", self.schedule_menu_keyboard())
            return
        await self.edit_callback_message(
            message,
            f"Удалить событие?\n\n{self.format_event_line(event)}",
            {
                "inline_keyboard": [
                    [{"text": "Удалить", "callback_data": f"confirm_delete:{index}"}],
                    [{"text": "Назад", "callback_data": "menu:delete"}],
                ]
            },
        )

    async def handle_confirm_delete_callback(self, message: dict[str, Any], data: str) -> None:
        index = self.callback_index(data)
        event = self.event_at(index)
        if index is None or event is None:
            await self.edit_callback_message(message, "Событие не найдено. Откройте меню заново.", self.schedule_menu_keyboard())
            return
        event_name = event.name
        self.event_store.delete_event(index)
        await self.reload_events()
        await self.edit_callback_message(
            message,
            f"Событие удалено:\n{event_name}",
            self.schedule_menu_keyboard(),
        )

    async def handle_send_callback(self, message: dict[str, Any], data: str) -> None:
        index = self.callback_index(data)
        event = self.event_at(index)
        if index is None or event is None:
            await self.edit_callback_message(message, "Событие не найдено. Откройте меню заново.", self.schedule_menu_keyboard())
            return
        await self.edit_callback_message(message, f"Отправляю событие:\n{event.name}")
        await self.send_events_manually([event])

    async def handle_test_callback(self, message: dict[str, Any], data: str) -> None:
        index = self.callback_index(data)
        event = self.event_at(index)
        if index is None or event is None:
            await self.edit_callback_message(message, "Событие не найдено. Откройте меню заново.", self.schedule_menu_keyboard())
            return
        await self.edit_callback_message(message, f"Отправляю тестовое событие:\n{event.name}")
        test_event = self.with_role_override(event, TEST_ROLE_ID)
        sent = await self.send_scheduled_message(test_event, dedupe=False, notify_success=False)
        result = "Тестовое уведомление отправлено" if sent else "Тестовое уведомление не отправилось"
        await self.notifier.send(f"{result}\n\nСобытие: {event.name}\nТестовая роль: {TEST_ROLE_ID}")

    async def send_events_manually(self, events: list[ScheduledEvent]) -> None:
        await self.notifier.send(
            "Запускаю ручную отправку...\n\n"
            f"Событий к отправке: {len(events)}"
        )

        sent_count = 0
        for event in events:
            if await self.send_scheduled_message(event, dedupe=False, notify_success=False):
                sent_count += 1

        await self.notifier.send(
            "Ручная отправка завершена\n\n"
            f"Отправлено: {sent_count} из {len(events)}"
        )

    @staticmethod
    def callback_index(data: str) -> int | None:
        try:
            return int(data.split(":", 1)[1])
        except (IndexError, ValueError):
            return None

    def event_at(self, index: int | None) -> ScheduledEvent | None:
        if index is None or index < 0 or index >= len(self.events):
            return None
        return self.events[index]

    def schedule_menu_keyboard(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": "Список событий", "callback_data": "menu:list"}],
                [{"text": "Добавить событие", "callback_data": "menu:add"}],
                [{"text": "Включить/отключить", "callback_data": "menu:toggle"}],
                [{"text": "Удалить событие", "callback_data": "menu:delete"}],
                [{"text": "Перечитать events.json", "callback_data": "menu:reload"}],
            ]
        }

    def event_keyboard(self, action: str) -> dict[str, Any]:
        keyboard = []
        if action == "send":
            keyboard.append([{"text": "Отправить все включенные", "callback_data": "send_all"}])
        for index, event in enumerate(self.events):
            keyboard.append([{"text": self.button_label(event), "callback_data": f"{action}:{index}"}])
        keyboard.append([{"text": "Назад", "callback_data": "menu:main"}])
        return {"inline_keyboard": keyboard}

    @staticmethod
    def button_label(event: ScheduledEvent) -> str:
        status = "Вкл" if event.enabled else "Выкл"
        clean_text = event.text.split(">", 1)[1].strip() if event.text.startswith("<@&") and ">" in event.text else event.text
        return f"{status} | {event.cron} | {clean_text[:28]}"

    def find_events(self, event_name: str) -> list[ScheduledEvent]:
        if not event_name:
            return [event for event in self.events if event.enabled]
        return [event for event in self.events if event.name == event_name]

    @staticmethod
    def with_role_override(event: ScheduledEvent, role_id: int) -> ScheduledEvent:
        text = event.text
        parts = text.split(">", 1)
        if len(parts) == 2 and parts[0].startswith("<@&"):
            text = f"<@&{role_id}>{parts[1]}"
        else:
            text = f"<@&{role_id}> {text}"

        return ScheduledEvent(
            name=f"test_{event.name}",
            channel_id=event.channel_id,
            text=text,
            reaction=event.reaction,
            cron=event.cron,
            allowed_mentions=event.allowed_mentions,
            enabled=event.enabled,
        )

    def format_event_names(self) -> str:
        return "\n".join(f"• {event.name}" for event in self.events)

    def format_event_line(self, event: ScheduledEvent) -> str:
        status = "включено" if event.enabled else "отключено"
        return f"{event.name}\nСтатус: {status}\nCron: {event.cron}\nТекст: {event.text}"

    def format_events_list(self) -> str:
        lines = ["События:"]
        for event in self.events:
            status = "вкл" if event.enabled else "выкл"
            lines.append(f"• {event.name} [{status}] — {event.cron}")
        return "\n".join(lines)

    def get_next_runs(self) -> list[tuple[ScheduledEvent, datetime]]:
        next_runs: list[tuple[ScheduledEvent, datetime]] = []
        for event in self.events:
            if not event.enabled:
                continue
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
            if event.enabled and event.name not in logged_events:
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
            "Бот запущен\n\n"
            f"Аккаунт: {user}\n"
            f"Событий в расписании: {len(self.events)}\n\n"
            f"{self.format_next_events(limit=5)}\n\n"
            "Telegram-команды:\n"
            "/status\n"
            "/schedule\n"
            "/next_events\n"
            "/send_now\n"
            "/test"
        )

    async def send_scheduled_message(
        self,
        event: ScheduledEvent,
        dedupe: bool = True,
        notify_success: bool = True,
    ) -> bool:
        if dedupe and self.last_sent_store.recently_sent(event.name):
            logger.warning("Skipped duplicate scheduled send for %s", event.name)
            await self.notifier.send(f"Пропущен дубль отправки по расписанию\n\nСобытие: {event.name}")
            return False

        try:
            channel = self.get_channel(event.channel_id)
            if channel is None:
                channel = await self.fetch_channel(event.channel_id)

            send = getattr(channel, "send", None)
            if not callable(send):
                logger.error("Channel %s for event %s is not messageable", event.channel_id, event.name)
                await self.notifier.send(
                    "Ошибка отправки сообщения\n\n"
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
                    "Сообщение отправлено, но реакция не поставилась\n\n"
                    f"Событие: {event.name}\n"
                    f"Канал: {event.channel_id}\n"
                    f"Реакция: {event.reaction}\n\n"
                    "Ошибка: нет прав Discord на добавление реакции"
                )
                return False
            except discord.HTTPException as exc:
                logger.exception("Discord API error while adding reaction for event %s", event.name)
                await self.notifier.send(
                    "Сообщение отправлено, но реакция не поставилась\n\n"
                    f"Событие: {event.name}\n"
                    f"Канал: {event.channel_id}\n"
                    f"Реакция: {event.reaction}\n\n"
                    f"Ошибка Discord API: {exc}"
                )
                return False

            if dedupe:
                self.last_sent_store.mark_sent(event.name)
            if notify_success:
                await self.notifier.send(
                    "Сообщение по расписанию отправлено\n\n"
                    f"Событие: {event.name}\n"
                    f"Канал: {event.channel_id}"
                )
            logger.info("Sent scheduled message for event %s", event.name)
            return True
        except discord.Forbidden as exc:
            logger.exception("Missing Discord permissions for event %s", event.name)
            await self.notifier.send(
                "Ошибка отправки сообщения\n\n"
                f"Событие: {event.name}\n"
                f"Канал: {event.channel_id}\n"
                f"Текст: {event.text}\n\n"
                f"Ошибка: нет прав Discord на отправку сообщения\n{exc}"
            )
        except discord.HTTPException as exc:
            logger.exception("Discord API error while sending event %s", event.name)
            await self.notifier.send(
                "Ошибка отправки сообщения\n\n"
                f"Событие: {event.name}\n"
                f"Канал: {event.channel_id}\n"
                f"Текст: {event.text}\n\n"
                f"Ошибка Discord API: {exc}"
            )
        except Exception as exc:
            logger.exception("Unexpected error while sending event %s", event.name)
            await self.notifier.send(
                "Неожиданная ошибка отправки сообщения\n\n"
                f"Событие: {event.name}\n"
                f"Канал: {event.channel_id}\n"
                f"Текст: {event.text}\n\n"
                f"Ошибка: {type(exc).__name__}: {exc}"
            )
        return False

    async def discord_send_now_command(self, interaction: discord.Interaction, event_name: str = "") -> None:
        if not self.can_use_manual_command(interaction):
            await interaction.response.send_message(
                "Эта команда доступна только администраторам или пользователям с правом Manage Server.",
                ephemeral=True,
            )
            return

        selected_events = self.find_events(event_name)
        if not selected_events:
            await interaction.response.send_message(
                f"Событие не найдено. Доступные имена: {', '.join(event.name for event in self.events)}",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        sent_count = 0
        for event in selected_events:
            if await self.send_scheduled_message(event, dedupe=False, notify_success=False):
                sent_count += 1

        await interaction.followup.send(
            f"Готово: отправлено {sent_count} из {len(selected_events)} сообщений.",
            ephemeral=True,
        )

    async def discord_next_events_command(self, interaction: discord.Interaction, limit: int = 5) -> None:
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
