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

import main  # noqa: E402


@pytest.fixture
def client():
    return TestClient(main.app)
