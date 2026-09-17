#!/usr/bin/env python3
"""Bearer tokens for the ibkr-data MCP server.

Tokens live in tokens.json beside the server:

    {"tokens": [{"name": "nic-laptop", "token": "...",
                 "created": "2026-08-26", "revoked": false}]}

Each token carries a name so one client can be cut off without rotating the
others. Revoking is a two-character edit: set "revoked" to true. The store
stats the file on every check and reloads when it changes, so a revocation
takes effect on the next request and never needs a restart.

Comparison is hmac.compare_digest against every entry, with no early exit, so
the time a request takes does not leak how much of a token was correct.

This module reads tokens.json and, under --add-token only, appends to it. The
MCP server itself never calls the writing path.
"""

import hmac
import json
import os
import secrets
from datetime import date


def bearer_from_header(value):
    """Pull the token out of an Authorization header value.

    Returns None for anything that is not exactly one Bearer credential. The
    scheme is matched case-insensitively because RFC 7235 says it is.
    """
    if not value:
        return None
    parts = value.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1] or None


def load_tokens(path):
    """Read tokens.json. A missing or broken file means no valid tokens.

    Failing closed matters more than failing loudly here. If the file is
    unreadable the server keeps serving /healthz and rejects every /mcp
    request, which is the safe direction even on a loopback-only listener:
    anything else running as this user could otherwise read the market data.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    entries = data.get("tokens")
    if not isinstance(entries, list):
        return []
    out = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        token = entry.get("token")
        if not isinstance(token, str) or not token:
            continue
        out.append({
            "name": str(entry.get("name") or "unnamed"),
            "token": token,
            "created": str(entry.get("created") or ""),
            "revoked": bool(entry.get("revoked", False)),
        })
    return out


class TokenStore:
    """Token list plus an mtime check, so revocation needs no restart."""

    def __init__(self, path):
        self.path = path
        self._key = object()   # sentinel that no stat result can equal
        self._tokens = []

    def _stat_key(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        # Size joins mtime because two writes inside one filesystem timestamp
        # tick are possible on a coarse-grained mtime, and a revocation edit
        # that changed nothing else would then be missed.
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def reload_if_changed(self):
        key = self._stat_key()
        if key == self._key:
            return False
        self._key = key
        self._tokens = load_tokens(self.path)
        return True

    def tokens(self):
        self.reload_if_changed()
        return list(self._tokens)

    def check(self, presented):
        """Return the name of the matching live token, or None.

        Every entry is compared even after a match is found. An early return
        would make a request against a long token list finish measurably
        sooner once the right prefix was guessed.
        """
        self.reload_if_changed()
        if not presented:
            return None
        want = presented.encode("utf-8")
        matched = None
        for entry in self._tokens:
            same = hmac.compare_digest(entry["token"].encode("utf-8"), want)
            if same and not entry["revoked"] and matched is None:
                matched = entry["name"]
        return matched


def add_token(path, name, today=None):
    """Generate a token, append it to tokens.json, and lock the file down.

    Returns the raw token. It is printed once by the CLI and never stored
    anywhere else, so a lost token is replaced rather than recovered.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("a token needs a name")

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        data = {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON, refusing to overwrite it: {exc}")

    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a JSON object, refusing to overwrite it")
    entries = data.get("tokens")
    if not isinstance(entries, list):
        entries = []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name") == name and not entry.get("revoked"):
            raise ValueError(f"a live token named {name!r} already exists; revoke it first")

    token = secrets.token_urlsafe(32)
    entries.append({
        "name": name,
        "token": token,
        "created": (today or date.today()).isoformat(),
        "revoked": False,
    })
    data["tokens"] = entries

    # Written through a temp file in the same directory so a crash mid-write
    # cannot leave a truncated tokens.json that locks every client out. The
    # temp file is created 600 from the start, so the secret is never briefly
    # world-readable.
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return token
