# GosZakup Telegram Monitor

Monitors the official Kazakhstan public procurement API (GosZakup, GraphQL V3)
for lots matching a keyword list, and sends a Telegram message once a
matching lot's application deadline is between 5 and 72 hours away.

## 1. Setup: add your tokens

Open the `.env` file in the project root and fill in:

```
GOSZAKUP_API_TOKEN=your_goszakup_api_token_here
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=
```

- `GOSZAKUP_API_TOKEN` — get it from your GosZakup account (Bearer token for
  `https://ows.goszakup.gov.kz/v3/graphql`).
- `TELEGRAM_BOT_TOKEN` — get it from [@BotFather](https://t.me/BotFather) on Telegram.
- `TELEGRAM_CHAT_ID` — **you can leave this empty.** On first start, the bot
  will print `Send /start to your Telegram bot.` Open your bot in Telegram and
  send `/start` — it will automatically remember that chat and use it for all
  future notifications. You'll get a confirmation message:
  `✅ GosZakup monitoring connected.`

## 2. Start

Windows:

```
run.bat
```

Linux/macOS:

```
./run.sh
```

The script creates a virtual environment, installs dependencies, and starts
the bot automatically. First launch runs a one-time bootstrap that scans the
last 90 days of listings for keyword matches (see `BOOTSTRAP_LOOKBACK_DAYS`
below), then the bot checks GosZakup every 5 minutes.

Run a single check-and-exit cycle instead of the continuous loop:

```
python -m app.main --once
```

## 3. Stop

Press `Ctrl+C`.

## 4. Where things live

| What | Where |
|---|---|
| Database (SQLite) | `data/tenders.db` — created automatically, survives restarts |
| Keywords | `config/keywords.yaml` — edit and restart the bot to apply changes |
| Deadline window (5h / 72h) | `.env` → `MIN_HOURS_REMAINING` / `MAX_HOURS_REMAINING` |
| Check interval (default 5 min) | `.env` → `CHECK_INTERVAL_SECONDS` |
| Timezone | `.env` → `APP_TIMEZONE` (default `Asia/Qyzylorda`) |
| How far back the first sync looks | `.env` → `BOOTSTRAP_LOOKBACK_DAYS` (default 90) |

## How it works, briefly

1. Every cycle, the bot asks GosZakup for lots updated since the last
   successful sync (with a small overlap window so nothing is missed near
   the boundary) whose name/description matches one of the configured
   keywords.
2. Every match is checked locally against `config/keywords.yaml` (the API's
   search is only used to narrow candidates — the final match decision is
   always made locally, case-insensitively, ignoring the `ё`/`е` distinction).
3. Matches are stored in SQLite as `pending`. Every cycle also re-checks all
   `pending` tenders, refreshing them from the API.
4. A tender is sent to Telegram, **exactly once**, the moment its remaining
   time to the application deadline is between 5 and 72 hours (inclusive).
   Once sent, it's marked `sent` and will never be sent again, even if it
   changes later. If it would otherwise pass the 5-hour mark without ever
   having been sent, it's marked `expired` and dropped.
5. Notifications are sent via a direct Telegram Bot API call and contain only:
   name, amount, delivery location, deadline, time remaining, and a link to
   the announcement on goszakup.gov.kz.

## Running tests

```
.venv\Scripts\python.exe -m pytest      # Windows
.venv/bin/python -m pytest              # Linux/macOS
```
