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


def _hc_book(bid, row_id=None):
    return {"id": row_id if row_id is not None else bid,
            "book": {"id": bid, "title": f"T {bid}", "image": None,
                     "contributions": [], "book_series": []},
            "status_id": 3, "rating": None}


def _count_resp(n):
    return {"data": {"me": [{"user_books_aggregate": {"aggregate": {"count": n}}}]}}


def _fake_hc(monkeypatch, pages, count=None, requests=None):
    """Patch sync.httpx.Client. pages are served in order (int = HTTP status, dict = raw body,
    list = user_books). count is the aggregate reply: int, dict body, or an HTTP status via tuple."""
    it = iter(pages)

    def handler(request):
        body = json.loads(request.content)
        if requests is not None:
            requests.append(body)
        if "user_books_aggregate" in body["query"]:
            if isinstance(count, tuple):
                return httpx.Response(count[0], json={})
            if isinstance(count, dict):
                return httpx.Response(200, json=count)
            return httpx.Response(200, json=_count_resp(count))
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


def _warnings(db):
    with db.db() as conn:
        return [r[0] for r in conn.execute("SELECT message FROM app_log WHERE level = 'warning'")]


def test_sync_prunes_removed_books(test_db, monkeypatch):
    _seed(test_db, [1, 2, 3, 4])
    _fake_hc(monkeypatch, [[_hc_book(1), _hc_book(2), _hc_book(3)], []], count=3)
    assert sync.sync_hardcover() == 3
    assert _stored(test_db) == [1, 2, 3]


def test_prune_detaches_linked_recommendation(test_db, monkeypatch):
    _seed(test_db, [1, 2, 3])
    rec_id = _add_rec(test_db, 3)
    _fake_hc(monkeypatch, [[_hc_book(1), _hc_book(2)], []], count=2)
    sync.sync_hardcover()
    assert _stored(test_db) == [1, 2]
    with test_db.db() as conn:
        row = conn.execute("SELECT hc_book_id FROM recommendations WHERE id = ?",
                           (rec_id,)).fetchone()
    assert row is not None and row[0] is None


def test_prune_keeps_link_to_kept_book(test_db, monkeypatch):
    _seed(test_db, [1, 2])
    rec_id = _add_rec(test_db, 1)
    _fake_hc(monkeypatch, [[_hc_book(1), _hc_book(2)], []], count=2)
    sync.sync_hardcover()
    with test_db.db() as conn:
        assert conn.execute("SELECT hc_book_id FROM recommendations WHERE id = ?",
                            (rec_id,)).fetchone()[0] == 1


def test_large_removal_prunes(test_db, monkeypatch):
    _seed(test_db, list(range(1, 11)))
    _fake_hc(monkeypatch, [[_hc_book(1), _hc_book(2)], []], count=2)
    sync.sync_hardcover()
    assert _stored(test_db) == [1, 2]


def test_keyset_variables_and_short_page_continues(test_db, monkeypatch):
    reqs = []
    _fake_hc(monkeypatch, [[_hc_book(5, 10), _hc_book(6, 20)], [_hc_book(7, 35)], []],
             count=3, requests=reqs)
    assert sync.sync_hardcover() == 3
    pages = [r for r in reqs if "user_books_aggregate" not in r["query"]]
    assert [p["variables"]["after"] for p in pages] == [0, 20, 35]
    assert "order_by: {id: asc}" in pages[0]["query"]
    assert "offset" not in pages[0]["query"]


def test_null_user_books_raises_and_keeps_data(test_db, monkeypatch):
    _seed(test_db, [1, 2])
    _fake_hc(monkeypatch, [{"data": {"me": [{"user_books": None}]}}], count=0)
    with pytest.raises(RuntimeError):
        sync.sync_hardcover()
    assert _stored(test_db) == [1, 2]


def test_http_error_mid_pagination_deletes_nothing(test_db, monkeypatch):
    _seed(test_db, [1000, 1001])
    _fake_hc(monkeypatch, [[_hc_book(i) for i in range(1, 101)], 500], count=101)
    with pytest.raises(httpx.HTTPStatusError):
        sync.sync_hardcover()
    assert 1000 in _stored(test_db) and 1001 in _stored(test_db)


def test_graphql_error_deletes_nothing(test_db, monkeypatch):
    _seed(test_db, [1, 2])
    _fake_hc(monkeypatch, [{"errors": [{"message": "boom"}]}], count=0)
    with pytest.raises(RuntimeError):
        sync.sync_hardcover()
    assert _stored(test_db) == [1, 2]


def test_zero_books_skips_prune(test_db, monkeypatch):
    _seed(test_db, [1, 2])
    _fake_hc(monkeypatch, [[]], count=0)
    assert sync.sync_hardcover() == 0
    assert _stored(test_db) == [1, 2]
    assert "zero books" in _warnings(test_db)[0]


def test_count_mismatch_skips_prune(test_db, monkeypatch):
    _seed(test_db, [1, 2, 3, 4])
    _fake_hc(monkeypatch, [[_hc_book(1), _hc_book(2)], []], count=3)
    assert sync.sync_hardcover() == 2
    assert _stored(test_db) == [1, 2, 3, 4]
    assert "saw 2 of 3" in _warnings(test_db)[0]


def test_count_query_error_skips_prune_but_sync_succeeds(test_db, monkeypatch):
    _seed(test_db, [1, 2, 3])
    _fake_hc(monkeypatch, [[_hc_book(1)], []], count=(500,))
    assert sync.sync_hardcover() == 1
    assert _stored(test_db) == [1, 2, 3]


def test_count_odd_shape_skips_prune(test_db, monkeypatch):
    _seed(test_db, [1, 2])
    _fake_hc(monkeypatch, [[_hc_book(1)], []], count={"data": {"me": []}})
    assert sync.sync_hardcover() == 1
    assert _stored(test_db) == [1, 2]
