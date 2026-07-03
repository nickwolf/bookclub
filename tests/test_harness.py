def test_db_fixture_creates_schema(test_db):
    with test_db.db() as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"profiles", "recommendations", "rec_interactions", "queue"} <= tables


def test_default_profile_seeded(test_db):
    p = test_db.get_profile(1)
    assert p is not None


def test_client_serves_home(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Recommendations" in resp.text
