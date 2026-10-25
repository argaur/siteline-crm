"""
main.py — Entry point for the Stylework B2B Sales CRM.

One FastAPI app serves the dashboard API, the landing signup and the Telegram
webhook (POST /telegram/webhook, see webhook.py). Each update runs on a fresh
python-telegram-bot Application whose state lives in Neon (persistence.py).

Usage:
  vercel (production)     # Vercel serves `app` directly; root directory bot/
  python main.py          # local emergency mode: API + polling, refuses while a webhook is set
"""

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    ApplicationBuilder,
    BasePersistence,
    CallbackQueryHandler,
    ContextTypes,
)

import db
import commands
import flows
import webhook
from persistence import NeonPersistence

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
APP_BASE_URL = os.getenv("APP_BASE_URL", "")
BOT_NAME = os.getenv("BOT_NAME", "")  # e.g. "SitelineCRMbot" (without @)

# Days without activity before a lead counts as stalled (dashboard views and snooze).
# The automatic nudge job was retired with the move to webhooks; managers nudge by hand.
NUDGE_STALE_DAYS = int(os.getenv("NUDGE_STALE_DAYS", "3"))

# Demo mode (off by default — set DEMO_MODE=true only on the presentation deploy).
# A rep who owns no leads reads the platform-wide pipeline instead of an empty one,
# so the dashboard demonstrates itself to a first-time visitor. This deliberately
# widens READ scope across users; it is never applied to writes (see
# _load_lead_or_403), because the demo database is shared and a stray edit would
# corrupt the seeded story for every later visitor.
DEMO_MODE = os.getenv("DEMO_MODE", "").strip().lower() in ("1", "true", "yes")

ACTIVE_STAGES = [s for s in db.STAGES if s not in db.CLOSED_STAGES]

# interactions.type is a Postgres enum with exactly these values — the schema
# has no "dashboard_note", so dashboard notes map onto addnote_command.
VALID_NOTE_SOURCES = {"whatsapp_forward", "voice_note", "screenshot", "addnote_command"}


async def require_user(request: Request) -> Dict[str, Any]:
    """Auth dependency: Bearer header preferred, ?token= fallback (link-embedded)."""
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else None
    if not token:
        token = request.query_params.get("token")
    if not token:
        raise HTTPException(status_code=401, detail="Missing dashboard token.")

    payload = db.verify_dashboard_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid dashboard token.")

    user = await db.get_user_by_id(payload["uid"])
    if user is None:
        raise HTTPException(status_code=401, detail="Unknown user.")
    # Role comes from the DB row, not the token — a stale token can't keep a
    # demoted manager's privileges.
    return user


async def require_manager(user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
    if user.get("role") != "manager":
        raise HTTPException(status_code=403, detail="Manager role required.")
    return user


def _can_touch_lead(user: Dict[str, Any], lead: Dict[str, Any]) -> bool:
    return user.get("role") == "manager" or lead.get("assigned_to") == user["id"]


async def _read_scope(user: Dict[str, Any]):
    """Resolve the pipeline read scope for a dashboard request.

    Returns (assigned_to, demo_mode, pipeline). Managers always read
    platform-wide. A rep normally reads only their own leads — but under
    DEMO_MODE, a rep who owns nothing falls back to platform-wide so a
    first-time visitor sees a working dashboard instead of an empty shell.

    The pipeline is returned alongside the scope because resolving the scope
    requires fetching it; callers reuse this instead of querying again.
    """
    own = await db.get_all_leads(assigned_to=user["id"])
    owns_nothing = not any(own.values())

    if user.get("role") == "manager":
        # Managers always read platform-wide — that is their job, not a
        # fallback. But a manager who owns no leads (every account minted by
        # /register) is still looking at seeded data, so it must be labelled:
        # demo_mode keys off ownership, not role.
        return None, DEMO_MODE and owns_nothing, await db.get_all_leads(assigned_to=None)

    if DEMO_MODE and owns_nothing:
        return None, True, await db.get_all_leads(assigned_to=None)
    return user["id"], False, own


def _public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": user["id"],
        "first_name": user["first_name"],
        "email": user.get("email"),
        "company": user.get("company"),
        "role": user["role"],
    }


async def _company_names(leads: List[Dict[str, Any]]) -> Dict[int, str]:
    names: Dict[int, str] = {}
    for company_id in {l["company_id"] for l in leads if l.get("company_id")}:
        company = await db.get_company_by_id(company_id)
        if company:
            names[company_id] = company["name"]
    return names


# ─── Automated stale-lead nudges (Phase 4) ─────────────────────

def _nudge_text(lead: Dict[str, Any], company_name: Optional[str]) -> str:
    days = max(0, (datetime.now(timezone.utc) - lead["last_activity_at"]).days)
    bits = []
    if company_name:
        bits.append(company_name)
    if lead.get("seat_count"):
        bits.append(f"{lead['seat_count']} seats")
    if lead.get("city"):
        bits.append(lead["city"])
    detail = f" ({', '.join(bits)})" if bits else ""
    return f"🔸 {lead['contact_name']}{detail} — {days} days quiet in {lead['stage']}."


def _nudge_keyboard(lead_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Keep chasing ✓", callback_data=f"nudge_ack:{lead_id}"),
        InlineKeyboardButton("Snooze", callback_data=f"nudge_snooze:{lead_id}"),
        InlineKeyboardButton("Mark Lost", callback_data=f"nudge_lost:{lead_id}"),
    ]])


async def _send_nudge(bot, lead: Dict[str, Any], rep: Dict[str, Any],
                      company_name: Optional[str]) -> None:
    await bot.send_message(
        chat_id=rep["telegram_id"],
        text=_nudge_text(lead, company_name),
        reply_markup=_nudge_keyboard(lead["id"]),
    )


async def nudge_callback(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles Keep chasing / Snooze / Mark Lost taps on nudge DMs."""
    query = update.callback_query
    action, lead_id_str = query.data.split(":", 1)
    lead_id = int(lead_id_str)
    base_text = query.message.text if query.message else ""

    try:
        if action == "nudge_ack":
            await query.answer("On it.")
            await query.edit_message_text(f"{base_text}\n✓ Kept on your list — chase it.")

        elif action == "nudge_snooze":
            # db.update_lead's whitelist doesn't include last_activity_at, so the
            # snooze is a logged interaction — log_interaction bumps
            # last_activity_at atomically, which resets the staleness clock
            # without touching the stage. Bonus: the snooze shows in history.
            user = await db.get_user_by_telegram_id(update.effective_user.id)
            await db.log_interaction(
                lead_id=lead_id,
                type="addnote_command",
                raw_content="Snoozed from stale-lead nudge",
                ai_summary=f"Nudge snoozed — follow-up timer reset for {NUDGE_STALE_DAYS} days.",
                user_id=user["id"] if user else None,
            )
            await query.answer(f"Snoozed {NUDGE_STALE_DAYS} days.")
            await query.edit_message_text(f"{base_text}\n💤 Snoozed — I'll check back in {NUDGE_STALE_DAYS} days.")

        elif action == "nudge_lost":
            lead = await db.mark_lost(lead_id)
            await query.answer("Marked Closed-Lost.")
            await query.edit_message_text(
                f"{base_text}\n✗ {lead['contact_name']} marked Closed-Lost."
            )
    except Exception:
        logger.exception(f"[nudge] callback {action} failed for lead {lead_id}")
        await query.answer("Something went wrong — try again.")


# ─── Build the Telegram application ───────────────────────────

def build_application(persistence: BasePersistence) -> Application:
    """Creates the PTB Application and registers all handlers. No job queue:
    the webhook builds one of these per update, so nothing could run between."""
    telegram_app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .persistence(persistence)
        .job_queue(None)
        .build()
    )

    # Register command + callback handlers from commands.py first
    for handler in commands.get_handlers():
        telegram_app.add_handler(handler)

    # Register flow handlers (addnote ConversationHandler before generic text handler)
    for handler in flows.get_handlers():
        telegram_app.add_handler(handler)

    # Nudge buttons — registered after the existing handlers so the documented
    # commands-then-flows order stays untouched (the pattern is disjoint anyway).
    telegram_app.add_handler(
        CallbackQueryHandler(nudge_callback, pattern="^nudge_(ack|snooze|lost):")
    )

    return telegram_app


# ─── FastAPI lifespan ─────────────────────────────────────────

@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    """Nothing to start: the pool opens lazily (init_pool is idempotent) and the
    bot is driven by the webhook. Shutdown closes the pool if one was opened."""
    yield
    await db.close_pool()


# ─── FastAPI app ───────────────────────────────────────────────

app = FastAPI(title="Stylework B2B Sales CRM API", lifespan=lifespan)

# Allow cross-origin requests from GitHub Pages (dashboard + landing page)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://siteline.gauravg.dev",
        "https://argaur.github.io",
        "http://localhost",
        "http://127.0.0.1",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
    ],
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["*"],
)


@app.middleware("http")
async def ensure_pool(request: Request, call_next):
    """A serverless cold start may skip startup, so open the pool per request.
    init_pool is idempotent, and min_size=0 means it opens no connection itself."""
    await db.init_pool()
    return await call_next(request)


# ─── Request/Response schemas ──────────────────────────────────

class RegisterRequest(BaseModel):
    first_name: str
    email: str
    company: str


class RegisterResponse(BaseModel):
    user_id: str
    deep_link: str
    dashboard_token: str


class DashboardLinkRequest(BaseModel):
    telegram_id: Optional[int] = None
    user_id: Optional[int] = None


class StageUpdateRequest(BaseModel):
    stage: str


class NoteRequest(BaseModel):
    content: str
    source: str = "addnote_command"


# ─── Public endpoints ─────────────────────────────────────────

@app.get("/health")
async def health():
    """Health check: confirms the API is up and can reach the database."""
    try:
        pool = await db.init_pool()
        await pool.fetchval("SELECT 1")
        db_status = "ok"
    except Exception:
        logger.exception("Health check: DB unreachable")
        db_status = "unavailable"
    return {"status": "ok", "db": db_status}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """Telegram delivers each update here. Delivery is at-least-once: a failed
    update's claim is released and a 500 tells Telegram to retry it."""
    given = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not TELEGRAM_WEBHOOK_SECRET or not secrets.compare_digest(given, TELEGRAM_WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="Bad webhook secret.")

    # 400, not 500, for a body we can never parse, so Telegram stops retrying it.
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Body is not JSON.")
    update_id = payload.get("update_id") if isinstance(payload, dict) else None
    if not isinstance(update_id, int) or isinstance(update_id, bool):
        raise HTTPException(status_code=400, detail="No integer update_id.")

    if not await db.claim_update(update_id):
        return {"ok": True, "duplicate": True}

    try:
        await webhook.handle_update(payload)
    except Exception:
        logger.exception(f"[webhook] update {update_id} failed; releasing for retry")
        await db.release_update(update_id)
        return JSONResponse(status_code=500, content={"ok": False})

    await db.finish_update(update_id)
    return {"ok": True}


@app.post("/register", response_model=RegisterResponse)
async def register(body: RegisterRequest):
    """
    Called by the landing page signup form.
    Creates a user in Postgres and returns a Telegram deep link + dashboard token.

    users.telegram_id is NOT NULL UNIQUE but web signup happens before Telegram
    is linked, so the row gets a unique negative placeholder (real Telegram IDs
    are positive — no collision). Known demo seam: /start registers by real
    telegram_id, so the web row isn't merged with the Telegram identity yet.

    role="manager" is deliberate for this path: self-signups here are reviewers
    testing the product, not real reps with assigned leads — manager role gets
    them straight to the seeded Team view (funnel/leaderboard/stalled leads)
    instead of a personal pipeline with zero leads assigned to them.
    """
    placeholder_telegram_id = -(secrets.randbelow(2**62) + 1)
    user = await db.create_user(
        telegram_id=placeholder_telegram_id,
        first_name=body.first_name,
        email=body.email,
        company=body.company,
        role="manager",
    )
    logger.info(f"Registered new user: {user['id']} ({body.email})")

    return RegisterResponse(
        user_id=str(user["id"]),
        deep_link=f"https://t.me/{BOT_NAME}?start={user['id']}",
        dashboard_token=db.make_dashboard_token(user["id"], user["role"]),
    )


@app.post("/dashboard-link")
async def dashboard_link(body: DashboardLinkRequest):
    """
    Mints a signed dashboard token for an existing user (by telegram_id or
    users.id). Unauthenticated by design — accepted 1-day-demo simplification,
    same trust level as /register itself.
    """
    if body.telegram_id is not None:
        user = await db.get_user_by_telegram_id(body.telegram_id)
    elif body.user_id is not None:
        user = await db.get_user_by_id(body.user_id)
    else:
        raise HTTPException(status_code=400, detail="Provide telegram_id or user_id.")

    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")

    return {
        "user_id": user["id"],
        "first_name": user["first_name"],
        "role": user["role"],
        "token": db.make_dashboard_token(user["id"], user["role"]),
    }


# ─── Authenticated dashboard API ──────────────────────────────

@app.get("/api/me")
async def api_me(user: Dict[str, Any] = Depends(require_user)):
    return _public_user(user)


@app.get("/api/leads")
async def api_leads(user: Dict[str, Any] = Depends(require_user)):
    """Managers get the team-wide pipeline; reps get only their own leads
    (or the seeded pipeline under DEMO_MODE — see _read_scope)."""
    _, demo_mode, pipeline = await _read_scope(user)

    all_leads = [lead for leads in pipeline.values() for lead in leads]
    company_names = await _company_names(all_leads)
    for lead in all_leads:
        lead["company"] = company_names.get(lead.get("company_id"))

    return {"user": _public_user(user), "pipeline": pipeline, "demo_mode": demo_mode}


async def _load_lead_or_403(
    lead_id: int, user: Dict[str, Any], write: bool = True
) -> Dict[str, Any]:
    """Load a lead, enforcing per-rep ownership.

    `write=False` marks a read-only endpoint, the only kind allowed to serve a
    seeded lead to a demo visitor. Mutations always require true ownership, so
    a demo visitor can browse the seeded pipeline but never alter it.
    """
    lead = await db.get_lead_by_id(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail=f"No lead with id {lead_id}.")
    if _can_touch_lead(user, lead):
        return lead
    if not write and DEMO_MODE:
        _, demo_mode, _ = await _read_scope(user)
        if demo_mode:
            return lead
    raise HTTPException(status_code=403, detail="This lead is assigned to another rep.")


@app.patch("/api/leads/{lead_id}/stage")
async def api_update_stage(
    lead_id: int,
    body: StageUpdateRequest,
    user: Dict[str, Any] = Depends(require_user),
):
    if body.stage not in db.STAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid stage {body.stage!r}. Valid: {', '.join(db.STAGES)}",
        )
    await _load_lead_or_403(lead_id, user)
    await db.update_lead_stage(lead_id, body.stage)
    updated = await db.get_lead_by_id(lead_id)
    updated["company"] = (await _company_names([updated])).get(updated.get("company_id"))
    return updated


@app.post("/api/leads/{lead_id}/notes", status_code=201)
async def api_add_note(
    lead_id: int,
    body: NoteRequest,
    user: Dict[str, Any] = Depends(require_user),
):
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Note content is empty.")
    if body.source not in VALID_NOTE_SOURCES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid source {body.source!r}. Valid: {', '.join(sorted(VALID_NOTE_SOURCES))}",
        )
    await _load_lead_or_403(lead_id, user)
    interaction = await db.log_interaction(
        lead_id=lead_id,
        type=body.source,
        raw_content=content,
        ai_summary=content,
        user_id=user["id"],
    )
    return interaction


@app.get("/api/leads/{lead_id}/interactions")
async def api_lead_interactions(
    lead_id: int,
    user: Dict[str, Any] = Depends(require_user),
):
    await _load_lead_or_403(lead_id, user, write=False)
    return await db.get_interactions(lead_id, limit=20)


@app.get("/api/overview")
async def api_overview(user: Dict[str, Any] = Depends(require_user)):
    """Role-scoped home overview (vision §5). Manager → platform-wide; rep →
    own leads for leads-derived sections. Inventory (org-level) and activity
    (platform-wide read) stay unscoped so a new rep's home is never blank.
    Aggregation mirrors /api/team/funnel exactly (value_of, month-start proxy,
    ACTIVE_STAGES). Under DEMO_MODE a lead-less rep reads platform-wide and the
    payload carries demo_mode=true so the dashboard can label the data as
    seeded — see _read_scope."""
    scope, demo_mode, pipeline = await _read_scope(user)

    stale = await db.get_stale_leads(days=NUDGE_STALE_DAYS, assigned_to=scope)
    spaces = await db.get_all_spaces()
    recent = await db.get_recent_interactions(limit=10)
    users = await db.get_all_users()
    users_by_id = {u["id"]: u for u in users}

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def value_of(lead: Dict[str, Any]) -> float:
        return float(lead.get("est_deal_value") or 0)

    def won_this_month(lead: Dict[str, Any]) -> bool:
        return lead["last_activity_at"] >= month_start

    def heat_score(lead: Dict[str, Any]) -> int:
        return (lead.get("heat_score") or {}).get("score", 0)

    all_leads = [l for leads in pipeline.values() for l in leads]
    company_names = await _company_names(all_leads)
    for lead in all_leads:
        lead["company"] = company_names.get(lead.get("company_id"))
    stale_names = await _company_names(stale)
    for lead in stale:
        lead["company"] = stale_names.get(lead.get("company_id"))

    active_leads = [l for s in ACTIVE_STAGES for l in pipeline[s]]
    won_leads = [l for l in pipeline["Closed-Won"] if won_this_month(l)]

    def move_in_iso(lead: Dict[str, Any]) -> Optional[str]:
        # move_in_date is a free-text TEXT column ("next month", "Q3 2026",
        # a real ISO date, or None) — never a date object. Pass through as-is.
        mid = lead.get("move_in_date")
        return mid if mid else None

    def lead_card(lead: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": lead["id"],
            "company": lead.get("company"),
            "contact_name": lead.get("contact_name"),
            "city": lead.get("city"),
            "seat_count": lead.get("seat_count"),
            "space_type": lead.get("space_type"),
            "stage": lead.get("stage"),
            "est_deal_value": value_of(lead),
            "heat_score": lead.get("heat_score"),
            "move_in_date": move_in_iso(lead),
            "signal_tag": lead.get("signal_tag"),
        }

    funnel = [
        {"stage": s, "count": len(pipeline[s]),
         "value": sum(value_of(l) for l in pipeline[s])}
        for s in db.STAGES
    ]

    heat = {"hot": 0, "warm": 0, "cold": 0}
    for lead in active_leads:
        label = (lead.get("heat_score") or {}).get("label", "Cold").lower()
        if label in heat:
            heat[label] += 1

    city_agg: Dict[str, Dict[str, Any]] = {}
    for lead in active_leads:
        city = lead.get("city")
        if not city:
            continue
        row = city_agg.setdefault(city, {"city": city, "active_count": 0, "pipeline_value": 0.0})
        row["active_count"] += 1
        row["pipeline_value"] += value_of(lead)
    by_city = sorted(city_agg.values(), key=lambda c: c["pipeline_value"], reverse=True)

    hottest = [lead_card(l) for l in sorted(active_leads, key=heat_score, reverse=True)[:4]]
    site_visits = [lead_card(l) for l in sorted(pipeline["Site Visit"], key=heat_score, reverse=True)]

    # attention = stalled ∪ (Hot with move-in ≤14d) ∪ (Negotiation quiet ≥3d), ≤5
    attention_leads: Dict[int, Dict[str, Any]] = {l["id"]: l for l in stale}
    for lead in active_leads:
        label = (lead.get("heat_score") or {}).get("label")
        mid = lead.get("move_in_date")
        if label == "Hot" and mid:
            # move_in_date is free text ("next month", "Q3 2026", ...) as
            # often as it's a real ISO date — only apply the within-14-days
            # urgency flag when it actually parses; otherwise skip silently.
            try:
                move_in = date.fromisoformat(mid)
            except (ValueError, TypeError):
                move_in = None
            if move_in and 0 <= (move_in - now.date()).days <= 14:
                attention_leads[lead["id"]] = lead
    for lead in pipeline["Negotiation"]:
        if max(0, (now - lead["last_activity_at"]).days) >= NUDGE_STALE_DAYS:
            attention_leads[lead["id"]] = lead

    def attention_item(lead: Dict[str, Any]) -> Dict[str, Any]:
        rep = users_by_id.get(lead.get("assigned_to"))
        return {
            "lead_id": lead["id"],
            "company": lead.get("company"),
            "contact_name": lead.get("contact_name"),
            "stage": lead.get("stage"),
            "est_deal_value": value_of(lead),
            "days_stalled": max(0, (now - lead["last_activity_at"]).days),
            "move_in_date": move_in_iso(lead),
            "assigned_to": {"user_id": rep["id"], "name": rep["first_name"]} if rep else None,
        }

    attention = sorted(
        (attention_item(l) for l in attention_leads.values()),
        key=lambda a: a["days_stalled"], reverse=True,
    )[:5]

    demand_by_city: Dict[str, int] = {}
    for lead in active_leads:
        city = lead.get("city")
        seats = lead.get("seat_count")
        if city and seats:
            demand_by_city[city] = demand_by_city.get(city, 0) + int(seats)

    inv_agg: Dict[str, Dict[str, Any]] = {}
    for space in spaces:
        city = space["city"]
        agg = inv_agg.setdefault(city, {
            "city": city, "space_count": 0, "total_seats": 0,
            "available_seats": 0, "min_price_per_seat": None, "demand_seats": 0,
        })
        agg["space_count"] += 1
        agg["total_seats"] += int(space.get("total_seats") or 0)
        agg["available_seats"] += int(space.get("available_seats") or 0)
        price = float(space["price_per_seat"]) if space.get("price_per_seat") is not None else None
        if price is not None:
            agg["min_price_per_seat"] = (
                price if agg["min_price_per_seat"] is None
                else min(agg["min_price_per_seat"], price)
            )
    for city, agg in inv_agg.items():
        agg["demand_seats"] = demand_by_city.get(city, 0)
    inventory = sorted(inv_agg.values(), key=lambda c: c["city"])

    # companies/contacts computed from the already-fetched (scope-respecting)
    # leads — avoids two unscoped COUNT(*) queries that would leak platform
    # totals to a rep. Not shown in the 5-stat band; carried for completeness.
    totals = {
        "pipeline_value": sum(value_of(l) for l in active_leads),
        "active_leads": len(active_leads),
        "closed_won_count": len(won_leads),
        "closed_won_value": sum(value_of(l) for l in won_leads),
        "stalled_count": len(stale),
        "companies": len({l["company_id"] for l in all_leads if l.get("company_id")}),
        "contacts": len(all_leads),
        "seats_demanded": sum(int(l["seat_count"]) for l in active_leads if l.get("seat_count")),
        "seats_available": sum(int(s.get("available_seats") or 0) for s in spaces),
    }

    activity = [
        {
            "id": r["id"], "lead_id": r["lead_id"], "company": r["company"],
            "type": r["type"], "ai_summary": r["ai_summary"], "user_name": r["user_name"],
            "logged_at": r["logged_at"].isoformat() if r["logged_at"] else None,
        }
        for r in recent
    ]

    return {
        "user": _public_user(user),
        "demo_mode": demo_mode,
        "totals": totals,
        "funnel": funnel,
        "heat": heat,
        "by_city": by_city,
        "hottest": hottest,
        "site_visits": site_visits,
        "attention": attention,
        "inventory": inventory,
        "activity": activity,
    }


@app.get("/api/leads/{lead_id}/matches")
async def api_lead_matches(
    lead_id: int,
    user: Dict[str, Any] = Depends(require_user),
):
    """Suggested spaces for a lead (Feature A). Read-only — never touches
    available_seats. Auth semantics inherited from _load_lead_or_403: reps
    see matches only for their own leads, managers for any. An unexpected DB
    error surfaces as FastAPI's standard 500, same as every other endpoint."""
    lead = await _load_lead_or_403(lead_id, user, write=False)
    result = await db.get_matches_for_lead(lead)
    return {"lead_id": lead_id, **result}


@app.get("/api/team/funnel")
async def api_team_funnel(user: Dict[str, Any] = Depends(require_manager)):
    """Manager-only team rollup. Shape matches DASHBOARD_MANAGER_VIEW_SPEC.md §5."""
    pipeline = await db.get_all_leads()
    users = await db.get_all_users()
    stale = await db.get_stale_leads(days=NUDGE_STALE_DAYS)

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def value_of(lead: Dict[str, Any]) -> float:
        return float(lead.get("est_deal_value") or 0)

    # No closed_at column exists; mark_won/mark_lost set last_activity_at = now(),
    # so last_activity_at is the honest available proxy for the close date.
    def won_this_month(lead: Dict[str, Any]) -> bool:
        return lead["last_activity_at"] >= month_start

    funnel = [
        {
            "stage": stage,
            "count": len(pipeline[stage]),
            "value": sum(value_of(l) for l in pipeline[stage]),
        }
        for stage in db.STAGES
    ]

    active_leads = [l for s in ACTIVE_STAGES for l in pipeline[s]]
    won_leads = [l for l in pipeline["Closed-Won"] if won_this_month(l)]
    stale_by_rep: Dict[int, int] = {}
    for lead in stale:
        if lead.get("assigned_to"):
            stale_by_rep[lead["assigned_to"]] = stale_by_rep.get(lead["assigned_to"], 0) + 1

    totals = {
        "pipeline_value": sum(value_of(l) for l in active_leads),
        "active_leads": len(active_leads),
        "closed_won_count": len(won_leads),
        "closed_won_value": sum(value_of(l) for l in won_leads),
        "stalled_count": len(stale),
    }

    all_leads = [l for leads in pipeline.values() for l in leads]
    leads_by_rep: Dict[int, List[Dict[str, Any]]] = {}
    for lead in all_leads:
        if lead.get("assigned_to"):
            leads_by_rep.setdefault(lead["assigned_to"], []).append(lead)

    reps = []
    for u in users:
        if u["role"] != "rep" and u["id"] not in leads_by_rep:
            continue
        rep_leads = leads_by_rep.get(u["id"], [])
        rep_active = [l for l in rep_leads if l["stage"] not in db.CLOSED_STAGES]
        rep_won = [l for l in rep_leads if l["stage"] == "Closed-Won" and won_this_month(l)]
        # Proxy: interactions aren't queryable per-user without a db.py change,
        # so "last activity" = the freshest last_activity_at across the rep's leads.
        last_activity = max((l["last_activity_at"] for l in rep_leads), default=None)
        reps.append({
            "user_id": u["id"],
            "name": u["first_name"],
            "active_leads": len(rep_active),
            "pipeline_value": sum(value_of(l) for l in rep_active),
            "closed_won_count": len(rep_won),
            "closed_won_value": sum(value_of(l) for l in rep_won),
            "stalled_count": stale_by_rep.get(u["id"], 0),
            "last_activity_at": last_activity,
        })
    reps.sort(key=lambda r: r["pipeline_value"], reverse=True)

    company_names = await _company_names(stale)
    users_by_id = {u["id"]: u for u in users}
    stalled = []
    for lead in stale:
        rep = users_by_id.get(lead.get("assigned_to"))
        stalled.append({
            "lead_id": lead["id"],
            "company": company_names.get(lead.get("company_id")),
            "contact_name": lead["contact_name"],
            "seat_count": lead.get("seat_count"),
            "space_type": lead.get("space_type"),
            "city": lead.get("city"),
            "stage": lead["stage"],
            "est_deal_value": float(lead["est_deal_value"]) if lead.get("est_deal_value") else None,
            "days_stalled": max(0, (now - lead["last_activity_at"]).days),
            "signal_tag": lead["signal_tag"],
            "assigned_to": {"user_id": rep["id"], "name": rep["first_name"]} if rep else None,
        })
    stalled.sort(key=lambda s: s["days_stalled"], reverse=True)

    return {
        "period": {"month": now.strftime("%Y-%m")},
        "totals": totals,
        "funnel": funnel,
        "reps": reps,
        "stalled": stalled,
    }


@app.post("/api/leads/{lead_id}/nudge", status_code=204)
async def api_manual_nudge(
    lead_id: int,
    user: Dict[str, Any] = Depends(require_manager),
):
    """Manager-triggered version of the automatic Phase 4 nudge (spec §5 item 7)."""
    lead = await db.get_lead_by_id(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail=f"No lead with id {lead_id}.")
    if not lead.get("assigned_to"):
        raise HTTPException(status_code=409, detail="Lead has no assigned rep to nudge.")

    rep = await db.get_user_by_id(lead["assigned_to"])
    if rep is None or not rep.get("telegram_id") or rep["telegram_id"] < 0:
        raise HTTPException(status_code=409, detail="Assigned rep has no linked Telegram account.")
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=503, detail="Telegram bot token is not configured.")

    company_names = await _company_names([lead])
    try:
        async with Bot(TELEGRAM_BOT_TOKEN) as bot:
            await _send_nudge(bot, lead, rep, company_names.get(lead.get("company_id")))
    except Exception as e:
        logger.exception(f"[nudge] manual nudge failed for lead {lead_id}")
        raise HTTPException(status_code=502, detail=f"Telegram send failed: {e}")
    return Response(status_code=204)


# ─── Local dev entry point ─────────────────────────────────────

async def _run_local() -> None:
    """Local emergency mode: API plus polling in one loop. Polling and a webhook
    cannot both be live, and silently deleting the webhook would take production
    down, so this refuses to start while one is set."""
    port = int(os.getenv("PORT", 8000))
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port))
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN is not set; serving the API only.")
        await server.serve()
        return

    await db.init_pool()
    telegram_app = build_application(NeonPersistence())
    await telegram_app.initialize()
    try:
        info = await telegram_app.bot.get_webhook_info()
        if info.url:
            logger.warning(
                f"A webhook is set ({info.url}). Polling would conflict with it. "
                "Remove it on purpose with deleteWebhook first. Exiting."
            )
            return
        await telegram_app.start()
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot is polling.")
        await server.serve()
        await telegram_app.updater.stop()
        await telegram_app.stop()
    finally:
        await telegram_app.shutdown()
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(_run_local())
