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
| Database (SQLite) | `data/tenders.db` — created automatically, survives restarts. Stores only sent lot ids (`sent_lots`), `app_state`, and `settings` — no pending/expired history |
| Keywords | `config/keywords.yaml` — edit and restart the bot to apply changes |
| Deadline window (5h / 72h) | `.env` → `MIN_HOURS_REMAINING` / `MAX_HOURS_REMAINING` |
| Check interval (default 5 min) | `.env` → `CHECK_INTERVAL_SECONDS` |
| Timezone | `.env` → `APP_TIMEZONE` (default `Asia/Qyzylorda`) |
| How far back the first sync looks | `.env` → `BOOTSTRAP_LOOKBACK_DAYS` (default 90) |
| Periodic full keyword re-scan interval | `.env` → `DISCOVERY_SCAN_INTERVAL_MINUTES` (default 120) |
| Discovery/bootstrap concurrent request pool size | `.env` → `DISCOVERY_CONCURRENCY` (default 4) — lower it (1-2) as a safety knob on a memory-constrained deployment |
| Minimum amount to send (exclusive) | `.env` → `MIN_AMOUNT_KZT` (default 100000) |

## How it works, briefly

Storage is intentionally minimal: the database holds only the ids of lots
that were **actually sent**, purely to prevent resending. Nothing else is
ever stored — no pending queue, no expired history, no cached tender
details. A lot outside the 5–72h window is simply skipped; the next
discovery cycle re-evaluates it from scratch, exactly as if seen for the
first time. Periodic discovery is therefore the source of truth for
everything that hasn't been sent yet.

Two independent discovery passes feed the same pipeline:

1. **Incremental sync** (every `CHECK_INTERVAL_SECONDS`, default 5 min): asks
   GosZakup only for lots updated since the last successful sync (with a
   small overlap window). Fast and cheap, but can only see tenders GosZakup
   has recently touched.
2. **Discovery scan** (every `DISCOVERY_SCAN_INTERVAL_MINUTES`, default 2h):
   re-searches every configured keyword individually, independent of when
   the tender was last updated. This exists because a tender's "last
   updated" timestamp reflects when GosZakup last edited it, not when its
   deadline enters the 5–72h window — a tender published weeks ago and never
   touched again would otherwise be invisible to incremental sync alone.

For every candidate, regardless of which pass found it:
- The final keyword decision checks **only the tender's title** (`nameRu` /
  `nameKz`) — the API's own search may use name+description to find
  candidates, but a match only counts if the keyword appears in the title
  itself.
- It must have `amount` strictly greater than `MIN_AMOUNT_KZT`.
- It's sent to Telegram **exactly once**, the moment its remaining time to
  the application deadline is between 5 and 72 hours (inclusive) — only then
  is its lot id written to the database, so it's never sent twice. Outside
  that window (too early or too late) it's skipped and nothing is stored;
  a too-early lot is simply picked up again by the next discovery cycle once
  it enters the window.
- Notifications are sent via a direct Telegram Bot API call and contain only:
  name, amount, delivery location, deadline, time remaining, and a link to
  the announcement on procurement.gov.kz, built from the announcement's real
  `trdBuyId` (never from the display-only `trdBuyNumberAnno`/`lotNumber`).

Each pass logs a one-line summary (`INCREMENTAL SYNC SUMMARY` / `DISCOVERY
SUMMARY` / `BOOTSTRAP SUMMARY`) with counts for requests, failures, matches,
and outcomes (sent/pending/expired/rejected).

**Memory**: every fetch is a streaming pipeline — API page → parse → title
match → send/skip → discard → next page — never a list/dict of all results
collected before filtering. Discovery's 92×2 keyword queries run through a
bounded worker pool (`DISCOVERY_CONCURRENCY`, default 4); a lot matching more
than one keyword is deduped via a lightweight set of lot ids, not full
payloads. This is what keeps memory roughly constant whether a query returns
a handful of lots or (as incremental sync's catch-up window historically saw)
160,000+. A periodic `MEMORY: rss_mb=...` log line tracks actual process
memory during long fetches (reads `/proc/self/status`, so only on Linux —
e.g. Railway; a no-op locally on Windows/macOS).

## Running tests

```
.venv\Scripts\python.exe -m pytest      # Windows
.venv/bin/python -m pytest              # Linux/macOS
```
