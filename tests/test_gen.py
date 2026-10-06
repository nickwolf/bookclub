import json
from types import SimpleNamespace as NS

import pytest

import gen


def _rec(title, series="", **kw):
    return {"title": title, "author": "A", "series": series, "type": "Book",
            "audiobook_available": "Yes", "confidence": 80, "reason": "Fits.",
            "tags": ["fantasy"], **kw}


def _msg(blocks, stop_reason="end_turn", stop_details=None):
    return NS(content=blocks, stop_reason=stop_reason, stop_details=stop_details,
              usage=NS(input_tokens=10, output_tokens=20))


def _text(recs):
    return NS(type="text", text=json.dumps({"recommendations": recs}))


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


class FakeClient:
    def __init__(self, msg):
        self.calls = []
        self.messages = NS(stream=self._stream)
        self.msg = msg

    def _stream(self, **kw):
        self.calls.append(kw)
        return FakeStream(self.msg)


@pytest.fixture
def fake(monkeypatch, test_db):
    monkeypatch.setattr(gen, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(gen.sync, "HARDCOVER_TOKEN", "")
    monkeypatch.setattr(gen, "_fetch_covers_sync", lambda recs: None)

    def install(msg):
        client = FakeClient(msg)
        monkeypatch.setattr(gen, "_client", lambda: client)
        return client
    return install


def _titles(db):
    with db.db() as conn:
        return {r["title"]: r for r in conn.execute("SELECT * FROM recommendations")}


def test_happy_path_inserts(fake, test_db):
    client = fake(_msg([_text([_rec("One"), _rec("Two", series="S")])]))
    assert gen.run_generation(1, 2) == {"added": 2, "unverified": 2}
    rows = _titles(test_db)
    assert set(rows) == {"One", "Two"}
    call = client.calls[0]
    assert call["max_tokens"] == 32000
    assert call["output_config"]["format"]["schema"] is gen.SCHEMA
    assert "thinking" not in call and "temperature" not in call


def test_empty_series_becomes_null(fake, test_db):
    fake(_msg([_text([_rec("One"), _rec("Two", series="S")])]))
    gen.run_generation(1, 2)
    rows = _titles(test_db)
    assert rows["One"]["series"] is None
    assert rows["Two"]["series"] == "S"


def test_thinking_block_before_text(fake, test_db):
    fake(_msg([NS(type="thinking", thinking="hmm"), _text([_rec("One")])]))
    assert gen.run_generation(1, 1) == {"added": 1, "unverified": 1}


def test_truncated_raises(fake, test_db):
    fake(_msg([_text([])], stop_reason="max_tokens"))
    with pytest.raises(RuntimeError, match="truncated"):
        gen.run_generation(1, 1)


def test_refusal_raises_with_details(fake, test_db):
    fake(_msg([], stop_reason="refusal",
              stop_details=NS(category="general_harms", explanation="not allowed")))
    with pytest.raises(RuntimeError, match="general_harms.*not allowed"):
        gen.run_generation(1, 1)


def test_no_em_dashes_in_prompt(test_db):
    prompt = gen.build_prompt(test_db.get_rec_context(), 5)
    assert "—" not in prompt and "–" not in prompt
