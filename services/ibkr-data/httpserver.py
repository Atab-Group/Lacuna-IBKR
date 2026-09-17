#!/usr/bin/env python3
"""ibkr-data over Streamable HTTP, so any session can reach the market data.

The stdio server in ``mcpserver.py`` is found by the plugin only relative to
the folder Claude was started in (``${CLAUDE_PROJECT_DIR}``). That means a
session opened anywhere else gets no market data at all, and Claude Cowork,
which never starts in this clone, cannot reach it under any circumstances.
This front end fixes both by putting the same market-data tools on a local HTTP port
that a client reaches by URL and bearer token, exactly the way the two remote
Lacuna services are already reached.

Nothing about the protocol is reimplemented here. Each POST is unwrapped to a
JSON-RPC message and handed to ``mcpserver.handle_message``, which is the same
function the stdio loop calls, so the two transports cannot drift apart. The
``mcpserver.Session`` and ``Ctx`` are built once at startup and shared by every
request, which is what the stdio ``serve()`` does too: the contract cache and
the negotiated era live there, and a per-request session would throw both away.

It binds 127.0.0.1 and nothing else. There is no TLS and no proxy in front of
it, so the token is the only thing between a caller and the data, and a token
travelling in clear text must never leave the loopback interface.

Usage:
    .venv/bin/python services/ibkr-data/httpserver.py
    .venv/bin/python services/ibkr-data/httpserver.py --add-token nic-laptop
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import mcpserver  # noqa: E402
import store      # noqa: E402
import tokens     # noqa: E402

SERVER_NAME = mcpserver.SERVER_NAME
SERVER_VERSION = mcpserver.SERVER_VERSION

DEFAULT_PORT = 8770     # 8765 to 8767 are taken on a Lacuna laptop: the
                        # notification hub, the news MCP, and the tunnel to the
                        # news feed. 8770 leaves that block alone.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_TOKENS = str(HERE / "tokens.json")

# MCP allocates -32001 for an unauthorised call. The 401 body is the same shape
# the notification-hub server sends, down to the wording, so a client that
# already reports one of these clearly reports the other identically.
E_UNAUTHORIZED = -32001

# The tool layer already refuses a payload over mcpserver.MAX_RESPONSE_BYTES.
# This second cap is on the whole JSON-RPC envelope, and it sits a little above
# the tool cap so that the polite refusal, which is itself a response, always
# fits through.
ENVELOPE_SLACK = 8192

_LOG_LOCK = threading.Lock()
_LOG_PATH = os.environ.get("IBKR_MCP_LOG")


def log(msg):
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {msg}"
    with _LOG_LOCK:
        if _LOG_PATH:
            try:
                with open(_LOG_PATH, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                return
            except OSError:
                pass    # fall through to stderr rather than lose the line
        print(line, file=sys.stderr, flush=True)


class Config:
    """What one running server needs. Built once, shared by every request."""

    def __init__(self, tokens_path, host=DEFAULT_HOST, port=DEFAULT_PORT, root_dir=None):
        self.tokens = tokens.TokenStore(tokens_path)
        self.host = host
        self.port = port
        self.root_dir = root_dir
        self.session = mcpserver.Session(mcpserver.Ctx(root_dir=root_dir))
        # dispatch mutates session.era on initialize, and two clients can be
        # mid-request at once on a threading server, so the shared session is
        # taken under a lock rather than raced.
        self.session_lock = threading.Lock()


def config_from_env(overrides=None):
    """Resolve config from flags first, then the environment, then defaults.

    The env names match the stdio server's own (LACUNA_IBKR_ROOT, IBKR_MCP_LOG)
    so one exported variable configures whichever transport is running.
    """
    overrides = overrides or {}
    tokens_path = overrides.get("tokens") or os.environ.get("IBKR_MCP_TOKENS") or DEFAULT_TOKENS
    host = overrides.get("host") or os.environ.get("IBKR_MCP_HOST") or DEFAULT_HOST
    port = overrides.get("port") or os.environ.get("IBKR_MCP_PORT") or DEFAULT_PORT
    root = overrides.get("root") or os.environ.get("LACUNA_IBKR_ROOT") or None
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    return Config(tokens_path, host, port, root)


class Handler(BaseHTTPRequestHandler):
    server_version = f"{SERVER_NAME}/{SERVER_VERSION}"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # ---- plumbing

    def log_message(self, fmt, *args):
        # Silences the default stderr access log. Everything worth keeping is
        # logged explicitly, with the token name attached.
        return

    @property
    def cfg(self):
        return self.server.cfg

    def path_only(self):
        return self.path.split("?", 1)[0].rstrip("/") or "/"

    def send_bytes(self, status, body, ctype="application/json", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def send_obj(self, status, obj, extra=None):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        cap = mcpserver.MAX_RESPONSE_BYTES
        if len(body) > cap + ENVELOPE_SLACK:
            # Better to refuse than to hand back half a megabyte of JSON that
            # crowds out everything else in the caller's context window.
            req_id = obj.get("id") if isinstance(obj, dict) else None
            obj = mcpserver.rpc_error(
                req_id, mcpserver.E_INTERNAL,
                f"the response produced {len(body)} bytes, over the {cap} byte cap. "
                f"Narrow the window, lower max_bars, or aggregate in ibkr_query_sql.")
            body = json.dumps(obj).encode("utf-8")
        self.send_bytes(status, body, "application/json", extra)

    # ---- auth

    def authorise(self):
        """Return the token name, or None after sending a 401."""
        presented = tokens.bearer_from_header(self.headers.get("Authorization"))
        name = self.cfg.tokens.check(presented) if presented else None
        if name:
            return name
        reason = "no bearer token" if not presented else "bad or revoked token"
        agent = self.headers.get("User-Agent", "-")
        log(f"401 {reason} ip={self.client_address[0]} method={self.command} "
            f"path={self.path_only()} agent={agent!r}")
        self.send_obj(401,
                      mcpserver.rpc_error(None, E_UNAUTHORIZED,
                                          "Unauthorized: a valid bearer token is required"),
                      extra={"WWW-Authenticate": f'Bearer realm="{SERVER_NAME}"'})
        return None

    # ---- routes

    def do_GET(self):
        try:
            path = self.path_only()
            if path == "/healthz":
                return self.healthz()
            if path == "/mcp":
                if self.authorise() is None:
                    return
                # This server never sends anything unprompted, so there is no
                # stream to hold open. 405 is the spec's answer for a transport
                # that does not offer the GET stream, and a client reads it and
                # falls back to plain POST.
                return self.send_obj(405, mcpserver.rpc_error(
                    None, mcpserver.E_METHOD_NOT_FOUND,
                    "this server answers POST /mcp only; it has nothing to stream"))
            return self.send_obj(404, mcpserver.rpc_error(
                None, mcpserver.E_METHOD_NOT_FOUND, f"no route {path}"))
        except Exception:
            self.fail_safely()

    def do_HEAD(self):
        try:
            if self.path_only() == "/healthz":
                return self.send_bytes(200, b"", "application/json")
            return self.send_bytes(404, b"", "application/json")
        except Exception:
            self.fail_safely()

    def do_POST(self):
        try:
            # Read the body BEFORE anything that can return early.
            #
            # A response sent without consuming the request body leaves those
            # bytes in the socket, and the next request on that kept-alive
            # connection parses the leftovers as its request line and dies. The
            # notification-hub server hit exactly this live on 26 Aug 2026: one
            # 401 with a body made the following valid request come back 400.
            # The failure lands on a different request from the one that caused
            # it, which is what makes it worth this comment.
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > mcpserver.MAX_REQUEST_BYTES:
                # The body was deliberately not drained, so this connection can
                # no longer be trusted for a second request.
                self.close_connection = True
                return self.send_obj(413, mcpserver.rpc_error(
                    None, mcpserver.E_INVALID_REQUEST,
                    f"request body over {mcpserver.MAX_REQUEST_BYTES} bytes"))
            raw = self.rfile.read(length) if length else b""

            if self.path_only() != "/mcp":
                return self.send_obj(404, mcpserver.rpc_error(
                    None, mcpserver.E_METHOD_NOT_FOUND, f"no route {self.path_only()}"))
            name = self.authorise()
            if name is None:
                return
            return self.handle_rpc(name, raw)
        except Exception:
            self.fail_safely()

    def fail_safely(self):
        """Last line of defence. One bad request must never take the server down."""
        log("ERROR unhandled in request handler")
        log(traceback.format_exc().rstrip())
        try:
            self.send_obj(500, mcpserver.rpc_error(None, mcpserver.E_INTERNAL, "internal error"))
        except Exception:
            pass

    # ---- the MCP endpoint

    def handle_rpc(self, token_name, raw):
        try:
            message = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, ValueError) as exc:
            return self.send_obj(400, mcpserver.rpc_error(
                None, mcpserver.E_PARSE, f"cannot parse JSON: {exc}"))

        if not isinstance(message, (dict, list)):
            return self.send_obj(400, mcpserver.rpc_error(
                None, mcpserver.E_INVALID_REQUEST, "body must be a JSON-RPC object"))

        with self.cfg.session_lock:
            response = mcpserver.handle_message(self.cfg.session, message)

        if response is None:
            # A notification carries no id and gets no body, per the transport
            # spec. 202 is what says "taken" without inventing a result.
            return self.send_bytes(202, b"", "application/json")

        method = message.get("method") if isinstance(message, dict) else "batch"
        params = message.get("params") if isinstance(message, dict) else None
        tool = (params or {}).get("name") if method == "tools/call" else None
        log(f"ok token={token_name} method={method}" + (f" tool={tool}" if tool else ""))
        return self.send_obj(200, response)

    def healthz(self):
        """Unauthenticated liveness check, the one route with no token.

        The watchdog has to be able to ask whether the server is up without
        holding a credential. It deliberately does not probe IB Gateway: that
        costs a 1.5 second socket timeout when the session is down, which is
        most of a weekend, and a down gateway is not this process being
        unhealthy because the cached bars still serve. Gateway state belongs to
        ibkr_status, which is a tool and needs a token.
        """
        try:
            root = str(store.root(self.cfg.root_dir))
        except Exception as exc:
            log(f"healthz failed: {type(exc).__name__}: {exc}")
            return self.send_obj(503, {"status": "error", "error": f"{type(exc).__name__}: {exc}"})
        return self.send_obj(200, {
            "status": "ok",
            "server": SERVER_NAME,
            "version": SERVER_VERSION,
            "transport": "streamable-http",
            "store_root": root,
            "tools": len(mcpserver.TOOL_NAMES),
            "live_tokens": sum(1 for t in self.cfg.tokens.tokens() if not t["revoked"]),
        })


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, cfg):
        self.cfg = cfg
        super().__init__(addr, Handler)

    def process_request_thread(self, request, client_address):
        """Give each connection thread the event loop ``ib_async`` expects.

        ``ThreadingHTTPServer`` creates a fresh worker thread per connection,
        while Python 3.11 no longer creates an asyncio loop implicitly in a
        worker. Even the synchronous ib_async facade asks for that thread's
        loop, so live market-data calls otherwise fail with ``RuntimeError``
        before they can reach Gateway. A connection owns its loop for its
        whole lifetime, including kept-alive requests, and closes it when the
        worker exits.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            super().process_request_thread(request, client_address)
        finally:
            try:
                loop.close()
            finally:
                asyncio.set_event_loop(None)

    def handle_error(self, request, client_address):
        # socketserver prints a traceback to stderr and carries on. Routing it
        # through log() keeps everything in one file, and carrying on is exactly
        # the behaviour we want.
        log(f"ERROR connection from {client_address}")
        log(traceback.format_exc().rstrip())


def make_server(cfg):
    return Server((cfg.host, cfg.port), cfg)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv=None):
    global _LOG_PATH

    parser = argparse.ArgumentParser(description="ibkr-data MCP server (Streamable HTTP)")
    parser.add_argument("--add-token", metavar="NAME",
                        help="generate a token, append it to tokens.json, print it once, and exit")
    parser.add_argument("--tokens", help="path to tokens.json")
    parser.add_argument("--port", type=int, help=f"port to bind on {DEFAULT_HOST} (default {DEFAULT_PORT})")
    parser.add_argument("--host", help=f"address to bind (default {DEFAULT_HOST})")
    parser.add_argument("--log", help="append log lines here instead of stderr")
    parser.add_argument("--root", help="store root, overriding LACUNA_IBKR_ROOT")
    args = parser.parse_args(argv)

    _LOG_PATH = args.log or _LOG_PATH

    cfg = config_from_env({"tokens": args.tokens, "port": args.port,
                           "host": args.host, "root": args.root})

    if args.add_token:
        try:
            token = tokens.add_token(cfg.tokens.path, args.add_token)
        except ValueError as exc:
            sys.exit(f"FATAL: {exc}")
        print(f"token for {args.add_token!r}, shown once and not stored anywhere else:\n")
        print(token)
        print(f"\nwritten to {cfg.tokens.path} (mode 600)")
        return 0

    if cfg.host != DEFAULT_HOST:
        # Not refused, because a test binds an explicit address, but said out
        # loud: the token crosses the wire in clear text and there is no TLS.
        log(f"WARN: bound to {cfg.host}, not {DEFAULT_HOST}; tokens travel unencrypted")
    if not cfg.tokens.tokens():
        log(f"WARN: no usable tokens in {cfg.tokens.path}; every /mcp request will get a 401")

    httpd = make_server(cfg)
    log(f"listening on http://{cfg.host}:{httpd.server_address[1]}/mcp "
        f"store={store.root(cfg.root_dir)} tokens={cfg.tokens.path}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
