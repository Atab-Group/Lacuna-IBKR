"""Tests for the HTTP transport.

Everything here runs against a real server bound to 127.0.0.1 on an ephemeral
port, because the thing under test is the transport and a mocked socket would
test nothing. Nothing reaches IB Gateway: the only tools called are tools/list
and the protocol handshake, and the Ctx is pointed at a temporary store.
"""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request

import pytest

import httpserver
import mcpserver
import tokens as tokenlib


@pytest.fixture()
def live(tmp_path):
    """A running server, its base URL, and a freshly minted token."""
    tokens_path = str(tmp_path / "tokens.json")
    token = tokenlib.add_token(tokens_path, "test-client")

    cfg = httpserver.Config(tokens_path, host="127.0.0.1", port=0, root_dir=str(tmp_path))
    httpd = httpserver.make_server(cfg)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base, token, tokens_path, cfg
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def post(base, payload, token=None):
    """POST one JSON-RPC message. Returns (status, parsed body or raw text)."""
    req = urllib.request.Request(
        base + "/mcp", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, (json.loads(raw) if raw else None)


def get(base, path, token=None):
    req = urllib.request.Request(base + path, method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, (json.loads(raw) if raw else None)


LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


# ------------------------------------------------------------------ auth ----

def test_a_minted_token_is_accepted(live):
    base, token, _, _ = live
    status, body = post(base, LIST, token)

    assert status == 200
    assert body["id"] == 1
    assert "error" not in body


def test_a_missing_token_is_a_401(live):
    base, _, _, _ = live
    status, body = post(base, LIST)

    assert status == 401
    assert body["error"]["code"] == httpserver.E_UNAUTHORIZED
    assert body["error"]["message"] == "Unauthorized: a valid bearer token is required"


def test_a_wrong_token_is_a_401(live):
    base, _, _, _ = live
    status, body = post(base, LIST, "not-the-token")

    assert status == 401
    assert body["error"]["code"] == httpserver.E_UNAUTHORIZED


def test_a_revoked_token_is_a_401_without_a_restart(live):
    """Revocation is a file edit, and the store reloads on mtime.

    The server is not restarted between the two calls here, because the whole
    point of the mtime check in tokens.py is that cutting a client off does not
    need one.
    """
    base, token, tokens_path, _ = live
    assert post(base, LIST, token)[0] == 200

    with open(tokens_path, encoding="utf-8") as fh:
        data = json.load(fh)
    data["tokens"][0]["revoked"] = True
    with open(tokens_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)

    status, body = post(base, LIST, token)
    assert status == 401
    assert body["error"]["code"] == httpserver.E_UNAUTHORIZED


# --------------------------------------------------------------- healthz ----

def test_healthz_needs_no_token(live):
    base, _, _, _ = live
    status, body = get(base, "/healthz")

    assert status == 200
    assert body["status"] == "ok"
    assert body["server"] == "ibkr-data"
    assert body["tools"] == 14


def test_healthz_is_the_only_unauthenticated_route(live):
    base, _, _, _ = live
    assert get(base, "/mcp")[0] == 401
    assert post(base, LIST)[0] == 401
    assert get(base, "/anything-else")[0] == 404


# ----------------------------------------------------------------- tools ----

def test_tools_list_over_http_returns_market_data_tools(live):
    base, token, _, _ = live
    status, body = post(base, LIST, token)

    assert status == 200
    names = [t["name"] for t in body["result"]["tools"]]
    assert len(names) == 14
    assert names == mcpserver.TOOL_NAMES
    assert "ibkr_get_bars" in names


def test_initialize_then_a_call_shares_one_session(live):
    """The negotiated era has to survive between two POSTs.

    Each request is independent on the wire, but the Session is built once at
    startup, so an initialize that pins the handshake era still governs the
    next call. Without that, a handshake-era client would get 2026-07-28
    envelopes it cannot read.
    """
    base, token, _, cfg = live
    status, body = post(base, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-06-18"}}, token)
    assert status == 200
    assert body["result"]["protocolVersion"] == "2025-06-18"
    assert cfg.session.era == "2025-06-18"

    status, body = post(base, LIST, token)
    assert status == 200
    # A handshake-era result carries no resultType, which is how we know the
    # era carried across the two requests.
    assert "resultType" not in body["result"]


def test_request_worker_has_an_asyncio_loop_for_ib_async(live, monkeypatch):
    """Authenticated dispatch runs where the synchronous ib_async facade can."""
    base, token, _, _ = live
    seen_open = []

    def fake_handle_message(session, message):
        loop = asyncio.get_event_loop()
        seen_open.append(not loop.is_closed())
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": {"ok": True}}

    monkeypatch.setattr(mcpserver, "handle_message", fake_handle_message)
    status, body = post(base, LIST, token)

    assert status == 200
    assert body["result"] == {"ok": True}
    assert seen_open == [True]


def test_a_notification_gets_no_body(live):
    base, token, _, _ = live
    status, body = post(base, {"jsonrpc": "2.0", "method": "notifications/initialized"}, token)

    assert status == 202
    assert body is None


def test_an_unparseable_body_is_a_400(live):
    base, token, _, _ = live
    req = urllib.request.Request(base + "/mcp", data=b"{not json",
                                 headers={"Authorization": f"Bearer {token}"}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 400
    assert json.loads(exc.value.read())["error"]["code"] == mcpserver.E_PARSE


# ------------------------------------------------------------- size caps ----

def test_an_oversize_response_is_capped(live, monkeypatch):
    """A runaway response must not eat the caller's context window.

    The cap is lowered rather than a giant response being generated, because
    what is under test is the envelope check in send_obj, not the tool that
    happens to trip it. The slack goes to zero with it: the real slack is 8 KB
    and tools/list is smaller than that, so leaving it in place would put the
    threshold above the response and quietly test nothing.
    """
    base, token, _, _ = live
    monkeypatch.setattr(mcpserver, "MAX_RESPONSE_BYTES", 400)
    monkeypatch.setattr(httpserver, "ENVELOPE_SLACK", 0)

    status, body = post(base, LIST, token)
    assert status == 200
    assert "result" not in body
    assert body["error"]["code"] == mcpserver.E_INTERNAL
    assert "over the 400 byte cap" in body["error"]["message"]


def test_an_oversize_request_is_refused(live):
    """An oversize body is refused on the Content-Length, not after reading it.

    Two outcomes are correct and which one a run gets is a race. The server
    answers 413 and hangs up without draining the body, deliberately, because
    reading a megabyte it has already decided to reject is the thing being
    avoided. The client is still writing when the socket closes, so it sees
    either the 413 or a broken pipe depending on how much fitted in the kernel
    buffer first. Asserting only on the 413 makes this test flaky, which it was
    on the first run that had the machine busy.
    """
    base, token, _, _ = live
    fat = {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
           "params": {"pad": "x" * (mcpserver.MAX_REQUEST_BYTES + 10)}}
    try:
        status, body = post(base, fat, token)
    except (urllib.error.URLError, ConnectionError):
        return    # hung up on, which is the refusal
    assert status == 413
    assert body["error"]["code"] == mcpserver.E_INVALID_REQUEST

    # Whichever way it went, the server is still serving. A refusal that took
    # the process down would be worse than accepting the body.
    assert post(base, LIST, token)[0] == 200


# ---------------------------------------------------------------- tokens ----

def test_a_second_live_token_with_the_same_name_is_refused(tmp_path):
    """Two live tokens under one name would make revocation ambiguous."""
    path = str(tmp_path / "tokens.json")
    tokenlib.add_token(path, "laptop")
    with pytest.raises(ValueError):
        tokenlib.add_token(path, "laptop")


@pytest.mark.parametrize("document", [[], "token", 17, None])
def test_a_non_object_token_document_fails_closed(tmp_path, document):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    assert tokenlib.load_tokens(str(path)) == []
    assert tokenlib.TokenStore(str(path)).check("anything") is None


def test_the_token_file_is_not_world_readable(tmp_path):
    import os
    path = str(tmp_path / "tokens.json")
    tokenlib.add_token(path, "laptop")
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
