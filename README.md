# Discord Scheduled Bot

Бот отправляет сообщения в Discord-канал по расписанию, тегает нужную роль и ставит реакцию `✅`.

Дополнительно есть:

- slash-команда `/send_now` для ручной отправки сообщений;
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

`channel_id`, `role_id`, `reaction` и `allowed_mentions` применяются ко всем событиям. В каждом событии достаточно указать:

- `name` - уникальное имя события;
- `text` - текст без роли, бот сам добавит `<@&role_id>`;
- `cron` - расписание в формате `минута час день_месяца месяц день_недели`.

## Slash-команда

Бот регистрирует команду:

```text
/send_now
```

Без параметров команда отправит все сообщения из `events.json` сразу.

Можно отправить одно событие по имени:

```text
/send_now event_name: weekly_25x25_common_monday
```

Командой могут пользоваться только пользователи с правом `Administrator` или `Manage Server`.

Чтобы slash-команда появилась быстро, добавьте в переменные окружения ID сервера:

```env
DISCORD_GUILD_ID=ваш_id_сервера
```

Если `DISCORD_GUILD_ID` не указан, команда синхронизируется глобально и может появиться в Discord не сразу.

## Права бота в Discord

При добавлении бота на сервер выберите scopes:

```text
bot
applications.commands
```

Права:

```text
View Channels
Send Messages
Add Reactions
Read Message History
Mention @everyone, @here, and All Roles
```

Если не выдавать право `Mention @everyone, @here, and All Roles`, роль `1199509896069124106` должна быть mentionable в настройках сервера.

## Локальный запуск

Перейдите в папку проекта:

```powershell
cd "C:\Users\godisjoke\Desktop\дсбот"
```

Создайте `.env`:

```powershell
Copy-Item .env.example .env
notepad .env
```

В `.env` укажите токен бота:

```env
DISCORD_TOKEN=ваш_токен_бота
TIMEZONE=Europe/Moscow
EVENTS_FILE=events.json
LOG_LEVEL=INFO
DISCORD_GUILD_ID=
LOCK_FILE=.bot.lock
```

Установите зависимости:

```powershell
pip install -r requirements.txt
```

Запустите бота:

```powershell
python main.py
```

После запуска в логах появятся ближайшие срабатывания:

```text
Next weekly_25x25_mafia_tuesday: 2026-06-23 18:20 MSK
```

## Защита от двойного запуска

Бот создает lock-файл `.bot.lock`. Если случайно запустить вторую копию, она завершится с ошибкой:

```text
Another bot process is already running
```

Это нужно, чтобы две копии бота не отправили одинаковые сообщения одновременно.

## Загрузка на GitHub

Репозиторий: `https://github.com/xyir4ik/beifonchik.git`

Команды из папки проекта:

```powershell
cd "C:\Users\godisjoke\Desktop\дсбот"
git init
git add .
git commit -m "Update scheduled Discord bot"
git branch -M main
git remote add origin https://github.com/xyir4ik/beifonchik.git
git push -u origin main
```

Если remote уже существует:

```powershell
git remote set-url origin https://github.com/xyir4ik/beifonchik.git
git push -u origin main
```

Файл `.env` не попадет в GitHub, потому что он добавлен в `.gitignore`. Токен бота нельзя коммитить в репозиторий.

## Деплой на BotHost

Dockerfile для этого бота не нужен. Проект обычный Python-проект: зависимости описаны в `requirements.txt`, запуск идет через `python main.py`.

На BotHost сделайте так:

1. Создайте новый проект/бот.
2. Подключите GitHub-репозиторий `xyir4ik/beifonchik`.
3. Укажите ветку `main`.
4. Выберите Python `3.11`, если BotHost предлагает версию.
5. Укажите команду запуска:

```bash
python main.py
```

6. Добавьте переменные окружения:

```env
DISCORD_TOKEN=ваш_токен_бота
TIMEZONE=Europe/Moscow
EVENTS_FILE=events.json
LOG_LEVEL=INFO
DISCORD_GUILD_ID=ваш_id_сервера
LOCK_FILE=.bot.lock
```

7. Сохраните настройки и запустите/перезапустите бота.

В логах после запуска должно появиться:

```text
Logged in as ...
Synced 1 slash command(s) to guild ...
Scheduled weekly_15x15_vzp_saturday with cron '10 18 * * sat'
Next weekly_15x15_vzp_saturday: ... MSK
Scheduler started
```

## Если slash-команда не появилась

Проверьте:

- бот был приглашен со scope `applications.commands`;
- в BotHost указан правильный `DISCORD_GUILD_ID`;
- бот был перезапущен после изменения переменных окружения;
- у вашего пользователя есть право `Administrator` или `Manage Server`.

## Python

Рекомендуемая версия для BotHost: Python `3.11`.

На Python `3.13+` проект тоже должен работать, потому что в `requirements.txt` добавлен пакет `audioop-lts`.
