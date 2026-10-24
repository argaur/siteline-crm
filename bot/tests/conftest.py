"""Shared test setup. Env is fixed BEFORE main is imported, so no real secret is ever used."""

import os
import sys
from pathlib import Path

os.environ["DASHBOARD_TOKEN_SECRET"] = "test-secret"
os.environ["TELEGRAM_WEBHOOK_SECRET"] = "hook-secret"
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST"
os.environ["BOT_NAME"] = "SitelineCRMbot"
os.environ["DATABASE_URL"] = "postgresql://unused"
os.environ["OPENAI_API_KEY"] = "sk-test-unused"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import db  # noqa: E402
import main  # noqa: E402
from tests.fakes import patched_bot  # noqa: E402,F401  (shared fixture)


@pytest.fixture(autouse=True)
def no_real_pool(monkeypatch):
    """Any test that forgets to fake a db call fails loudly instead of dialling out."""
    async def refuse(*args, **kwargs):
        raise RuntimeError("test tried to open a real DB pool")

    monkeypatch.setattr(db, "init_pool", refuse)


@pytest.fixture
def client():
    return TestClient(main.app)
