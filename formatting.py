from datetime import datetime

from models import ScheduledEvent


class EventFormatter:
    def event_names(self, events: list[ScheduledEvent]) -> str:
        return "\n".join(f"• {self.event_display(event)}" for event in events)

    def event_line(self, event: ScheduledEvent) -> str:
        status = "включено" if event.enabled else "отключено"
        return (
            f"{self.event_display(event)}\n"
            f"Статус: {status}\n"
            f"Имя: {event.name}\n"
            f"Cron: {event.cron}"
        )

    def events_list(self, events: list[ScheduledEvent]) -> str:
        lines = ["События:"]
        for event in events:
            status = "вкл" if event.enabled else "выкл"
            lines.append(f"• {self.event_display(event)} [{status}]")
        return "\n".join(lines)

    def event_display(self, event: ScheduledEvent) -> str:
        return f"{self.human_schedule(event)} — {self.clean_event_text(event)}"

    def button_label(self, event: ScheduledEvent) -> str:
        status = "Вкл" if event.enabled else "Выкл"
        return f"{status} | {self.human_schedule(event)} | {self.clean_event_text(event)[:28]}"

    def format_next_events(
        self,
        next_runs: list[tuple[ScheduledEvent, datetime]],
        limit: int = 5,
    ) -> str:
        items = next_runs[:limit]
        if not items:
            return "Ближайшие события не найдены."

        lines = ["Ближайшие события:"]
        for event, _ in items:
            lines.append(f"• {self.event_display(event)}")
        return "\n".join(lines)

    @staticmethod
    def clean_event_text(event: ScheduledEvent) -> str:
        if event.text.startswith("<@&") and ">" in event.text:
            return event.text.split(">", 1)[1].strip()
        return event.text

    @staticmethod
    def human_schedule(event: ScheduledEvent) -> str:
        parts = event.cron.split()
        if len(parts) != 5:
            return event.cron

        minute, hour, _, _, weekday = parts
        day_names = {
            "mon": "Пн",
            "tue": "Вт",
            "wed": "Ср",
            "thu": "Чт",
            "fri": "Пт",
            "sat": "Сб",
            "sun": "Вс",
        }
        day = day_names.get(weekday.lower(), weekday)

        try:
            time = f"{int(hour):02d}:{int(minute):02d}"
        except ValueError:
            time = f"{hour}:{minute}"

        return f"{day} {time}"
