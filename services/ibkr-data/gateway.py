#!/usr/bin/env python3
"""Animated console for bringing the IBKR gateway up and logging in.

    ./services/ibkr-data/gateway          # log in (asks for creds first time)
    ./services/ibkr-data/gateway status   # is the session alive?
    ./services/ibkr-data/gateway down     # stop the gateway
    ./services/ibkr-data/gateway creds    # re-enter username and password

First run asks for the IBKR username and password (password typed masked,
stored in deploy/.env with owner-only permissions) and an optional TOTP
secret. Every run after that goes straight to the login, which walks five
steps with a live spinner and asks for the six digit authenticator code at
the exact moment the gateway wants it. Nothing here can place an order: the
account is read-only and the API is started with READ_ONLY_API=yes.
"""

from __future__ import annotations

import re
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from rich.align import Align
from rich.console import Console, Group
from rich.panel import Panel
from rich.prompt import Prompt
from rich.spinner import Spinner
from rich.live import Live
from rich.table import Table
from rich.text import Text

HERE = Path(__file__).resolve().parent
DEPLOY = HERE / "deploy"
ENV = DEPLOY / ".env"
VNC = "127.0.0.1::5901"
API_HOST, API_PORT = "127.0.0.1", 4001
FIELD_XY = (511, 381)

DIALOG = "Second Factor Authentication initiated"
THROTTLED = "Too many failed login attempts"
LOGGED_IN = re.compile(r"API server listening|Login has completed", re.I)
IBC_TS = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}):\d{3}")
THROTTLE_COOLDOWN = 15 * 60
# Raised from IBC's default 180 by deploy/ibc-config.ini.tmpl.
CODE_WINDOW = 900

console = Console()

PENDING, RUNNING, DONE, FAILED, SKIPPED = "pending", "running", "done", "failed", "skipped"
MARKS = {
    PENDING: ("○", "grey50"),
    DONE: ("●", "green"),
    FAILED: ("●", "red"),
    SKIPPED: ("○", "grey50"),
}


class Steps:
    """The five stage checklist, rendered live with a spinner on the active one."""

    def __init__(self, names: list[str]) -> None:
        self.names = names
        self.state = {n: PENDING for n in names}
        self.notes: dict[str, str] = {}
        self.spinner = Spinner("dots", style="cyan")
        self.started = time.time()

    def set(self, name: str, state: str, note: str = "") -> None:
        self.state[name] = state
        if note:
            self.notes[name] = note

    def render(self) -> Panel:
        table = Table.grid(padding=(0, 1))
        table.add_column(width=2)
        table.add_column(min_width=34)
        table.add_column(style="grey62")
        for name in self.names:
            state = self.state[name]
            if state == RUNNING:
                mark = self.spinner
                label = Text(name, style="bold cyan")
            else:
                glyph, colour = MARKS[state]
                mark = Text(glyph, style=colour)
                style = {
                    DONE: "white", FAILED: "red",
                    SKIPPED: "grey50", PENDING: "grey50",
                }[state]
                label = Text(name, style=style)
            table.add_row(mark, label, self.notes.get(name, ""))

        elapsed = int(time.time() - self.started)
        footer = Text(f"  {elapsed}s elapsed", style="grey50")
        return Panel(
            Group(table, footer),
            title="[bold]IBKR gateway[/bold]",
            subtitle="[grey50]read-only session[/grey50]",
            border_style="cyan",
            padding=(1, 2),
        )


def env_value(key: str) -> str | None:
    if not ENV.exists():
        return None
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{key}=") and not line.startswith("#"):
            return line.split("=", 1)[1].strip() or None
    return None


def compose(*args: str) -> str:
    result = subprocess.run(
        ["docker", "compose", *args],
        cwd=DEPLOY, capture_output=True, text=True, timeout=180,
    )
    return (result.stdout or "") + (result.stderr or "")


def logs(tail: int = 400, since: float | None = None) -> str:
    """Container logs; with `since`, only lines after that unix time.

    The wait loop must pass `since`: container logs survive across logins, so
    a dialog line from a previous attempt reads exactly like a live one and
    would have us typing a code into a dead screen."""
    args = ["logs", "--tail", str(tail)]
    if since is not None:
        args += ["--since", str(int(since))]
    return compose(*args)


def port_open() -> bool:
    with socket.socket() as s:
        s.settimeout(2)
        try:
            s.connect((API_HOST, API_PORT))
            return True
        except OSError:
            return False


def api_session() -> list[str] | None:
    """Managed accounts if the API is really serving, else None.

    The port opens before the login finishes, so a socket check alone reports
    success too early. Only a completed handshake proves the session."""
    if not port_open():
        return None
    try:
        from ib_async import IB, util

        util.logToConsole("CRITICAL")
        ib = IB()
        # Skip IB.connect's order/account startup sync on a read-only API.
        ib.client.connect(API_HOST, API_PORT, clientId=int(time.time()) % 900 + 60,
                          timeout=8)
        accounts = list(ib.managedAccounts())
        ib.disconnect()
        return accounts
    except Exception:
        return None


ENV_DEFAULTS = {
    "TRADING_MODE": "live",
    "READ_ONLY_API": "yes",
    "AUTO_RESTART_TIME": "11:59 PM",
    "TIME_ZONE": "Africa/Johannesburg",
    "VNC_SERVER_PASSWORD": "lacunavnc",
}


def write_env(values: dict[str, str]) -> None:
    """Rewrite deploy/.env from known keys, owner-readable only.

    Deliberately regenerated rather than patched in place, so a stale or
    hand-mangled file cannot half-apply."""
    merged = dict(ENV_DEFAULTS)
    for key in ("TWS_USERID", "TWS_PASSWORD", "TOTP_SECRET", *ENV_DEFAULTS):
        existing = env_value(key)
        if existing:
            merged[key] = existing
    merged.update(values)

    lines = ["# IBKR gateway credentials. Never commit. Regenerated by",
             "# services/ibkr-data/gateway (creds command)."]
    for key, value in merged.items():
        lines.append(f"{key}={value}")
    ENV.write_text("\n".join(lines) + "\n")
    ENV.chmod(0o600)


def ensure_credentials(force: bool = False) -> bool:
    """Prompt for anything missing from deploy/.env. True if ready to log in."""
    user = env_value("TWS_USERID")
    password = env_value("TWS_PASSWORD")
    if user and password and not force:
        return True

    title = "credentials" if not (user or password) else "update credentials"
    console.print()
    console.print(Panel(
        Text.from_markup(
            "IBKR username and password for the gateway login.\n\n"
            "They are written to [bold]services/ibkr-data/deploy/.env[/bold], "
            "readable only by\nyour user and ignored by git. The password is "
            "the one for the IBKR website."),
        title=f"[bold cyan]{title}[/bold cyan]", border_style="cyan",
        padding=(1, 2)))

    entered_user = Prompt.ask("  [bold cyan]username[/bold cyan]",
                              default=user or None).strip()
    entered_pw = Prompt.ask("  [bold cyan]password[/bold cyan]",
                            password=True).strip()
    if not entered_user or not entered_pw:
        console.print("  [red]both are needed, nothing saved[/red]")
        return False

    values = {"TWS_USERID": entered_user, "TWS_PASSWORD": entered_pw}

    console.print(Text.from_markup(
        "\n  Optional: the TOTP secret (the base32 key behind the "
        "authenticator QR).\n  With it stored, logins need no typed code at "
        "all, including the weekly\n  Sunday re-auth. It does put both auth "
        "factors in this one file, and on a\n  shared account the owner "
        "should agree first. Enter to skip.", style="grey62"))
    secret = Prompt.ask("  [bold cyan]totp secret[/bold cyan]", password=True,
                        default="").strip().replace(" ", "")
    if secret:
        values["TOTP_SECRET"] = secret

    write_env(values)
    console.print(f"  [green]saved[/green] for [bold]{entered_user}[/bold]"
                  + (" with TOTP secret" if secret else ""))
    return True


def throttle_age() -> float | None:
    text = logs(400)
    latest = None
    for line in text.splitlines():
        if THROTTLED in line:
            match = IBC_TS.search(line)
            if match:
                latest = match.group(1)
    if latest is None:
        return None
    try:
        stamp = datetime.strptime(latest, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return (datetime.now() - stamp).total_seconds()


def type_code(code: str) -> None:
    """One vncdo invocation. Each opens a fresh connection taking seconds, so
    a per-keystroke loop would outlive the 30 second TOTP window."""
    password = env_value("VNC_SERVER_PASSWORD") or "lacunavnc"
    x, y = FIELD_XY
    subprocess.run(
        [sys.executable.replace("/python", "/vncdo"), "-s", VNC, "-p", password,
         "move", str(x), str(y), "click", "1", "type", code, "key", "enter"],
        check=True, capture_output=True, timeout=60,
    )


def ask_for_code(deadline: float) -> str:
    secret = env_value("TOTP_SECRET")
    if secret:
        try:
            import pyotp

            left = 30 - (int(time.time()) % 30)
            if left < 8:
                time.sleep(left + 1)
            code = pyotp.TOTP(secret.replace(" ", "")).now()
            console.print(f"  [green]generated[/green] [bold]{code}[/bold] "
                          "from TOTP_SECRET")
            return code
        except Exception:
            # A six digit code saved as the "secret" is the common mistake.
            # Fall through to the prompt instead of dying.
            console.print("  [red]TOTP_SECRET in deploy/.env is not valid "
                          "base32; ignoring it[/red]")

    remaining = int(deadline - time.time())
    body = Text.from_markup(
        "The gateway is waiting for your authenticator code.\n\n"
        "Open the app and read a [bold]fresh[/bold] code, then type the six "
        "digits below.\n"
        f"The window closes in about [bold]{remaining // 60} minutes[/bold], "
        "so there is no rush."
    )
    console.print()
    console.print(Panel(body, title="[bold yellow]code needed[/bold yellow]",
                        border_style="yellow", padding=(1, 2)))

    while True:
        code = Prompt.ask("  [bold cyan]code[/bold cyan]").strip().replace(" ", "")
        if code.isdigit() and len(code) == 6:
            return code
        if code.isdigit():
            console.print(f"  [red]that is {len(code)} digits, IBKR wants "
                          f"exactly 6[/red]")
        else:
            console.print("  [red]digits only[/red]")


def success(accounts: list[str]) -> None:
    body = Text.from_markup(
        f"Account [bold]{', '.join(accounts)}[/bold] is live on "
        f"[bold]{API_HOST}:{API_PORT}[/bold].\n\n"
        "Pull something:\n"
        "  [cyan].venv/bin/python services/ibkr-data/check_connection.py "
        "AAPL MSFT[/cyan]\n\n"
        "[grey62]The gateway restarts itself nightly without asking again. "
        "IBKR expires the\nsession every Sunday at 01:00 US Eastern, which is "
        "the next time it needs a code.[/grey62]"
    )
    console.print()
    console.print(Panel(Align.left(body), title="[bold green]logged in[/bold green]",
                        border_style="green", padding=(1, 2)))


def cmd_status() -> int:
    with console.status("[cyan]checking the session"):
        accounts = api_session()
    if accounts:
        console.print(f"[green]●[/green] serving on {API_HOST}:{API_PORT}, "
                      f"account [bold]{', '.join(accounts)}[/bold]")
        return 0
    running = "lacuna-ib-gateway" in compose("ps")
    where = "container up, session not authenticated" if running else "container stopped"
    console.print(f"[red]●[/red] no API session ({where})")
    console.print("  start one:  [cyan]./services/ibkr-data/gateway.py[/cyan]")
    return 1


def cmd_down() -> int:
    with console.status("[cyan]stopping the gateway"):
        compose("stop")
    console.print("[grey62]gateway stopped. The session token is kept in the "
                  "jts-settings volume.[/grey62]")
    return 0


def cmd_up() -> int:
    if not ensure_credentials():
        return 1
    console.print()
    with console.status("[cyan]checking for an existing session"):
        accounts = api_session()
    if accounts:
        console.print(Panel(
            Text.from_markup(
                f"Already logged in as [bold]{', '.join(accounts)}[/bold]. "
                "Nothing to do."),
            border_style="green", padding=(1, 2)))
        return 0

    age = throttle_age()
    if age is not None and age < THROTTLE_COOLDOWN:
        wait = int((THROTTLE_COOLDOWN - age) // 60) + 1
        console.print(Panel(
            Text.from_markup(
                f"IBKR throttled this login [bold]{int(age // 60)} minutes "
                f"ago[/bold] for too many failed attempts.\n"
                f"Trying again now risks locking the account. "
                f"Wait about [bold]{wait} minutes[/bold]."),
            title="[bold red]throttled[/bold red]", border_style="red",
            padding=(1, 2)))
        if Prompt.ask("  try anyway?", choices=["y", "n"], default="n") != "y":
            return 1

    names = ["starting the container", "waiting for the gateway",
             "authenticator code", "sending the code", "opening the API"]
    steps = Steps(names)
    deadline = None
    code = None

    with Live(steps.render(), console=console, refresh_per_second=12,
              transient=False) as live:
        steps.set(names[0], RUNNING)
        live.update(steps.render())
        started_at = time.time() - 1
        # A running container with no API session is mid-login or wedged.
        # Restart it so the login sequence begins fresh and every log line we
        # act on is newer than started_at; acting on an old dialog line means
        # typing a code into a dead screen.
        if "lacuna-ib-gateway" in compose("ps"):
            compose("restart")
            steps.set(names[0], DONE, "restarted")
        else:
            compose("up", "-d")
            steps.set(names[0], DONE)

        steps.set(names[1], RUNNING)
        live.update(steps.render())
        found = False
        limit = time.time() + 180
        while time.time() < limit:
            time.sleep(2)
            live.update(steps.render())
            current = logs(200, since=started_at)
            if THROTTLED in current:
                steps.set(names[1], FAILED, "IBKR refused: too many attempts")
                live.update(steps.render())
                compose("stop")
                return 1
            if LOGGED_IN.search(current):
                steps.set(names[1], DONE, "session restored, no code needed")
                for n in names[2:4]:
                    steps.set(n, SKIPPED)
                found = True
                code = ""
                break
            if DIALOG in current:
                steps.set(names[1], DONE)
                deadline = time.time() + CODE_WINDOW
                found = True
                break
        if not found:
            steps.set(names[1], FAILED, "no dialog appeared")
            live.update(steps.render())
            return 1
        steps.set(names[2], RUNNING if deadline else SKIPPED)
        live.update(steps.render())

    if deadline:
        code = ask_for_code(deadline)
        steps.set(names[2], DONE)

    with Live(steps.render(), console=console, refresh_per_second=12) as live:
        if code:
            steps.set(names[3], RUNNING)
            live.update(steps.render())
            try:
                type_code(code)
            except subprocess.SubprocessError as exc:
                steps.set(names[3], FAILED, str(exc)[:40])
                live.update(steps.render())
                return 1
            steps.set(names[3], DONE)

        steps.set(names[4], RUNNING)
        live.update(steps.render())
        limit = time.time() + 150
        accounts = None
        while time.time() < limit:
            time.sleep(3)
            live.update(steps.render())
            accounts = api_session()
            if accounts:
                break
            if THROTTLED in logs(60, since=started_at):
                steps.set(names[4], FAILED, "rejected, code was probably stale")
                live.update(steps.render())
                return 1
        if not accounts:
            steps.set(names[4], FAILED, "no API session")
            live.update(steps.render())
            return 1
        steps.set(names[4], DONE, ", ".join(accounts))
        live.update(steps.render())

    success(accounts)
    return 0


def main(argv: list[str]) -> int:
    command = (argv[0] if argv else "up").lower()
    try:
        if command in ("up", "login"):
            return cmd_up()
        if command == "status":
            return cmd_status()
        if command in ("down", "stop"):
            return cmd_down()
        if command in ("creds", "credentials", "setup"):
            done = ensure_credentials(force=True)
            if done:
                console.print("  restart the gateway to use them:  "
                              "[cyan]./services/ibkr-data/gateway down && "
                              "./services/ibkr-data/gateway[/cyan]")
            return 0 if done else 1
    except KeyboardInterrupt:
        console.print("\n[grey62]cancelled. The container is left as it "
                      "was.[/grey62]")
        return 130
    except EOFError:
        console.print(
            "\n[red]no interactive input available.[/red] This command asks "
            "questions, so run it\nin a normal terminal window:\n\n"
            f'  [cyan]cd "{HERE.parent.parent}" && '
            "./services/ibkr-data/gateway[/cyan]")
        return 1
    console.print(f"unknown command {command!r}. Use: up | status | down | creds")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
