import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from constants import MANUAL_SEND_COOLDOWN_SECONDS, TEST_ROLE_ID
from formatting import EventFormatter
from models import ScheduledEvent
from schedule_service import ScheduleService
from telegram_client import TelegramNotifier


logger = logging.getLogger("scheduled-discord-bot")


class TelegramAdminBot:
    def __init__(
        self,
        notifier: TelegramNotifier,
        schedule: ScheduleService,
        formatter: EventFormatter,
        discord_user_label: Callable[[], str] | None = None,
    ) -> None:
        self.notifier = notifier
        self.schedule = schedule
        self.formatter = formatter
        self.discord_user_label = discord_user_label
        self.pending_actions: dict[str, str] = {}
        self.pending_event_drafts: dict[str, dict[str, str]] = {}
        self.last_manual_send_at: dict[str, float] = {}
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if not self.notifier.enabled:
            logger.info(
                "Telegram commands are disabled because TG_BOT_TOKEN/TELEGRAM_BOT_TOKEN or TG_CHAT_ID/TELEGRAM_CHAT_ID is not set"
            )
            return
        if self.task and not self.task.done():
            return

        self.task = asyncio.create_task(self.polling_loop())
        logger.info("Telegram commands polling started")

    def cancel(self) -> None:
        if self.task:
            self.task.cancel()

    async def polling_loop(self) -> None:
        offset = await self.get_initial_offset()

        while True:
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
                await self.handle_update(update)

            await asyncio.sleep(0.2)

    async def get_initial_offset(self) -> int | None:
        updates = await self.notifier.get_updates(offset=None, timeout_seconds=1)
        if updates is None:
            logger.error("Telegram getUpdates failed during startup. Check TELEGRAM_BOT_TOKEN or TG_BOT_TOKEN.")
            return None

        update_ids = [update.get("update_id") for update in updates if isinstance(update.get("update_id"), int)]
        if not update_ids:
            return None
        return max(update_ids) + 1

    async def handle_update(self, update: dict[str, Any]) -> None:
        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            await self.handle_callback(callback_query)
            return

        message = update.get("message")
        if not isinstance(message, dict):
            return

        chat_id = self.message_chat_id(message)
        if chat_id is None or not self.is_authorized(message):
            logger.warning("Ignoring Telegram command from unauthorized chat_id=%s", chat_id)
            return

        text = str(message.get("text") or "").strip()
        if not text:
            return

        pending_action = self.pending_actions.get(chat_id)
        if pending_action and not text.startswith("/"):
            if pending_action.startswith("add_event:"):
                await self.handle_add_event_input(chat_id, text)
                return
            if pending_action.startswith("edit_event:"):
                await self.handle_edit_event_input(chat_id, text)
                return
            return

        if not text.startswith("/"):
            return

        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        argument = argument.strip()

        if command in {"/start", "/help"}:
            await self.notifier.send(self.help_text())
        elif command == "/status":
            await self.handle_status()
        elif command == "/reload":
            await self.handle_reload()
        elif command == "/schedule":
            await self.handle_schedule_menu()
        elif command == "/next_events":
            await self.handle_next_events(argument)
        elif command == "/send_now":
            await self.handle_send_now(argument)
        elif command == "/test":
            await self.handle_test()
        else:
            await self.notifier.send("Неизвестная команда. Напишите /help.")

    def help_text(self) -> str:
        return (
            "Команды админа:\n\n"
            "/status - статус бота\n"
            "/schedule - управление расписанием кнопками\n"
            "/reload - перечитать events.json\n"
            "/next_events - показать 5 ближайших событий\n"
            "/next_events 10 - показать до 10 событий\n"
            "/send_now - выбрать ручную отправку кнопкой\n"
            "/test - выбрать тестовое событие кнопкой"
        )

    async def handle_status(self) -> None:
        active_count = sum(1 for event in self.schedule.events if event.enabled)
        disabled_count = len(self.schedule.events) - active_count
        discord_line = ""
        if self.discord_user_label is not None:
            discord_line = f"Discord: онлайн как {self.discord_user_label()}\n"
        await self.notifier.send(
            "Статус бота\n\n"
            f"{discord_line}"
            f"Часовой пояс: {self.schedule.timezone.key}\n"
            f"Событий всего: {len(self.schedule.events)}\n"
            f"Включено: {active_count}\n"
            f"Отключено: {disabled_count}\n"
            f"Telegram-команды: {'включены' if self.notifier.enabled else 'выключены'}\n\n"
            f"{self.schedule.format_next_events(limit=5)}"
        )

    async def handle_reload(self) -> None:
        try:
            await self.schedule.reload_events()
            await self.notifier.send(
                "Расписание перечитано\n\n"
                f"Событий: {len(self.schedule.events)}\n\n"
                f"{self.schedule.format_next_events(limit=5)}"
            )
        except Exception as exc:
            logger.exception("Could not reload schedule")
            await self.notifier.send(f"Не удалось перечитать расписание: {type(exc).__name__}: {exc}")

    async def handle_schedule_menu(self) -> None:
        await self.notifier.send(
            "Управление расписанием",
            reply_markup=self.schedule_menu_keyboard(),
        )

    async def handle_test(self) -> None:
        await self.notifier.send(
            "Выберите тестовое уведомление для отправки в Discord.\n\n"
            f"В тесте будет тегаться роль: {TEST_ROLE_ID}",
            reply_markup=self.event_keyboard("test"),
        )

    async def handle_send_now(self, event_name: str) -> None:
        if not event_name:
            await self.notifier.send(
                "Выберите событие для ручной отправки:",
                reply_markup=self.event_keyboard("send"),
            )
            return

        selected_events = self.schedule.find_events(event_name)
        if not selected_events:
            await self.notifier.send(
                "Событие не найдено.\n\n"
                f"Доступные события:\n{self.formatter.event_names(self.schedule.events)}"
            )
            return

        event = selected_events[0]
        index = self.schedule.events.index(event)
        await self.notifier.send(
            "Подтвердите ручную отправку.\n\n"
            f"{self.formatter.event_display(event)}",
            reply_markup=self.confirm_keyboard(f"confirm_send:{index}", "send"),
        )

    async def handle_next_events(self, argument: str) -> None:
        limit = 5
        if argument:
            try:
                limit = int(argument)
            except ValueError:
                await self.notifier.send("Лимит должен быть числом, например: /next_events 10")
                return

        limit = max(1, min(limit, 10))
        await self.notifier.send(self.schedule.format_next_events(limit=limit))

    async def handle_add_event_input(self, chat_id: str, text: str) -> None:
        step = self.pending_actions.get(chat_id)
        draft = self.pending_event_drafts.setdefault(chat_id, {})

        if step == "add_event:name":
            draft["name"] = text
            self.pending_actions[chat_id] = "add_event:text"
            await self.notifier.send(
                "Введите текст сообщения.\n\n"
                "Например:\n"
                "реаки 25x25 общее взх\n\n"
                "Роль добавится автоматически.",
                reply_markup=self.back_keyboard(),
            )
            return

        if step == "add_event:text":
            draft["text"] = text
            self.pending_actions[chat_id] = "add_event:cron"
            await self.notifier.send(
                "Введите расписание в формате cron.\n\n"
                "Примеры:\n"
                "10 18 * * sat — каждую субботу в 18:10\n"
                "20 18 * * mon — каждый понедельник в 18:20\n\n"
                "Дни недели: mon, tue, wed, thu, fri, sat, sun",
                reply_markup=self.back_keyboard(),
            )
            return

        if step == "add_event:cron":
            draft["cron"] = text
            try:
                self.schedule.add_event(name=draft["name"], cron=draft["cron"], text=draft["text"])
                await self.schedule.reload_events()
                added_event = next(event for event in self.schedule.events if event.name == draft["name"])
                self.clear_pending(chat_id)
                await self.notifier.send(
                    "Событие добавлено и включено:\n"
                    f"{self.formatter.event_display(added_event)}",
                    reply_markup=self.schedule_menu_keyboard(),
                )
            except Exception as exc:
                logger.exception("Could not add event")
                await self.notifier.send(
                    "Не удалось добавить событие.\n\n"
                    f"Ошибка: {type(exc).__name__}: {exc}\n\n"
                    "Введите cron еще раз или нажмите Назад.",
                    reply_markup=self.back_keyboard(),
                )

    async def handle_edit_event_input(self, chat_id: str, text: str) -> None:
        step = self.pending_actions.get(chat_id)
        if not step:
            return

        _, raw_index, field = step.split(":", 2)
        try:
            index = int(raw_index)
        except ValueError:
            self.clear_pending(chat_id)
            await self.notifier.send("Событие не найдено. Откройте меню заново.", reply_markup=self.schedule_menu_keyboard())
            return

        event = self.schedule.event_at(index)
        if event is None:
            self.clear_pending(chat_id)
            await self.notifier.send("Событие не найдено. Откройте меню заново.", reply_markup=self.schedule_menu_keyboard())
            return

        try:
            self.schedule.update_event_field(index=index, field=field, value=text)
            await self.schedule.reload_events()
            updated = self.schedule.events[index]
            self.clear_pending(chat_id)
            await self.notifier.send(
                "Событие изменено:\n"
                f"{self.formatter.event_line(updated)}",
                reply_markup=self.edit_field_keyboard(index),
            )
        except Exception as exc:
            logger.exception("Could not edit event")
            await self.notifier.send(
                "Не удалось изменить событие.\n\n"
                f"Ошибка: {type(exc).__name__}: {exc}\n\n"
                "Введите значение еще раз или нажмите Назад.",
                reply_markup=self.back_to_edit_keyboard(index),
            )

    async def handle_callback(self, callback_query: dict[str, Any]) -> None:
        callback_query_id = str(callback_query.get("id") or "")
        data = str(callback_query.get("data") or "")
        message = callback_query.get("message")

        if not isinstance(message, dict):
            return

        if not self.is_authorized(message, callback_query=callback_query):
            chat_id = self.message_chat_id(message)
            logger.warning("Ignoring Telegram callback from unauthorized chat_id=%s", chat_id)
            if callback_query_id:
                await self.notifier.answer_callback_query(callback_query_id, "Нет доступа")
            return

        chat_id = self.message_chat_id(message)
        if chat_id is None:
            return

        if callback_query_id:
            await self.notifier.answer_callback_query(callback_query_id, "Принято")

        if data == "menu:main":
            self.clear_pending(chat_id)
            await self.edit_callback_message(message, "Управление расписанием", self.schedule_menu_keyboard())
        elif data == "menu:list":
            await self.edit_callback_message(message, self.formatter.events_list(self.schedule.events), self.back_keyboard())
        elif data == "menu:send":
            await self.edit_callback_message(message, "Выберите событие для ручной отправки:", self.event_keyboard("send"))
        elif data == "menu:test":
            await self.edit_callback_message(
                message,
                "Выберите тестовое уведомление для отправки в Discord.\n\n"
                f"В тесте будет тегаться роль: {TEST_ROLE_ID}",
                self.event_keyboard("test"),
            )
        elif data == "menu:toggle":
            await self.edit_callback_message(message, "Выберите событие для включения/отключения:", self.event_keyboard("toggle"))
        elif data == "menu:edit":
            await self.edit_callback_message(message, "Выберите событие для изменения:", self.event_keyboard("edit"))
        elif data == "menu:delete":
            await self.edit_callback_message(message, "Выберите событие для удаления:", self.event_keyboard("delete"))
        elif data == "menu:add":
            self.pending_actions[chat_id] = "add_event:name"
            self.pending_event_drafts[chat_id] = {}
            await self.edit_callback_message(
                message,
                "Добавление события\n\n"
                "Введите название события.\n\n"
                "Например:\n"
                "25x25 общее понедельник",
                self.back_keyboard(),
            )
        elif data == "menu:reload":
            await self.handle_reload()
        elif data.startswith("toggle:"):
            await self.handle_toggle_callback(message, data)
        elif data.startswith("edit_field:"):
            await self.handle_edit_field_callback(message, chat_id, data)
        elif data.startswith("edit_toggle:"):
            await self.handle_edit_toggle_callback(message, data)
        elif data.startswith("edit:"):
            await self.handle_edit_callback(message, data)
        elif data.startswith("delete:"):
            await self.handle_delete_callback(message, data)
        elif data.startswith("confirm_delete:"):
            await self.handle_confirm_delete_callback(message, data)
        elif data == "send_all":
            await self.handle_send_all_callback(message)
        elif data.startswith("confirm_send:"):
            await self.handle_confirm_send_callback(message, chat_id, data)
        elif data.startswith("confirm_test:"):
            await self.handle_confirm_test_callback(message, chat_id, data)
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
        event = self.schedule.event_at(index)
        if index is None or event is None:
            await self.edit_callback_message(message, "Событие не найдено. Откройте меню заново.", self.schedule_menu_keyboard())
            return
        self.schedule.set_enabled(index, not event.enabled)
        await self.schedule.reload_events()
        updated = self.schedule.events[index]
        await self.edit_callback_message(
            message,
            f"Событие обновлено:\n{self.formatter.event_line(updated)}",
            self.event_keyboard("toggle"),
        )

    async def handle_edit_callback(self, message: dict[str, Any], data: str) -> None:
        index = self.callback_index(data)
        event = self.schedule.event_at(index)
        if index is None or event is None:
            await self.edit_callback_message(message, "Событие не найдено. Откройте меню заново.", self.schedule_menu_keyboard())
            return
        await self.edit_callback_message(
            message,
            "Что изменить?\n\n"
            f"{self.formatter.event_line(event)}",
            self.edit_field_keyboard(index),
        )

    async def handle_edit_field_callback(self, message: dict[str, Any], chat_id: str, data: str) -> None:
        parsed = self.edit_field_callback_data(data)
        if parsed is None:
            await self.edit_callback_message(message, "Событие не найдено. Откройте меню заново.", self.schedule_menu_keyboard())
            return

        index, field = parsed
        event = self.schedule.event_at(index)
        if event is None:
            await self.edit_callback_message(message, "Событие не найдено. Откройте меню заново.", self.schedule_menu_keyboard())
            return

        self.pending_actions[chat_id] = f"edit_event:{index}:{field}"
        await self.edit_callback_message(
            message,
            self.edit_prompt(event, field),
            self.back_to_edit_keyboard(index),
        )

    async def handle_edit_toggle_callback(self, message: dict[str, Any], data: str) -> None:
        index = self.callback_index(data)
        event = self.schedule.event_at(index)
        if index is None or event is None:
            await self.edit_callback_message(message, "Событие не найдено. Откройте меню заново.", self.schedule_menu_keyboard())
            return

        self.schedule.toggle_enabled(index)
        await self.schedule.reload_events()
        updated = self.schedule.events[index]
        await self.edit_callback_message(
            message,
            f"Событие обновлено:\n{self.formatter.event_line(updated)}",
            self.edit_field_keyboard(index),
        )

    async def handle_delete_callback(self, message: dict[str, Any], data: str) -> None:
        index = self.callback_index(data)
        event = self.schedule.event_at(index)
        if index is None or event is None:
            await self.edit_callback_message(message, "Событие не найдено. Откройте меню заново.", self.schedule_menu_keyboard())
            return
        await self.edit_callback_message(
            message,
            f"Удалить событие?\n\n{self.formatter.event_line(event)}",
            {
                "inline_keyboard": [
                    [{"text": "Удалить", "callback_data": f"confirm_delete:{index}"}],
                    [{"text": "Назад", "callback_data": "menu:delete"}],
                ]
            },
        )

    async def handle_confirm_delete_callback(self, message: dict[str, Any], data: str) -> None:
        index = self.callback_index(data)
        event = self.schedule.event_at(index)
        if index is None or event is None:
            await self.edit_callback_message(message, "Событие не найдено. Откройте меню заново.", self.schedule_menu_keyboard())
            return
        event_label = self.formatter.event_display(event)
        self.schedule.delete_event(index)
        await self.schedule.reload_events()
        await self.edit_callback_message(
            message,
            f"Событие удалено:\n{event_label}",
            self.schedule_menu_keyboard(),
        )

    async def handle_send_callback(self, message: dict[str, Any], data: str) -> None:
        index = self.callback_index(data)
        event = self.schedule.event_at(index)
        if index is None or event is None:
            await self.edit_callback_message(message, "Событие не найдено. Откройте меню заново.", self.schedule_menu_keyboard())
            return
        await self.edit_callback_message(
            message,
            "Подтвердите ручную отправку.\n\n"
            f"{self.formatter.event_display(event)}",
            self.confirm_keyboard(f"confirm_send:{index}", "send"),
        )

    async def handle_send_all_callback(self, message: dict[str, Any]) -> None:
        enabled_events = [event for event in self.schedule.events if event.enabled]
        await self.edit_callback_message(
            message,
            "Подтвердите ручную отправку всех включенных событий.\n\n"
            f"Событий к отправке: {len(enabled_events)}",
            self.confirm_keyboard("confirm_send:all", "send"),
        )

    async def handle_confirm_send_callback(self, message: dict[str, Any], chat_id: str, data: str) -> None:
        if not await self.check_manual_send_cooldown(chat_id):
            return

        target = data.split(":", 1)[1]
        if target == "all":
            events = [event for event in self.schedule.events if event.enabled]
        else:
            index = self.callback_index(data)
            event = self.schedule.event_at(index)
            if index is None or event is None:
                await self.edit_callback_message(message, "Событие не найдено. Откройте меню заново.", self.schedule_menu_keyboard())
                return
            events = [event]

        self.mark_manual_send(chat_id)
        await self.edit_callback_message(message, "Отправляю подтвержденную ручную отправку...")
        await self.schedule.send_events_manually(events)

    async def handle_test_callback(self, message: dict[str, Any], data: str) -> None:
        index = self.callback_index(data)
        event = self.schedule.event_at(index)
        if index is None or event is None:
            await self.edit_callback_message(message, "Событие не найдено. Откройте меню заново.", self.schedule_menu_keyboard())
            return
        await self.edit_callback_message(
            message,
            "Подтвердите тестовую отправку.\n\n"
            f"{self.formatter.event_display(event)}\n"
            f"Тестовая роль: {TEST_ROLE_ID}",
            self.confirm_keyboard(f"confirm_test:{index}", "test"),
        )

    async def handle_confirm_test_callback(self, message: dict[str, Any], chat_id: str, data: str) -> None:
        if not await self.check_manual_send_cooldown(chat_id):
            return

        index = self.callback_index(data)
        event = self.schedule.event_at(index)
        if index is None or event is None:
            await self.edit_callback_message(message, "Событие не найдено. Откройте меню заново.", self.schedule_menu_keyboard())
            return

        self.mark_manual_send(chat_id)
        await self.edit_callback_message(message, f"Отправляю тестовое событие:\n{self.formatter.event_display(event)}")
        sent = await self.schedule.send_test_event(event)
        result = "Тестовое уведомление отправлено" if sent else "Тестовое уведомление не отправилось"
        await self.notifier.send(
            f"{result}\n\n"
            f"Событие: {self.formatter.event_display(event)}\n"
            f"Тестовая роль: {TEST_ROLE_ID}"
        )

    @staticmethod
    def callback_index(data: str) -> int | None:
        try:
            return int(data.split(":", 1)[1])
        except (IndexError, ValueError):
            return None

    def clear_pending(self, chat_id: str) -> None:
        self.pending_actions.pop(chat_id, None)
        self.pending_event_drafts.pop(chat_id, None)

    def schedule_menu_keyboard(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": "Список событий", "callback_data": "menu:list"}],
                [{"text": "Добавить событие", "callback_data": "menu:add"}],
                [{"text": "Изменить событие", "callback_data": "menu:edit"}],
                [{"text": "Включить/отключить", "callback_data": "menu:toggle"}],
                [{"text": "Удалить событие", "callback_data": "menu:delete"}],
                [{"text": "Перечитать events.json", "callback_data": "menu:reload"}],
            ]
        }

    @staticmethod
    def back_keyboard() -> dict[str, Any]:
        return {"inline_keyboard": [[{"text": "Назад", "callback_data": "menu:main"}]]}

    def event_keyboard(self, action: str) -> dict[str, Any]:
        keyboard = []
        if action == "send":
            keyboard.append([{"text": "Отправить все включенные", "callback_data": "send_all"}])
        for index, event in enumerate(self.schedule.events):
            keyboard.append([{"text": self.formatter.button_label(event), "callback_data": f"{action}:{index}"}])
        keyboard.append([{"text": "Назад", "callback_data": "menu:main"}])
        return {"inline_keyboard": keyboard}

    @staticmethod
    def edit_field_keyboard(index: int) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": "Название", "callback_data": f"edit_field:{index}:name"}],
                [{"text": "Текст", "callback_data": f"edit_field:{index}:text"}],
                [{"text": "Расписание", "callback_data": f"edit_field:{index}:cron"}],
                [{"text": "Канал", "callback_data": f"edit_field:{index}:channel_id"}],
                [{"text": "Роль", "callback_data": f"edit_field:{index}:role_id"}],
                [{"text": "Реакция", "callback_data": f"edit_field:{index}:reaction"}],
                [{"text": "Включить/отключить", "callback_data": f"edit_toggle:{index}"}],
                [{"text": "Назад", "callback_data": "menu:edit"}],
            ]
        }

    @staticmethod
    def back_to_edit_keyboard(index: int) -> dict[str, Any]:
        return {"inline_keyboard": [[{"text": "Назад", "callback_data": f"edit:{index}"}]]}

    def edit_prompt(self, event: ScheduledEvent, field: str) -> str:
        if field == "name":
            return (
                "Введите новое название события.\n\n"
                f"Сейчас:\n{self.formatter.event_display(event)}"
            )
        if field == "text":
            return (
                "Введите новый текст сообщения.\n\n"
                "Роль добавится автоматически.\n\n"
                f"Сейчас:\n{self.formatter.event_display(event)}"
            )
        if field == "cron":
            return (
                "Введите новое расписание в формате cron.\n\n"
                "Примеры:\n"
                "10 18 * * sat — каждую субботу в 18:10\n"
                "20 18 * * mon — каждый понедельник в 18:20\n\n"
                "Дни недели: mon, tue, wed, thu, fri, sat, sun\n\n"
                f"Сейчас:\n{self.formatter.event_display(event)}"
            )
        if field == "channel_id":
            return (
                "Введите новый ID Discord-канала.\n\n"
                "Например:\n"
                "1199512515755909171\n\n"
                f"Сейчас: {event.channel_id}"
            )
        if field == "role_id":
            return (
                "Введите новый ID Discord-роли.\n\n"
                "Например:\n"
                "1199509896069124106\n\n"
                "В текст роль добавлять не нужно."
            )
        if field == "reaction":
            return (
                "Введите новую реакцию.\n\n"
                "Например:\n"
                "✅"
            )
        return "Введите новое значение."

    @staticmethod
    def edit_field_callback_data(data: str) -> tuple[int, str] | None:
        try:
            _, raw_index, field = data.split(":", 2)
            if field not in {"name", "text", "cron", "channel_id", "role_id", "reaction"}:
                return None
            return int(raw_index), field
        except ValueError:
            return None

    @staticmethod
    def confirm_keyboard(confirm_callback: str, back_action: str) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": "Подтвердить", "callback_data": confirm_callback}],
                [{"text": "Назад", "callback_data": f"menu:{back_action}"}],
            ]
        }

    async def check_manual_send_cooldown(self, chat_id: str) -> bool:
        now = time.monotonic()
        last_at = self.last_manual_send_at.get(chat_id, 0)
        remaining = int(MANUAL_SEND_COOLDOWN_SECONDS - (now - last_at))
        if remaining <= 0:
            return True

        await self.notifier.send(
            "Ручная отправка временно заблокирована.\n\n"
            f"Повторите через {remaining} сек."
        )
        return False

    def mark_manual_send(self, chat_id: str) -> None:
        self.last_manual_send_at[chat_id] = time.monotonic()

    def is_authorized(
        self,
        message: dict[str, Any],
        callback_query: dict[str, Any] | None = None,
    ) -> bool:
        chat_id = self.message_chat_id(message)
        if chat_id != self.notifier.chat_id:
            return False

        if not self.notifier.admin_user_id:
            logger.warning("Ignoring Telegram command because TG_ADMIN_USER_ID is not set")
            return False

        source = callback_query if callback_query is not None else message
        sender = source.get("from")
        if not isinstance(sender, dict):
            return False

        return str(sender.get("id")) == self.notifier.admin_user_id

    @staticmethod
    def message_chat_id(message: dict[str, Any]) -> str | None:
        chat = message.get("chat")
        if not isinstance(chat, dict):
            return None
        return str(chat.get("id"))
