"""
persistence.py — PTB persistence backed by the Neon `ptb_state` table.

On Vercel every webhook request builds a fresh Application, so anything PTB keeps
in memory (ConversationHandler states, context.user_data) would be lost between
messages. This class stores it in Postgres instead. Rows are keyed by `kind`
("user_data", "chat_data", "bot_data", "conv:<handler name>") and `key`.

Values go through encode()/decode() so the types the handlers keep in user_data
(datetime, date, Decimal, dicts with int keys, tuples) survive the JSON round-trip.
"""

import json
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Optional

from telegram.ext import BasePersistence, PersistenceInput

import db

logger = logging.getLogger(__name__)

_TAG = "__t"


def encode(obj: Any) -> Any:
    """Convert a value into plain JSON types, tagging the ones JSON cannot hold."""
    if isinstance(obj, datetime):
        return {_TAG: "datetime", "v": obj.isoformat()}
    if isinstance(obj, date):
        return {_TAG: "date", "v": obj.isoformat()}
    if isinstance(obj, Decimal):
        return {_TAG: "decimal", "v": str(obj)}
    if isinstance(obj, tuple):
        return {_TAG: "tuple", "v": [encode(x) for x in obj]}
    if isinstance(obj, dict):
        # A plain dict that already holds _TAG takes the tagged form, so decode()
        # can never mistake user data for a type tag.
        if _TAG not in obj and all(isinstance(k, str) for k in obj):
            return {k: encode(v) for k, v in obj.items()}
        return {_TAG: "dict", "v": [[encode(k), encode(v)] for k, v in obj.items()]}
    if isinstance(obj, list):
        return [encode(x) for x in obj]
    return obj


def decode(obj: Any) -> Any:
    """Inverse of encode()."""
    if isinstance(obj, list):
        return [decode(x) for x in obj]
    if not isinstance(obj, dict):
        return obj
    tag = obj.get(_TAG)
    if tag == "datetime":
        return datetime.fromisoformat(obj["v"])
    if tag == "date":
        return date.fromisoformat(obj["v"])
    if tag == "decimal":
        return Decimal(obj["v"])
    if tag == "tuple":
        return tuple(decode(x) for x in obj["v"])
    if tag == "dict":
        return {decode(k): decode(v) for k, v in obj["v"]}
    return {k: decode(v) for k, v in obj.items()}


def _conv_kind(name: str) -> str:
    return f"conv:{name}"


def _decode_rows(kind: str, rows: Dict[str, Any], parse_key) -> Dict[Any, Any]:
    """Decode every row, skipping and logging any that fail.

    PTB loads all state at Application.initialize, so one bad row raising here
    would fail every webhook request until someone fixed the table by hand.
    """
    out = {}
    for k, v in rows.items():
        try:
            out[parse_key(k)] = decode(v)
        except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
            logger.error("ptb_state: skipped bad row kind=%s key=%r: %s", kind, k, exc)
    return out


class NeonPersistence(BasePersistence):
    """Writes through to the store on every update_* call; reads on get_*."""

    def __init__(self, store: Any = db) -> None:
        super().__init__(store_data=PersistenceInput(callback_data=False))
        self._store = store

    # ── reads (PTB calls these during Application.initialize) ──

    async def get_user_data(self) -> Dict[int, Any]:
        rows = await self._store.ptb_state_load("user_data")
        return _decode_rows("user_data", rows, int)

    async def get_chat_data(self) -> Dict[int, Any]:
        rows = await self._store.ptb_state_load("chat_data")
        return _decode_rows("chat_data", rows, int)

    async def get_bot_data(self) -> Dict[Any, Any]:
        rows = await self._store.ptb_state_load("bot_data")
        return _decode_rows("bot_data", rows, str).get("bot_data", {})

    async def get_callback_data(self) -> Optional[Any]:
        return None

    async def get_conversations(self, name: str) -> Dict[tuple, object]:
        kind = _conv_kind(name)
        rows = await self._store.ptb_state_load(kind)
        return _decode_rows(kind, rows, lambda k: tuple(json.loads(k)))

    # ── writes ──

    async def update_conversation(self, name: str, key: tuple, new_state: Optional[object]) -> None:
        row_key = json.dumps(list(key))
        if new_state is None:
            await self._store.ptb_state_delete(_conv_kind(name), row_key)
        else:
            await self._store.ptb_state_save(_conv_kind(name), row_key, encode(new_state))

    async def update_user_data(self, user_id: int, data: Any) -> None:
        await self._store.ptb_state_save("user_data", str(user_id), encode(data))

    async def update_chat_data(self, chat_id: int, data: Any) -> None:
        await self._store.ptb_state_save("chat_data", str(chat_id), encode(data))

    async def update_bot_data(self, data: Any) -> None:
        await self._store.ptb_state_save("bot_data", "bot_data", encode(data))

    async def update_callback_data(self, data: Any) -> None:
        return None

    async def drop_user_data(self, user_id: int) -> None:
        await self._store.ptb_state_delete("user_data", str(user_id))

    async def drop_chat_data(self, chat_id: int) -> None:
        await self._store.ptb_state_delete("chat_data", str(chat_id))

    # ── no-ops: every write already went straight to the store ──

    async def refresh_user_data(self, user_id: int, user_data: Any) -> None:
        return None

    async def refresh_chat_data(self, chat_id: int, chat_data: Any) -> None:
        return None

    async def refresh_bot_data(self, bot_data: Any) -> None:
        return None

    async def flush(self) -> None:
        return None
