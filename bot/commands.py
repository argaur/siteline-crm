import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.helpers import escape_markdown

import db
import ai

logger = logging.getLogger(__name__)

DASHBOARD_BASE_URL = "https://siteline.gauravg.dev/dashboard/"

# ─── ConversationHandler states for /addcontact ───────────────
ADD_NAME, ADD_COMPANY, ADD_STAGE, ADD_SOURCE = range(4)

OPEN_STAGES = [s for s in db.STAGES if s not in db.CLOSED_STAGES]

# Human-readable labels shown to user → source values stored in DB
SOURCE_LABELS = ["WhatsApp", "Phone / Meeting", "LinkedIn", "Referral", "Other"]
SOURCE_VALUES = ["whatsapp_forward", "manual", "manual", "manual", "other"]


# ─── Utilities ────────────────────────────────────────────────

def md(text) -> str:
    """Escape text for Telegram MarkdownV2."""
    return escape_markdown(str(text) if text is not None else "", version=2)


def fmt_inr(value) -> str:
    v = float(value)
    if v >= 1e7:
        return f"₹{v / 1e7:.1f}Cr"
    if v >= 1e5:
        return f"₹{v / 1e5:.1f}L"
    return f"₹{v:,.0f}"


async def _get_user(telegram_id: int):
    try:
        return await db.get_user_by_telegram_id(telegram_id)
    except Exception as e:
        logger.error(f"Error fetching user {telegram_id}: {e}")
        return None


def _pipeline_scope(user: dict):
    """Managers see the team-wide pipeline; reps see only their own leads."""
    return None if user.get("role") == "manager" else user["id"]


async def _company_name(lead: dict) -> str:
    if not lead.get("company_id"):
        return ""
    company = await db.get_company_by_id(lead["company_id"])
    return company["name"] if company else ""


async def _company_names(leads: list) -> dict:
    names = {}
    for company_id in {l["company_id"] for l in leads if l.get("company_id")}:
        company = await db.get_company_by_id(company_id)
        if company:
            names[company_id] = company["name"]
    return names


def _lead_details(lead: dict) -> str:
    parts = []
    if lead.get("seat_count"):
        parts.append(f"{lead['seat_count']} seats")
    if lead.get("city"):
        parts.append(lead["city"])
    if lead.get("est_deal_value"):
        parts.append(fmt_inr(lead["est_deal_value"]))
    return " · ".join(parts)


async def _not_registered(update: Update):
    await update.message.reply_text(
        "You're not registered yet\\. Send /start to set up your account\\.",
        parse_mode="MarkdownV2",
    )


# ─── /start ───────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Two cases:
    1. /start from a new user — auto-register (users are keyed on telegram_id)
    2. /start from a returning registered user — welcome back + pipeline count
    """
    telegram_id = update.effective_user.id
    user = await _get_user(telegram_id)

    if user is None:
        first_name = update.effective_user.first_name or "there"
        try:
            user = await db.create_user(telegram_id, first_name)
        except Exception as e:
            logger.error(f"Error registering user {telegram_id}: {e}")
            await update.message.reply_text(
                "Something went wrong during setup\\. Please try /start again\\.",
                parse_mode="MarkdownV2",
            )
            return

        await update.message.reply_text(
            f"You're set up, *{md(user['first_name'])}*\\! "
            "Forward any WhatsApp chat or send a voice note to log your first lead\\.\n\n"
            "Use /help to see all commands\\.",
            parse_mode="MarkdownV2",
        )
        return

    try:
        pipeline = await db.get_all_leads(_pipeline_scope(user))
        active_count = sum(
            len(v) for k, v in pipeline.items() if k not in db.CLOSED_STAGES
        )
    except Exception as e:
        logger.error(f"Pipeline count error for {telegram_id}: {e}")
        active_count = 0

    await update.message.reply_text(
        f"Welcome back, *{md(user['first_name'])}*\\! "
        f"You have *{active_count}* active deal\\(s\\)\\.\n\n"
        "Use /pipeline to see your full view\\.",
        parse_mode="MarkdownV2",
    )


# ─── /dashboard ───────────────────────────────────────────────

async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a returning user a fresh dashboard sign-in link. This is the
    bot-side half of the promise landing/dashboard/index.html's sign-in
    fallback screen already makes ("open your dashboard from the Telegram
    bot") — without this command that promise had no code behind it."""
    telegram_id = update.effective_user.id
    user = await _get_user(telegram_id)
    if not user:
        await _not_registered(update)
        return

    token = db.make_dashboard_token(user["id"], user["role"])
    link = f"{DASHBOARD_BASE_URL}?token={token}"
    await update.message.reply_text(
        f"Here's your dashboard, *{md(user['first_name'])}*\\.\n\n"
        f"[Open Dashboard]({md(link)})",
        parse_mode="MarkdownV2",
        disable_web_page_preview=True,
    )


# ─── /help ────────────────────────────────────────────────────

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*Coworking Sales CRM — Commands*\n\n"
        "/pipeline — View pipeline grouped by stage \\(managers see the whole team\\)\n"
        "/deals — Same as /pipeline\n"
        "/context \\[name\\] — Pre\\-call brief for a lead\n"
        "/ask \\[question\\] — Natural language pipeline query\n"
        "/addnote — Add a note to a deal \\(guided\\)\n"
        "/note \\[text\\] — Quick note to your most recent deal\n"
        "/won \\[name\\] — Mark a deal Closed\\-Won\n"
        "/lost \\[name\\] — Mark a deal Closed\\-Lost\n"
        "/addcontact — Add a lead manually \\(guided\\)\n"
        "/dashboard — Get a fresh link to your dashboard\n"
        "/reassign \\[lead id\\] \\[rep name\\] — Reassign a lead \\(managers only\\)\n"
        "/cancel — Exit any active flow\n\n"
        "Or just *forward a WhatsApp chat* or send a *voice note* — AI captures the deal automatically\\."
    )
    await update.message.reply_text(text, parse_mode="MarkdownV2")


# ─── /pipeline (and /deals) ───────────────────────────────────

async def pipeline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Text Kanban grouped by stage. Shows top 3 per stage sorted by heat score,
    with seat count / city / est. deal value per lead and total open pipeline value.
    """
    telegram_id = update.effective_user.id
    user = await _get_user(telegram_id)
    if not user:
        await _not_registered(update)
        return

    try:
        pipeline_data = await db.get_all_leads(_pipeline_scope(user))
    except Exception as e:
        logger.error(f"Pipeline fetch error: {e}")
        await update.message.reply_text(
            "Couldn't fetch pipeline\\. Try again\\.", parse_mode="MarkdownV2"
        )
        return

    total_active = sum(len(pipeline_data.get(s, [])) for s in OPEN_STAGES)
    won_count = len(pipeline_data.get("Closed-Won", []))
    lost_count = len(pipeline_data.get("Closed-Lost", []))

    if total_active == 0 and won_count == 0 and lost_count == 0:
        await update.message.reply_text(
            "No leads yet\\. Forward a WhatsApp conversation to get started\\.",
            parse_mode="MarkdownV2",
        )
        return

    open_leads = [l for s in OPEN_STAGES for l in pipeline_data.get(s, [])]
    company_names = await _company_names(open_leads)
    pipeline_value = sum(float(l["est_deal_value"] or 0) for l in open_leads)

    title = "*TEAM PIPELINE*" if user.get("role") == "manager" else "*YOUR PIPELINE*"
    lines = [title, "─────────────────────"]

    for stage in OPEN_STAGES:
        leads = pipeline_data.get(stage, [])
        leads_sorted = sorted(
            leads, key=lambda l: l["heat_score"]["score"], reverse=True
        )
        top3 = leads_sorted[:3]

        lines.append(f"*{md(stage.upper())} \\({len(leads)}\\)*")
        if not top3:
            lines.append("  _none_")
        else:
            for lead in top3:
                company = company_names.get(lead["company_id"], "?")
                heat = lead["heat_score"]
                lines.append(
                    f"  • {md(lead['contact_name'])} @ {md(company)} "
                    f"\\[{md(heat['label'])} {heat['score']}\\]"
                )
                details = _lead_details(lead)
                if details:
                    lines.append(f"    {md(details)}")

    lines.append("─────────────────────")
    if pipeline_value > 0:
        lines.append(f"Open pipeline value: {md(fmt_inr(pipeline_value))}")
    lines.append(f"CLOSED\\-WON: {won_count} \\| CLOSED\\-LOST: {lost_count}")

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


# ─── /context [name] ──────────────────────────────────────────

async def context_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Pre-call brief. If multiple leads match, shows an inline keyboard for
    disambiguation. Callback data format: "ctx:{lead_id}"
    """
    telegram_id = update.effective_user.id
    user = await _get_user(telegram_id)
    if not user:
        await _not_registered(update)
        return

    name_query = " ".join(context.args).strip() if context.args else ""

    if not name_query:
        await update.message.reply_text(
            "Which lead? Usage: /context \\[name\\]", parse_mode="MarkdownV2"
        )
        return

    try:
        matches = await db.find_leads(name_query)
    except Exception as e:
        logger.error(f"Lead search error: {e}")
        await update.message.reply_text("Search failed\\. Try again\\.", parse_mode="MarkdownV2")
        return

    if not matches:
        await update.message.reply_text(
            f"No lead found matching _{md(name_query)}_\\.", parse_mode="MarkdownV2"
        )
        return

    if len(matches) > 1:
        company_names = await _company_names(matches[:5])
        buttons = [
            [InlineKeyboardButton(
                f"{lead['contact_name']} @ {company_names.get(lead['company_id'], '?')}",
                callback_data=f"ctx:{lead['id']}",
            )]
            for lead in matches[:5]
        ]
        await update.message.reply_text(
            "Multiple matches found\\. Which one?",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="MarkdownV2",
        )
        return

    await _send_context_brief(update, matches[0], via_callback=False)


async def context_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles disambiguation button press for /context."""
    query = update.callback_query
    await query.answer()
    lead_id = int(query.data.split(":", 1)[1])

    try:
        lead = await db.get_lead_by_id(lead_id)
    except Exception as e:
        logger.error(f"Lead fetch error: {e}")
        lead = None

    if not lead:
        await query.edit_message_text("Couldn't load lead\\.", parse_mode="MarkdownV2")
        return

    await _send_context_brief(update, lead, via_callback=True)


async def _send_context_brief(update: Update, lead: dict, via_callback: bool):
    """Fetches recent interactions and generates an AI pre-call brief."""
    company_name = await _company_name(lead)
    heat = lead["heat_score"]

    try:
        interactions = await db.get_interactions(lead["id"])
        summaries = [r["ai_summary"] for r in interactions if r.get("ai_summary")]

        budget_signal = None
        if lead.get("budget_per_seat"):
            budget_signal = f"₹{float(lead['budget_per_seat']):,.0f}/seat/month"

        brief = await ai.generate_context_brief(
            {
                "contact_name": lead["contact_name"],
                "company": company_name,
                "stage": lead["stage"],
                "heat_score": f"{heat['score']} ({heat['label']})",
                "budget_signal": budget_signal,
            },
            summaries,
        )
    except Exception as e:
        logger.error(f"Brief generation error: {e}", exc_info=True)
        brief = f"Error: {e}"

    facts = [f"Stage: {lead['stage']}"]
    details = _lead_details(lead)
    if details:
        facts.append(details)

    # AI output is escaped to prevent MarkdownV2 parse errors from unpredictable content
    msg = (
        f"*Pre\\-call Brief: {md(lead['contact_name'])}*\n"
        f"{md(' | '.join(facts))}\n\n{md(brief)}"
    )

    if via_callback:
        await update.callback_query.edit_message_text(msg, parse_mode="MarkdownV2")
    else:
        await update.message.reply_text(msg, parse_mode="MarkdownV2")


# ─── /won and /lost ───────────────────────────────────────────

async def won_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _mark_handler(update, context, action="won")


async def lost_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _mark_handler(update, context, action="lost")


async def _mark_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str):
    """
    Shared logic for /won and /lost, with an inline Yes/Cancel keyboard.
    Callback data format: "mark_won:{lead_id}" or "mark_cancel:{lead_id}"
    """
    telegram_id = update.effective_user.id
    user = await _get_user(telegram_id)
    if not user:
        await _not_registered(update)
        return

    name_query = " ".join(context.args).strip() if context.args else ""

    if not name_query:
        await update.message.reply_text(
            f"Usage: /{action} \\[name\\]", parse_mode="MarkdownV2"
        )
        return

    try:
        matches = await db.find_leads(name_query)
    except Exception as e:
        logger.error(f"Lead search error: {e}")
        await update.message.reply_text("Search failed\\. Try again\\.", parse_mode="MarkdownV2")
        return

    if not matches:
        await update.message.reply_text(
            f"No lead found matching _{md(name_query)}_\\.", parse_mode="MarkdownV2"
        )
        return

    lead = matches[0]
    company_name = await _company_name(lead)
    label = "CLOSED-WON" if action == "won" else "CLOSED-LOST"

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"Yes, mark as {label}", callback_data=f"mark_{action}:{lead['id']}"
        ),
        InlineKeyboardButton("Cancel", callback_data=f"mark_cancel:{lead['id']}"),
    ]])

    await update.message.reply_text(
        f"Mark *{md(lead['contact_name'])}* from *{md(company_name or '?')}* as *{md(label)}*?",
        reply_markup=keyboard,
        parse_mode="MarkdownV2",
    )


async def mark_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles Yes/Cancel callbacks for /won and /lost."""
    query = update.callback_query
    await query.answer()

    action_key, lead_id_str = query.data.split(":", 1)
    lead_id = int(lead_id_str)

    if action_key == "mark_cancel":
        await query.edit_message_text("Cancelled\\.", parse_mode="MarkdownV2")
        return

    try:
        if action_key == "mark_won":
            lead = await db.mark_won(lead_id)
        else:
            lead = await db.mark_lost(lead_id)

        label = "CLOSED-WON" if action_key == "mark_won" else "CLOSED-LOST"
        note_tip = "Add a win note: /addnote" if action_key == "mark_won" else "Log what happened: /addnote"

        await query.edit_message_text(
            f"*{md(lead['contact_name'])}* marked as *{md(label)}*\\.\n\n{md(note_tip)}",
            parse_mode="MarkdownV2",
        )
    except Exception as e:
        logger.error(f"Mark callback error ({action_key}): {e}")
        await query.edit_message_text("Update failed\\. Try again\\.", parse_mode="MarkdownV2")


# ─── /ask [question] ──────────────────────────────────────────

async def ask_pipeline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Natural language Q&A over the pipeline.
    Serializes all leads as plain text, passes to ai.answer_pipeline_query().
    """
    telegram_id = update.effective_user.id
    user = await _get_user(telegram_id)
    if not user:
        await _not_registered(update)
        return

    question = " ".join(context.args).strip() if context.args else ""

    if not question:
        await update.message.reply_text(
            "Ask me anything about your pipeline\\.\n"
            "E\\.g\\. /ask who should I follow up today?",
            parse_mode="MarkdownV2",
        )
        return

    try:
        pipeline_data = await db.get_all_leads(_pipeline_scope(user))
        all_leads = [l for leads in pipeline_data.values() for l in leads]
        company_names = await _company_names(all_leads)
        users_by_id = {u["id"]: u["first_name"] for u in await db.get_all_users()}
    except Exception as e:
        logger.error(f"Pipeline fetch error: {e}")
        await update.message.reply_text("Couldn't fetch pipeline data\\.", parse_mode="MarkdownV2")
        return

    lines = []
    for lead in all_leads:
        heat = lead["heat_score"]
        parts = [
            f"{lead['contact_name']} @ {company_names.get(lead['company_id'], 'unknown company')}",
            f"Stage: {lead['stage']}",
            f"Heat: {heat['score']} ({heat['label']})",
        ]
        if lead.get("seat_count"):
            parts.append(f"Seats: {lead['seat_count']}")
        if lead.get("city"):
            parts.append(f"City: {lead['city']}")
        if lead.get("est_deal_value"):
            parts.append(f"Est value: {fmt_inr(lead['est_deal_value'])}")
        rep = users_by_id.get(lead.get("assigned_to"))
        if rep:
            parts.append(f"Rep: {rep}")
        lines.append("- " + " | ".join(parts))

    pipeline_context = "\n".join(lines) if lines else "No leads in pipeline."

    try:
        answer = await ai.answer_pipeline_query(question, pipeline_context)
    except Exception as e:
        logger.error(f"AI query error: {e}")
        await update.message.reply_text("AI query failed\\. Try again\\.", parse_mode="MarkdownV2")
        return

    await update.message.reply_text(md(answer), parse_mode="MarkdownV2")


# ─── /reassign [lead id] [rep name] — manager only ────────────

async def reassign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user = await _get_user(telegram_id)
    if not user:
        await _not_registered(update)
        return

    if user.get("role") != "manager":
        await update.message.reply_text(
            "Only managers can reassign leads\\.", parse_mode="MarkdownV2"
        )
        return

    args = context.args or []
    usage = "Usage: /reassign \\[lead id\\] \\[rep name\\]"
    if len(args) < 2:
        await update.message.reply_text(usage, parse_mode="MarkdownV2")
        return

    try:
        lead_id = int(args[0])
    except ValueError:
        await update.message.reply_text(usage, parse_mode="MarkdownV2")
        return

    rep_query = " ".join(args[1:]).strip()

    try:
        lead = await db.get_lead_by_id(lead_id)
        if not lead:
            await update.message.reply_text(
                f"No lead with ID {lead_id}\\.", parse_mode="MarkdownV2"
            )
            return

        users = await db.get_all_users()
        rep = next(
            (u for u in users if u["first_name"].lower() == rep_query.lower()), None
        )
        if not rep:
            names = ", ".join(u["first_name"] for u in users) or "none"
            await update.message.reply_text(
                f"No rep named _{md(rep_query)}_\\. Team: {md(names)}\\.",
                parse_mode="MarkdownV2",
            )
            return

        await db.assign_lead(lead_id, rep["id"])
    except Exception as e:
        logger.error(f"Reassign error: {e}")
        await update.message.reply_text("Reassign failed\\. Try again\\.", parse_mode="MarkdownV2")
        return

    await update.message.reply_text(
        f"*{md(lead['contact_name'])}* reassigned to *{md(rep['first_name'])}*\\.",
        parse_mode="MarkdownV2",
    )


# ─── /addcontact — ConversationHandler ───────────────────────

async def addcontact_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await _get_user(update.effective_user.id)
    if not user:
        await _not_registered(update)
        return ConversationHandler.END

    context.user_data["addcontact"] = {}
    await update.message.reply_text(
        "Let's add a new lead\\. What's the contact's *name*?", parse_mode="MarkdownV2"
    )
    return ADD_NAME


async def addcontact_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Please enter a valid name\\.", parse_mode="MarkdownV2")
        return ADD_NAME

    context.user_data["addcontact"]["name"] = name
    await update.message.reply_text(
        f"*{md(name)}* — got it\\. What *company* are they from?", parse_mode="MarkdownV2"
    )
    return ADD_COMPANY


async def addcontact_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    company = update.message.text.strip()
    context.user_data["addcontact"]["company"] = company

    stage_list = "\n".join(f"{i + 1}\\. {md(s)}" for i, s in enumerate(OPEN_STAGES))
    await update.message.reply_text(
        f"*Company:* {md(company)}\n\nWhat *pipeline stage* are they at? "
        f"Reply with a number:\n{stage_list}",
        parse_mode="MarkdownV2",
    )
    return ADD_STAGE


async def addcontact_stage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    stage = None
    try:
        idx = int(text) - 1
        if 0 <= idx < len(OPEN_STAGES):
            stage = OPEN_STAGES[idx]
    except ValueError:
        stage = next((s for s in OPEN_STAGES if s.lower() == text.lower()), None)

    if not stage:
        stage_list = "\n".join(f"{i + 1}\\. {md(s)}" for i, s in enumerate(OPEN_STAGES))
        await update.message.reply_text(
            f"Please pick a number 1\\-{len(OPEN_STAGES)}:\n{stage_list}",
            parse_mode="MarkdownV2",
        )
        return ADD_STAGE

    context.user_data["addcontact"]["stage"] = stage

    source_list = "\n".join(f"{i + 1}\\. {md(s)}" for i, s in enumerate(SOURCE_LABELS))
    await update.message.reply_text(
        f"*Stage:* {md(stage)}\n\nWhere did you first connect with them? Reply with a number:\n{source_list}",
        parse_mode="MarkdownV2",
    )
    return ADD_SOURCE


async def addcontact_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    source = None
    try:
        idx = int(text) - 1
        if 0 <= idx < len(SOURCE_LABELS):
            source = SOURCE_VALUES[idx]  # store the DB value, not the display label
    except ValueError:
        match_idx = next(
            (i for i, s in enumerate(SOURCE_LABELS) if s.lower() == text.lower()), None
        )
        if match_idx is not None:
            source = SOURCE_VALUES[match_idx]

    if not source:
        source_list = "\n".join(f"{i + 1}\\. {md(s)}" for i, s in enumerate(SOURCE_LABELS))
        await update.message.reply_text(
            f"Please pick 1\\-{len(SOURCE_LABELS)}:\n{source_list}", parse_mode="MarkdownV2"
        )
        return ADD_SOURCE

    data = context.user_data.get("addcontact", {})
    data["source"] = source

    user = await _get_user(update.effective_user.id)
    if not user:
        await _not_registered(update)
        context.user_data.pop("addcontact", None)
        return ConversationHandler.END

    try:
        company_id = None
        if data["company"]:
            company = await db.find_or_create_company(data["company"])
            company_id = company["id"]

        await db.create_lead(
            contact_name=data["name"],
            company_id=company_id,
            stage=data["stage"],
            assigned_to=user["id"],
            source=source,
        )
    except Exception as e:
        logger.error(f"Create lead error: {e}")
        await update.message.reply_text(
            "Failed to save lead\\. Try again\\.", parse_mode="MarkdownV2"
        )
        context.user_data.pop("addcontact", None)
        return ConversationHandler.END

    await update.message.reply_text(
        f"*{md(data['name'])}* from *{md(data['company'])}* added\\!\n"
        f"Stage: {md(data['stage'])} \\| Source: {md(source)}\n\n"
        "Forward a WhatsApp chat to log their first interaction\\.",
        parse_mode="MarkdownV2",
    )
    context.user_data.pop("addcontact", None)
    return ConversationHandler.END


# ─── /cancel ─────────────────────────────────────────────────

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelled\\.", parse_mode="MarkdownV2")
    return ConversationHandler.END


# ─── Handler registration ─────────────────────────────────────

def get_handlers() -> list:
    """
    Returns all handlers to register in main.py.

    Callback query handlers are split by pattern to avoid routing collisions:
      - "^ctx:"      → context disambiguation
      - "^mark_"     → won/lost confirmation

    The ConversationHandler for /addcontact must be registered before the
    generic MessageHandler in flows.py, otherwise text replies during the
    conversation would be intercepted by the forward/text capture flow.
    """
    addcontact_conv = ConversationHandler(
        entry_points=[CommandHandler("addcontact", addcontact_start)],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, addcontact_name)],
            ADD_COMPANY: [MessageHandler(filters.TEXT & ~filters.COMMAND, addcontact_company)],
            ADD_STAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, addcontact_stage)],
            ADD_SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, addcontact_source)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="addcontact",
        persistent=True,
    )

    return [
        CommandHandler("start", start),
        CommandHandler("help", help_command),
        CommandHandler("pipeline", pipeline),
        CommandHandler("deals", pipeline),
        CommandHandler("context", context_cmd),
        CommandHandler("won", won_handler),
        CommandHandler("lost", lost_handler),
        CommandHandler("ask", ask_pipeline),
        CommandHandler("reassign", reassign),
        CommandHandler("dashboard", dashboard_command),
        CommandHandler("cancel", cancel),
        CallbackQueryHandler(context_callback, pattern="^ctx:"),
        CallbackQueryHandler(mark_callback, pattern="^mark_"),
        addcontact_conv,
    ]
