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
