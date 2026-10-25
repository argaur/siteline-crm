import base64
import hashlib
import hmac
import json
import os
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import asyncpg
from dotenv import load_dotenv

load_dotenv()

# Canonical pipeline stage list — single source of truth for the whole app.
# ai.py's extraction may return "unknown" as a fallback; that value is
# extraction-only and must never be persisted, which stage validation enforces.
STAGES = [
    "Inquiry",
    "Qualified",
    "Site Visit",
    "Proposal",
    "Negotiation",
    "Closed-Won",
    "Closed-Lost",
]

CLOSED_STAGES = ["Closed-Won", "Closed-Lost"]

_pool: Optional[asyncpg.Pool] = None


async def init_pool() -> asyncpg.Pool:
    """Create the module-wide connection pool.

    Call once at startup (main.py lifespan) before any other db.* function.
    """
    global _pool
    if _pool is None:
        dsn = os.getenv("DATABASE_URL")
        if not dsn:
            raise RuntimeError("DATABASE_URL is not set")
        # statement_cache_size=0: Neon's pooled endpoint fronts PgBouncer in
        # transaction mode, where server-side prepared statements break.
        # Small pool: on Vercel each warm instance holds its own, and Neon's
        # pooler does the real fan-in.
        _pool = await asyncpg.create_pool(
            dsn, statement_cache_size=0, min_size=0, max_size=2
        )
    return _pool


async def close_pool() -> None:
    """Close the pool. Call once at shutdown (main.py lifespan)."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# ─── Telegram webhook delivery claims ─────────────────────────
# At-least-once: a failed update's row is deleted so Telegram's retry runs it
# again; a row stuck in 'processing' for 2+ minutes (function killed mid-run)
# is taken over by the next retry.

async def claim_update(update_id: int) -> bool:
    """True when this caller should process the update."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM telegram_updates WHERE received_at < now() - interval '7 days'"
        )
        claimed = await conn.fetchval(
            """
            INSERT INTO telegram_updates (update_id, status, received_at)
            VALUES ($1, 'processing', now())
            ON CONFLICT (update_id) DO UPDATE
                SET received_at = now()
                WHERE telegram_updates.status = 'processing'
                  AND telegram_updates.received_at < now() - interval '2 minutes'
            RETURNING update_id
            """,
            update_id,
        )
    return claimed is not None


async def finish_update(update_id: int) -> None:
    await _get_pool().execute(
        "UPDATE telegram_updates SET status = 'done' WHERE update_id = $1", update_id
    )


async def release_update(update_id: int) -> None:
    await _get_pool().execute("DELETE FROM telegram_updates WHERE update_id = $1", update_id)


# ─── PTB persistence store (see persistence.py) ───────────────

async def ptb_state_load(kind: str) -> Dict[str, Any]:
    rows = await _get_pool().fetch("SELECT key, data FROM ptb_state WHERE kind = $1", kind)
    return {r["key"]: json.loads(r["data"]) for r in rows}


async def ptb_state_save(kind: str, key: str, data: Any) -> None:
    await _get_pool().execute(
        """
        INSERT INTO ptb_state (kind, key, data, updated_at) VALUES ($1, $2, $3::jsonb, now())
        ON CONFLICT (kind, key) DO UPDATE SET data = EXCLUDED.data, updated_at = now()
        """,
        kind,
        key,
        json.dumps(data),
    )


async def ptb_state_delete(kind: str, key: str) -> None:
    await _get_pool().execute("DELETE FROM ptb_state WHERE kind = $1 AND key = $2", kind, key)


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("db pool not initialized — call db.init_pool() first")
    return _pool


def _validate_stage(stage: str) -> None:
    if stage not in STAGES:
        raise ValueError(f"Invalid stage: {stage!r} (valid: {', '.join(STAGES)})")


# ─── Signed dashboard tokens (stdlib HMAC, no new dependency) ──
# Deliberate 1-day-demo simplification: a signed, non-expiring token embedded
# in each user's dashboard link, verified per-request. Not OAuth, no
# revocation, no expiry. Lives here (not main.py) so commands.py can mint
# tokens too, without a circular import.

def _signing_key() -> bytes:
    return os.environ["DASHBOARD_TOKEN_SECRET"].encode()


def make_dashboard_token(user_id: int, role: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"uid": int(user_id), "role": role}, separators=(",", ":")).encode()
    ).rstrip(b"=")
    signature = hmac.new(_signing_key(), payload, hashlib.sha256).hexdigest()
    return f"{payload.decode()}.{signature}"


def verify_dashboard_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        payload_b64, signature = token.split(".", 1)
        expected = hmac.new(_signing_key(), payload_b64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        if not isinstance(payload.get("uid"), int):
            return None
        return payload
    except Exception:
        return None


# --- HEAT SCORE ---

def calculate_heat_score(lead: Dict[str, Any]) -> Dict[str, Any]:
    """Compute the dynamic heat score for a lead dict (never stored).

    score = recency (0-60) + engagement (0-15) + deal size (0-25), clamped 0-100.
    - recency: 60 at day 0, minus 6 per day since last activity; a lead with no
      logged interactions gets 0 recency — never-touched leads read cold.
    - engagement: 3 points per logged interaction, capped at 15.
    - deal size: log10-scaled est_deal_value, ~5 points per order of magnitude
      above 1,000, capped at 25 — so a 10,000-seat deal outranks a 4-desk deal
      at equal recency.
    """
    interaction_count = lead.get("interaction_count") or 0
    last_activity = lead.get("last_activity_at")

    if interaction_count > 0 and last_activity is not None:
        days_since = (datetime.now(timezone.utc) - last_activity).days
        recency = max(0, 60 - days_since * 6)
    else:
        recency = 0

    engagement = min(15, interaction_count * 3)

    value = float(lead.get("est_deal_value") or 0)
    value_points = min(25.0, max(0.0, (math.log10(value) - 3) * 5)) if value > 0 else 0.0

    score = int(round(min(100, recency + engagement + value_points)))

    if score >= 70:
        label = "Hot"
    elif score >= 40:
        label = "Warm"
    else:
        label = "Cold"

    return {"score": score, "label": label}


# --- SIGNAL TAG (Feature C) ---

STALE_TAG_DAYS = int(os.getenv("NUDGE_STALE_DAYS", "3"))   # same env var + default as main.py's NUDGE_STALE_DAYS


def _fmt_inr(value) -> str:
    """Compact INR (mirrors flows.fmt_inr — accepted micro-duplication:
    importing flows from db would invert the dependency direction)."""
    v = float(value)
    if v >= 1e7:
        return f"₹{v / 1e7:.1f}Cr"
    if v >= 1e5:
        return f"₹{v / 1e5:.1f}L"
    return f"₹{v:,.0f}"


def lead_signal_tag(lead: Dict[str, Any]) -> str:
    """Pure. One-line 'why this lead, why now' synthesis. Never raises —
    None/missing fields coerce like calculate_heat_score does.

    Deliberate phrasing: "N touches, last Dd ago" (not "N in D days") — a
    first-interaction timestamp would cost a per-lead query; not worth it.
    """
    interaction_count = lead.get("interaction_count") or 0
    if interaction_count == 0:
        return "New — no touches logged yet"

    last_activity = lead.get("last_activity_at")
    days = (datetime.now(timezone.utc) - last_activity).days if last_activity else 0
    heat = lead.get("heat_score") or calculate_heat_score(lead)

    if days >= STALE_TAG_DAYS:
        tag = f"Stalled — no contact in {days} days"
        # What the lead read as when last touched: recency forced to its
        # 60-point max, engagement + deal size unchanged (same formulas as
        # calculate_heat_score). Suffix only when the label actually differs
        # — "Stalled — ..., was Cold" would add nothing.
        engagement = min(15, interaction_count * 3)
        value = float(lead.get("est_deal_value") or 0)
        value_points = (
            min(25.0, max(0.0, (math.log10(value) - 3) * 5)) if value > 0 else 0.0
        )
        potential = int(round(min(100, 60 + engagement + value_points)))
        potential_label = "Hot" if potential >= 70 else "Warm" if potential >= 40 else "Cold"
        if potential_label != heat["label"]:
            tag += f", was {potential_label}"
        return tag

    touches = f"{interaction_count} touch" + ("" if interaction_count == 1 else "es")
    tag = f"{heat['label']} — {touches}, last {days}d ago"
    if lead.get("est_deal_value"):
        tag += f", {_fmt_inr(lead['est_deal_value'])} deal"
    return tag


def _lead_with_heat(row: asyncpg.Record) -> Dict[str, Any]:
    lead = dict(row)
    lead["heat_score"] = calculate_heat_score(lead)
    lead["signal_tag"] = lead_signal_tag(lead)
    return lead


# interaction_count is derived at read time (no stored counter to drift).
_LEAD_SELECT = """
    SELECT l.*,
           (SELECT count(*) FROM interactions i WHERE i.lead_id = l.id)::int
               AS interaction_count
    FROM leads l
"""


# --- USER FUNCTIONS ---

async def create_user(
    telegram_id: int,
    first_name: str,
    email: Optional[str] = None,
    company: Optional[str] = None,
    role: str = "rep",
) -> Dict[str, Any]:
    """Create a user keyed on their Telegram ID. role: 'rep' or 'manager'."""
    row = await _get_pool().fetchrow(
        """
        INSERT INTO users (telegram_id, first_name, email, company, role)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING *
        """,
        int(telegram_id), first_name, email, company, role,
    )
    return dict(row)


async def get_user_by_telegram_id(telegram_id: int) -> Optional[Dict[str, Any]]:
    row = await _get_pool().fetchrow(
        "SELECT * FROM users WHERE telegram_id = $1", int(telegram_id)
    )
    return dict(row) if row else None


async def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    row = await _get_pool().fetchrow("SELECT * FROM users WHERE id = $1", user_id)
    return dict(row) if row else None


async def get_all_users() -> List[Dict[str, Any]]:
    rows = await _get_pool().fetch("SELECT * FROM users ORDER BY joined_at")
    return [dict(r) for r in rows]


# --- COMPANY FUNCTIONS ---

async def find_or_create_company(
    name: str,
    industry: Optional[str] = None,
    city: Optional[str] = None,
) -> Dict[str, Any]:
    """Find a company by case-insensitive exact name, or create it."""
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM companies WHERE lower(name) = lower($1)", name
    )
    if row:
        return dict(row)
    row = await pool.fetchrow(
        "INSERT INTO companies (name, industry, city) VALUES ($1, $2, $3) RETURNING *",
        name, industry, city,
    )
    return dict(row)


async def get_company_by_id(company_id: int) -> Optional[Dict[str, Any]]:
    row = await _get_pool().fetchrow(
        "SELECT * FROM companies WHERE id = $1", company_id
    )
    return dict(row) if row else None


# --- LEAD FUNCTIONS ---

async def create_lead(
    contact_name: str,
    company_id: Optional[int] = None,
    contact_role: Optional[str] = None,
    phone: Optional[str] = None,
    stage: str = "Inquiry",
    seat_count: Optional[int] = None,
    city: Optional[str] = None,
    space_type: Optional[str] = None,
    budget_per_seat: Optional[float] = None,
    est_deal_value: Optional[float] = None,
    move_in_date: Optional[str] = None,
    assigned_to: Optional[int] = None,
    source: Optional[str] = None,
) -> Dict[str, Any]:
    _validate_stage(stage)
    row = await _get_pool().fetchrow(
        """
        INSERT INTO leads (
            contact_name, company_id, contact_role, phone, stage, seat_count,
            city, space_type, budget_per_seat, est_deal_value, move_in_date,
            assigned_to, source
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
        RETURNING *
        """,
        contact_name, company_id, contact_role, phone, stage, seat_count,
        city, space_type, budget_per_seat, est_deal_value, move_in_date,
        assigned_to, source,
    )
    lead = dict(row)
    lead["interaction_count"] = 0
    lead["heat_score"] = calculate_heat_score(lead)
    return lead


async def find_leads(
    partial_name: str,
    assigned_to: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Case-insensitive substring search on contact_name (parameterized ILIKE)."""
    pattern = f"%{partial_name}%"
    if assigned_to is None:
        rows = await _get_pool().fetch(
            _LEAD_SELECT + " WHERE l.contact_name ILIKE $1 ORDER BY l.last_activity_at DESC",
            pattern,
        )
    else:
        rows = await _get_pool().fetch(
            _LEAD_SELECT
            + " WHERE l.contact_name ILIKE $1 AND l.assigned_to = $2"
            + " ORDER BY l.last_activity_at DESC",
            pattern, assigned_to,
        )
    return [_lead_with_heat(r) for r in rows]


async def get_lead_by_id(lead_id: int) -> Optional[Dict[str, Any]]:
    row = await _get_pool().fetchrow(_LEAD_SELECT + " WHERE l.id = $1", lead_id)
    return _lead_with_heat(row) if row else None


async def update_lead_stage(lead_id: int, stage: str) -> Dict[str, Any]:
    _validate_stage(stage)
    row = await _get_pool().fetchrow(
        "UPDATE leads SET stage = $2, last_activity_at = now() WHERE id = $1 RETURNING *",
        lead_id, stage,
    )
    return dict(row)


_UPDATABLE_LEAD_COLUMNS = {
    "contact_name", "contact_role", "phone", "seat_count", "city",
    "space_type", "budget_per_seat", "est_deal_value", "move_in_date",
    "company_id", "assigned_to", "source",
}


async def update_lead(lead_id: int, **fields: Any) -> Dict[str, Any]:
    """Update whitelisted lead columns. Use update_lead_stage() for stage changes."""
    invalid = set(fields) - _UPDATABLE_LEAD_COLUMNS
    if invalid:
        raise ValueError(f"Cannot update lead columns: {', '.join(sorted(invalid))}")
    if not fields:
        raise ValueError("No fields to update")

    columns = sorted(fields)
    assignments = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(columns))
    row = await _get_pool().fetchrow(
        f"UPDATE leads SET {assignments} WHERE id = $1 RETURNING *",
        lead_id, *(fields[col] for col in columns),
    )
    return dict(row)


async def assign_lead(lead_id: int, user_id: Optional[int]) -> Dict[str, Any]:
    """Assign (or unassign, with None) a lead to a user."""
    return await update_lead(lead_id, assigned_to=user_id)


async def get_all_leads(
    assigned_to: Optional[int] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """All leads grouped by stage, heat_score injected.

    assigned_to=None returns the team-wide pipeline (manager view);
    pass a users.id to scope to one rep.
    """
    if assigned_to is None:
        rows = await _get_pool().fetch(
            _LEAD_SELECT + " ORDER BY l.last_activity_at DESC"
        )
    else:
        rows = await _get_pool().fetch(
            _LEAD_SELECT + " WHERE l.assigned_to = $1 ORDER BY l.last_activity_at DESC",
            assigned_to,
        )

    pipeline: Dict[str, List[Dict[str, Any]]] = {stage: [] for stage in STAGES}
    for row in rows:
        pipeline[row["stage"]].append(_lead_with_heat(row))
    return pipeline


async def mark_won(lead_id: int) -> Dict[str, Any]:
    return await update_lead_stage(lead_id, "Closed-Won")


async def mark_lost(lead_id: int) -> Dict[str, Any]:
    return await update_lead_stage(lead_id, "Closed-Lost")


async def get_stale_leads(
    days: int = 3,
    assigned_to: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Open leads with no activity for >= `days` days, heat_score injected."""
    query = (
        _LEAD_SELECT
        + " WHERE l.last_activity_at <= now() - make_interval(days => $1)"
        + " AND NOT (l.stage = ANY($2::lead_stage[]))"
    )
    args: List[Any] = [days, CLOSED_STAGES]
    if assigned_to is not None:
        query += " AND l.assigned_to = $3"
        args.append(assigned_to)
    query += " ORDER BY l.last_activity_at ASC"

    rows = await _get_pool().fetch(query, *args)
    return [_lead_with_heat(r) for r in rows]


# --- INTERACTION FUNCTIONS ---

async def log_interaction(
    lead_id: int,
    type: str,
    raw_content: str,
    ai_summary: str,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Insert an interaction and bump the lead's last_activity_at atomically.

    type: whatsapp_forward | voice_note | screenshot | addnote_command
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO interactions (lead_id, user_id, type, raw_content, ai_summary)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING *
                """,
                lead_id, user_id, type, raw_content, ai_summary,
            )
            await conn.execute(
                "UPDATE leads SET last_activity_at = now() WHERE id = $1", lead_id
            )
    return dict(row)


async def get_interactions(lead_id: int, limit: int = 5) -> List[Dict[str, Any]]:
    rows = await _get_pool().fetch(
        "SELECT * FROM interactions WHERE lead_id = $1 ORDER BY logged_at DESC LIMIT $2",
        lead_id, limit,
    )
    return [dict(r) for r in rows]


async def get_recent_interactions(limit: int = 10) -> List[Dict[str, Any]]:
    """Most-recent interactions platform-wide, joined lead → company → user,
    for the home activity feed. Mirrors get_interactions' style but cross-lead.
    Always platform-wide (activity summaries aren't sensitive between reps and
    a scoped read would blank a new rep's home)."""
    rows = await _get_pool().fetch(
        """
        SELECT i.id, i.lead_id, i.type, i.ai_summary, i.logged_at,
               c.name        AS company,
               u.first_name  AS user_name
        FROM interactions i
        JOIN leads l      ON l.id = i.lead_id
        LEFT JOIN companies c ON c.id = l.company_id
        LEFT JOIN users u     ON u.id = i.user_id
        ORDER BY i.logged_at DESC
        LIMIT $1
        """,
        limit,
    )
    return [dict(r) for r in rows]


# --- SPACE FUNCTIONS ---

async def get_all_spaces(city: Optional[str] = None) -> List[Dict[str, Any]]:
    """Inventory read for the stretch matching feature; optional city filter."""
    if city is None:
        rows = await _get_pool().fetch(
            "SELECT * FROM spaces ORDER BY city, name"
        )
    else:
        rows = await _get_pool().fetch(
            "SELECT * FROM spaces WHERE lower(city) = lower($1) ORDER BY name", city
        )
    return [dict(r) for r in rows]


# --- SPACE MATCHING (Feature A) ---
# Pure functions + one async wrapper (Task 2). Same precedent as
# calculate_heat_score: computed at read time, never stored. Matching is done
# in Python after a single get_all_spaces(city) fetch — the spaces table has
# ~10 rows; SQL ranking is deliberate non-scope (see design doc 2026-07-17).
# available_seats is NEVER decremented here — read-only suggestion engine.

MATCH_REQUIRED_FIELDS = ("city", "seat_count")  # budget/space_type optional


def is_matchable(lead: Dict[str, Any]) -> bool:
    """True if the lead has a city and a positive seat_count."""
    if not lead.get("city"):
        return False
    try:
        return int(lead.get("seat_count") or 0) > 0
    except (TypeError, ValueError):
        return False


def score_space_for_lead(lead: Dict[str, Any], space: Dict[str, Any]) -> Dict[str, Any]:
    """Pure 0-100 match score. Assumes hard filters already passed
    (same city, available_seats >= seat_count).

    Weights: budget fit 45, space type 30, capacity fit 25.
    Returns {"score": int, "reasons": List[str]} — reasons are short human
    strings, ordered [type?, budget?, capacity].
    """
    seat_count = int(lead["seat_count"])
    available = int(space["available_seats"])
    reasons: List[str] = []

    # Budget fit (max 45). NUMERIC columns arrive as Decimal from asyncpg —
    # float() at the boundary, same as calculate_heat_score does.
    budget = float(lead["budget_per_seat"]) if lead.get("budget_per_seat") else None
    price = float(space["price_per_seat"]) if space.get("price_per_seat") else None
    if budget and price:
        over = max(0.0, price / budget - 1)
        budget_points = 45.0 * max(0.0, 1 - over / 0.3)
        if price <= budget:
            pct_under = round((1 - price / budget) * 100)
            if pct_under > 0:
                reasons.append(
                    f"₹{price:,.0f}/seat vs ₹{budget:,.0f} budget ({pct_under}% under)"
                )
            else:
                reasons.append(f"₹{price:,.0f}/seat at budget")
        else:
            reasons.append(f"₹{price:,.0f}/seat ({round(over * 100)}% over budget)")
    else:
        budget_points = 22.0  # either value missing/null → neutral
        if price:
            reasons.append(f"₹{price:,.0f}/seat")

    # Space type (max 30) — preferred, never required. A Managed Office lead
    # should still see a great Private Cabin deal, just ranked lower.
    lead_type = lead.get("space_type")
    if lead_type and space.get("space_type") == lead_type:
        type_points = 30.0
        reasons.insert(0, "Exact space type")
    elif not lead_type:
        type_points = 15.0  # lead has no preference → neutral
    else:
        type_points = 0.0

    # Capacity fit (max 25) — right-sizing: 50 into 60 beats 50 into 500.
    # Hard filter guarantees ratio <= 1, no clamp needed.
    capacity_points = 25.0 * (seat_count / available) if available else 0.0
    reasons.append(f"{available} seats free for {seat_count}")

    return {
        "score": int(round(budget_points + type_points + capacity_points)),
        "reasons": reasons,
    }


async def get_matches_for_lead(lead: Dict[str, Any], limit: int = 3) -> Dict[str, Any]:
    """Top-N space suggestions for a lead. One get_all_spaces(city) fetch,
    then pure filtering/scoring/sorting in Python.

    Returns {"status": str, "matches": List[dict], "largest_available": Optional[int]}:
      ok                   — >=1 space passed hard filters; matches = top-N scored
      not_enough_info      — lead missing city or positive seat_count (normal
                             state for early leads, never an exception)
      no_city_inventory    — zero spaces rows in the lead's city
      undersized_inventory — city rows exist but every available_seats <
                             seat_count; largest_available = max in that city

    Hard filters (disqualify entirely): wrong city (enforced by the
    case-insensitive SQL filter in get_all_spaces) and available_seats <
    seat_count. Everything else only lowers rank — no minimum-score cutoff.
    """
    if not is_matchable(lead):
        return {"status": "not_enough_info", "matches": [], "largest_available": None}

    spaces = await get_all_spaces(lead["city"])
    if not spaces:
        return {"status": "no_city_inventory", "matches": [], "largest_available": None}

    seat_count = int(lead["seat_count"])
    fitting = [s for s in spaces if int(s["available_seats"]) >= seat_count]
    if not fitting:
        return {
            "status": "undersized_inventory",
            "matches": [],
            "largest_available": max(int(s["available_seats"]) for s in spaces),
        }

    scored = []
    for space in fitting:
        match = dict(space)
        # Plain floats so FastAPI/json emit numbers, mirroring what
        # api_team_funnel does for est_deal_value.
        if match.get("price_per_seat") is not None:
            match["price_per_seat"] = float(match["price_per_seat"])
        match.update(score_space_for_lead(lead, space))
        scored.append(match)

    # Sort: score desc, then price asc (nulls last), then higher capacity
    # utilization (tighter fit), then id asc (stable, deterministic).
    scored.sort(key=lambda m: (
        -m["score"],
        m["price_per_seat"] if m.get("price_per_seat") is not None else float("inf"),
        -(seat_count / int(m["available_seats"])),
        m["id"],
    ))
    return {"status": "ok", "matches": scored[:limit], "largest_available": None}
