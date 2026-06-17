# claude-usage

Terminal monitor and GNOME panel indicator for Claude Code rate-limit usage. Reads your local OAuth token and queries Anthropic's usage endpoint to show session and weekly utilisation, burndown rates, and pace-based projections.

No external dependencies — stdlib only (except the optional GNOME indicator).

---

## Tools

### `claude_usage.py` — terminal monitor

One-shot snapshot or live watch mode for all active usage windows.

```
python3 claude_usage.py              # one-shot snapshot
python3 claude_usage.py --watch      # live, refreshes every 180s
python3 claude_usage.py --json       # raw API JSON
python3 claude_usage.py --interval 300 --watch
python3 claude_usage.py --no-color
```

**What it shows:**

- Usage bar per window (Session 5h, Weekly 7d, Sonnet, Opus)
- `|` marker = where you'd be if you'd burned evenly
- Colour = pace: green (on track), orange (over pace), red (blowing out)
- Headroom (`+N%` / `-N%`) = how far ahead of or behind budget pace you are
- Burndown rates over 1h/30m/5m (session) or 1d/4h/1h (weekly) with projected runout time

### `claude_budget.py` — waking-hours rate limiter

Gates commands or scripts against your Claude budget so heavy jobs don't drain a session too fast during the day. Lifts the cap overnight so deferred work can drain into windows that would otherwise reset empty.

**Modes:**

| Mode    | Under budget | At/over budget            |
|---------|--------------|---------------------------|
| `batch` | run          | exit immediately          |
| `wait`  | run          | block until window resets |
| `drip`  | pace against time-elapsed marker | block at cap |

```
# print the current decision
python3 claude_budget.py status

# gate a command
python3 claude_budget.py run --mode drip -- pytest -q
python3 claude_budget.py run --mode batch --budget 0.25 -- ./run_eval.sh

# scriptable check (exit code carries the decision)
python3 claude_budget.py check --quiet && ./my_job.sh
```

**Exit codes:** `0` GO · `10` DRIP · `20` WAIT · `30` STOP · `40` UNKNOWN

**Policy file** (`~/.claude/budget-policy.json`):

```json
{
  "budget": 0.20,
  "sleep_budget": 1.0,
  "waking_start": 7,
  "waking_end": 23,
  "window": "five_hour",
  "mode": "drip",
  "weekly_backstop": 0.80
}
```

### `claude_indicator.py` — GNOME panel indicator

Tray icon showing worst-case utilisation. Colour matches pace (green/amber/red). Click for per-window breakdown.

**Requirements:**

```
sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1
```

Also requires the **AppIndicator Support** GNOME extension.

```
./claude_indicator.py
```

If the indicator gets stuck showing an error after a token expiry, restart it — it doesn't always pick up a freshly issued OAuth token without a full restart:

```
pkill -f claude_indicator.py && ./claude_indicator.py &
```

To autostart, create `~/.config/autostart/claude-usage.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=Claude Usage Indicator
Exec=/path/to/claude_indicator.py
```

---

## Authentication

All three tools read the OAuth token that Claude Code stores in `~/.claude/.credentials.json`. Log in to Claude Code at least once before running them. The token is refreshed automatically on 401.

You can also set `CLAUDE_CODE_OAUTH_TOKEN` to override.

---

## License

MIT
