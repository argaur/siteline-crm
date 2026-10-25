import commands


def test_cors_preflight_allows_siteline_origin(client):
    res = client.options(
        "/api/me",
        headers={"Origin": "https://siteline.gauravg.dev", "Access-Control-Request-Method": "GET"},
    )
    assert res.status_code == 200
    assert res.headers.get("access-control-allow-origin") == "https://siteline.gauravg.dev"


def test_dashboard_link_base_is_siteline_domain():
    assert commands.DASHBOARD_BASE_URL == "https://siteline.gauravg.dev/dashboard/"
