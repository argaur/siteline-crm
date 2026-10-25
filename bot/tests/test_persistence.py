import json
from datetime import datetime, timezone
from decimal import Decimal

from persistence import NeonPersistence, decode, encode
from tests.fakes import FakeStore


async def test_user_data_round_trips_across_instances():
    store = FakeStore()
    await NeonPersistence(store=store).update_user_data(42, {"addcontact": {"name": "Asha"}})

    fresh = NeonPersistence(store=store)
    assert await fresh.get_user_data() == {42: {"addcontact": {"name": "Asha"}}}


async def test_conversation_state_round_trips():
    store = FakeStore()
    await NeonPersistence(store=store).update_conversation("addcontact", (7, 42), 2)

    fresh = NeonPersistence(store=store)
    assert await fresh.get_conversations("addcontact") == {(7, 42): 2}


async def test_conversation_end_deletes_key():
    store = FakeStore()
    first = NeonPersistence(store=store)
    await first.update_conversation("addcontact", (7, 42), 2)
    await first.update_conversation("addcontact", (7, 42), None)

    assert await NeonPersistence(store=store).get_conversations("addcontact") == {}


async def test_encode_round_trips_rich_types():
    original = {
        "d": Decimal("12.50"),
        "t": datetime(2026, 10, 24, tzinfo=timezone.utc),
        5: "x",
    }
    stored = json.loads(json.dumps(encode(original)))  # what jsonb would hand back
    assert decode(stored) == original


async def test_dict_holding_the_tag_key_round_trips_as_data():
    # A user-typed value must never be read back as a type tag.
    original = {"__t": "datetime", "v": "not-a-date"}
    stored = json.loads(json.dumps(encode(original)))
    assert decode(stored) == original


async def test_corrupt_row_is_skipped_not_fatal(caplog):
    # One bad row must not stop every other user's state from loading.
    store = FakeStore()
    store.rows["user_data"] = {
        "1": {"__t": "datetime", "v": "garbage"},
        "2": {"k": 1},
    }
    assert await NeonPersistence(store=store).get_user_data() == {2: {"k": 1}}
    assert "user_data" in caplog.text


async def test_drop_user_data_removes_row():
    store = FakeStore()
    first = NeonPersistence(store=store)
    await first.update_user_data(42, {"k": 1})
    await first.drop_user_data(42)

    assert await NeonPersistence(store=store).get_user_data() == {}
