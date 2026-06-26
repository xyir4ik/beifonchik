import logging

import discord

from formatting import EventFormatter
from models import ScheduledEvent
from stores import LastSentStore
from telegram_client import TelegramNotifier


logger = logging.getLogger("scheduled-discord-bot")


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


class DiscordPublisher:
    def __init__(
        self,
        client: discord.Client,
        notifier: TelegramNotifier,
        formatter: EventFormatter,
        last_sent_store: LastSentStore,
    ) -> None:
        self.client = client
        self.notifier = notifier
        self.formatter = formatter
        self.last_sent_store = last_sent_store

    async def send_scheduled_message(
        self,
        event: ScheduledEvent,
        dedupe: bool = True,
        notify_success: bool = True,
    ) -> bool:
        if dedupe and self.last_sent_store.recently_sent(event.name):
            logger.warning("Skipped duplicate scheduled send for %s", event.name)
            await self.notifier.send(
                "Пропущен дубль отправки по расписанию\n\n"
                f"Событие: {self.formatter.event_display(event)}"
            )
            return False

        try:
            channel = self.client.get_channel(event.channel_id)
            if channel is None:
                channel = await self.client.fetch_channel(event.channel_id)

            send = getattr(channel, "send", None)
            if not callable(send):
                logger.error("Channel %s for event %s is not messageable", event.channel_id, event.name)
                await self.notifier.send(
                    "Ошибка отправки сообщения\n\n"
                    f"Событие: {self.formatter.event_display(event)}\n"
                    f"Канал: {event.channel_id}\n"
                    f"Текст: {event.text}\n\n"
                    "Ошибка: канал не поддерживает отправку сообщений"
                )
                return False

            message = await send(
                event.text,
                allowed_mentions=build_allowed_mentions(event.allowed_mentions),
            )

            if dedupe:
                self.last_sent_store.mark_sent(event.name)

            reaction_ok = await self.add_reaction(message, event)
            if notify_success and reaction_ok:
                await self.notifier.send(
                    "Сообщение по расписанию отправлено\n\n"
                    f"Событие: {self.formatter.event_display(event)}\n"
                    f"Канал: {event.channel_id}"
                )

            logger.info("Sent scheduled message for event %s", event.name)
            return True
        except discord.Forbidden as exc:
            logger.exception("Missing Discord permissions for event %s", event.name)
            await self.notifier.send(
                "Ошибка отправки сообщения\n\n"
                f"Событие: {self.formatter.event_display(event)}\n"
                f"Канал: {event.channel_id}\n"
                f"Текст: {event.text}\n\n"
                f"Ошибка: нет прав Discord на отправку сообщения\n{exc}"
            )
        except discord.HTTPException as exc:
            logger.exception("Discord API error while sending event %s", event.name)
            await self.notifier.send(
                "Ошибка отправки сообщения\n\n"
                f"Событие: {self.formatter.event_display(event)}\n"
                f"Канал: {event.channel_id}\n"
                f"Текст: {event.text}\n\n"
                f"Ошибка Discord API: {exc}"
            )
        except Exception as exc:
            logger.exception("Unexpected error while sending event %s", event.name)
            await self.notifier.send(
                "Неожиданная ошибка отправки сообщения\n\n"
                f"Событие: {self.formatter.event_display(event)}\n"
                f"Канал: {event.channel_id}\n"
                f"Текст: {event.text}\n\n"
                f"Ошибка: {type(exc).__name__}: {exc}"
            )
        return False

    async def add_reaction(self, message: discord.Message, event: ScheduledEvent) -> bool:
        try:
            await message.add_reaction(event.reaction)
            return True
        except discord.Forbidden:
            logger.exception("Missing Discord permissions to add reaction for event %s", event.name)
            await self.notifier.send(
                "Сообщение отправлено, но реакция не поставилась\n\n"
                f"Событие: {self.formatter.event_display(event)}\n"
                f"Канал: {event.channel_id}\n"
                f"Реакция: {event.reaction}\n\n"
                "Ошибка: нет прав Discord на добавление реакции"
            )
            return False
        except discord.HTTPException as exc:
            logger.exception("Discord API error while adding reaction for event %s", event.name)
            await self.notifier.send(
                "Сообщение отправлено, но реакция не поставилась\n\n"
                f"Событие: {self.formatter.event_display(event)}\n"
                f"Канал: {event.channel_id}\n"
                f"Реакция: {event.reaction}\n\n"
                f"Ошибка Discord API: {exc}"
            )
            return False
