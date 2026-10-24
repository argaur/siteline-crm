# Move SitelineCRM bot + API from Railway to Vercel

Date: 2026-10-24. Status: draft, awaiting Gaurav's approval.

## Why

Railway had a credential breach on 2026-10-21. Gaurav cancelled the Railway plan on 2026-10-24, and Railway
removed the running service at once. Since then the bot (`@SitelineCRMbot`) is offline and the API
(`founder-crm-bot-production.up.railway.app`) returns 404. SitelineCRM is a portfolio demo linked from
LinkedIn Featured, so it needs to come back.

The hosting rule (settled 2026-07-31) says a service that needs a server goes to Vercel. The Oracle VM is ruled
out for this bot (956 MB RAM). Data is already on Neon (`winter-haze-14475661`), so only compute moves.

## Goals

1. The bot answers on Telegram again, with every current command and capture flow working as before.
2. The dashboard and the landing signup work from `https://siteline.gauravg.dev`.
3. Nothing runs on Railway. The `founder-crm` Railway project can be deleted.

## Non-goals

- No new features. No change to AI prompts, schema meaning or dashboard UI.
- Stale-lead nudges stay off. `nudge_job` is not ported and gets no cron. The manual nudge endpoint
  (`POST /api/leads/{id}/nudge`) keeps working, since it is request-driven.
- The landing page stays on GitHub Pages for now. Its move to Cloudflare is separate work.

## Anti-goals (a failure even if it "works")

- A multi-step flow (`/addcontact`, `/addnote`, the "needs more detail" follow-up, `/note` contact pick)
  silently loses its place between messages.
- One Telegram message saved twice because Telegram retried the webhook.
- Any secret printed into chat, a log or a committed file.

## Current state (verified 2026-10-24)

- `bot/main.py` runs FastAPI and PTB v21 polling in one process via a lifespan handler. PTB JobQueue runs nudges.
- Conversation state lives in memory: `ConversationHandler(name="addcontact"|"addnote", persistent=False)` plus
  `context.user_data` keys `pending_capture`, `addnote`, `addcontact`, `quick_note_text`.
- `db.py` uses one module-wide asyncpg pool (`statement_cache_size=0` for Neon PgBouncer).
- CORS allows only `https://argaur.github.io` and localhost. The live landing origin is `https://siteline.gauravg.dev`.
- Live `landing/dashboard/config.js` (built by `.github/workflows/pages.yml` from repo variable `API_BASE_URL`)
  is `NOT_YET_DEPLOYED`: the variable was never set, so the dashboard has not had a working API at all.
- `landing/index.html:1318` hardcodes `RAILWAY_URL` for signup.
- Secrets live in gopass under `founder-crm/bot/*` (rotated 2026-10-24).

## The how (decided)

**Host.** New Vercel project `siteline-api`, team `argaurs-projects`, root `bot/`, Python runtime, region
`iad1` (Neon is `us-east-1`). Served at `https://siteline-api.gauravg.dev` (Cloudflare CNAME, DNS only).
Hobby plan: production is public, which is fine because the API already uses its own token auth.

**Telegram: polling becomes a webhook.** `POST /telegram/webhook`:
1. Reject unless header `X-Telegram-Bot-Api-Secret-Token` matches env `TELEGRAM_WEBHOOK_SECRET` (new secret,
   generated into gopass `founder-crm/bot/TELEGRAM_WEBHOOK_SECRET`).
2. Record `update_id` in a new table `telegram_updates(update_id bigint primary key, status text,
   received_at timestamptz)`, following the delivery rule below.
3. Build the update with `Update.de_json`, run `application.process_update`, return 200.
Register once with `setWebhook(url, secret_token, allowed_updates=["message","callback_query"])`.

**Conversation state goes to Neon.** A small custom PTB `BasePersistence` subclass backed by one table
`ptb_state(kind text, key text, data jsonb, primary key(kind, key))`, storing `user_data`, `chat_data`,
`bot_data` and `conversations`. Both ConversationHandlers switch to `persistent=True`. The `Application` is built
with this persistence. Each webhook request loads state on initialize and flushes on shutdown or after the update.

**App lifecycle per request (Claude's call).** Serverless has no long-lived process, so the lifespan polling
start goes away. **A fresh PTB `Application` is built, initialized, run and shut down for every webhook request.**
It is never reused while warm. Reason: PTB persistence reads state only at `initialize()`, so a reused instance
would act on stale state when two instances run. Cost is a few extra queries per update, which is fine at demo
traffic. Built without a JobQueue. The DB pool is created lazily and kept small (`min_size=0`, `max_size=2`).
The event-loop handling must be proven by a test.

**Delivery guarantee (Claude's call): at-least-once, never silently dropped.** `telegram_updates` gets a
`status` column (`processing` / `done`). On a new `update_id`, insert as `processing`. On success, set `done`.
On an exception, delete the row and return 500, so Telegram retries. A repeat of a `done` id returns 200 and is
skipped. A repeat of a `processing` id younger than 2 minutes returns 200 (a retry racing the first attempt);
older than that, it is reprocessed. Tradeoff: a crash after the database write but before the reply can save a
capture twice. That is rarer and less harmful than losing a message. Rows older than 7 days are deleted on each
request (cheap `DELETE ... WHERE received_at < now() - interval '7 days'`).

**Deploys (Claude's call): Vercel Git integration on `main`, root `bot/`.** Not `vercel deploy` from the working
tree, because that uploads untracked files (`bot/.env`, `bot/deploy/`, `.venv/`). An Ignored Build Step skips
builds when nothing under `bot/` changed, so landing-only pushes do not redeploy the API. Claude can push and
deploy (Vercel CLI and `gh` are not blocked, unlike Railway).

**Testing environment (Claude's call): no second bot or database.** The pytest suite runs with fakes, no network.
Preview deployments test only the HTTP API (`/health`, CORS, a signed-in dashboard call). The bot itself is first
tested on production. That is safe because the bot is already offline. Local polling needs `deleteWebhook`
first, which takes the live bot off its webhook, so local bot testing is for emergencies only and must end with
`setWebhook` again. Live test messages land in the shared demo database, so Gaurav uses one throwaway contact
name and deletes it afterwards.

**Small fixes found in review (Claude's call).**
- The manual nudge endpoint checks `telegram_app.running`, which is never true in webhook mode. It is changed to
  send through a `Bot` instance, so it does not return 503.
- `user_data` / `bot_data` can hold `datetime`, `Decimal` and int-keyed dicts, so persistence uses a JSON encoder
  that round-trips those types. `bot_data["nudged_at"]` is no longer written (nudges are off).
- The hardcoded dashboard link base in `commands.py` is pointed at `https://siteline.gauravg.dev`.
- Vercel function `maxDuration` is set to 60 s, which covers Whisper plus `gpt-4o-mini` on a voice note.

**Local dev keeps polling.** `python main.py` still runs polling with the same persistence, so local testing
needs no public URL.

**CORS.** Add `https://siteline.gauravg.dev`. Keep `https://argaur.github.io` and localhost.

**Landing and dashboard URLs.** Set repo variable `API_BASE_URL=https://siteline-api.gauravg.dev` (gh CLI). Change
`landing/index.html:1318` to that URL. The Pages workflow redeploys on push to `main`.

**Env vars on Vercel** (production + preview, piped from gopass, never pasted): `TELEGRAM_BOT_TOKEN`, `BOT_NAME`,
`DATABASE_URL`, `OPENAI_API_KEY`, `DASHBOARD_TOKEN_SECRET`, `TELEGRAM_WEBHOOK_SECRET`, `APP_BASE_URL`
(the new API URL), `DEMO_MODE=true`.

**Analytics.** Per estate rules, the API is not a web page, so no PostHog snippet here. The landing already has one.

## Testing

Test first (`superpowers:test-driven-development`). The repo has no test suite, so this adds `bot/tests/` with pytest:
- webhook rejects a wrong or missing secret (401/403), accepts the right one;
- a repeated `update_id` that finished is skipped; one whose processing raised is processed again on retry;
- persistence round-trip: a conversation state and `user_data` written in one request are read back in the next
  (fresh Application per request, so the test proves state survives a cold start);
- CORS preflight from `https://siteline.gauravg.dev` succeeds.
Then a live check after deploy: `/health` shows `db: ok`, `getWebhookInfo` shows the URL with no `last_error`,
and Gaurav runs `/start`, one `/addcontact` flow end to end, one forwarded message, and a dashboard sign-in.

## Cutover

1. Build and test on a preview deployment. Railway is already down, so there is no dual-polling clash.
2. Promote to production, add the Cloudflare CNAME, verify `/health`.
3. Run `setWebhook`. Telegram then pushes to Vercel.
4. Push the landing changes and set `API_BASE_URL`. The Pages workflow rebuilds `config.js`.
5. Gaurav verifies the four live checks above.
6. Delete the Railway `founder-crm` project (Gaurav, after confirming nothing else lives there).
Rollback: `deleteWebhook`, then run the bot locally with polling until fixed.

## Left to the builder

File layout inside `bot/` (for example `api/index.py` as the Vercel entry), exact `vercel.json` shape, how the
persistence class flushes (per update or on shutdown), test fixtures, and migration SQL file naming.

## Open

- Cloudflare CNAME `siteline-api` → `cname.vercel-dns.com` (DNS only): Gaurav adds it in the Cloudflare dashboard,
  as one guided step. Claude has no Cloudflare login on this machine (checked 2026-10-24).
- `bot/.env.example` has one uncommitted change and `bot/deploy/` (VM deploy files from 2026-09-17) is
  untracked. Neither is part of this move. Leave them untouched unless Gaurav says otherwise.
