# Discord Scheduled Bot

Бот отправляет сообщения в Discord-канал по расписанию, тегает нужную роль и ставит реакцию `✅`.

Дополнительно есть:

- Telegram-команда `/send_now` для ручной отправки сообщений в Discord;
- Telegram-команда `/next_events` для просмотра ближайших событий;
- Telegram-уведомления на русском о запуске, ошибках и ручных отправках;
- лог ближайшего времени срабатывания каждого события;
- защита от случайного запуска двух копий бота;
- удобный `events.json`, где канал, роль и реакция указываются один раз.

## Боевое расписание

Часовой пояс: `Europe/Moscow`.

Канал: `1199512515755909171`

Роль: `1199509896069124106`

| День | Время МСК | Текст |
| --- | --- | --- |
| Суббота | 18:10 | `<@&1199509896069124106> реаки 15x15 ВЗП` |
| Понедельник | 18:20 | `<@&1199509896069124106> реаки 25x25 общее взх` |
| Вторник | 18:20 | `<@&1199509896069124106> реаки 25x25 взх мафий` |
| Четверг | 18:20 | `<@&1199509896069124106> реаки 25x25 общее взх` |
| Воскресенье | 18:20 | `<@&1199509896069124106> реаки 25x25 взх мафий` |

## Telegram-команды

Команды пишутся вашему Telegram-боту, который указан в `TELEGRAM_BOT_TOKEN` или `TG_BOT_TOKEN`.

Доступ разрешен только чату из переменной `TELEGRAM_CHAT_ID` или `TG_CHAT_ID`.

```text
/next_events
```

Показывает 5 ближайших событий.

```text
/next_events 10
```

Показывает до 10 ближайших событий.

```text
/send_now
```

Сразу отправляет все сообщения из `events.json` в Discord.

```text
/send_now weekly_25x25_common_monday
```

Отправляет одно событие по имени.

```text
/help
```

Показывает список Telegram-команд.

## Telegram-уведомления

Чтобы включить уведомления и команды:

1. Откройте Telegram и напишите `@BotFather`.
2. Создайте бота командой `/newbot`.
3. Скопируйте токен Telegram-бота.
4. Напишите любое сообщение созданному Telegram-боту.
5. Узнайте свой `chat_id`, например через `@userinfobot` или через запрос:

```text
https://api.telegram.org/botTELEGRAM_BOT_TOKEN/getUpdates
```

На BotHost добавьте:

```env
TELEGRAM_BOT_TOKEN=токен_telegram_бота
TELEGRAM_CHAT_ID=ваш_chat_id
```

Если BotHost не сохраняет переменную `TELEGRAM_BOT_TOKEN`, используйте короткие имена:

```env
TG_BOT_TOKEN=токен_telegram_бота
TG_CHAT_ID=ваш_chat_id
```

`TELEGRAM_BOT_TOKEN` вставляется без слова `bot` в начале. Правильно:

```env
TELEGRAM_BOT_TOKEN=1234567890:AA...
```

Неправильно:

```env
TELEGRAM_BOT_TOKEN=bot1234567890:AA...
```

При запуске придет сообщение:

```text
Бот запущен

Аккаунт: бейфончик#0638
Событий в расписании: 5

Ближайшие события:
• weekly_25x25_common_thursday — 25.06.2026 18:20 МСК

Telegram-команды:
/next_events
/send_now
```

Если в логах есть ошибка:

```text
Telegram API getUpdates failed: HTTP 404 {'ok': False, 'error_code': 404, 'description': 'Not Found'}
```

значит Telegram не нашел бота по токену. Проверьте `TELEGRAM_BOT_TOKEN` или `TG_BOT_TOKEN`: он должен быть токеном от `@BotFather`, без лишних пробелов, кавычек и без слова `bot` в начале.

## Конфиг событий

События лежат в `events.json`:

```json
{
  "channel_id": 1199512515755909171,
  "role_id": 1199509896069124106,
  "reaction": "✅",
  "allowed_mentions": "roles",
  "events": [
    {
      "name": "weekly_15x15_vzp_saturday",
      "text": "реаки 15x15 ВЗП",
      "cron": "10 18 * * sat"
    }
  ]
}
```

`channel_id`, `role_id`, `reaction` и `allowed_mentions` применяются ко всем событиям.

## Права бота в Discord

При добавлении бота на сервер выберите scope:

```text
bot
```

Права:

```text
View Channels
Send Messages
Add Reactions
Read Message History
Mention @everyone, @here, and All Roles
```

Scope `applications.commands` нужен только если вы специально включаете Discord slash-команды через `ENABLE_DISCORD_COMMANDS=true`.

## Переменные окружения BotHost

```env
DISCORD_TOKEN=ваш_токен_бота
TIMEZONE=Europe/Moscow
EVENTS_FILE=events.json
LOG_LEVEL=INFO
LOCK_FILE=.bot.lock
TELEGRAM_BOT_TOKEN=токен_telegram_бота
TELEGRAM_CHAT_ID=ваш_chat_id
TG_BOT_TOKEN=
TG_CHAT_ID=
ENABLE_DISCORD_COMMANDS=false
DISCORD_GUILD_ID=
```

Для обычной работы `DISCORD_GUILD_ID` можно оставить пустым.

## Деплой на BotHost

1. Подключите GitHub-репозиторий `xyir4ik/beifonchik`.
2. Укажите ветку `main`.
3. Выберите Python `3.11`, если BotHost предлагает версию.
4. Команда запуска:

```bash
python main.py
```

5. Добавьте переменные окружения из раздела выше.
6. Сохраните настройки и перезапустите бота.

В логах должно быть:

```text
Logged in as ...
Scheduled weekly_15x15_vzp_saturday with cron '10 18 * * sat'
Next weekly_15x15_vzp_saturday: ... MSK
Scheduler started
Telegram commands polling started
```

## Локальный запуск

```powershell
cd "C:\Users\godisjoke\Desktop\дсбот"
Copy-Item .env.example .env
notepad .env
pip install -r requirements.txt
python main.py
```

## Защита от двойного запуска

Бот создает lock-файл `.bot.lock`. Если случайно запустить вторую копию, она завершится с ошибкой:

```text
Another bot process is already running
```

## Python

Рекомендуемая версия для BotHost: Python `3.11`.

На Python `3.13+` проект тоже должен работать, потому что в `requirements.txt` добавлен пакет `audioop-lts`.
