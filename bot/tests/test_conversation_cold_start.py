import json

import commands
import db
import webhook
from tests.fakes import FakeStore

CHAT = USER = 4242


def _msg(update_id: int, text: str) -> dict:
    message = {
        "message_id": update_id, "date": 0, "text": text,
        "chat": {"id": CHAT, "type": "private"},
        "from": {"id": USER, "is_bot": False, "first_name": "Asha"},
    }
    if text.startswith("/"):
        message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text)}]
    return {"update_id": update_id, "message": message}


async def test_addcontact_continues_after_cold_start(monkeypatch, patched_bot):
    store = FakeStore()
    monkeypatch.setattr(db, "ptb_state_load", store.ptb_state_load)
    monkeypatch.setattr(db, "ptb_state_save", store.ptb_state_save)
    monkeypatch.setattr(db, "ptb_state_delete", store.ptb_state_delete)

    async def registered_rep(telegram_id):
        return {"id": 9, "telegram_id": telegram_id, "first_name": "Asha", "role": "rep"}

    monkeypatch.setattr(db, "get_user_by_telegram_id", registered_rep)

    # Each call builds a fresh Application, like three different Vercel instances.
    await webhook.handle_update(_msg(1, "/addcontact"))
    await webhook.handle_update(_msg(2, "Asha Rao"))
    await webhook.handle_update(_msg(3, "Acme Corp"))

    assert len(patched_bot) == 3
    assert "pipeline stage" in patched_bot[2]["text"]
    key = json.dumps([CHAT, USER])
    assert store.rows["conv:addcontact"][key] == commands.ADD_STAGE
    assert store.rows["user_data"][str(USER)]["addcontact"] == {"name": "Asha Rao", "company": "Acme Corp"}
