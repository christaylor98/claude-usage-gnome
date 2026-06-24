#!/usr/bin/python3
"""claude_indicator.py — GNOME AppIndicator for Claude usage.

Shows worst-case utilisation in the top bar; hover for detail tooltip,
click for the full per-window breakdown menu.

Requires:
  - python3-gi, gir1.2-ayatanaappindicator3-0.1
  - GNOME extension: ubuntu-appindicators (or AppIndicator support)

Run:
  ./claude_indicator.py
  # or autostart: ln -s $PWD/claude_indicator.py ~/.config/autostart/claude-usage.desktop
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3, GLib, Gio, Gtk  # noqa: E402

# Suppress the cosmetic GTK-CRITICAL from AyatanaAppIndicator3's icon loader.
# It fires on gtk_widget_get_scale_factor before the widget is realized and is
# harmless — the indicator appears and functions correctly.
def _gtk_log_null(domain, level, message, *args):
    pass
GLib.log_set_handler("Gtk", GLib.LogLevelFlags.LEVEL_CRITICAL, _gtk_log_null)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_usage as cu  # noqa: E402

REFRESH_INTERVAL = 180  # seconds; mirrors watch-mode default
APP_ID = "claude-usage"
ICON_NAME = "claude-usage"
ICONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
# Widest possible label — used as the width hint so the panel slot doesn't jump
LABEL_GUIDE = "🔴 100% -200%"

# Pango hex colors for the rich click menu
_HEX_GREEN  = "#00cc66"
_HEX_ORANGE = "#ff8c00"
_HEX_RED    = "#ff4444"
_HEX_BLUE   = "#4499ff"
_HEX_GREY   = "#555555"
_HEX_WHITE  = "#cccccc"
_HEX_DIM    = "#999999"



def _col_hex(ansi: str) -> str:
    """Map a C.* ANSI constant to a Pango hex color."""
    return {cu.C.GREEN: _HEX_GREEN, cu.C.ORANGE: _HEX_ORANGE, cu.C.RED: _HEX_RED}.get(ansi, _HEX_BLUE)


# ---------------------------------------------------------------------------
# Token handling
# ---------------------------------------------------------------------------

def _proactive_refresh() -> None:
    """Refresh the OAuth token if it expires within the next 5 minutes."""
    try:
        with open(cu.CREDENTIALS_PATH, "r", encoding="utf-8") as fh:
            creds = json.load(fh)
        expires_at = (creds.get("claudeAiOauth") or {}).get("expiresAt")
        if isinstance(expires_at, (int, float)) and expires_at / 1000.0 < time.time() + 300:
            cu.try_refresh_token()
    except Exception:
        pass


def _fetch() -> tuple[dict | None, list[dict], str | None]:
    """Fetch usage data. Returns (data, history, error_message)."""
    _proactive_refresh()
    ua = f"claude-code/{cu.detect_claude_version()}"
    try:
        token = cu.read_token()
        data = cu.query_usage(token, ua)
        return data, cu.record_snapshot(data), None
    except cu.UsageError as exc:
        msg = str(exc)
        if "401" in msg and cu.try_refresh_token():
            try:
                data = cu.query_usage(cu.read_token(), ua)
                return data, cu.record_snapshot(data), None
            except cu.UsageError as exc2:
                msg = str(exc2)
            except Exception as exc2:
                msg = str(exc2)
        return None, cu.load_history(), msg
    except Exception as exc:
        return None, cu.load_history(), str(exc)


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _worst_window(data: dict) -> tuple[str, float, str] | None:
    """(key, util%, resets_at) for the window with the worst pace (projected overrun)."""
    best: tuple[str, float, str] | None = None
    best_projected = -1.0
    for key, _ in cu.WINDOWS:
        win = data.get(key)
        if not win:
            continue
        util = win.get("utilization")
        if util is None:
            continue
        resets_at = win.get("resets_at", "")
        elapsed = cu.elapsed_fraction(key, resets_at)
        projected = (util / elapsed) if elapsed else util
        if projected > best_projected:
            best_projected = projected
            best = (key, util, resets_at)
    return best


def _panel_label(data: dict | None, history: list[dict]) -> str:
    if data is None:
        return "⚠ –"
    worst = _worst_window(data)
    if worst is None:
        return "–"
    key, util, resets_at = worst
    elapsed = cu.elapsed_fraction(key, resets_at)
    hw = cu.pace_headroom(util, elapsed)
    hw_str = f" {hw:+.0f}%" if hw is not None else ""
    return f"{util:.0f}%{hw_str}"


def _pace_icon(data: dict | None, history: list[dict]) -> str:
    """Return the icon name matching the worst-window pace: 1=green, 2=amber, 3=red."""
    if data is None:
        return "claude-usage-1"
    worst = _worst_window(data)
    if worst is None:
        return "claude-usage-1"
    key, util, resets_at = worst
    col = cu.blowout_colour(history, key, util, resets_at)
    if col == cu.C.RED:
        return "claude-usage-3"
    if col == cu.C.ORANGE:
        return "claude-usage-2"
    return "claude-usage-1"


def _pango_bar(util: float, elapsed: float | None, width: int, fill_hex: str) -> str:
    """Progress bar as Pango markup — colored background spaces for fill, ─ for empty."""
    util = max(0.0, min(100.0, util))
    filled = int(round((util / 100.0) * width))
    marker_pos = None
    if elapsed is not None:
        marker_pos = min(width - 1, int(round(elapsed * width)))
    cells = []
    for i in range(width):
        is_marker = (i == marker_pos)
        if is_marker:
            cells.append(f'<span foreground="{_HEX_WHITE}">|</span>')
        elif i < filled:
            cells.append(f'<span background="{fill_hex}"> </span>')
        else:
            cells.append(f'<span foreground="{_HEX_GREY}">─</span>')
    return "".join(cells)



def _make_mono_item(markup: str) -> Gtk.MenuItem:
    """Display-only menu item with a left-aligned monospace label using Pango markup."""
    item = Gtk.MenuItem()
    label = Gtk.Label()
    label.set_use_markup(True)
    label.set_markup(f'<span font_family="monospace">{markup}</span>')
    label.set_halign(Gtk.Align.START)
    label.set_margin_start(4)
    label.set_margin_end(8)
    item.add(label)
    return item


def _tooltip(data: dict | None, history: list[dict], last_error: str | None = None) -> str:
    if data is None:
        lines = ["Claude usage: unavailable"]
        if last_error:
            lines.append(last_error.split("\n")[0][:120])
            if any(k in last_error for k in ("401", "token", "Token", "auth", "login")):
                lines.append("Run 'claude' in a terminal to re-login")
            else:
                lines.append("Will retry automatically")
        return "\n".join(lines)
    lines = ["Claude usage"]
    for key, label in cu.WINDOWS:
        win = data.get(key)
        if not win:
            continue
        util = win.get("utilization")
        if util is None:
            continue
        resets_at = win.get("resets_at", "")
        elapsed = cu.elapsed_fraction(key, resets_at)
        hw = cu.pace_headroom(util, elapsed)
        hw_str = f"  {hw:+.0f}% vs pace" if hw is not None else ""
        burn_parts = []
        for wl, wsecs in cu.burn_windows_for(key):
            r = cu.burn_rate(history, key, wsecs)
            if r is not None:
                ro = cu.fmt_runout(r, util, resets_at)
                burn_parts.append(f"{wl}: {r:+.1f}/h → {ro}")
        burn_str = ("  |  " + "   ".join(burn_parts)) if burn_parts else ""
        lines.append(
            f"{label}: {util:.1f}%"
            f"{hw_str}"
            f"  (resets {cu.fmt_countdown(resets_at)})"
            f"{burn_str}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Indicator
# ---------------------------------------------------------------------------

class ClaudeIndicator:
    def __init__(self) -> None:
        self._data: dict | None = None
        self._history: list[dict] = []
        self._last_error: str | None = None

        self._ind = AyatanaAppIndicator3.Indicator.new(
            APP_ID,
            ICON_NAME,
            AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self._ind.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
        self._ind.set_label("…", LABEL_GUIDE)


        self._menu = Gtk.Menu()
        self._detail_items: list[Gtk.Widget] = []
        self._ind.set_menu(self._menu)

        # initial fetch in background so GTK loop starts immediately
        threading.Thread(target=self._do_refresh, daemon=True).start()
        # periodic refresh
        GLib.timeout_add_seconds(REFRESH_INTERVAL, self._schedule_refresh)

    # -- background work -----------------------------------------------------

    def _do_refresh(self) -> None:
        data, history, error = _fetch()
        self._data = data
        self._history = history
        self._last_error = error
        GLib.idle_add(self._update_ui)

    def _schedule_refresh(self) -> bool:
        threading.Thread(target=self._do_refresh, daemon=True).start()
        return True  # keep repeating

    def _on_refresh_clicked(self, _widget) -> None:
        self._ind.set_label("…", LABEL_GUIDE)
        threading.Thread(target=self._do_refresh, daemon=True).start()

    # -- UI updates (must run on GTK main thread) ----------------------------

    def _update_ui(self) -> bool:
        self._ind.set_label(_panel_label(self._data, self._history), LABEL_GUIDE)
        self._ind.set_title(_tooltip(self._data, self._history, self._last_error))
        self._ind.set_icon_full(_pace_icon(self._data, self._history), "Claude usage")
        self._rebuild_menu()
        return False  # GLib.idle_add: don't repeat

    def _rebuild_menu(self) -> None:
        for item in self._detail_items:
            self._menu.remove(item)
        self._detail_items.clear()

        def add(widget: Gtk.Widget) -> None:
            self._menu.append(widget)
            self._detail_items.append(widget)

        def add_mono(markup: str) -> None:
            add(_make_mono_item(markup))

        bar_width = 28

        if self._data:
            now = time.strftime("%H:%M:%S")
            version = cu.detect_claude_version()
            add_mono(
                f'<b>Claude usage</b>'
                f'<span foreground="{_HEX_DIM}"> · {now} · UA claude-code/{version}</span>'
            )
            add_mono("")

            for key, win_label in cu.WINDOWS:
                win = self._data.get(key)
                if not win:
                    continue
                util = win.get("utilization")
                if util is None:
                    continue
                resets_at = win.get("resets_at", "")
                elapsed = cu.elapsed_fraction(key, resets_at)
                bar_col = cu.blowout_colour(self._history, key, util, resets_at)
                fill_hex = _col_hex(bar_col)

                bar = _pango_bar(util, elapsed, bar_width, fill_hex)
                pct = f'<span foreground="{fill_hex}">{util:5.1f}%</span>'

                hw = cu.pace_headroom(util, elapsed)
                if hw is not None:
                    hw_hex = _HEX_GREEN if hw >= 0 else _HEX_RED
                    hw_str = f'  <span foreground="{hw_hex}">{hw:+.0f}%</span>'
                else:
                    hw_str = ""

                reset = f'<span foreground="{_HEX_DIM}">resets {cu.fmt_countdown(resets_at)}</span>'
                label_col = f'<span foreground="{_HEX_DIM}">{win_label}</span>'

                add_mono(f'  {bar}  {pct}{hw_str}  {label_col}  {reset}')

        else:
            item = Gtk.MenuItem(label="⚠  Usage unavailable")
            item.set_sensitive(False)
            add(item)
            if self._last_error:
                is_auth = any(k in self._last_error for k in ("401", "token", "Token", "auth", "login"))
                short = self._last_error.split("\n")[0][:80]
                reason = Gtk.MenuItem(label=f"   {short}")
                reason.set_sensitive(False)
                add(reason)
                if is_auth:
                    fix = Gtk.MenuItem(label="   → Run 'claude' in a terminal to re-login")
                    fix.set_sensitive(False)
                    add(fix)
                else:
                    fix = Gtk.MenuItem(label="   → Will retry automatically")
                    fix.set_sensitive(False)
                    add(fix)

        add(Gtk.SeparatorMenuItem())

        refresh = Gtk.MenuItem(label="Refresh now")
        refresh.connect("activate", self._on_refresh_clicked)
        add(refresh)

        add(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda _: Gtk.main_quit())
        add(quit_item)

        self._menu.show_all()


# ---------------------------------------------------------------------------

def main() -> None:
    _ind: list[ClaudeIndicator | None] = [None]

    def _on_watcher_appeared(connection, name, name_owner):
        if _ind[0] is None:
            _ind[0] = ClaudeIndicator()

    Gio.bus_watch_name(
        Gio.BusType.SESSION,
        "org.kde.StatusNotifierWatcher",
        Gio.BusNameWatcherFlags.NONE,
        _on_watcher_appeared,
        None,
    )
    Gtk.main()


if __name__ == "__main__":
    main()
