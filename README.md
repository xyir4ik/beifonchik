# Discord Scheduled Bot

Бот отправляет сообщения в Discord-канал по расписанию, тегает нужную роль и ставит реакцию `✅`.

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

Сами события лежат в `events.json`.

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
```

Установите зависимости:

```powershell
pip install -r requirements.txt
```

Запустите бота:

```powershell
python main.py
```

После запуска бот будет ждать ближайшее событие по расписанию. Тестовый режим удален, в проекте оставлена только боевая логика.

## Загрузка на GitHub

Создайте новый пустой репозиторий на GitHub. Потом выполните команды из папки проекта:

```powershell
cd "C:\Users\godisjoke\Desktop\дсбот"
git init
git add .
git commit -m "Initial Discord scheduled bot"
git branch -M main
git remote add origin https://github.com/USERNAME/REPOSITORY.git
git push -u origin main
```

Замените:

```text
USERNAME
REPOSITORY
```

на ваш GitHub-логин и название репозитория.

Файл `.env` не попадет в GitHub, потому что он добавлен в `.gitignore`. Токен бота нельзя коммитить в репозиторий.

## Деплой на BotHost

Dockerfile для этого бота не нужен. Проект обычный Python-проект: зависимости описаны в `requirements.txt`, запуск идет через `python main.py`.

На BotHost сделайте так:

1. Создайте новый проект/бот.
2. Подключите GitHub-репозиторий с этим проектом.
3. Укажите ветку `main`.
4. Укажите команду запуска:

```bash
python main.py
```

5. Добавьте переменные окружения:

```env
DISCORD_TOKEN=ваш_токен_бота
TIMEZONE=Europe/Moscow
EVENTS_FILE=events.json
LOG_LEVEL=INFO
```

6. Сохраните настройки и запустите/перезапустите бота.

В логах после запуска должно появиться, что бот вошел в Discord и запланировал события:

```text
Logged in as ...
Scheduled weekly_15x15_vzp_saturday with cron '10 18 * * sat'
Scheduled weekly_25x25_common_monday with cron '20 18 * * mon'
Scheduled weekly_25x25_mafia_tuesday with cron '20 18 * * tue'
Scheduled weekly_25x25_common_thursday with cron '20 18 * * thu'
Scheduled weekly_25x25_mafia_sunday with cron '20 18 * * sun'
```

## Если BotHost спросит версию Python

Можно выбрать Python `3.12`, если такая настройка есть. На Python `3.13+` проект тоже должен работать, потому что в `requirements.txt` добавлен пакет `audioop-lts`.
