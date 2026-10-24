import pytest

import db
import main
import webhook
from tests.fakes import FakeStore

REAL_HANDLE_UPDATE = webhook.handle_update  # kept before any test patches it

GOOD = {"X-Telegram-Bot-Api-Secret-Token": "hook-secret"}
UPDATE = {"update_id": 501, "message": {"message_id": 1, "date": 0, "chat": {"id": 7, "type": "private"}}}


@pytest.fixture
def calls(monkeypatch):
    """Record every claim call; handle_update succeeds unless a test swaps it."""
    log = {"pool": [], "claim": [], "finish": [], "release": [], "handle": []}

    async def init_pool():
        log["pool"].append(len(log["claim"]))  # how many claims ran before the pool opened

    async def claim(update_id):
        log["claim"].append(update_id)
        return log.get("claim_result", True)

    async def finish(update_id):
        log["finish"].append(update_id)

    async def release(update_id):
        log["release"].append(update_id)

    async def handle(payload):
        log["handle"].append(payload)

    monkeypatch.setattr(db, "init_pool", init_pool)
    monkeypatch.setattr(db, "claim_update", claim)
    monkeypatch.setattr(db, "finish_update", finish)
    monkeypatch.setattr(db, "release_update", release)
    monkeypatch.setattr(webhook, "handle_update", handle)
    return log


def test_webhook_rejects_missing_secret(client, calls):
    r = client.post("/telegram/webhook", json=UPDATE)
    assert r.status_code == 403
    assert calls["claim"] == []


def test_webhook_rejects_wrong_secret(client, calls):
    r = client.post("/telegram/webhook", json=UPDATE,
                    headers={"X-Telegram-Bot-Api-Secret-Token": "nope"})
    assert r.status_code == 403
    assert calls["claim"] == []


def test_webhook_rejects_non_json_body(client, calls):
    r = client.post("/telegram/webhook", content=b"not json", headers=GOOD)
    assert r.status_code == 400
    assert calls["claim"] == []


def test_webhook_rejects_missing_update_id(client, calls):
    r = client.post("/telegram/webhook", json={"message": {}}, headers=GOOD)
    assert r.status_code == 400
    assert calls["claim"] == []


def test_webhook_duplicate_is_skipped(client, calls):
    calls["claim_result"] = False
    r = client.post("/telegram/webhook", json=UPDATE, headers=GOOD)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "duplicate": True}
    assert calls["handle"] == []


def test_webhook_success_marks_done(client, calls):
    r = client.post("/telegram/webhook", json=UPDATE, headers=GOOD)
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert calls["finish"] == [501]


def test_webhook_opens_pool_before_claiming(client, calls):
    # A cold serverless instance has no pool yet; the route must not rely on startup.
    client.post("/telegram/webhook", json=UPDATE, headers=GOOD)
    assert calls["pool"] == [0]


def test_webhook_failure_releases_for_retry(client, calls, monkeypatch):
    async def boom(payload):
        raise RuntimeError("handler blew up")

    monkeypatch.setattr(webhook, "handle_update", boom)
    r = client.post("/telegram/webhook", json=UPDATE, headers=GOOD)
    assert r.status_code == 500
    assert calls["release"] == [501]
    assert calls["finish"] == []


def test_unhandled_update_type_is_done(client, calls, monkeypatch, patched_bot):
    # Real handle_update over a fake store. An edited location matches no handler,
    # so it must still be marked done rather than retried forever.
    store = FakeStore()
    monkeypatch.setattr(webhook, "handle_update", REAL_HANDLE_UPDATE)
    monkeypatch.setattr(db, "ptb_state_load", store.ptb_state_load)
    monkeypatch.setattr(db, "ptb_state_save", store.ptb_state_save)
    monkeypatch.setattr(db, "ptb_state_delete", store.ptb_state_delete)
    edited = {"update_id": 502, "edited_message": {
        "message_id": 1, "date": 0, "edit_date": 0,
        "chat": {"id": 7, "type": "private"}, "from": {"id": 7, "is_bot": False, "first_name": "A"},
        "location": {"latitude": 26.9, "longitude": 75.8}}}
    r = client.post("/telegram/webhook", json=edited, headers=GOOD)
    assert r.status_code == 200
    assert calls["finish"] == [502]
    assert patched_bot == []
