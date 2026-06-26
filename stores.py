import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from apscheduler.triggers.cron import CronTrigger

from constants import DUPLICATE_WINDOW_SECONDS
from models import ScheduledEvent


logger = logging.getLogger("scheduled-discord-bot")


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

        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.data, ensure_ascii=False, indent=2) + "\n"
        tmp_path = self.path.with_name(f"{self.path.name}.tmp")
        tmp_path.write_text(payload, encoding="utf-8")
        tmp_path.replace(self.path)

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
                raise ValueError("Event is missing channel_id, reaction, or another required default") from exc

            if event.name in event_names:
                raise ValueError(f"Event name must be unique: {event.name}")

            self.validate_cron(event.cron)
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
        self.validate_cron(cron)

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

    def update_event_field(self, index: int, field: str, value: str) -> None:
        if field not in {"name", "text", "cron"}:
            raise ValueError("Можно изменить только название, текст или расписание")

        events = self.events_raw()
        if index < 0 or index >= len(events):
            raise IndexError("Событие не найдено")

        event = events[index]
        if not isinstance(event, dict):
            raise ValueError("Событие повреждено")

        value = value.strip()
        if not value:
            raise ValueError("Значение не может быть пустым")

        if field == "name":
            if any(
                item_index != index
                and isinstance(item, dict)
                and str(item.get("name")) == value
                for item_index, item in enumerate(events)
            ):
                raise ValueError(f"Событие с именем {value} уже существует")
        elif field == "cron":
            self.validate_cron(value)

        event[field] = value
        self.parse_events(self.data)
        self.save()

    @staticmethod
    def validate_cron(cron: str) -> None:
        CronTrigger.from_crontab(cron)
        parts = cron.split()
        if len(parts) != 5:
            return

        weekday = parts[4].lower()
        tokens = weekday.replace(",", " ").replace("-", " ").replace("/", " ").split()
        if any(token.isdigit() for token in tokens):
            raise ValueError("Используйте дни недели словами: mon, tue, wed, thu, fri, sat, sun")


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
            payload = json.dumps(self.data, indent=2)
            tmp_path = self.path.with_name(f"{self.path.name}.tmp")
            tmp_path.write_text(payload, encoding="utf-8")
            tmp_path.replace(self.path)
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
