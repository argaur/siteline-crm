# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Framework state
- **Framework:** Blueprint. There is only one, so this is not a choice.
- **Rubric score:** none. `/rubric` was skipped in July because the ship-by date of 2026-07-17
  did not allow for it. That date has passed, so the reason no longer holds. **Open: run
  `/rubric` and record the score and date here.** The contract has no permanent skip.
- Vertical chosen: **Stylework B2B Sales**. Working off `IMPLEMENTATION_PLAN.md`. See
  `memory/decision-b2b-vertical-and-db-migration.md` for the reasoning this plan is built on.

**Agent policy: removed 2026-08-07.** This file used to set a standing split, Fable 5 for
planning agents and Opus for execution agents. Claude Code 2.1.219+ suppresses subagents on
Opus 5 unless they are asked for, so a standing policy no longer fires at all. Reading it
would tell a session that dispatch is configured when it is not. Name the agent when you want
one. The original wording is in `memory/feedback-fable5-only.md`.

## What this is
Merged from two previously separate repos (`founder-crm-bot`, `founder-crm-landing`)
on 2026-07-16, to serve as the reusable base for a Stylework referral proposed
solution — see `STYLEWORK_CONTEXT.md` for the full brief and problem statement
before starting any build work.

## Layout
```
bot/       — Telegram bot backend (Python/FastAPI), deploys to Railway
landing/   — Landing page + dashboard (static HTML/JS), deploys to GitHub Pages
```

## Commands

```bash
# Bot — local dev
cd bot
pip install -r requirements.txt
python main.py                          # starts bot (polling) + FastAPI on port 8000

# Bot — Railway (production)
uvicorn main:app --host 0.0.0.0 --port $PORT   # Railway Procfile command

# Bot — seed demo data
python seed.py --seed                   # 1 manager + 2 reps + 13 leads across all 7 stages
python seed.py --clear                  # removes only the rows seed.py created

# Bot — extraction accuracy eval
python eval/score_extraction.py                      # print field-level accuracy
python eval/score_extraction.py --verbose             # + per-example pass/fail
python eval/score_extraction.py --min-accuracy 0.8    # exit 1 if below threshold

# Landing — local dev (no build step)
cd landing
python -m http.server 8080    # then open http://localhost:8080
```

No end-to-end test suite exists for either side — verify bot/API changes by running
`bot/main.py` locally and doing real `curl` round-trips (or against Telegram directly
for bot commands); verify the dashboard by serving `landing/` statically alongside a
locally-running `bot/main.py`. See `bot/CLAUDE.md` and `landing/CLAUDE.md` for the
full detail on each surface — this file only covers what's shared/cross-cutting.

## Architecture

### bot/ — five source files, single asyncio process, fully async
```
main.py      — FastAPI lifespan wires everything: bot polling (if TELEGRAM_BOT_TOKEN
               set — else API-only mode) + nudge JobQueue on startup, /api/* endpoints
db.py        — asyncpg queries against Neon Postgres (ASYNC — always await these calls)
ai.py        — AsyncOpenAI (gpt-4o-mini + Whisper) (ASYNC — always await)
commands.py  — All slash command handlers (/pipeline, /context, /won, /lost, /ask, /addcontact)
flows.py     — All message capture handlers (forwarded text, voice, image, /addnote, /note)
```

### landing/ — static, no framework, no bundler
```
index.html       — Landing page (marketing) + signup form — DESIGN_BRIEF.md reference impl
dashboard/       — Dashboard SPA: rep pipeline view + role-gated manager ("Team") view,
                   authenticated against bot/'s /api/* endpoints, config in gitignored config.js
style.css        — Shared styles (minimal — most styling is inline per-page)
assets/          — Static assets
```

## Critical Constraints

**Async everywhere:** `bot/db.py` and `bot/ai.py` are fully async — `asyncpg` and
`AsyncOpenAI` both support it natively. Every `db.*`/`ai.*` call must be `await`ed.
(This inverts an earlier sync-Airtable-era constraint — see `bot/CLAUDE.md` if you
find stale references to "never await" anywhere.)

**Handler registration order in `bot/main.py`:**
1. `commands.get_handlers()` — all slash commands + ConversationHandler for `/addcontact`
2. `flows.get_handlers()` — ConversationHandler for `/addnote` must come before the
   generic `MessageHandler(filters.TEXT)`, the catch-all for forwarded messages

If the order is swapped, text replies during ConversationHandler flows get
intercepted by the forward/text capture handler.

**Lead record structure:** `db.py` returns flat dicts (Postgres rows), not Airtable's
nested `{id, fields: {...}}` shape — `lead["id"]` is a Postgres bigserial int,
`lead["contact_name"]`/`lead["stage"]`/`lead["seat_count"]`/etc. are top-level keys,
and `lead["heat_score"]` (`{"score": int, "label": str}`) is computed dynamically by
`db.calculate_heat_score()`, never stored. Full detail in `bot/CLAUDE.md`.

**Callback data separator:** Uses `:` (colon) throughout, not `_` (underscore), for
Telegram inline-keyboard callback data — kept from the original design even though
Postgres ids are plain integers now (no longer strictly required for id-safety, but
changing it isn't in scope). Always `split(":", 1)` or `split(":", 2)`.

**Dashboard config:** `landing/dashboard/config.js` is gitignored — copy
`config.example.js` to `config.js` and fill in `API_BASE_URL` (no credentials belong
in this file anymore — the dashboard authenticates via signed per-user tokens against
`bot/main.py`, not a database credential shipped to the browser). A prior version of
this dashboard called Airtable directly from the browser with a client-side PAT — a
real credential-exposure bug, now closed; see `memory/project-founder-crm-security-note.md`
and `landing/CLAUDE.md` for the auth flow that replaced it.

## Data Flow: Capture Path

Forwarded text → `forward_or_text_handler` → `ai.extract_from_text()` →
`ai.evaluate_note_quality()` → `_save_capture()`

Voice note → `voice_handler` → Whisper transcription → `ai.classify_intent()` →
if "recall": generate brief; if "capture": `ai.extract_from_voice()` →
`_save_capture()` (no quality gate for voice)

Image/screenshot → `image_handler` → `ai.extract_from_image()` (gpt-4o-mini vision) →
`_save_capture()`

`_save_capture()` in `flows.py` is the shared write path: find-or-create company +
lead, `db.log_interaction()` (transactionally bumps `last_activity_at`), then sends a
confirmation card with "Looks good / Edit stage" inline keyboard. It never
auto-regresses a lead's stage backwards from a vague recapture.

## Postgres Schema (`bot/schema.sql`)

Five tables: `users`, `companies`, `leads`, `interactions`, `spaces`.

- `leads.stage` — 7-value enum: `Inquiry`, `Qualified`, `Site Visit`, `Proposal`,
  `Negotiation`, `Closed-Won`, `Closed-Lost` (single source of truth: `db.STAGES`)
- `leads.space_type` — enum: `Dedicated Desk`, `Private Cabin`, `Managed Office`, `Day Pass`
- `leads.heat_score` is NOT a column — computed dynamically, see above
- `interactions.type` — enum: `whatsapp_forward`, `voice_note`, `screenshot`,
  `addnote_command` (dashboard-created notes map onto `addnote_command`, there is no
  separate `dashboard_note`/`manual_note` value)
- `spaces` — inventory rows for the (stretch, not built) matching feature, seeded once
  by `bot/scripts/apply_schema.py`
- Demo data (1 manager + 2 reps + 13 leads across all 7 stages) lives under fixed
  negative `telegram_id`s seeded by `bot/seed.py --seed`

## Environment Variables

```
TELEGRAM_BOT_TOKEN      # blank → bot starts in API-only mode (dashboard/API still work)
BOT_NAME                # Telegram bot username without @ — used for deep links
DATABASE_URL            # Neon pooled connection string
OPENAI_API_KEY          # required for all AI: extraction/reasoning (gpt-4o-mini) + transcription (Whisper)
APP_BASE_URL            # Railway URL — used in /start deep link and signup redirect
DASHBOARD_TOKEN_SECRET  # HMAC signing key for dashboard tokens
```

## Deployment
**Both targets are on hosts estate.md retired on 2026-07-31.** Recorded here because it is where the
code runs today, not because it is where it should run. Moving them is open work under "continue
stack migration": a FastAPI service needs a server, so the bot goes to Vercel or the Oracle VM, and
the static landing page goes to Cloudflare. Do not add new deployment work on either host.
- `bot/` → Railway (`Procfile`, `railway.json`), FastAPI served via `uvicorn main:app`
- `landing/` → GitHub Pages (https://argaur.github.io/siteline-crm/)

## Status
- **State:** Phases 0-7 built and live-verified on branch `stylework-migration`.
  Neon Postgres schema, fully async `db.py`/`ai.py`/`commands.py`/`flows.py`/`main.py`,
  nudge job, all `/api/*` endpoints, HMAC dashboard auth (Phases 0-5). Dashboard fully
  rewired off direct-Airtable onto the authenticated API, restyled to `DESIGN_BRIEF.md`'s
  light/monochrome system, manager ("Team") view built — all live-verified via real
  `curl` round-trips against Neon, including 403 enforcement for non-manager roles
  (Phase 6). `seed.py` rewritten for the new schema (1 manager + 2 reps + 13 leads
  across all 7 stages, several cities) and live-verified; `eval/dataset.jsonl` +
  `score_extraction.py` ported to the coworking domain and the new extraction call,
  but not yet run — see Blocker (Phase 7). Both `CLAUDE.md`s and this file's docs
  brought current, `bot/README.md` rewritten as a portfolio case-study section
  (Phase 8). AI provider consolidated from Anthropic+OpenAI onto OpenAI alone
  (`gpt-4o-mini` for extraction/reasoning + Whisper for transcription, one API key,
  Gaurav's call on 2026-07-17 to reduce accounts to manage) — `anthropic` dropped from
  `requirements.txt`, `ANTHROPIC_API_KEY` removed everywhere. A follow-up plan then
  shipped the auto space-matching engine (bot follow-up message after capture +
  `GET /api/leads/{id}/matches` + a dashboard "Suggested Matches" card on contact
  detail), dashboard-only lead signal tags across the pipeline/follow-ups/stalled views,
  and a `/dashboard` bot command sending returning users a fresh sign-in link.
  A **demo mode** then shipped (2026-07-18, commit `2bdeec3`): under the `DEMO_MODE`
  env flag a visitor who owns no leads reads the seeded platform-wide pipeline
  instead of an empty dashboard, labelled by a sample-data banner and clearable via
  a per-browser preference. Reads only — cross-user writes stay 403 in both modes.
  See `docs/superpowers/specs/2026-07-18-demo-mode-design.md`.
  Demo mode is **fully deployed and verified in production** (2026-07-18):
  `DEMO_MODE=true` on Railway, build `1568cd3` live. A later fix made `demo_mode`
  key off lead *ownership* rather than role — `/register` mints every new account
  as a `manager`, and managers were bypassing the banner entirely, so a reviewer
  signing up saw seeded data with no label at all.
- **Renamed to SitelineCRM (2026-07-18, commit `aa43678`).** Harshit's review asked
  for one name; the product had three live at once. Wordmark is now `SITELINECRM`,
  the bot is `@SitelineCRMbot` (a **new** bot — Telegram cannot rename a bot's
  username, so the old `@RethinkCRMbot` was recreated, meaning a new
  `TELEGRAM_BOT_TOKEN`), and the repo/Pages path is `argaur/siteline-crm`. The old
  Pages URL now 404s by design. Deliberately NOT renamed: the Railway service
  hostname (`founder-crm-bot-production.up.railway.app`, never on screen) and the
  `siteline.demoCleared` / `siteline_wx_` localStorage keys.
- **Current task:** confirm the renamed deep link end to end (`/dashboard` on the
  new bot must return a `siteline-crm` link — this needs the Railway redeploy),
  plus the still-outstanding demo-spine walkthrough and browser click-through of
  the demo banner/clear flow and space-matching/signal-tag surfaces.
- **Extraction eval:** run 2026-07-18 — **93.5% (129/138)**, above the 0.8 gate.
  All 9 misses are `stage` defaulting to `Inquiry` instead of `Qualified`/`unknown`;
  a prompt-tuning item in `ai.py`, not a regression.
- **Nudge 204 path:** untestable against seeded data by design — `main.py` rejects
  reps with negative `telegram_id` (all seeded reps) with a 409 before calling
  Telegram. Testing it needs a lead temporarily assigned to a real positive
  `telegram_id` (user 14).
- **Blocker:** none on deploy. Railway commands are blocked for Claude by the
  permission classifier, so any future `railway up` must be run by Gaurav.
  Note `railway up` uploads the *working directory*, not the pushed commit —
  deploy after committing, or an older build ships. **Post-rename, Railway also
  needs `BOT_NAME=SitelineCRMbot` and the new bot's `TELEGRAM_BOT_TOKEN`** — until
  both land plus a redeploy, the bot hands out dead `founder-crm` dashboard links.
- **Last updated:** 2026-07-18

## Model notes
**This section expires. Review it at every model launch and every Claude Code version bump.**
Current as of 2026-08-05: Opus 5 / Sonnet 5 / Fable 5, Claude Code 2.1.222.
Re-checked 2026-08-07 by `Claude Optimisation/scripts/claude-md-eval.sh`. NOT clean: it reports the
Railway deploy target, retired in estate.md, and this file being over the 200-line guidance.
Both are real and both are open. Do not delete the finding; fix the cause.
- Delegation is not automatic. Claude Code 2.1.219 and later suppress subagents on Opus 5 unless
  the user asks for one, so name the agent when you want it.
- Do not add verification, anti-laziness or hedging instructions. These models self-verify, are
  direct by default, and obey a hedge literally by reporting less.
- Reasoning: `Claude Optimisation/docs/setup-versions/artifacts/2026-08-05-model5-migration/`.
