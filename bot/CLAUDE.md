# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Local dev
pip install -r requirements.txt
python main.py                          # local emergency mode: polling + FastAPI on port 8000
                                          # refuses to poll while a webhook is set; blank token = API-only

# Tests (no network, no DB)
.venv/Scripts/python.exe -m pytest -q

# Production: Vercel Git integration on main. Never `vercel deploy` from the tree.

# Apply schema + seed spaces inventory (once, against a fresh DATABASE_URL)
python scripts/apply_schema.py

# Seed demo data — 1 manager + 2 reps + 13 leads across all 7 stages
python seed.py --seed
python seed.py --clear                   # removes only the rows seed.py created

# Extraction accuracy eval (eval/)
python eval/score_extraction.py                      # print field-level accuracy on ai.extract_from_voice()
python eval/score_extraction.py --verbose             # + per-example pass/fail
python eval/score_extraction.py --min-accuracy 0.8    # exit 1 if below threshold
```

No end-to-end test suite exists — verify bot changes by running locally and testing against Telegram,
and verify dashboard/API changes with real `curl` round-trips against a locally-running `main.py` plus
a statically-served `landing/` (see `landing/CLAUDE.md`). `eval/` is a narrower thing: a scalar accuracy
metric for the extraction prompt specifically, not a general test suite. See `eval/README.md` for the
metric definition and the read-only trust boundary rule (never edit `eval/dataset.jsonl` or
`eval/score_extraction.py` to make the score pass — only the prompt in `ai.py`). Requires
`OPENAI_API_KEY` — makes real API calls (gpt-4o-mini, ~20-25 short calls).

## Architecture

Five source files, all fully async on a single asyncio event loop (python-telegram-bot v21 + FastAPI
share the loop via `main.py`'s lifespan handler).

```
main.py      — FastAPI lifespan wires everything: bot polling (if configured) + nudge JobQueue on startup,
               all /api/* dashboard endpoints, /register + /dashboard-link signup/auth
db.py        — asyncpg queries against Neon Postgres — ASYNC (always await these calls)
ai.py        — OpenAI only (gpt-4o-mini for extraction/reasoning, Whisper for transcription) — ASYNC
commands.py  — All slash command handlers (/pipeline, /context, /won, /lost, /ask, /addcontact)
flows.py     — All message capture handlers (forwarded text, voice, image, /addnote, /note)
```

## Critical Constraints

**Sync/async:** `db.py` and `ai.py` are fully async — `asyncpg` and `AsyncOpenAI` both support it
natively. Every `db.*`/`ai.*` call must be `await`ed. (This inverts an earlier, now-stale constraint from
the original Airtable/sync-pyairtable prototype — if you see "never await db.*" anywhere, it's describing
the pre-migration architecture, not this one.)

**Single AI provider (OpenAI):** originally split across Anthropic (Claude Haiku, for extraction/
reasoning) and OpenAI (Whisper, for transcription only) — consolidated onto OpenAI alone on 2026-07-17
("less accounts to manage and top-up"). `ai.py` now uses `gpt-4o-mini` (cheap, supports vision + JSON
mode) for every extraction/reasoning task and Whisper for transcription, both through one
`AsyncOpenAI` client. Claude has no audio-input endpoint at all, so full single-provider consolidation
meant picking the provider that covers both jobs — OpenAI, not Anthropic. If cost/quality becomes a
concern later, a clean single-provider swap to something like OpenRouter or openmodels.market (both
OpenAI-SDK-compatible — just a `base_url`/key change) is the natural next step, not a same-day one.

**Handler registration order in `main.py`:**
1. `commands.get_handlers()` — all slash commands + ConversationHandler for `/addcontact`
2. `flows.get_handlers()` — ConversationHandler for `/addnote` must come before the generic
   `MessageHandler(filters.TEXT)`, which is the catch-all for forwarded messages

If the order is swapped, text replies during ConversationHandler flows get intercepted by the
forward/text capture handler.

**Lead record structure** (`db.py` returns flat dicts, not nested — no Airtable-style `fields` wrapper):
- `lead["id"]` → Postgres bigserial id (int, not a string record id)
- `lead["contact_name"]`, `lead["company"]` (name injected by callers via `_company_names`/joins — not
  a raw column on `leads`, which only stores `company_id`), `lead["stage"]`, `lead["seat_count"]`,
  `lead["city"]`, `lead["space_type"]`, `lead["est_deal_value"]`, `lead["budget_per_seat"]`,
  `lead["assigned_to"]` (user id or null)
- `lead["heat_score"]` → `{"score": int, "label": "Hot"|"Warm"|"Cold"}` injected by
  `db._lead_with_heat()`/`db.calculate_heat_score()`, NOT stored — recomputed on every read from
  recency + interaction count + deal size (see `db.py`'s docstring on `calculate_heat_score` for the
  exact formula; it deliberately treats a never-touched lead as Cold, not Hot)
- `lead["interaction_count"]` → derived at read time via a subquery, no stored counter to drift

**Stage list is one source of truth:** `db.STAGES` (also mirrored as `ai.STAGES` for the extraction
prompt) — `Inquiry, Qualified, Site Visit, Proposal, Negotiation, Closed-Won, Closed-Lost`, exact
strings including hyphenation. `db.CLOSED_STAGES` = the last two. `ai.py`'s extraction may return
`"unknown"` as a fallback when the stage genuinely can't be inferred — that value is extraction-only and
must never be persisted; `db._validate_stage()` enforces this by rejecting anything not in `db.STAGES`.

**Dashboard auth:** signed, non-expiring HMAC tokens (`DASHBOARD_TOKEN_SECRET`), minted by `/register`
(new signup) or `/dashboard-link` (existing user, by `telegram_id` or `user_id`), verified per-request by
`require_user`/`require_manager` in `main.py`. Role is re-read from the DB on every request, never
trusted from the token payload, so a demoted manager can't keep manager-only access via a stale token.
This is a deliberate 1-day-demo simplification — no OAuth, no expiry, no revocation.

## Data Flow: Capture Path

Forwarded text → `forward_or_text_handler` → `ai.extract_from_text()` → `ai.evaluate_note_quality()` →
`_save_capture()`

Voice note → `voice_handler` → Whisper transcription → `ai.classify_intent()` → if "recall": generate
brief; if "capture": `ai.extract_from_voice()` → `_save_capture()` (no quality gate for voice)

Image/screenshot → `image_handler` → `ai.extract_from_image()` (gpt-4o-mini vision) → `_save_capture()`

`_save_capture()` in `flows.py` is the shared write path: find-or-create company + lead,
`db.log_interaction()` (transactionally bumps `last_activity_at`), then sends a confirmation card with
"Looks good / Edit stage" inline keyboard. It never auto-regresses a lead's stage backwards from a vague
recapture — if the extracted stage is earlier than the lead's current stage, the current stage is kept
and flagged for manual confirmation instead.

## Postgres Schema (`schema.sql`)

Five tables: `users`, `companies`, `leads`, `interactions`, `spaces`.

- `leads.stage` — `lead_stage` enum, the 7 canonical stages above
- `leads.space_type` — `space_type` enum: `Dedicated Desk`, `Private Cabin`, `Managed Office`, `Day Pass`
- `leads.heat_score` is NOT a column — computed dynamically by `db.calculate_heat_score()`
- `interactions.type` — `interaction_type` enum: `whatsapp_forward`, `voice_note`, `screenshot`,
  `addnote_command` (dashboard-created notes map onto `addnote_command` — there is no separate
  `dashboard_note`/`manual_note` value)
- `spaces` — inventory rows (name, city, locality, seat counts, price, space_type), seeded once by
  `scripts/apply_schema.py`, used by the (stretch, not built) inventory-matching feature
- Demo data (1 manager + 2 reps + 13 leads spanning all 7 stages/several cities) lives under fixed
  negative `telegram_id`s seeded by `seed.py` — real Telegram users always have positive ids, so there's
  no collision, and `seed.py --clear` can find and remove exactly the rows it created

## Environment Variables

```
TELEGRAM_BOT_TOKEN      # blank → bot starts in API-only mode (dashboard/API still work, no bot/nudges)
BOT_NAME                # Telegram bot username without @ — used for deep links
DATABASE_URL            # Neon pooled connection string
OPENAI_API_KEY          # required for all AI: extraction/reasoning (gpt-4o-mini) + transcription (Whisper)
APP_BASE_URL            # API base URL (https://siteline-api.gauravg.dev) — /start deep link and signup redirect
DASHBOARD_TOKEN_SECRET  # HMAC signing key for dashboard tokens
TELEGRAM_WEBHOOK_SECRET # must match the secret header Telegram sends; gopass founder-crm/bot/TELEGRAM_WEBHOOK_SECRET
```

## Deployment
**Live on Vercel since 2026-10-25** (plan `docs/superpowers/plans/2026-10-24-vercel-rehost.md`).
- Vercel project `siteline-api` (`prj_1aOwM50fSAm5Tz0daBKLhUnJUPzP`), team `argaurs-projects`, root
  `bot/`, region `iad1`, 60 s timeout, Git integration on `main`. Builds skip when `bot/` is unchanged.
  Domain `https://siteline-api.gauravg.dev`. Never `vercel deploy` from the tree.
- Env vars (Production and Preview) come from gopass `founder-crm/bot/*` by pipe, plus `DEMO_MODE=true`.
- Telegram reaches the bot by webhook at `POST /telegram/webhook` (secret header
  `X-Telegram-Bot-Api-Secret-Token`, `allowed_updates` = message, callback_query). Each update runs on a
  fresh PTB Application; conversation and user_data state live in Neon `ptb_state`, delivery claims in
  `telegram_updates` (migration `migrations/2026-10-24-webhook-state.sql`, applied and verified 2026-10-25).
- Re-run `setWebhook` (url, secret_token, allowed_updates) after any bot token rotation.
- `httpx` logs at WARNING only: at INFO it logged Telegram URLs, which carry the bot token (fixed 2026-10-25).
- The automatic nudge job is retired. `POST /api/leads/{id}/nudge` still works.
- Live-verified 2026-10-25: `/health` db ok, CORS for `siteline.gauravg.dev`, 403 without the secret,
  `/start`, full `/addcontact` across function calls, text capture. Not live-tested: AI extraction card,
  inline-button callbacks, voice, image.
- Landing + dashboard deploy separately to GitHub Pages — see `landing/CLAUDE.md`

**Old target, retired:** Railway project `founder-crm`, account cancelled after the 2026-10-21 breach.

## Status
- **State:** Postgres/FastAPI migration complete, dashboard rework complete, all live-verified against
  real Neon data (round-trips for rep pipeline, notes, stage changes, and the manager funnel/leaderboard/
  stalled-leads views, including 403 enforcement for non-manager roles). AI provider consolidated from
  Anthropic+OpenAI onto OpenAI alone (`gpt-4o-mini` + Whisper, one key). A follow-up plan added the
  auto space-matching engine (`flows.py`'s post-capture follow-up message + `GET /api/leads/{id}/matches`,
  scored in `db.py`'s `score_space_for_lead`), dashboard-only lead signal tags (e.g. "Stalled — no
  contact in N days", driven by `db.STALE_TAG_DAYS`, which now reads the same `NUDGE_STALE_DAYS` env var
  as `main.py`'s nudge job), and a `/dashboard` command for returning users to get a fresh sign-in link.
- **Current task:** Both keys are filled in and rotated; the bot is deployed live on
  Railway (see Deploy target above) and confirmed polling Telegram. `eval/score_extraction.py`
  ran 2026-07-18: **93.5% (129/138)**, above the 0.8 gate — all 9 misses are `stage`
  defaulting to `Inquiry` rather than `Qualified`/`unknown` (prompt-tuning item in `ai.py`,
  not a regression). Remaining: a full end-to-end demo-spine walkthrough (including the
  space-matching bot follow-up and the `/dashboard` command), plus a browser click-through
  of the dashboard's matching/signal-tag surfaces.
- **Demo mode (`DEMO_MODE`, default off):** added 2026-07-18 (commit `2bdeec3`). A rep who
  owns no leads reads the platform-wide pipeline so the dashboard demonstrates itself to a
  first-time visitor. `_read_scope()` is the single resolver, used by `/api/overview` and
  `/api/leads`; both emit `demo_mode` in the payload. The scope widening is **read-only** —
  `_load_lead_or_403(..., write=False)` marks the only two endpoints (`/interactions`,
  `/matches`) allowed to serve a seeded lead to a demo visitor, and every mutation still
  requires true ownership because the demo DB is shared. Verified both directions against
  Neon; see `docs/superpowers/specs/2026-07-18-demo-mode-design.md`.
- **Nudge 204 path:** still unverified, and untestable against seeded data by design — the
  endpoint 409s on any rep with a negative `telegram_id` (all seeded reps) before calling
  Telegram. Needs a lead temporarily assigned to a real positive `telegram_id` (user 14).
- **Blocker:** none. Demo mode is deployed and verified in production (`DEMO_MODE=true`,
  build `1568cd3`). Railway commands remain blocked for Claude by the permission classifier,
  so Gaurav runs any future `railway up` — and note it uploads the *working directory*, not
  the pushed commit, so deploy after committing or an older build ships.
- **Last updated:** 2026-07-18
