import logging

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from discord import app_commands

from app_config import data_file
from discord_publisher import DiscordPublisher
from formatting import EventFormatter
from models import BotConfig
from schedule_service import ScheduleService
from stores import EventStore, LastSentStore
from telegram_admin import TelegramAdminBot
from telegram_client import TelegramNotifier


logger = logging.getLogger("scheduled-discord-bot")


class ScheduledDiscordBot(discord.Client):
    def __init__(self, config: BotConfig, notifier: TelegramNotifier) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)

        event_store = config.event_store
        if not isinstance(event_store, EventStore):
            raise TypeError("config.event_store must be EventStore")

        self.guild_id = config.guild_id
        self.enable_discord_commands = config.enable_discord_commands
        self.notifier = notifier
        self.formatter = EventFormatter()
        self.scheduler = AsyncIOScheduler(timezone=config.timezone)
        self.publisher = DiscordPublisher(
            client=self,
            notifier=self.notifier,
            formatter=self.formatter,
            last_sent_store=LastSentStore(data_file("last_sent.json")),
        )
        self.schedule = ScheduleService(
            events=config.events,
            event_store=event_store,
            scheduler=self.scheduler,
            timezone=config.timezone,
            publisher=self.publisher,
            notifier=self.notifier,
            formatter=self.formatter,
        )
        self.telegram_admin = TelegramAdminBot(
            notifier=self.notifier,
            schedule=self.schedule,
            formatter=self.formatter,
            discord_user_label=lambda: f"{self.user}",
        )

        self.tree: app_commands.CommandTree | None = None
        self._scheduled = False
        self._commands_synced = False

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

        self.schedule.start()
        self._scheduled = True
        self.schedule.log_next_runs()
        await self.notify_started()
        self.telegram_admin.start()

    async def close(self) -> None:
        self.telegram_admin.cancel()
        await super().close()

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

    async def notify_started(self) -> None:
        user = f"{self.user}" if self.user else "неизвестно"
        await self.notifier.send(
            "Бот запущен\n\n"
            f"Аккаунт: {user}\n"
            f"Событий в расписании: {len(self.schedule.events)}\n\n"
            f"{self.schedule.format_next_events(limit=5)}\n\n"
            "Telegram-команды:\n"
            "/status\n"
            "/schedule\n"
            "/next_events\n"
            "/send_now\n"
            "/test"
        )

    async def discord_send_now_command(self, interaction: discord.Interaction, event_name: str = "") -> None:
        if not self.can_use_manual_command(interaction):
            await interaction.response.send_message(
                "Эта команда доступна только администраторам или пользователям с правом Manage Server.",
                ephemeral=True,
            )
            return

        selected_events = self.schedule.find_events(event_name)
        if not selected_events:
            await interaction.response.send_message(
                f"Событие не найдено. Доступные события:\n{self.formatter.event_names(self.schedule.events)}",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        sent_count = 0
        for event in selected_events:
            if await self.publisher.send_scheduled_message(event, dedupe=False, notify_success=False):
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
        await interaction.response.send_message(self.schedule.format_next_events(limit=limit), ephemeral=True)

    @staticmethod
    def can_use_manual_command(interaction: discord.Interaction) -> bool:
        permissions = getattr(interaction.user, "guild_permissions", None)
        return bool(permissions and (permissions.administrator or permissions.manage_guild))
