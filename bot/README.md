# `bot/` — the Telegram chat handler

A Cloudflare Worker that takes taps and writes rows: `/start`, station pick,
profile pick, `/about`, `/pause`, `/stop`, and the 👍/👎 under each daily alert.

It holds no arithmetic. The breakpoint table, the sub-index formula, the CPCB
advisory text and the message wording are all Python, in
[`scripts/aqi.py`](../scripts/aqi.py) and
[`scripts/send_alerts.py`](../scripts/send_alerts.py), next to their tests and
next to where Phase 4 needs the identical numbers. The same table written in two
languages goes stale in one of them.

## Why a Worker and not the Python process

The daily message is sent by GitHub Actions, which is fine for a job that runs
once a day. A *reply* is not: a person tapping a button waits for it, and there
is no free Python host that answers instantly and never sleeps. A Worker has no
cold start worth the name.

## Why it is separate from `trigger/`

`trigger/` holds a GitHub personal access token and has no public address at all
— it exports only `scheduled`. This Worker is internet-facing by definition,
because a Telegram webhook is a URL anyone can POST to. They share no secret,
and a compromise of this one cannot start a workflow run.

## Deploying

```bash
cd bot
npm ci                      # never `npm install -g wrangler`; the lockfile is the pin
npx wrangler deploy
npx wrangler secret put TELEGRAM_BOT_TOKEN      # from @BotFather
npx wrangler secret put TELEGRAM_WEBHOOK_SECRET # any long random string you choose
npx wrangler secret put DATABASE_URL            # the same Neon string, verify-full
```

Then register the webhook once, from a shell:

```bash
curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
  -H 'content-type: application/json' \
  -d '{"url":"https://aqi-nowcast-bot.<subdomain>.workers.dev",
       "secret_token":"<the same TELEGRAM_WEBHOOK_SECRET>"}'
```

`TELEGRAM_BOT_TOKEN` also goes into the GitHub repository's Actions secrets, by
hand, because `send_alerts.py` sends with it.

## The secret-token header is not optional

The webhook URL is public. Without the `X-Telegram-Bot-Api-Secret-Token` check —
which happens **before the body is parsed** — anyone who finds the URL can POST a
forged update and create subscriptions or forge feedback taps, and Gate 2 is
counted from that table. A request without the header gets 401 and nothing else.

The second half of the same argument is in the feedback handler: `callback_data`
is echoed back by the client, so the insert filters `sent_log` by
`chat_id = <the tapper>`. Without that, a forged id rates someone else's message.

## What is stored about a person

Chat id, station, profile. Nothing else — no name, no username, no coordinates,
no health record (build plan §5). `/stop` deletes the row. Past `feedback` rows
survive it deliberately: `feedback.chat_id` is not a foreign key, because the
record that a message was once useful is the retention measurement and it names
nobody.

Nothing here logs message text.

## Known gotchas

- **A non-2xx response makes Telegram redeliver the update.** That is wanted —
  every write is an upsert or a delete keyed on the chat id, so a repeat is a
  no-op, and a failure Telegram forgets about is a failure nobody sees. It also
  means an error that is *not* transient retries forever, which is why the
  station lookups happen before the inserts rather than letting a foreign-key
  violation escape.
- **`nodejs_compat` is required.** `@neondatabase/serverless` reaches for
  `node:buffer` and `node:events`; without the flag the deploy succeeds and the
  first database call fails at runtime.
- **A Worker cannot open a TCP Postgres connection**, which is why the Neon HTTP
  driver is here at all. It is the only dependency, pinned at 1.1.0 with a
  lockfile.
- **The account needs a `workers.dev` subdomain to exist.** Same trap as
  `trigger/wrangler.toml` documents (Cloudflare error 10063).
