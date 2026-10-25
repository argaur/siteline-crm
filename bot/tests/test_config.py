import logging

import commands
import main


def test_httpx_request_lines_are_not_logged():
    # httpx logs each request URL at INFO, and Telegram URLs carry the bot token.
    assert main is not None
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)


def test_cors_preflight_allows_siteline_origin(client):
    res = client.options(
        "/api/me",
        headers={"Origin": "https://siteline.gauravg.dev", "Access-Control-Request-Method": "GET"},
    )
    assert res.status_code == 200
    assert res.headers.get("access-control-allow-origin") == "https://siteline.gauravg.dev"


def test_cors_rejects_github_pages_origin(client):
    # argaur.github.io/siteline-crm only 301s to siteline.gauravg.dev now; no app code runs there.
    res = client.options(
        "/api/me",
        headers={"Origin": "https://argaur.github.io", "Access-Control-Request-Method": "GET"},
    )
    assert res.headers.get("access-control-allow-origin") is None


def test_dashboard_link_base_is_siteline_domain():
    assert commands.DASHBOARD_BASE_URL == "https://siteline.gauravg.dev/dashboard/"
