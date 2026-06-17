#!/usr/bin/env python3
"""
claude_usage.py — terminal monitor for Claude rate-limit usage.

Reads the OAuth token that Claude Code stores locally and queries Anthropic's
undocumented server-side usage endpoint:

    GET https://api.anthropic.com/api/oauth/usage

Stdlib only. No external dependencies.

Usage:
    python3 claude_usage.py            # one-shot snapshot
    python3 claude_usage.py --watch    # live, refreshes every 180s
    python3 claude_usage.py --json     # raw API JSON
    python3 claude_usage.py --interval 300 --watch
    python3 claude_usage.py --no-color
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "https://claude.ai/oauth/claude-code-client-metadata"

# The endpoint refuses to play nicely unless the User-Agent looks like Claude
# Code. We try to read the real installed version (see detect_claude_version);
# this is only the fallback if `claude --version` can't be found/parsed.
FALLBACK_CLAUDE_VERSION = "1.0.0"

CREDENTIALS_PATH = os.path.expanduser("~/.claude/.credentials.json")
ENV_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"

# Server-imposed rate limit is comfortable at 180s with the right UA.
MIN_INTERVAL = 60
DEFAULT_INTERVAL = 180

# Window durations (seconds) used to compute the "time elapsed" marker.
WINDOW_SECONDS = {
    "five_hour": 5 * 3600,
    "seven_day": 7 * 86400,
    "seven_day_sonnet": 7 * 86400,
    "seven_day_opus": 7 * 86400,
}

# Human labels + display order.
WINDOWS = [
    ("five_hour", "Session (5h)"),
    ("seven_day", "Weekly (7d)"),
    ("seven_day_sonnet", "Weekly Sonnet"),
    ("seven_day_opus", "Weekly Opus"),
]

HIGH_THRESHOLD = 80.0  # turn the bar red at/above this %

HISTORY_PATH = os.path.expanduser("~/.claude/usage-history.json")
HISTORY_MAX_AGE = 2 * 86400  # keep 2 days (covers 1d lookback window)

# Per-window-type lookback periods for burndown rates.
# Session (5h): short windows to catch spikes quickly.
# Weekly (7d): longer windows scaled to the window length.
BURN_WINDOWS_SESSION = [("1h", 3600), ("30m", 1800), ("5m", 300)]
BURN_WINDOWS_WEEKLY  = [("1d", 86400), ("4h", 14400), ("1h", 3600)]


# ---------------------------------------------------------------------------
# Colour handling
# ---------------------------------------------------------------------------

class C:
    RESET = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    BLUE = "\033[38;5;39m"
    ORANGE = "\033[38;5;208m"
    RED = "\033[38;5;203m"
    GREEN = "\033[38;5;42m"
    GREY = "\033[38;5;240m"
    WHITE = "\033[97m"
    YELLOW = "\033[38;5;220m"


def colour_enabled(force_off: bool) -> bool:
    if force_off or os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

class UsageError(Exception):
    """User-facing error with a clean message (no traceback)."""


def read_token() -> str:
    """Return an OAuth access token from env var or the local credentials file."""
    env = os.environ.get(ENV_TOKEN)
    if env:
        return env.strip()

    if not os.path.exists(CREDENTIALS_PATH):
        raise UsageError(
            f"No token found.\n"
            f"  - {CREDENTIALS_PATH} does not exist, and\n"
            f"  - ${ENV_TOKEN} is not set.\n"
            f"Log in to Claude Code (`claude`) at least once, then retry."
        )

    try:
        with open(CREDENTIALS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(f"Could not read {CREDENTIALS_PATH}: {exc}") from exc

    oauth = data.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        raise UsageError(
            f"{CREDENTIALS_PATH} has no claudeAiOauth.accessToken. "
            f"Re-login to Claude Code."
        )

    expires_at = oauth.get("expiresAt")  # epoch ms
    if isinstance(expires_at, (int, float)):
        if expires_at / 1000.0 < time.time() + 60:
            sys.stderr.write(
                f"{C.YELLOW}warning:{C.RESET} stored token looks expired. "
                f"If you get a 401, run `claude update` (or just use Claude Code) "
                f"to refresh it.\n"
            )
    return token.strip()


def detect_claude_version() -> str:
    """Best-effort: parse `claude --version`, else fall back to a constant."""
    exe = shutil.which("claude")
    if not exe:
        return FALLBACK_CLAUDE_VERSION
    try:
        out = subprocess.run(
            [exe, "--version"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return FALLBACK_CLAUDE_VERSION
    m = re.search(r"(\d+\.\d+\.\d+)", out)
    return m.group(1) if m else FALLBACK_CLAUDE_VERSION


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

def try_refresh_token() -> bool:
    """Exchange the stored refresh token for a new access token.

    Updates ~/.claude/.credentials.json in-place. Returns True on success.
    """
    try:
        with open(CREDENTIALS_PATH, "r", encoding="utf-8") as fh:
            creds = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return False

    oauth = creds.get("claudeAiOauth") or {}
    refresh_tok = oauth.get("refreshToken")
    if not refresh_tok:
        return False

    payload = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_tok,
        "client_id": OAUTH_CLIENT_ID,
    }).encode("utf-8")

    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return False

    new_token = result.get("access_token")
    if not new_token:
        return False

    oauth["accessToken"] = new_token
    if result.get("refresh_token"):
        oauth["refreshToken"] = result["refresh_token"]
    expires_in = result.get("expires_in")
    if expires_in:
        oauth["expiresAt"] = int((time.time() + int(expires_in)) * 1000)
    creds["claudeAiOauth"] = oauth

    try:
        tmp = CREDENTIALS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(creds, fh, indent=2)
        os.replace(tmp, CREDENTIALS_PATH)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def query_usage(token: str, user_agent: str) -> dict:
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": OAUTH_BETA,
            "User-Agent": user_agent,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise UsageError(
                "401 Unauthorized — token expired or invalid. "
                "Run `claude update` or use Claude Code to refresh, then retry."
            ) from exc
        if exc.code == 429:
            raise UsageError(
                "429 Too Many Requests — backing off. "
                "Poll no faster than every 180s, and make sure the User-Agent "
                "is recognised (current: %r)." % user_agent
            ) from exc
        raise UsageError(f"HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise UsageError(f"Network error reaching api.anthropic.com: {exc.reason}") from exc


# ---------------------------------------------------------------------------
# Burndown history
# ---------------------------------------------------------------------------

def load_history() -> list[dict]:
    if not os.path.exists(HISTORY_PATH):
        return []
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return raw if isinstance(raw, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_history(history: list[dict]) -> None:
    try:
        tmp = HISTORY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(history, fh, separators=(",", ":"))
        os.replace(tmp, HISTORY_PATH)
    except OSError:
        pass


def record_snapshot(data: dict) -> list[dict]:
    """Append current utilisation values to disk history; prune entries > 2h old."""
    history = load_history()
    ts = time.time()
    entry: dict = {"t": ts}
    for key, _ in WINDOWS:
        win = data.get(key)
        if win and win.get("utilization") is not None:
            entry[key] = win["utilization"]
    history.append(entry)
    cutoff = ts - HISTORY_MAX_AGE
    history = [h for h in history if h["t"] >= cutoff]
    save_history(history)
    return history


def burn_rate(history: list[dict], key: str, span_secs: int) -> float | None:
    """Return %/hour rate of change over the last span_secs. Positive = burning."""
    cutoff = time.time() - span_secs
    pts = [(h["t"], h[key]) for h in history if key in h and h["t"] >= cutoff]
    if len(pts) < 2:
        return None
    dt = pts[-1][0] - pts[0][0]
    if dt < 30:
        return None
    return (pts[-1][1] - pts[0][1]) / (dt / 3600.0)


def fmt_runout(rate: float, util: float, resets_at: str) -> str:
    """Time until util hits 100% at current rate, 'safe' if reset comes first."""
    if rate <= 0:
        return "stable"
    remaining = 100.0 - util
    if remaining <= 0:
        return "now"
    secs = int(remaining / rate * 3600)
    try:
        target = parse_ts(resets_at)
        reset_secs = int((target - datetime.now(timezone.utc)).total_seconds())
        if secs >= max(reset_secs, 0):
            return "safe"
    except (ValueError, TypeError):
        pass
    h, rem = divmod(max(secs, 0), 3600)
    m = rem // 60
    return f"~{h}h{m:02d}m" if h else f"~{m}m"


def burn_windows_for(key: str) -> list:
    return BURN_WINDOWS_SESSION if key == "five_hour" else BURN_WINDOWS_WEEKLY


def pace_headroom(util: float, elapsed_frac: float | None) -> float | None:
    """How far ahead of (positive) or behind (negative) pace we are.

    Returns 100 - projected, where projected = util / elapsed_frac.
    Positive → slack, can burn faster.  Negative → over pace, must slow down.
    Returns None when elapsed is too small to be meaningful.
    """
    if not elapsed_frac or elapsed_frac < 0.02:
        return None
    return 100.0 - (util / elapsed_frac)


def blowout_colour(history: list[dict], key: str, util: float, resets_at: str) -> str:
    """Colour based on pace: current utilisation vs fraction of window elapsed.

    Projects final utilisation linearly: projected = util / elapsed_fraction.
    - GREEN  projected <  100% (under pace, will finish within budget)
    - ORANGE projected >= 100% (over pace, on track to blow out)
    - RED    projected >= 120% (significantly over pace, clear blowout)
    """
    elapsed = elapsed_fraction(key, resets_at)
    if not elapsed:
        return C.GREEN
    projected = util / elapsed
    if projected >= 120:
        return C.RED
    if projected >= 100:
        return C.ORANGE
    return C.GREEN


def render_burn_line(
    history: list[dict], key: str, util: float, resets_at: str, use_colour: bool
) -> str | None:
    """One-line burndown summary for a usage window, or None if no history yet.

    Each rate window independently estimates: will we blow the budget, and when?
    """
    def c(code: str) -> str:
        return code if use_colour else ""

    bw = burn_windows_for(key)
    rates = [(wl, burn_rate(history, key, wsecs)) for wl, wsecs in bw]
    if all(r is None for _, r in rates):
        return None

    parts = []
    for wl, r in rates:
        if r is None:
            parts.append(f"{c(C.GREY)}{wl}:--{c(C.RESET)}")
            continue
        sign = "+" if r >= 0 else ""
        rate_str = f"{sign}{r:.1f}/h"
        ro = fmt_runout(r, util, resets_at)
        if ro in ("safe", "stable"):
            ro_col = C.GREEN
        else:
            burn_secs = (100.0 - util) / r * 3600 if r > 0 else float("inf")
            ro_col = C.RED if burn_secs <= 3600 else C.ORANGE
        parts.append(
            f"{c(C.DIM)}{wl}:{rate_str}{c(C.RESET)}"
            f"{c(C.DIM)}→{c(C.RESET)}{c(ro_col)}{ro}{c(C.RESET)}"
        )

    return f"  {'':14} {'  '.join(parts)}"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def parse_ts(ts: str) -> datetime:
    # API uses e.g. "2026-04-11T07:00:00.528743+00:00"
    return datetime.fromisoformat(ts)


def fmt_countdown(resets_at: str) -> str:
    try:
        target = parse_ts(resets_at)
    except (ValueError, TypeError):
        return "?"
    delta = target - datetime.now(timezone.utc)
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "due"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def elapsed_fraction(key: str, resets_at: str) -> float | None:
    """How far through the window we are (0..1), for the time marker."""
    span = WINDOW_SECONDS.get(key)
    if not span or not resets_at:
        return None
    try:
        target = parse_ts(resets_at)
    except (ValueError, TypeError):
        return None
    remaining = (target - datetime.now(timezone.utc)).total_seconds()
    frac = 1.0 - (remaining / span)
    return max(0.0, min(1.0, frac))


def render_bar(util: float, elapsed: float | None, width: int, use_colour: bool,
               fill_colour: str | None = None) -> str:
    util = max(0.0, min(100.0, util))
    filled = int(round((util / 100.0) * width))
    marker_pos = None
    if elapsed is not None:
        marker_pos = min(width - 1, int(round(elapsed * width)))

    if fill_colour is None:
        fill_colour = C.RED if util >= HIGH_THRESHOLD else C.BLUE

    cells = []
    for i in range(width):
        on = i < filled
        is_marker = (i == marker_pos)
        if use_colour:
            if is_marker:
                # white time marker sits on top of fill/empty
                cells.append(f"{C.WHITE}|{C.RESET}")
            elif on:
                cells.append(f"{fill_colour}█{C.RESET}")
            else:
                cells.append(f"{C.GREY}─{C.RESET}")
        else:
            if is_marker:
                cells.append("|")
            elif on:
                cells.append("#")
            else:
                cells.append("-")
    return "".join(cells)


def render(data: dict, use_colour: bool, version: str, history: list[dict] | None = None) -> str:
    def c(code: str) -> str:
        return code if use_colour else ""

    width = 28
    lines = []
    now = datetime.now().strftime("%H:%M:%S")
    lines.append(
        f"{c(C.BOLD)}Claude usage{c(C.RESET)} "
        f"{c(C.DIM)}· {now} · UA claude-code/{version}{c(C.RESET)}"
    )
    lines.append("")

    any_window = False
    for key, label in WINDOWS:
        win = data.get(key)
        if not win:
            continue
        util = win.get("utilization")
        if util is None:
            continue
        any_window = True
        resets_at = win.get("resets_at", "")
        elapsed = elapsed_fraction(key, resets_at)
        bar_col = blowout_colour(history or [], key, util, resets_at)
        bar = render_bar(util, elapsed, width, use_colour, bar_col if use_colour else None)
        pct_colour = bar_col
        pct = f"{c(pct_colour)}{util:5.1f}%{c(C.RESET)}"
        hw = pace_headroom(util, elapsed)
        if hw is not None:
            hw_col = C.GREEN if hw >= 0 else C.RED
            hw_str = f"  {c(hw_col)}{hw:+.0f}%{c(C.RESET)}"
        else:
            hw_str = ""
        reset = f"{c(C.DIM)}resets {fmt_countdown(resets_at)}{c(C.RESET)}"
        lines.append(f"  {label:<14} {bar} {pct}{hw_str}  {reset}")
        burn = render_burn_line(history or [], key, util, resets_at, use_colour)
        if burn:
            lines.append(burn)

    if not any_window:
        lines.append(f"  {c(C.DIM)}No active usage windows.{c(C.RESET)}")

    extra = data.get("extra_usage") or {}
    if extra.get("is_enabled"):
        used = extra.get("used_credits")
        limit = extra.get("monthly_limit")
        eu = extra.get("utilization")
        bits = []
        if eu is not None:
            bits.append(f"{eu:.1f}%")
        if used is not None and limit is not None:
            bits.append(f"{used}/{limit}")
        lines.append(f"  {'Extra usage':<14} {c(C.DIM)}{'  '.join(bits)}{c(C.RESET)}")

    if use_colour:
        lines.append("")
        lines.append(
            f"  {c(C.WHITE)}|{c(C.RESET)}{c(C.DIM)} = time elapsed  "
            f"· {c(C.RESET)}{c(C.GREEN)}green{c(C.RESET)}{c(C.DIM)} = on pace  "
            f"{c(C.ORANGE)}orange{c(C.RESET)}{c(C.DIM)} = over pace  "
            f"{c(C.RED)}red{c(C.RESET)}{c(C.DIM)} = blowing out{c(C.RESET)}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def one_shot(args, user_agent: str) -> dict:
    token = read_token()
    return query_usage(token, user_agent)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Monitor Claude rate-limit usage.")
    p.add_argument("--watch", action="store_true", help="refresh continuously")
    p.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                   help=f"watch poll interval in seconds (min {MIN_INTERVAL}, default {DEFAULT_INTERVAL})")
    p.add_argument("--json", action="store_true", help="print raw API JSON and exit")
    p.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    args = p.parse_args(argv)

    use_colour = colour_enabled(args.no_color)
    user_agent = f"claude-code/{detect_claude_version()}"

    try:
        if args.json:
            print(json.dumps(one_shot(args, user_agent), indent=2))
            return 0

        if not args.watch:
            data = one_shot(args, user_agent)
            history = record_snapshot(data)
            print(render(data, use_colour, user_agent.split("/")[-1], history))
            return 0

        interval = max(MIN_INTERVAL, args.interval)
        backoff = interval
        while True:
            try:
                data = one_shot(args, user_agent)
                history = record_snapshot(data)
                backoff = interval
                if use_colour:
                    sys.stdout.write("\033[2J\033[H")  # clear screen
                print(render(data, use_colour, user_agent.split("/")[-1], history))
                print(f"\n{C.DIM if use_colour else ''}refreshing every {interval}s "
                      f"· Ctrl-C to quit{C.RESET if use_colour else ''}")
                time.sleep(interval)
            except UsageError as exc:
                msg = str(exc)
                if "401" in msg:
                    dim = C.DIM if use_colour else ""
                    rst = C.RESET if use_colour else ""
                    sys.stderr.write(f"{dim}token expired — refreshing…{rst}\n")
                    if try_refresh_token():
                        sys.stderr.write(f"{dim}token refreshed, retrying{rst}\n")
                        continue  # immediate retry with new token
                    sys.stderr.write(
                        f"{C.YELLOW if use_colour else ''}refresh failed — "
                        f"run `claude` to renew manually{rst}\n"
                    )
                    time.sleep(interval)
                elif "429" in msg:
                    backoff = min(backoff * 2, 900)  # cap 15 min
                    sys.stderr.write(f"429 — sleeping {backoff}s before retry\n")
                    time.sleep(backoff)
                else:
                    print(f"{C.RED if use_colour else ''}{msg}{C.RESET if use_colour else ''}",
                          file=sys.stderr)
                    time.sleep(interval)
    except UsageError as exc:
        print(f"{C.RED if use_colour else ''}error:{C.RESET if use_colour else ''} {exc}",
              file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())