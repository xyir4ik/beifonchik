import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from constants import TEST_ROLE_ID
from discord_publisher import DiscordPublisher
from formatting import EventFormatter
from models import ScheduledEvent
from stores import EventStore
from telegram_client import TelegramNotifier


logger = logging.getLogger("scheduled-discord-bot")


class ScheduleService:
    def __init__(
        self,
        events: list[ScheduledEvent],
        event_store: EventStore,
        scheduler: AsyncIOScheduler,
        timezone: ZoneInfo,
        publisher: DiscordPublisher,
        notifier: TelegramNotifier,
        formatter: EventFormatter,
    ) -> None:
        self.events = events
        self.event_store = event_store
        self.scheduler = scheduler
        self.timezone = timezone
        self.publisher = publisher
        self.notifier = notifier
        self.formatter = formatter

    def start(self) -> None:
        self.rebuild_schedule()
        self.scheduler.start()

    def rebuild_schedule(self) -> None:
        self.scheduler.remove_all_jobs()
        for event in self.events:
            if not event.enabled:
                logger.info("Skipped disabled event %s", event.name)
                continue

            trigger = CronTrigger.from_crontab(event.cron, timezone=self.scheduler.timezone)
            self.scheduler.add_job(
                self.publisher.send_scheduled_message,
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

    def get_next_runs(self) -> list[tuple[ScheduledEvent, datetime]]:
        next_runs: list[tuple[ScheduledEvent, datetime]] = []
        for event in self.events:
            if not event.enabled:
                continue
            job = self.scheduler.get_job(event.name)
            next_run_time = getattr(job, "next_run_time", None)
            if job is None or next_run_time is None:
                continue
            next_runs.append((event, next_run_time.astimezone(self.timezone)))

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
        return self.formatter.format_next_events(self.get_next_runs(), limit=limit)

    def find_events(self, event_name: str) -> list[ScheduledEvent]:
        if not event_name:
            return [event for event in self.events if event.enabled]
        return [event for event in self.events if event.name == event_name]

    def event_at(self, index: int | None) -> ScheduledEvent | None:
        if index is None or index < 0 or index >= len(self.events):
            return None
        return self.events[index]

    def set_enabled(self, index: int, enabled: bool) -> None:
        self.event_store.set_enabled(index, enabled)

    def delete_event(self, index: int) -> None:
        self.event_store.delete_event(index)

    def add_event(self, name: str, cron: str, text: str) -> None:
        self.event_store.add_event(name=name, cron=cron, text=text)

    def update_event_field(self, index: int, field: str, value: str) -> None:
        self.event_store.update_event_field(index=index, field=field, value=value)

    def toggle_enabled(self, index: int) -> None:
        event = self.event_at(index)
        if event is None:
            raise IndexError("Событие не найдено")
        self.event_store.set_enabled(index, not event.enabled)

    async def send_events_manually(self, events: list[ScheduledEvent]) -> None:
        await self.notifier.send(
            "Запускаю ручную отправку...\n\n"
            f"Событий к отправке: {len(events)}"
        )

        sent_count = 0
        for event in events:
            if await self.publisher.send_scheduled_message(event, dedupe=False, notify_success=False):
                sent_count += 1

        await self.notifier.send(
            "Ручная отправка завершена\n\n"
            f"Отправлено: {sent_count} из {len(events)}"
        )

    async def send_test_event(self, event: ScheduledEvent) -> bool:
        test_event = self.with_role_override(event, TEST_ROLE_ID)
        return await self.publisher.send_scheduled_message(test_event, dedupe=False, notify_success=False)

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
