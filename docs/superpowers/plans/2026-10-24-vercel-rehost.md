# SitelineCRM Vercel Rehost Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring `@SitelineCRMbot` and its dashboard API back online on Vercel, with Telegram webhooks and conversation state stored in Neon.

**Architecture:** `bot/main.py` stays the FastAPI app (Vercel zero-config FastAPI, root `bot/`, same as Trivo's `trivo-api`). A new `POST /telegram/webhook` checks a secret header, claims the `update_id` in Neon, builds a fresh PTB `Application` with a Neon-backed `BasePersistence`, processes the update and shuts the app down. Polling stays only as a local emergency mode.

**Tech Stack:** Python, FastAPI, python-telegram-bot 21.*, asyncpg, Neon Postgres, pytest + pytest-asyncio, Vercel (Git integration), Cloudflare DNS API, gopass.

**Spec:** `docs/superpowers/specs/2026-10-24-vercel-rehost-design.md`

## Global Constraints

- No new features. No change to AI prompts, schema meaning or dashboard UI.
- `nudge_job` is not ported and gets no cron. `POST /api/leads/{id}/nudge` keeps working.
- Landing stays on GitHub Pages.
- Fresh PTB `Application` per webhook request. Never reused while warm.
- At-least-once delivery: `telegram_updates.status` is `processing` / `done`; failures delete the row and return 500; a `processing` row older than 2 minutes is reclaimed; rows older than 7 days are deleted.
- Webhook header `X-Telegram-Bot-Api-Secret-Token` must equal env `TELEGRAM_WEBHOOK_SECRET`.
- `setWebhook` with `allowed_updates=["message","callback_query"]`.
- Vercel: project `siteline-api`, team `argaurs-projects`, root `bot/`, region `iad1`, function max duration 60 s, Git integration on `main`, Ignored Build Step skips when nothing under `bot/` changed. Never `vercel deploy` from the working tree.
- Domain `https://siteline-api.gauravg.dev`, Cloudflare CNAME to `cname.vercel-dns.com`, DNS only (not proxied).
- CORS origins: `https://siteline.gauravg.dev`, `https://argaur.github.io`, plus the existing localhost entries.
- Dashboard link base: `https://siteline.gauravg.dev/dashboard/`.
- DB pool: `min_size=0`, `max_size=2`, keep `statement_cache_size=0`.
- Secrets come from gopass `founder-crm/bot/*` by pipe or temp file, never printed. New secret: `founder-crm/bot/TELEGRAM_WEBHOOK_SECRET` (64 hex chars).
- Do not touch `bot/.env.example` (uncommitted edit) or untracked `bot/deploy/`.
- Repo `argaur/siteline-crm` is PUBLIC. Nothing secret in commits.

## Review Focus

1. **Malformed or non-JSON webhook body.** Expect 400 and no DB claim, so Telegram does not retry forever. Test in Task 4.
2. **Update types the bot has no handler for** (e.g. `edited_message`). Expect 200 and the row marked `done`. Test in Task 4.
3. **A voice note that runs past the 60 s limit.** The row stays `processing`. Telegram's retry within 2 minutes is skipped (200), and a later retry is reclaimed and processed. Pinned by the SQL check in Task 2.
4. **A user mid-`/addcontact` whose next message lands on a cold instance.** The flow continues at the right question. Test in Task 5.
5. **The bot's `/dashboard` reply opened from a phone.** The link points to `siteline.gauravg.dev`, not the dead github.io path. Test in Task 1.

---

### Task 1: Test harness, CORS origin, dashboard link base

**Files:**
- Create: `bot/requirements-dev.txt`, `bot/pytest.ini`, `bot/tests/__init__.py`, `bot/tests/conftest.py`, `bot/tests/test_config.py`
- Modify: `bot/main.py:347-358` (CORS list), `bot/commands.py:19` (`DASHBOARD_BASE_URL`)

**Interfaces:**
- Produces: `conftest.py` sets env `DASHBOARD_TOKEN_SECRET=test-secret`, `TELEGRAM_WEBHOOK_SECRET=hook-secret`, `TELEGRAM_BOT_TOKEN=123456:TEST`, `BOT_NAME=SitelineCRMbot`, `DATABASE_URL=postgresql://unused` before importing `main`. Fixture `client` returns `fastapi.testclient.TestClient(main.app)`.

- [ ] **Step 1: Write failing tests** in `bot/tests/test_config.py`:
  - `test_cors_preflight_allows_siteline_origin`: `client.options("/api/me", headers={"Origin": "https://siteline.gauravg.dev", "Access-Control-Request-Method": "GET"})` returns 200 and `access-control-allow-origin == "https://siteline.gauravg.dev"`.
  - `test_dashboard_link_base_is_siteline_domain`: `commands.DASHBOARD_BASE_URL == "https://siteline.gauravg.dev/dashboard/"`.
- [ ] **Step 2: Run** `cd bot && python -m pytest tests/test_config.py -v`. Expected: 2 FAIL.
- [ ] **Step 3: Implement.** `requirements-dev.txt`: `-r requirements.txt`, `pytest==8.*`, `pytest-asyncio==0.24.*`, `httpx` (TestClient). `pytest.ini`: `asyncio_mode = auto`, `testpaths = tests`. Add the origin to CORS and change `DASHBOARD_BASE_URL`.
- [ ] **Step 4: Run** the same command. Expected: 2 PASS.
- [ ] **Step 5: Commit** `test: add pytest harness; allow siteline origin; point dashboard link at siteline domain`.

### Task 2: Neon tables and DB functions for state and update claims

**Files:**
- Create: `bot/migrations/2026-10-24-webhook-state.sql`, `bot/scripts/verify_webhook_state.py`
- Modify: `bot/schema.sql` (append the same two tables), `bot/db.py:33-46` (pool sizes) and new functions after `close_pool`

**Interfaces:**
- Produces in `db.py`:
  - `async def claim_update(update_id: int) -> bool` : True when this caller should process. Inserts `(update_id, 'processing', now())`; on conflict, takes over only a `processing` row older than 2 minutes (update `received_at`); returns False for `done` rows and fresh `processing` rows. Also runs the 7-day `DELETE`.
  - `async def finish_update(update_id: int) -> None` : sets `status='done'`.
  - `async def release_update(update_id: int) -> None` : deletes the row.
  - `async def ptb_state_load(kind: str) -> Dict[str, Any]` : returns `{key: data}` for that kind.
  - `async def ptb_state_save(kind: str, key: str, data: Any) -> None` : upsert, `data` stored as `jsonb`.
  - `async def ptb_state_delete(kind: str, key: str) -> None`.
- Tables: `telegram_updates(update_id bigint primary key, status text not null check (status in ('processing','done')), received_at timestamptz not null default now())` and `ptb_state(kind text not null, key text not null, data jsonb not null, updated_at timestamptz not null default now(), primary key (kind, key))`.

- [ ] **Step 1: Write** `bot/scripts/verify_webhook_state.py`. It runs against the real DB with `update_id` values below `-1_000_000` and `kind='zz_verify'` (never real ids), asserts each line, and cleans up:
  - first `claim_update(x)` is True, second is False;
  - after `finish_update(x)`, `claim_update(x)` is False;
  - after `release_update(y)` on a claimed `y`, `claim_update(y)` is True again;
  - a row forced to `received_at = now() - interval '3 minutes'` with status `processing` is reclaimed (True);
  - `ptb_state_save` then `ptb_state_load` round-trips `{"a": 1, "n": [1, 2]}`; `ptb_state_delete` removes it.
- [ ] **Step 2: Run it before the migration** (DATABASE_URL piped from gopass `founder-crm/bot/DATABASE_URL`). Expected: FAIL, relation `telegram_updates` does not exist.
- [ ] **Step 3: Implement** the migration SQL (with `IF NOT EXISTS`), the schema.sql append, the pool sizes and the six functions. Apply the migration to Neon project `winter-haze-14475661` with a short asyncpg one-off.
- [ ] **Step 4: Run the script again.** Expected: every assertion prints OK and the cleanup leaves zero `zz_verify` / negative rows.
- [ ] **Step 5: Commit** `feat(db): add telegram_updates claims and ptb_state store`.

### Task 3: Neon-backed PTB persistence

**Files:**
- Create: `bot/persistence.py`, `bot/tests/test_persistence.py`

**Interfaces:**
- Consumes: Task 2's `ptb_state_load/save/delete` signatures (tests use an in-memory fake with the same three coroutines).
- Produces: `class NeonPersistence(telegram.ext.BasePersistence)` with `__init__(self, store=db)` where `store` exposes the three coroutines. `store_data=PersistenceInput(callback_data=False)`. Kinds: `user_data`, `chat_data`, `bot_data`, `conv:<name>`. Conversation keys stored as a JSON list string, e.g. `"[123, 456]"`. Functions `encode(obj) -> Any` / `decode(obj) -> Any` round-trip `datetime` (tz-aware), `date`, `Decimal`, and dicts with int keys.

- [ ] **Step 1: Write failing tests:**
  - `test_user_data_round_trips_across_instances`: instance A `update_user_data(42, {"addcontact": {"name": "Asha"}})`; new instance B over the same fake store, `get_user_data()` returns `{42: {"addcontact": {"name": "Asha"}}}`.
  - `test_conversation_state_round_trips`: A `update_conversation("addcontact", (7, 42), 2)`; B `get_conversations("addcontact") == {(7, 42): 2}`.
  - `test_conversation_end_deletes_key`: `update_conversation("addcontact", (7, 42), None)` then B returns `{}`.
  - `test_encode_round_trips_rich_types`: `decode(encode({"d": Decimal("12.50"), "t": datetime(2026,10,24,tzinfo=timezone.utc), 5: "x"}))` equals the input, including the int key `5`.
  - `test_drop_user_data_removes_row`.
- [ ] **Step 2: Run** `python -m pytest tests/test_persistence.py -v`. Expected: FAIL, module not found.
- [ ] **Step 3: Implement** every abstract method PTB 21 requires. `refresh_*` and `flush` are no-ops apart from what the tests need. `get_callback_data` returns None.
- [ ] **Step 4: Run.** Expected: 5 PASS.
- [ ] **Step 5: Commit** `feat: add Neon-backed PTB persistence`.

### Task 4: Webhook endpoint and per-request application

**Files:**
- Create: `bot/webhook.py`, `bot/tests/test_webhook.py`, `bot/tests/fakes.py` (`FakeStore` and `patched_bot`, specified in Task 5's Interfaces)
- Modify: `bot/main.py` (replace module-level `telegram_app = _build_application()` with `build_application`; add the route), `bot/commands.py:742-753` and `bot/flows.py:768-778` (`persistent=True`)

**Interfaces:**
- Consumes: Task 2 claim functions, Task 3 `NeonPersistence`.
- Produces:
  - `main.build_application(persistence: BasePersistence) -> Application` : same handler order as today (commands, flows, `nudge_callback`), `.job_queue(None)`, no nudge job.
  - `async def webhook.handle_update(payload: dict) -> None` : builds the app with `NeonPersistence()`, `initialize()`, `process_update(Update.de_json(payload, app.bot))`, then `shutdown()` in `finally` (which flushes persistence).
  - Route `POST /telegram/webhook` in `main.py`: wrong or missing secret → 403, nothing claimed; body not a JSON object or no int `update_id` → 400; `claim_update` False → 200 `{"ok": true, "duplicate": true}`; `handle_update` raises → `release_update`, 500; success → `finish_update`, 200 `{"ok": true}`.

- [ ] **Step 1: Write failing tests** (monkeypatch `db.claim_update/finish_update/release_update` and `webhook.handle_update` with recorders):
  - `test_webhook_rejects_missing_secret` and `test_webhook_rejects_wrong_secret`: 403, claim not called.
  - `test_webhook_rejects_non_json_body`: raw `b"not json"` with the right secret → 400, claim not called.
  - `test_webhook_duplicate_is_skipped`: claim returns False → 200, `handle_update` not called.
  - `test_webhook_success_marks_done`: claim True → 200, `finish_update(update_id)` called once.
  - `test_webhook_failure_releases_for_retry`: `handle_update` raises → 500, `release_update` called, `finish_update` not called.
  - `test_unhandled_update_type_is_done`: real `handle_update` with Task 5's patched bot and an `edited_message` payload → 200 and `finish_update` called.
- [ ] **Step 2: Run** `python -m pytest tests/test_webhook.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `build_application`, `webhook.py`, the route and `persistent=True` on both ConversationHandlers.
- [ ] **Step 4: Run** the full suite `python -m pytest -v`. Expected: all PASS.
- [ ] **Step 5: Commit** `feat: Telegram webhook with secret check, update claims and per-request app`.

### Task 5: Conversation survives a cold start

**Files:**
- Create: `bot/tests/test_conversation_cold_start.py`

**Interfaces:**
- Consumes: Task 4 `webhook.handle_update` and `tests/fakes.py`, Task 3 `NeonPersistence`.
- Specifies (built in Task 4): `tests/fakes.py` with `FakeStore` (in-memory `ptb_state_*`) and fixture `patched_bot` that monkeypatches `telegram.Bot.get_me` (returns `User(1, "Siteline", True, username="SitelineCRMbot")`), `Bot.initialize`, `Bot.shutdown`, and records `Bot.send_message` calls. `db.get_user_by_telegram_id` patched to return a registered rep.

- [ ] **Step 1: Write the failing test** `test_addcontact_continues_after_cold_start`: three separate `handle_update` calls (each builds a fresh Application over the same `FakeStore`) for `/addcontact`, `"Asha Rao"`, `"Acme Corp"`. Assert the third reply is the stage question that `addcontact_company` sends, and the store's `conv:addcontact` key `"[<chat>, <user>]"` holds the `ADD_STAGE` state.
- [ ] **Step 2: Run.** Expected: FAIL before Task 4's `persistent=True` (if run out of order) or on any persistence bug.
- [ ] **Step 3: Fix** whatever the test exposes in `persistence.py` or `webhook.py`. No test-only branches in product code.
- [ ] **Step 4: Run** `python -m pytest -v`. Expected: all PASS.
- [ ] **Step 5: Commit** `test: prove addcontact flow survives a cold start`.

### Task 6: Manual nudge without a running app; retire polling lifespan

**Files:**
- Modify: `bot/main.py` (lifespan, `nudge_job` removal, `api_manual_nudge`, `__main__`), `bot/requirements.txt` (drop the `[job-queue]` extra), `bot/Procfile` and `bot/railway.json` (delete)
- Test: `bot/tests/test_nudge_endpoint.py`

**Interfaces:**
- Produces: `api_manual_nudge` sends through `telegram.Bot(TELEGRAM_BOT_TOKEN)` used as an async context manager; 503 only when `TELEGRAM_BOT_TOKEN` is unset. Lifespan only closes the DB pool on shutdown. `python main.py` runs local polling: `deleteWebhook` is NOT called automatically; it prints a warning and exits if a webhook is set (`get_webhook_info().url` non-empty).

- [ ] **Step 1: Write failing tests:** `test_manual_nudge_sends_without_running_app` (manager token, lead with a positive-telegram_id rep, patched `Bot.send_message` called once, response 204) and `test_manual_nudge_503_without_token`.
- [ ] **Step 2: Run.** Expected: first test FAIL with 503.
- [ ] **Step 3: Implement.** Remove `nudge_job` and its scheduling. Keep `_send_nudge` and `nudge_callback`.
- [ ] **Step 4: Run** `python -m pytest -v`. Expected: all PASS.
- [ ] **Step 5: Commit** `refactor: drop polling lifespan and nudge job; nudge endpoint sends directly`.

### Task 7: Vercel project, env vars, preview check

**Files:**
- Create: `bot/.vercelignore` (`.env`, `.venv/`, `deploy/`, `eval/`, `tests/`, `__pycache__/`)
- Modify: `bot/CLAUDE.md` and root `CLAUDE.md` Deployment and Environment sections (new target, new env var, Railway retired)

**Interfaces:**
- Produces: Vercel project `siteline-api` linked to `argaur/siteline-crm`, root `bot/`, region `iad1`, max duration 60 s, Ignored Build Step `git diff --quiet HEAD^ HEAD -- .` (run from `bot/`). Env vars for Production and Preview: `TELEGRAM_BOT_TOKEN`, `BOT_NAME`, `DATABASE_URL`, `OPENAI_API_KEY`, `DASHBOARD_TOKEN_SECRET`, `TELEGRAM_WEBHOOK_SECRET`, `APP_BASE_URL=https://siteline-api.gauravg.dev`, `DEMO_MODE=true`.

- [ ] **Step 1:** Generate `TELEGRAM_WEBHOOK_SECRET` into gopass (64 hex, round-trip check).
- [ ] **Step 2:** Create and link the project (`vercel project add`, `vercel link`, Git connect), then set root, region, duration and the Ignored Build Step. Verify with `vercel project inspect siteline-api`: root `bot`, region `iad1`.
- [ ] **Step 3:** Push each env var from gopass through a no-newline temp file (same pattern as Trivo). Verify names with `vercel env ls`.
- [ ] **Step 4:** Push the branch and open a PR. Vercel builds a preview. Verify `GET <preview>/health` returns `{"status":"ok","db":"ok"}`, and a CORS preflight from `https://siteline.gauravg.dev` succeeds. Preview is behind Vercel protection, so use `vercel curl` or a bypass token.
- [ ] **Step 5: Commit** docs and `.vercelignore` on the same branch.

### Task 8: Cutover

**Files:**
- Modify: `landing/index.html:1318` (`RAILWAY_URL` renamed to `API_URL` = `https://siteline-api.gauravg.dev`)

- [ ] **Step 1:** Gaurav confirms the PR. Merge to `main`. Verify the production deployment `/health` on the `*.vercel.app` URL.
- [ ] **Step 2:** Add domain `siteline-api.gauravg.dev` to the Vercel project. Create the Cloudflare CNAME with the API token from gopass `cloudflare/DNS_API_TOKEN` (`proxied: false`). Verify `https://siteline-api.gauravg.dev/health` returns `db: ok`.
- [ ] **Step 3:** `setWebhook` to `https://siteline-api.gauravg.dev/telegram/webhook` with the secret and `allowed_updates`. Verify `getWebhookInfo`: URL set, `pending_update_count` 0 after a minute, no `last_error_message`.
- [ ] **Step 4:** `gh variable set API_BASE_URL -R argaur/siteline-crm --body https://siteline-api.gauravg.dev`, commit the `landing/index.html` change, and push. Verify the live `https://siteline.gauravg.dev/dashboard/config.js` contains the new URL.
- [ ] **Step 5:** Gaurav live checks, one at a time: `/start`; a full `/addcontact` with a throwaway name; one forwarded message; `/dashboard` link opens a signed-in dashboard; then delete the throwaway lead. Claude checks Vercel runtime logs for errors after each.
- [ ] **Step 6:** Evidence first, then ask: show that the Railway `founder-crm` project serves nothing. Gaurav deletes it only on an explicit yes. Update `project_stack_migration.md`.
