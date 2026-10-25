"""Test doubles shared across the suite. No network, no database."""

import copy
from typing import Any, Dict, List

import pytest
from telegram import Bot, User

BOT_USER = User(1, "Siteline", True, username="SitelineCRMbot")


@pytest.fixture
def patched_bot(monkeypatch) -> List[Dict[str, Any]]:
    """Stop telegram.Bot touching the network. Returns the list of send_message calls."""
    sent: List[Dict[str, Any]] = []

    async def get_me(self, *args, **kwargs):
        self._bot_user = BOT_USER
        return BOT_USER

    async def initialize(self):
        await get_me(self)
        self._initialized = True

    async def shutdown(self):
        self._initialized = False

    async def send_message(self, chat_id, text, *args, **kwargs):
        sent.append({"chat_id": chat_id, "text": text, **kwargs})

    monkeypatch.setattr(Bot, "get_me", get_me)
    monkeypatch.setattr(Bot, "initialize", initialize)
    monkeypatch.setattr(Bot, "shutdown", shutdown)
    monkeypatch.setattr(Bot, "send_message", send_message)
    return sent


class FakeStore:
    """In-memory stand-in for db.ptb_state_load / ptb_state_save / ptb_state_delete.

    Values are deep-copied in and out, like a real round-trip through JSON storage,
    so a test cannot pass by sharing one live dict between two persistence instances.
    """

    def __init__(self) -> None:
        self.rows: Dict[str, Dict[str, Any]] = {}

    async def ptb_state_load(self, kind: str) -> Dict[str, Any]:
        return copy.deepcopy(self.rows.get(kind, {}))

    async def ptb_state_save(self, kind: str, key: str, data: Any) -> None:
        self.rows.setdefault(kind, {})[key] = copy.deepcopy(data)

    async def ptb_state_delete(self, kind: str, key: str) -> None:
        self.rows.get(kind, {}).pop(key, None)
