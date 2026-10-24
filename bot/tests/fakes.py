"""Test doubles shared across the suite. No network, no database."""

import copy
from typing import Any, Dict


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
