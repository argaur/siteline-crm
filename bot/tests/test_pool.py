import db


def test_dashboard_request_opens_pool_first(client, monkeypatch):
    # Startup is not guaranteed on a serverless cold start, so each request opens the pool.
    order = []

    async def init_pool():
        order.append("pool")

    async def get_user_by_id(uid):
        order.append("query")
        return {"id": uid, "first_name": "Mona", "role": "manager"}

    monkeypatch.setattr(db, "init_pool", init_pool)
    monkeypatch.setattr(db, "get_user_by_id", get_user_by_id)
    token = db.make_dashboard_token(1, "manager")
    r = client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert order == ["pool", "query"]
