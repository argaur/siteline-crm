import os
import logging
import tempfile
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
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

# ConversationHandler states for /addnote
SELECTING_CONTACT, ADDING_NOTE, AWAITING_FOLLOWUP = range(3)

SPACE_TYPES = {"Dedicated Desk", "Private Cabin", "Managed Office", "Day Pass"}


# ─── Helpers ──────────────────────────────────────────────────

def md(text) -> str:
    return escape_markdown(str(text) if text is not None else "", version=2)


def fmt_inr(value) -> str:
    v = float(value)
    if v >= 1e7:
        return f"₹{v / 1e7:.1f}Cr"
    if v >= 1e5:
        return f"₹{v / 1e5:.1f}L"
    return f"₹{v:,.0f}"


def _as_int(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_float(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


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


def _lead_details(lead: dict) -> str:
    parts = []
    if lead.get("seat_count"):
        parts.append(f"{lead['seat_count']} seats")
    if lead.get("city"):
        parts.append(lead["city"])
    if lead.get("est_deal_value"):
        parts.append(fmt_inr(lead["est_deal_value"]))
    return " · ".join(parts)


def _format_matches_message(lead: dict, result: dict, company_name: str = ""):
    """Builds the follow-up suggested-spaces message after a capture (Feature A).

    Returns a MarkdownV2-escaped string ready for parse_mode="MarkdownV2",
    or None when nothing should be sent (status == "not_enough_info": the
    next capture may enrich the lead — nagging for city/seats here would
    fight the evaluate_note_quality follow-up flow, which owns that job).
    Plain numbered text, no inline buttons — actions are Feature B, not built.
    """
    status = result.get("status")
    if status == "not_enough_info":
        return None

    if status == "no_city_inventory":
        return md(f"No partner spaces in {lead.get('city')} in inventory yet.")

    if status == "undersized_inventory":
        return md(
            f"No single space fits {lead.get('seat_count')} seats in "
            f"{lead.get('city')} — largest available has "
            f"{result.get('largest_available')}."
        )

    matches = result.get("matches") or []
    if not matches:  # defensive — "ok" always carries >=1 match
        return None

    display_name = company_name or lead.get("contact_name") or "this lead"
    lines = [
        f"Suggested spaces for {display_name} "
        f"({lead.get('seat_count')} seats, {lead.get('city')}):",
        "",
    ]
    budget = float(lead["budget_per_seat"]) if lead.get("budget_per_seat") else None

    for i, m in enumerate(matches[:2], start=1):  # top 2 — chat real estate
        bits = [f"{m['available_seats']} seats free"]
        price = m.get("price_per_seat")
        if price is not None and budget:
            if price <= budget:
                bits.append(f"₹{price:,.0f}/seat vs ₹{budget:,.0f} budget")
            else:
                bits.append(
                    f"₹{price:,.0f}/seat ({round((price / budget - 1) * 100)}% over budget)"
                )
        elif price is not None:
            bits.append(f"₹{price:,.0f}/seat")
        bits.append(f"match {m['score']}")
        lines.append(f"{i}. {m['name']} — {m['space_type']}")
        lines.append(f"   {' · '.join(bits)}")

    return md("\n".join(lines))


async def _not_registered(update: Update):
    await update.message.reply_text(
        "You're not registered yet\\. Send /start to set up your account\\.",
        parse_mode="MarkdownV2",
    )


# ─── Core save logic (shared by all capture flows) ───────────

async def _save_capture(
    update: Update, user: dict, extracted: dict, raw_text: str, source: str = "whatsapp_forward"
):
    """
    Finds or creates the lead, logs the interaction, and sends the
    confirmation card with Looks good / Edit stage buttons.

    Stage rule: an extraction can move a lead FORWARD in the pipeline but never
    backwards — a vague forwarded message must not knock a Negotiation-stage
    deal back to Inquiry. Backwards extractions keep the current stage and get
    flagged on the card for manual override. "unknown" never touches the stage.
    """
    contact_name = extracted.get("contact_name") or "Unknown"
    company = extracted.get("company")
    role = extracted.get("role")
    summary = extracted.get("summary") or ""
    next_action = extracted.get("next_action")
    extracted_stage = extracted.get("stage")

    seat_count = _as_int(extracted.get("seat_count"))
    city = extracted.get("city")
    raw_space_type = extracted.get("space_type")
    space_type = raw_space_type if raw_space_type in SPACE_TYPES else None
    budget_per_seat = _as_float(extracted.get("budget_per_seat"))
    move_in_date = extracted.get("move_in_date")
    # Monthly contract value — the number the pipeline view aggregates.
    est_deal_value = (
        seat_count * budget_per_seat if seat_count and budget_per_seat else None
    )

    stage_note = None
    matches = await db.find_leads(contact_name)

    if not matches:
        stage = extracted_stage if extracted_stage in db.STAGES else "Inquiry"
        company_id = None
        if company:
            company_rec = await db.find_or_create_company(company, city=city)
            company_id = company_rec["id"]

        lead = await db.create_lead(
            contact_name=contact_name,
            company_id=company_id,
            contact_role=role,
            stage=stage,
            seat_count=seat_count,
            city=city,
            space_type=space_type,
            budget_per_seat=budget_per_seat,
            est_deal_value=est_deal_value,
            move_in_date=str(move_in_date) if move_in_date is not None else None,
            assigned_to=user["id"],
            source=source,
        )
        lead_id = lead["id"]
        # Fresh lead → matching inputs are fresh by definition.
        matching_inputs_changed = True
    else:
        lead = matches[0]
        lead_id = lead["id"]
        current_stage = lead["stage"]

        if extracted_stage in db.STAGES:
            if db.STAGES.index(extracted_stage) >= db.STAGES.index(current_stage):
                if extracted_stage != current_stage:
                    await db.update_lead_stage(lead_id, extracted_stage)
            else:
                stage_note = (
                    f"Extracted stage ({extracted_stage}) is earlier than current "
                    f"({current_stage}) — kept current stage, tap Edit stage to override."
                )

        # Fill in B2B fields the lead is still missing — never overwrite.
        updates = {}
        if company and not lead.get("company_id"):
            company_rec = await db.find_or_create_company(company, city=city)
            updates["company_id"] = company_rec["id"]
        if seat_count and not lead.get("seat_count"):
            updates["seat_count"] = seat_count
        if city and not lead.get("city"):
            updates["city"] = city
        if space_type and not lead.get("space_type"):
            updates["space_type"] = space_type
        if budget_per_seat and not lead.get("budget_per_seat"):
            updates["budget_per_seat"] = budget_per_seat
        if est_deal_value and not lead.get("est_deal_value"):
            updates["est_deal_value"] = est_deal_value
        if move_in_date and not lead.get("move_in_date"):
            updates["move_in_date"] = str(move_in_date)
        if updates:
            await db.update_lead(lead_id, **updates)

        # Anti-spam gate: only re-suggest when this capture actually filled in
        # a matching input. Recapturing a vague note against a complete lead
        # must NOT re-send the same match list.
        matching_inputs_changed = any(
            k in updates for k in ("city", "seat_count", "space_type", "budget_per_seat")
        )

    # No next_action column in the new schema — carried in the interaction summary.
    ai_summary = f"{summary} Next action: {next_action}".strip() if next_action else summary

    await db.log_interaction(
        lead_id=lead_id,
        type=source,
        raw_content=raw_text[:5000],
        ai_summary=ai_summary,
        user_id=user["id"],
    )

    updated = await db.get_lead_by_id(lead_id)
    heat = updated["heat_score"]
    company_name = await _company_name(updated)

    card = (
        f"*Got it\\.*\n\n"
        f"*{md(updated['contact_name'])}* @ {md(company_name or 'Unknown')}\n"
        f"Stage: {md(updated['stage'])} \\| Heat: {heat['score']} \\({md(heat['label'])}\\)\n"
    )
    details = _lead_details(updated)
    if details:
        card += f"{md(details)}\n"
    card += f"\n*Summary:* {md(summary)}"
    if next_action:
        card += f"\n*Next:* {md(next_action)}"
    if stage_note:
        card += f"\n\n_{md(stage_note)}_"

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Looks good ✓", callback_data="capture_ok"),
        InlineKeyboardButton("Edit stage", callback_data=f"edit_stage:{lead_id}"),
    ]])

    await update.message.reply_text(card, reply_markup=keyboard, parse_mode="MarkdownV2")

    if matching_inputs_changed:
        try:
            result = await db.get_matches_for_lead(updated)
            text = _format_matches_message(updated, result, company_name)
            if text:
                await update.message.reply_text(text, parse_mode="MarkdownV2")
        except Exception:
            # Capture already succeeded and was confirmed — the match message
            # is a bonus, the saved lead is the product. Log loudly (project
            # rule: no silent except), never surface this to the rep.
            logger.exception(f"[match] suggestion failed for lead {lead_id}")


# ─── Forwarded text / plain text capture ─────────────────────

async def forward_or_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles two cases:
    1. Normal: forwarded message or text > 50 chars → extract → quality check → save
    2. Follow-up: if context.user_data has a pending_capture (incomplete note from
       a previous message), this message is the follow-up answer → concatenate → save

    Note: pending_capture state is stored in user_data instead of a ConversationHandler
    because entry points for ConversationHandlers can't overlap with plain MessageHandlers.
    """
    try:
        telegram_id = update.effective_user.id
        logger.info(f"[capture] text received from {telegram_id}, len={len(update.message.text or '')}")

        user = await _get_user(telegram_id)
        if not user:
            await _not_registered(update)
            return

        # Case 2: pending follow-up from a previous incomplete capture
        if context.user_data.get("pending_capture"):
            pending = context.user_data.pop("pending_capture")
            combined = pending["raw_text"] + "\n" + update.message.text
            await _save_capture(update, user, pending["extracted"], combined)
            return

        # Case 1: new capture
        content = update.message.text or ""

        if len(content) <= 50:
            logger.info(f"[capture] ignored — too short ({len(content)} chars)")
            return

        logger.info("[capture] calling ai.extract_from_text...")
        extracted = await ai.extract_from_text(content)
        logger.info(f"[capture] extracted={extracted}")

        if not extracted.get("contact_name"):
            await update.message.reply_text(
                "I couldn't identify a contact\\. Try /addcontact to add manually\\.",
                parse_mode="MarkdownV2",
            )
            return

        quality = await ai.evaluate_note_quality(content)
        logger.info(f"[capture] quality={quality}")

        if not quality.get("is_complete"):
            context.user_data["pending_capture"] = {
                "raw_text": content,
                "extracted": extracted,
            }
            followup_q = quality.get("follow_up_question") or "Could you share more details about this interaction?"
            await update.message.reply_text(md(followup_q), parse_mode="MarkdownV2")
            return

        await _save_capture(update, user, extracted, content)

    except Exception as e:
        logger.exception(f"[capture] unhandled error: {e}")
        await update.message.reply_text(f"Error: {e}", parse_mode=None)


# ─── Voice capture ────────────────────────────────────────────

async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Downloads voice file → transcribes with Whisper → classifies intent.
    - "recall": user wants a pre-call brief (e.g. "prep me for call with Arjun")
    - "capture": user is logging an interaction → same flow as text capture
    Temp file is always deleted via try/finally.
    """
    telegram_id = update.effective_user.id
    user = await _get_user(telegram_id)
    if not user:
        await _not_registered(update)
        return

    status_msg = await update.message.reply_text("Transcribing\\.\\.\\.", parse_mode="MarkdownV2")

    # Use tempfile so this works on both Windows and Linux
    tmp_path = os.path.join(
        tempfile.gettempdir(), f"voice_{update.message.message_id}.ogg"
    )

    try:
        voice_file = await update.message.voice.get_file()
        await voice_file.download_to_drive(tmp_path)

        transcript = await ai.transcribe_voice(tmp_path)
        logger.info(f"[voice] transcript={transcript[:120]}")

        # classify_intent returns the string "capture" or "recall" — not a dict
        intent = await ai.classify_intent(transcript)
        logger.info(f"[voice] intent={intent}")

        if intent == "recall":
            # Extract contact name from transcript to know who they're asking about
            extracted = await ai.extract_from_voice(transcript)
            contact_name = extracted.get("contact_name")

            if contact_name:
                matches = await db.find_leads(contact_name)
                if matches:
                    lead = matches[0]
                    heat = lead["heat_score"]
                    company_name = await _company_name(lead)
                    interactions = await db.get_interactions(lead["id"])
                    summaries = [
                        r["ai_summary"] for r in interactions if r.get("ai_summary")
                    ]

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
                    await status_msg.edit_text(md(brief), parse_mode="MarkdownV2")
                    return

            await status_msg.edit_text(
                "Couldn't identify the lead\\. Try /context \\[name\\]\\.",
                parse_mode="MarkdownV2",
            )

        else:
            # Capture intent — extract and save immediately, no quality gate for voice
            extracted = await ai.extract_from_voice(transcript)
            logger.info(f"[voice] extracted={extracted}")
            await status_msg.delete()

            if not extracted.get("contact_name"):
                await update.message.reply_text(
                    "I couldn't identify a contact\\. Try /addcontact\\.",
                    parse_mode="MarkdownV2",
                )
                return

            await _save_capture(update, user, extracted, transcript, source="voice_note")

    except Exception as e:
        logger.exception(f"[voice] unhandled error: {e}")
        try:
            await status_msg.edit_text(f"Error processing voice note: {e}", parse_mode=None)
        except Exception:
            await update.message.reply_text(f"Error processing voice note: {e}", parse_mode=None)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


# ─── Image / screenshot capture ───────────────────────────────

async def image_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Downloads the highest-resolution photo and extracts lead info via Claude Vision."""
    telegram_id = update.effective_user.id
    user = await _get_user(telegram_id)
    if not user:
        await _not_registered(update)
        return

    status_msg = await update.message.reply_text("Reading image\\.\\.\\.", parse_mode="MarkdownV2")

    try:
        # photo[-1] is always the highest resolution version
        photo_file = await update.message.photo[-1].get_file()
        image_bytes = await photo_file.download_as_bytearray()
        logger.info(f"[image] downloaded {len(image_bytes)} bytes")

        extracted = await ai.extract_from_image(bytes(image_bytes), "image/jpeg")
        logger.info(f"[image] extracted={extracted}")
        await status_msg.delete()

        if not extracted.get("contact_name"):
            if extracted.get("company"):
                extracted["contact_name"] = extracted["company"]
            else:
                await update.message.reply_text(
                    "I couldn't extract contact info from this image\\. Try /addcontact to add manually\\.",
                    parse_mode="MarkdownV2",
                )
                return

        await _save_capture(update, user, extracted, "Contact info from screenshot", source="screenshot")

    except Exception as e:
        logger.exception(f"[image] unhandled error: {e}")
        try:
            await status_msg.edit_text(f"Error processing image: {e}", parse_mode=None)
        except Exception:
            await update.message.reply_text(f"Error processing image: {e}", parse_mode=None)


# ─── /addnote ConversationHandler ────────────────────────────

async def addnote_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await _get_user(update.effective_user.id)
    if not user:
        await _not_registered(update)
        return ConversationHandler.END

    context.user_data["addnote"] = {"user_id": user["id"]}
    await update.message.reply_text("Which lead is this note for?")
    return SELECTING_CONTACT


async def addnote_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    SELECTING_CONTACT state. Searches for the lead.
    - 1 result: stores lead, moves to ADDING_NOTE
    - 0 results: prompts again (stays in SELECTING_CONTACT)
    - Multiple: shows ReplyKeyboard with names; next message (exact name from keyboard)
      comes back to this same handler and matches as 1 result
    """
    query = update.message.text.strip()

    try:
        matches = await db.find_leads(query)
    except Exception as e:
        logger.error(f"Lead search error: {e}")
        await update.message.reply_text("Search failed\\. Try again or /cancel\\.", parse_mode="MarkdownV2")
        return SELECTING_CONTACT

    if not matches:
        await update.message.reply_text(
            "No lead found\\. Try a different name or /cancel\\.",
            parse_mode="MarkdownV2",
        )
        return SELECTING_CONTACT

    if len(matches) == 1:
        context.user_data["addnote"]["lead"] = matches[0]
        await update.message.reply_text(
            f"*{md(matches[0]['contact_name'])}* — what happened?",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="MarkdownV2",
        )
        return ADDING_NOTE

    # Multiple matches — show keyboard so user picks exact name
    buttons = [[m["contact_name"]] for m in matches[:5]]
    await update.message.reply_text(
        "Multiple matches — pick one:",
        reply_markup=ReplyKeyboardMarkup(buttons, one_time_keyboard=True, resize_keyboard=True),
    )
    return SELECTING_CONTACT  # Keyboard selection comes back here as exact name


async def addnote_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ADDING_NOTE state. Saves the note directly — no quality check for manual notes.
    Quality check is only meaningful for auto-captured text where context may be missing."""
    note_text = update.message.text.strip()
    await _save_addnote(update, context, note_text)
    return ConversationHandler.END


async def addnote_followup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """AWAITING_FOLLOWUP state. Concatenates follow-up with original note and saves."""
    followup = update.message.text.strip()
    original = context.user_data.get("addnote", {}).get("note", "")
    combined = original + " " + followup
    await _save_addnote(update, context, combined)
    return ConversationHandler.END


async def _save_addnote(update: Update, context: ContextTypes.DEFAULT_TYPE, note_text: str):
    """Writes the note as an interaction and sends confirmation."""
    data = context.user_data.get("addnote", {})
    lead = data.get("lead")
    if not lead:
        await update.message.reply_text(
            "Something went wrong\\. Try /addnote again\\.", parse_mode="MarkdownV2"
        )
        context.user_data.pop("addnote", None)
        return

    try:
        await db.log_interaction(
            lead_id=lead["id"],
            type="addnote_command",
            raw_content=note_text,
            ai_summary=note_text,  # Manual notes are self-descriptive
            user_id=data.get("user_id"),
        )
        await update.message.reply_text(
            f"Note saved for *{md(lead['contact_name'])}*\\.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="MarkdownV2",
        )
    except Exception as e:
        logger.error(f"Save addnote error: {e}")
        await update.message.reply_text("Failed to save note\\. Try again\\.", parse_mode="MarkdownV2")
    finally:
        context.user_data.pop("addnote", None)


# ─── /note [text] ─────────────────────────────────────────────

async def quick_note_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Quick note to the most recently active lead.
    If the top two leads share the same last_activity_at, shows inline buttons to disambiguate.
    """
    telegram_id = update.effective_user.id
    user = await _get_user(telegram_id)
    if not user:
        await _not_registered(update)
        return

    note_text = " ".join(context.args).strip() if context.args else ""

    if not note_text:
        await update.message.reply_text(
            "Usage: /note \\[your note text\\]", parse_mode="MarkdownV2"
        )
        return

    try:
        pipeline = await db.get_all_leads(_pipeline_scope(user))
        all_leads = [lead for leads in pipeline.values() for lead in leads]
        epoch = datetime.min.replace(tzinfo=timezone.utc)
        all_leads.sort(key=lambda l: l.get("last_activity_at") or epoch, reverse=True)
        latest = all_leads[:2]
    except Exception as e:
        logger.error(f"Quick note pipeline fetch error: {e}")
        await update.message.reply_text("Couldn't fetch leads\\.", parse_mode="MarkdownV2")
        return

    if not latest:
        await update.message.reply_text(
            "No active deals found\\. Forward a message first\\.", parse_mode="MarkdownV2"
        )
        return

    # Unambiguous if only one lead, or top two have different timestamps
    if len(latest) == 1 or (
        latest[0].get("last_activity_at") != latest[1].get("last_activity_at")
    ):
        lead = latest[0]
        try:
            await db.log_interaction(
                lead_id=lead["id"],
                type="addnote_command",
                raw_content=note_text,
                ai_summary=note_text,
                user_id=user["id"],
            )
            await update.message.reply_text(
                f"Note added to *{md(lead['contact_name'])}*\\.", parse_mode="MarkdownV2"
            )
        except Exception as e:
            logger.error(f"Quick note save error: {e}")
            await update.message.reply_text("Failed to save note\\.", parse_mode="MarkdownV2")
    else:
        # Same timestamp — ask which one
        context.user_data["quick_note_text"] = note_text
        buttons = []
        for lead in latest:
            company_name = await _company_name(lead)
            buttons.append([
                InlineKeyboardButton(
                    f"{lead['contact_name']} @ {company_name or '?'}",
                    callback_data=f"qnote:{lead['id']}",
                )
            ])
        await update.message.reply_text(
            "Which lead is this note for?",
            reply_markup=InlineKeyboardMarkup(buttons),
        )


# ─── Callback handlers ────────────────────────────────────────

async def capture_ok_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User tapped 'Looks good ✓' — remove the inline keyboard."""
    query = update.callback_query
    await query.answer("Saved!")
    await query.edit_message_reply_markup(reply_markup=None)


async def edit_stage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User tapped 'Edit stage' — replace buttons with a stage picker."""
    query = update.callback_query
    await query.answer()
    lead_id = query.data.split(":", 1)[1]

    buttons = [
        [InlineKeyboardButton(s, callback_data=f"set_stage:{lead_id}:{s}")]
        for s in db.STAGES
    ]
    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))


async def set_stage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Applies the chosen stage.
    Callback format: "set_stage:{lead_id}:{stage_name}"
    Uses split(":", 2) — stage names contain spaces/hyphens, so stage is the last segment.
    """
    query = update.callback_query
    _, lead_id_str, new_stage = query.data.split(":", 2)
    lead_id = int(lead_id_str)

    try:
        lead = await db.update_lead_stage(lead_id, new_stage)
        await query.answer("Stage updated!")
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            f"Stage updated: *{md(lead['contact_name'])}* → *{md(new_stage)}*\\.",
            parse_mode="MarkdownV2",
        )
    except Exception as e:
        logger.error(f"Set stage callback error: {e}")
        await query.answer("Update failed. Try again.")


async def quick_note_contact_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles lead selection when /note was ambiguous."""
    query = update.callback_query
    await query.answer()
    lead_id = int(query.data.split(":", 1)[1])
    note_text = context.user_data.pop("quick_note_text", "")

    if not note_text:
        await query.edit_message_text("Note text was lost\\. Try /note again\\.", parse_mode="MarkdownV2")
        return

    user = await _get_user(update.effective_user.id)

    try:
        await db.log_interaction(
            lead_id=lead_id,
            type="addnote_command",
            raw_content=note_text,
            ai_summary=note_text,
            user_id=user["id"] if user else None,
        )
        lead = await db.get_lead_by_id(lead_id)
        name = lead["contact_name"] if lead else "Lead"
        await query.edit_message_text(
            f"Note added to *{md(name)}*\\.", parse_mode="MarkdownV2"
        )
    except Exception as e:
        logger.error(f"Quick note callback error: {e}")
        await query.edit_message_text("Failed to save note\\.", parse_mode="MarkdownV2")


# ─── Cancel (fallback for addnote ConversationHandler) ────────

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelled\\.", parse_mode="MarkdownV2")
    return ConversationHandler.END


# ─── Handler registration ─────────────────────────────────────

def get_addnote_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("addnote", addnote_start)],
        states={
            SELECTING_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, addnote_search)],
            ADDING_NOTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, addnote_note)],
            AWAITING_FOLLOWUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, addnote_followup)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="addnote",
        persistent=True,
    )


def get_handlers() -> list:
    """
    Returns all flow handlers. Register these in main.py / run_bot.py AFTER
    commands.py handlers. Handler order matters:
    - addnote ConversationHandler must come before the generic text MessageHandler
    - Voice and photo handlers are filtered by message type so order doesn't matter
    - forward_or_text_handler (generic TEXT) must be last — it's the catch-all
    """
    return [
        CommandHandler("note", quick_note_handler),
        CallbackQueryHandler(capture_ok_callback, pattern="^capture_ok$"),
        CallbackQueryHandler(edit_stage_callback, pattern="^edit_stage:"),
        CallbackQueryHandler(set_stage_callback, pattern="^set_stage:"),
        CallbackQueryHandler(quick_note_contact_callback, pattern="^qnote:"),
        get_addnote_conversation(),
        MessageHandler(filters.VOICE, voice_handler),
        MessageHandler(filters.PHOTO, image_handler),
        MessageHandler(filters.TEXT & ~filters.COMMAND, forward_or_text_handler),
    ]
