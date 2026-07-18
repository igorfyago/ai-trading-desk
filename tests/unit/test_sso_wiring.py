"""
SSO wired into the desk's handlers · permissive, and the precedence rule.

The estate rollout is a dark launch: recognise a token if one arrives, never
reject a request for not having one. The desk is a public demo and most of its
traffic will never hold an account, so "unchanged when anonymous" is not a
nice-to-have, it is the requirement.

The three lessons the directive asks every app for are 1 to 3. Lesson 4 is the
one it does not ask for and the one that leaks:

    lesson 1  a valid desk token identifies the caller
    lesson 2  no token · every route behaves exactly as it did before
    lesson 3  a token for another app's audience identifies nobody
    lesson 4  a valid token PLUS somebody else's session acts as the TOKEN's
              owner, never as the session in the request

Lesson 4 passes trivially today (nothing is enforced) and would still pass the
other three with the precedence inverted, because none of them sends a token
and a foreign session together. It is written now, while it is free, rather
than on activation day when it is a live IDOR.

The tokens here are stubs: real RS256 validation is sso_client's own job and
has 72 tests of its own. What these prove is WHICH session gets served, which
is this module's responsibility and nobody else's.
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web"))

from common.sso_client import SsoUser  # noqa: E402
from web import server  # noqa: E402

ALICE = "sso|alice-0001"
BOB = "sso|bob-0002"


@pytest.fixture
def client(monkeypatch):
    """A desk whose token validation is a stub keyed 'audience:subject'."""

    async def fake_validate_bearer(header_value, audience=None):
        if not header_value or not header_value.startswith("Bearer "):
            return None
        raw = header_value[7:]
        aud, _, sub = raw.partition(":")
        if aud != "desk.b4rruf3t.com":      # wrong audience identifies nobody
            return None
        return SsoUser(sub=sub, name="", email="")

    import common.sso_client as sso

    monkeypatch.setattr(sso, "validate_bearer", fake_validate_bearer)
    return TestClient(server.app)


def token(audience, subject):
    return {"Authorization": f"Bearer {audience}:{subject}"}


def desk_token(subject):
    return token("desk.b4rruf3t.com", subject)


# --------------------------------------------------------------------------
def test_lesson1_a_valid_token_identifies_the_caller(client, monkeypatch):
    """The tool runs as the token's owner even when the body says otherwise."""
    seen = {}

    def spy(persona, name, arguments, session=None):
        seen["session"] = session
        return "ok"

    monkeypatch.setattr(server, "run_tool", spy)

    r = client.post("/tool/marcus",
                    json={"name": "desk_status", "arguments": {}, "session": "browser-xyz"},
                    headers=desk_token(ALICE))
    assert r.status_code == 200, r.text
    assert seen["session"] == ALICE, "the token, not the body, decides who acted"


def test_lesson2_no_token_is_byte_identical(client, monkeypatch):
    """The public demo has no accounts · nothing may change for it."""
    seen = {}

    def spy(persona, name, arguments, session=None):
        seen["session"] = session
        return "ok"

    monkeypatch.setattr(server, "run_tool", spy)

    r = client.post("/tool/marcus",
                    json={"name": "desk_status", "arguments": {}, "session": "browser-xyz"})
    assert r.status_code == 200
    assert seen["session"] == "browser-xyz", "anonymous callers keep their own session"

    # and nothing anywhere answers 401 or 403
    for path in ("/api/trades", "/api/score", "/api/chatlog?session=browser-xyz"):
        assert client.get(path).status_code not in (401, 403), f"{path} must not gate"


def test_lesson3_wrong_audience_identifies_nobody(client, monkeypatch):
    """A token minted for the shop must not open the desk."""
    seen = {}

    def spy(persona, name, arguments, session=None):
        seen["session"] = session
        return "ok"

    monkeypatch.setattr(server, "run_tool", spy)

    r = client.post("/tool/marcus",
                    json={"name": "desk_status", "arguments": {}, "session": "browser-xyz"},
                    headers=token("mart.b4rruf3t.com", ALICE))
    assert r.status_code == 200, "a wrong-audience token is not an error during a dark launch"
    assert seen["session"] == "browser-xyz", "it attached nothing, so the body stood"


def test_lesson4_the_token_beats_someone_elses_session(client, monkeypatch):
    """THE IDOR. Bob, authenticated, acting on Alice's session."""
    seen = {}

    def spy(persona, name, arguments, session=None):
        seen["session"] = session
        return "ok"

    monkeypatch.setattr(server, "run_tool", spy)

    r = client.post("/tool/marcus",
                    json={"name": "close_position", "arguments": {}, "session": ALICE},
                    headers=desk_token(BOB))
    assert r.status_code == 200
    assert seen["session"] == BOB, "Bob is Bob whatever the body claims · he must not close Alice's position"
    assert seen["session"] != ALICE


def test_lesson4b_the_same_rule_on_the_transcript(client):
    """Reading a chat log is the same question: whose transcript is it?

    Alice has a private line in her transcript. Bob, authenticated, asks for
    hers by session id. He must get his own (empty), not hers.
    """
    from common.db import get_connection

    secret = "alice's private line, not bob's to read"
    conn = get_connection()
    conn.execute("INSERT INTO chat_log(created_at, session, agent, role, content)"
                 " VALUES (datetime('now'),?,?,?,?)",
                 (ALICE, "marcus", "user", secret))
    conn.commit()
    conn.close()

    try:
        r = client.get(f"/api/chatlog?session={ALICE}", headers=desk_token(BOB))
        assert r.status_code == 200, r.text
        contents = [m["content"] for m in r.json()["messages"]]
        assert secret not in contents, "Bob read Alice's transcript by passing her session id"
        assert contents == [], "Bob's own transcript is empty, which is what he should get"

        # and the control: Alice herself still sees it
        r = client.get(f"/api/chatlog?session={ALICE}", headers=desk_token(ALICE))
        assert secret in [m["content"] for m in r.json()["messages"]],             "the owner must still be able to read her own transcript"

        # and anonymously, nothing changed: the session parameter still stands
        r = client.get(f"/api/chatlog?session={ALICE}")
        assert secret in [m["content"] for m in r.json()["messages"]],             "the public demo is unchanged · this is what permissive means"
    finally:
        conn = get_connection()
        conn.execute("DELETE FROM chat_log WHERE content = ?", (secret,))
        conn.commit()
        conn.close()


def test_an_empty_sub_is_not_an_identity(client, monkeypatch):
    """The validator accepts an empty sub for parity with the Java client.

    Booking trades against an empty owner would be worse than staying
    anonymous, so the wiring requires a truthy sub. Flagged by the port's own
    review, pinned here.
    """
    seen = {}

    def spy(persona, name, arguments, session=None):
        seen["session"] = session
        return "ok"

    monkeypatch.setattr(server, "run_tool", spy)

    r = client.post("/tool/marcus",
                    json={"name": "desk_status", "arguments": {}, "session": "browser-xyz"},
                    headers=desk_token(""))
    assert r.status_code == 200
    assert seen["session"] == "browser-xyz", "an empty sub must fall through to the session, not become the owner"


def test_a_broken_identity_provider_degrades_to_anonymous(client, monkeypatch):
    """An SSO outage is an outage in the directory, not in the desk."""
    seen = {}

    def spy(persona, name, arguments, session=None):
        seen["session"] = session
        return "ok"

    async def exploding(header_value, audience=None):
        raise RuntimeError("jwks unreachable")

    import common.sso_client as sso

    monkeypatch.setattr(sso, "validate_bearer", exploding)
    monkeypatch.setattr(server, "run_tool", spy)

    r = client.post("/tool/marcus",
                    json={"name": "desk_status", "arguments": {}, "session": "browser-xyz"},
                    headers=desk_token(ALICE))
    assert r.status_code == 200, "a dead identity provider must not 500 the desk"
    assert seen["session"] == "browser-xyz"
