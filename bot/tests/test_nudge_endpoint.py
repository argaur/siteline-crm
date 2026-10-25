from datetime import datetime, timedelta, timezone

import pytest

import db
import main

MANAGER = {"id": 1, "first_name": "Mona", "role": "manager", "telegram_id": -1}
REP = {"id": 2, "first_name": "Ravi", "role": "rep", "telegram_id": 777}
LEAD = {
    "id": 50, "contact_name": "Asha Rao", "company_id": None, "assigned_to": 2,
    "stage": "Proposal", "seat_count": 10, "city": "Jaipur",
    "last_activity_at": datetime.now(timezone.utc) - timedelta(days=5),
}


@pytest.fixture
def fake_db(monkeypatch):
    async def get_user_by_id(uid):
        return {1: MANAGER, 2: REP}.get(uid)

    async def get_lead_by_id(lead_id):
        return LEAD if lead_id == 50 else None

    monkeypatch.setattr(db, "get_user_by_id", get_user_by_id)
    monkeypatch.setattr(db, "get_lead_by_id", get_lead_by_id)
    return {"Authorization": f"Bearer {db.make_dashboard_token(1, 'manager')}"}


def test_manual_nudge_sends_without_running_app(client, fake_db, patched_bot):
    r = client.post("/api/leads/50/nudge", headers=fake_db)
    assert r.status_code == 204
    assert len(patched_bot) == 1
    assert patched_bot[0]["chat_id"] == 777
    assert "Asha Rao" in patched_bot[0]["text"]


def test_manual_nudge_503_without_token(client, fake_db, patched_bot, monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", None)
    r = client.post("/api/leads/50/nudge", headers=fake_db)
    assert r.status_code == 503
    assert patched_bot == []
