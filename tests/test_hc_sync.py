import json

import httpx
import pytest

import sync


def _client(statuses):
    calls = iter(statuses)

    def handler(request):
        code = next(calls)
        headers = {"retry-after": "3"} if code == 429 else {}
        return httpx.Response(code, headers=headers, json={"data": {}})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_hc_post_retries_after_429(test_db, monkeypatch):
    slept = []
    monkeypatch.setattr(sync, "_sleep", slept.append)
    resp = sync._hc_post(_client([429, 429, 200]), {"query": "q"})
    assert resp.status_code == 200
    assert slept == [3.0, 3.0]


def test_hc_post_gives_up_after_attempts(test_db, monkeypatch):
    monkeypatch.setattr(sync, "_sleep", lambda s: None)
    with pytest.raises(httpx.HTTPStatusError):
        sync._hc_post(_client([429] * 3), {"query": "q"}, attempts=3)


def _hc_book(bid, title="T"):
    return {"book": {"id": bid, "title": f"{title} {bid}", "image": None,
                     "contributions": [], "book_series": []},
            "status_id": 3, "rating": None}


def _fake_hc(monkeypatch, pages, queries=None):
    """Patch sync.httpx.Client to serve pages in order; an int entry is an HTTP status."""
    it = iter(pages)

    def handler(request):
        if queries is not None:
            queries.append(json.loads(request.content)["query"])
        page = next(it)
        if isinstance(page, int):
            return httpx.Response(page, json={})
        if isinstance(page, dict):
            return httpx.Response(200, json=page)
        return httpx.Response(200, json={"data": {"me": [{"user_books": page}]}})

    real = httpx.Client
    monkeypatch.setattr(sync.httpx, "Client",
                        lambda **kw: real(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(sync, "_sleep", lambda s: None)


def _seed(db, ids):
    for i in ids:
        db.upsert_hc_book(i, f"T {i}", "A", None, None, None, 3, None)


def _stored(db):
    with db.db() as conn:
        return sorted(r[0] for r in conn.execute("SELECT id FROM hc_books"))


def _add_rec(db, hc_book_id):
    with db.db() as conn:
        cur = conn.execute("INSERT INTO recommendations (title, hc_book_id) VALUES ('R', ?)",
                           (hc_book_id,))
        return cur.lastrowid


def test_sync_prunes_removed_books(test_db, monkeypatch):
    _seed(test_db, [1, 2, 3, 4])
    _fake_hc(monkeypatch, [[_hc_book(1), _hc_book(2), _hc_book(3)]])
    assert sync.sync_hardcover() == 3
    assert _stored(test_db) == [1, 2, 3]


def test_prune_detaches_linked_recommendation(test_db, monkeypatch):
    _seed(test_db, [1, 2, 3])
    rec_id = _add_rec(test_db, 3)
    _fake_hc(monkeypatch, [[_hc_book(1), _hc_book(2)]])
    sync.sync_hardcover()
    assert _stored(test_db) == [1, 2]
    with test_db.db() as conn:
        row = conn.execute("SELECT hc_book_id FROM recommendations WHERE id = ?",
                           (rec_id,)).fetchone()
    assert row is not None and row[0] is None


def test_prune_keeps_link_to_kept_book(test_db, monkeypatch):
    _seed(test_db, [1, 2])
    rec_id = _add_rec(test_db, 1)
    _fake_hc(monkeypatch, [[_hc_book(1), _hc_book(2)]])
    sync.sync_hardcover()
    with test_db.db() as conn:
        assert conn.execute("SELECT hc_book_id FROM recommendations WHERE id = ?",
                            (rec_id,)).fetchone()[0] == 1


def test_prune_spans_pages(test_db, monkeypatch):
    _seed(test_db, [1000])
    page1 = [_hc_book(i) for i in range(1, 101)]
    _fake_hc(monkeypatch, [page1, [_hc_book(101)]])
    assert sync.sync_hardcover() == 101
    assert 1000 not in _stored(test_db)
    assert len(_stored(test_db)) == 101


def test_http_error_mid_pagination_deletes_nothing(test_db, monkeypatch):
    _seed(test_db, [1000, 1001])
    _fake_hc(monkeypatch, [[_hc_book(i) for i in range(1, 101)], 500])
    with pytest.raises(httpx.HTTPStatusError):
        sync.sync_hardcover()
    assert 1000 in _stored(test_db) and 1001 in _stored(test_db)


def test_graphql_error_deletes_nothing(test_db, monkeypatch):
    _seed(test_db, [1, 2])
    _fake_hc(monkeypatch, [{"errors": [{"message": "boom"}]}])
    with pytest.raises(RuntimeError):
        sync.sync_hardcover()
    assert _stored(test_db) == [1, 2]


def test_zero_books_skips_prune(test_db, monkeypatch):
    _seed(test_db, [1, 2])
    _fake_hc(monkeypatch, [[]])
    assert sync.sync_hardcover() == 0
    assert _stored(test_db) == [1, 2]
    with test_db.db() as conn:
        msg = conn.execute("SELECT message FROM app_log WHERE level = 'warning'").fetchone()
    assert "zero books" in msg[0]


def test_below_half_skips_prune(test_db, monkeypatch):
    _seed(test_db, [1, 2, 3, 4, 5])
    _fake_hc(monkeypatch, [[_hc_book(1), _hc_book(2)]])
    sync.sync_hardcover()
    assert _stored(test_db) == [1, 2, 3, 4, 5]


def test_exactly_half_still_prunes(test_db, monkeypatch):
    _seed(test_db, [1, 2, 3, 4])
    _fake_hc(monkeypatch, [[_hc_book(1), _hc_book(2)]])
    sync.sync_hardcover()
    assert _stored(test_db) == [1, 2]


def test_query_orders_by_id(test_db, monkeypatch):
    queries = []
    _fake_hc(monkeypatch, [[_hc_book(1)]], queries)
    sync.sync_hardcover()
    assert "order_by: {id: asc}" in queries[0]
