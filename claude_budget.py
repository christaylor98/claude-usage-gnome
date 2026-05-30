#!/usr/bin/env python3
"""
claude_budget.py — waking-hours rate limiter for Claude usage.

Caps how much of your Claude window you burn during waking hours, so heavy /
AI-intensive jobs don't run through a session too quickly. Overnight the cap
lifts so deferred work can drain into windows that would otherwise reset empty.

Uses claude_usage.py (sitting next to this file) as the usage sensor: it reads
the server-side utilisation of the 5-hour session window and decides whether a
job may start now.

Three modes describe what happens as you approach / hit the budget:
  batch  run flat-out under budget, then STOP (exit) at the cap
  wait   run under budget, then BLOCK until window reset at the cap
  drip   pace continuously against the time-elapsed marker (low steady load),
         BLOCK at the cap

Usage:
    # report the current decision (and exit code = action)
    python3 claude_budget.py status

    # gate a command: run it now / drip / wait-for-reset / refuse per policy
    python3 claude_budget.py run --mode drip -- pytest -q
    python3 claude_budget.py run --mode batch --budget 0.25 -- ./run_eval.sh

    # just ask "can I go?" for use in scripts (exit code carries the answer)
    python3 claude_budget.py check --quiet && ./my_job.sh

Exit codes (status/check/run-refusal):
    0  GO      under budget, run now
    10 DRIP    under budget but ahead of pace (drip would delay)
    20 WAIT    at/over budget, would block until reset
    30 STOP    at/over budget, batch mode would exit
    40 UNKNOWN usage unreadable (fail-safe)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Import the monitor (same directory) as the usage sensor.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_usage as cu  # noqa: E402


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

DEFAULT_POLICY_PATH = os.path.expanduser("~/.claude/budget-policy.json")

# Action codes (also used as process exit codes for scriptability).
GO, DRIP, WAIT, STOP, UNKNOWN = "GO", "DRIP", "WAIT", "STOP", "UNKNOWN"
EXIT = {GO: 0, DRIP: 10, WAIT: 20, STOP: 30, UNKNOWN: 40}


@dataclass
class Policy:
    budget: float = 0.20           # waking-hours utilisation ceiling (0..1)
    sleep_budget: float = 1.00     # overnight ceiling (1.0 = uncapped)
    waking_start: int = 7          # local hour inclusive
    waking_end: int = 23           # local hour exclusive
    window: str = "five_hour"      # which usage window to pace against
    mode: str = "drip"             # batch | wait | drip
    max_drip_sleep: int = 900      # cap a single drip sleep (s) so we re-eval
    weekly_backstop: float | None = None  # optional hard 7d ceiling (0..1) or None
    poll_on_wait: int = 180        # re-check cadence while blocked (s)

    @classmethod
    def load(cls, path: str | None) -> "Policy":
        p = cls()
        path = path or DEFAULT_POLICY_PATH
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                for k, v in data.items():
                    if hasattr(p, k):
                        setattr(p, k, v)
            except (OSError, json.JSONDecodeError) as exc:
                sys.stderr.write(f"warning: ignoring bad policy file {path}: {exc}\n")
        return p


@dataclass
class Decision:
    action: str
    delay: int = 0          # seconds to sleep (DRIP) or until reset (WAIT)
    reason: str = ""
    util: float | None = None
    ceiling: float = 0.0
    waking: bool = True
    window: str = "five_hour"


# ---------------------------------------------------------------------------
# Core decision logic
# ---------------------------------------------------------------------------

def is_waking(now: datetime, pol: Policy) -> bool:
    h = now.hour
    if pol.waking_start <= pol.waking_end:
        return pol.waking_start <= h < pol.waking_end
    # wraps midnight (e.g. 22 -> 6)
    return h >= pol.waking_start or h < pol.waking_end


def window_state(usage: dict, key: str):
    """Return (util_fraction, elapsed_fraction, reset_in_s, span_s) or None."""
    win = usage.get(key)
    if not win:
        return None
    util = win.get("utilization")
    if util is None:
        return None
    resets_at = win.get("resets_at", "")
    span = cu.WINDOW_SECONDS.get(key)
    reset_in = 0
    elapsed = 0.0
    if resets_at and span:
        try:
            target = cu.parse_ts(resets_at)
            reset_in = max(0, int((target - datetime.now(timezone.utc)).total_seconds()))
            elapsed = max(0.0, min(1.0, 1.0 - reset_in / span))
        except (ValueError, TypeError):
            pass
    return util / 100.0, elapsed, reset_in, span or 0


def decide(usage: dict | None, pol: Policy, now: datetime | None = None) -> Decision:
    now = now or datetime.now()
    waking = is_waking(now, pol)
    ceiling = pol.budget if waking else pol.sleep_budget

    if usage is None:
        return Decision(UNKNOWN, reason="usage unreadable", ceiling=ceiling,
                        waking=waking, window=pol.window)

    # Optional weekly hard backstop (applies regardless of time of day).
    if pol.weekly_backstop is not None:
        wk = window_state(usage, "seven_day")
        if wk and wk[0] >= pol.weekly_backstop:
            return Decision(WAIT, delay=wk[2],
                            reason=f"weekly backstop hit ({wk[0]*100:.0f}% >= "
                                   f"{pol.weekly_backstop*100:.0f}%)",
                            util=wk[0], ceiling=pol.weekly_backstop,
                            waking=waking, window="seven_day")

    st = window_state(usage, pol.window)
    if st is None:
        return Decision(UNKNOWN, reason=f"window '{pol.window}' has no data",
                        ceiling=ceiling, waking=waking, window=pol.window)
    util, elapsed, reset_in, span = st

    # Over budget -> mode decides whether we stop (batch) or wait (wait/drip).
    if util >= ceiling:
        if pol.mode == "batch":
            return Decision(STOP, reason=f"budget spent ({util*100:.0f}% >= "
                            f"{ceiling*100:.0f}%), batch stops",
                            util=util, ceiling=ceiling, waking=waking, window=pol.window)
        return Decision(WAIT, delay=reset_in,
                        reason=f"budget reached ({util*100:.0f}% >= {ceiling*100:.0f}%), "
                               f"waiting for reset",
                        util=util, ceiling=ceiling, waking=waking, window=pol.window)

    # Under budget. Pacing (drip) is a waking-hours concern only; overnight we
    # drain flat-out up to the (higher) ceiling regardless of mode.
    if pol.mode in ("batch", "wait") or not waking:
        return Decision(GO, reason=f"under budget ({util*100:.0f}% < {ceiling*100:.0f}%)"
                        + ("" if waking else ", overnight drain"),
                        util=util, ceiling=ceiling, waking=waking, window=pol.window)

    # drip: pace against the time-elapsed marker, scaled to the budget.
    # Target by now = ceiling * elapsed_fraction. If we're at/under target, go.
    if span and ceiling > 0:
        target = ceiling * elapsed
        if util <= target:
            return Decision(GO, reason=f"on pace ({util*100:.0f}% <= target "
                            f"{target*100:.0f}%)", util=util, ceiling=ceiling,
                            waking=waking, window=pol.window)
        # Ahead of pace: wait until the marker catches up to current util.
        # ceiling*(elapsed + d/span) = util  ->  d = (util/ceiling - elapsed)*span
        d = int(max(0.0, (util / ceiling - elapsed) * span))
        d = min(d, reset_in or d, pol.max_drip_sleep)
        return Decision(DRIP, delay=max(1, d),
                        reason=f"ahead of pace ({util*100:.0f}% > target "
                               f"{target*100:.0f}%), dripping",
                        util=util, ceiling=ceiling, waking=waking, window=pol.window)

    # No span info (e.g. missing reset time) -> behave like wait under budget.
    return Decision(GO, reason="under budget (no pacing data)",
                    util=util, ceiling=ceiling, waking=waking, window=pol.window)


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------

def fetch_usage() -> dict | None:
    try:
        token = cu.read_token()
        ua = f"claude-code/{cu.detect_claude_version()}"
        return cu.query_usage(token, ua)
    except cu.UsageError as exc:
        sys.stderr.write(f"sensor error: {exc}\n")
        return None


# ---------------------------------------------------------------------------
# Rendering / CLI
# ---------------------------------------------------------------------------

def describe(d: Decision) -> str:
    phase = "waking" if d.waking else "overnight"
    u = f"{d.util*100:.1f}%" if d.util is not None else "?"
    line = f"[{d.action}] {phase} · {d.window} {u} / cap {d.ceiling*100:.0f}% · {d.reason}"
    if d.action == DRIP:
        line += f" · sleep {d.delay}s"
    elif d.action == WAIT:
        line += f" · reset in {d.delay//60}m"
    return line


def run_command(cmd: list[str], pol: Policy, no_block: bool) -> int:
    """Gate then run a command, honouring the decision. Returns the child's
    exit code, or an EXIT[...] code if the job was refused."""
    while True:
        d = decide(fetch_usage(), pol)
        sys.stderr.write(describe(d) + "\n")

        if d.action == GO:
            return subprocess.run(cmd).returncode

        if d.action == DRIP:
            if no_block:
                return EXIT[DRIP]
            time.sleep(d.delay)
            continue  # re-evaluate; pace will have advanced

        if d.action == WAIT:
            if no_block:
                return EXIT[WAIT]
            # sleep in poll-sized chunks so we notice an early reset / activity
            sleep_for = max(1, min(d.delay or pol.poll_on_wait, pol.poll_on_wait))
            time.sleep(sleep_for)
            continue

        if d.action == STOP:
            return EXIT[STOP]

        # UNKNOWN -> fail-safe: refuse rather than run blind.
        return EXIT[UNKNOWN]


def cmd_status(args, pol: Policy) -> int:
    d = decide(fetch_usage(), pol)
    print(describe(d))
    return EXIT[d.action]


def cmd_check(args, pol: Policy) -> int:
    d = decide(fetch_usage(), pol)
    if not args.quiet:
        print(describe(d))
    return EXIT[d.action]


def cmd_run(args, pol: Policy) -> int:
    if not args.command:
        sys.stderr.write("error: no command given after --\n")
        return 2
    return run_command(args.command, pol, no_block=args.no_block)


def add_policy_args(sp):
    sp.add_argument("--budget", type=float, help="waking-hours ceiling 0..1 (default 0.20)")
    sp.add_argument("--sleep-budget", type=float, dest="sleep_budget",
                    help="overnight ceiling 0..1 (default 1.0 = uncapped)")
    sp.add_argument("--mode", choices=["batch", "wait", "drip"], help="default drip")
    sp.add_argument("--window", help="usage window to pace (default five_hour)")
    sp.add_argument("--waking", help="waking hours as START-END, e.g. 7-23")
    sp.add_argument("--weekly-backstop", type=float, dest="weekly_backstop",
                    help="hard 7d ceiling 0..1 (default off)")
    sp.add_argument("--config", help=f"policy JSON (default {DEFAULT_POLICY_PATH})")


def apply_overrides(pol: Policy, args) -> Policy:
    for k in ("budget", "sleep_budget", "mode", "window", "weekly_backstop"):
        v = getattr(args, k, None)
        if v is not None:
            setattr(pol, k, v)
    if getattr(args, "waking", None):
        try:
            s, e = args.waking.split("-")
            pol.waking_start, pol.waking_end = int(s), int(e)
        except ValueError:
            sys.stderr.write(f"warning: bad --waking '{args.waking}', ignoring\n")
    return pol


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Waking-hours Claude usage rate limiter.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp_status = sub.add_parser("status", help="print the current decision")
    add_policy_args(sp_status)

    sp_check = sub.add_parser("check", help="exit code carries the decision")
    sp_check.add_argument("--quiet", action="store_true")
    add_policy_args(sp_check)

    sp_run = sub.add_parser("run", help="gate and run a command")
    sp_run.add_argument("--no-block", action="store_true",
                        help="don't sleep; return immediately with the action's exit code")
    add_policy_args(sp_run)
    sp_run.add_argument("command", nargs=argparse.REMAINDER,
                        help="command after -- , e.g. -- pytest -q")

    args = ap.parse_args(argv)
    pol = apply_overrides(Policy.load(getattr(args, "config", None)), args)

    # strip a leading '--' from REMAINDER
    if getattr(args, "command", None) and args.command and args.command[0] == "--":
        args.command = args.command[1:]

    if args.cmd == "status":
        return cmd_status(args, pol)
    if args.cmd == "check":
        return cmd_check(args, pol)
    if args.cmd == "run":
        return cmd_run(args, pol)
    return 2


if __name__ == "__main__":
    sys.exit(main())