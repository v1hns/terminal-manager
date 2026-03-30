#!/usr/bin/env python3
"""
tm - terminal manager for macOS
Shows all Terminal.app windows, what they're doing, and lets you jump to any.

Controls:
  ↑↓ / j k   Navigate
  Enter       Switch to that terminal
  R           Refresh
  q / Esc     Quit
"""

import curses
import subprocess
import sys
import os
import re
import time
import threading
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# ─── Data model ──────────────────────────────────────────────────────────────

@dataclass
class Tab:
    win_idx: int      # Terminal.app window index (1-based, front = 1)
    tab_idx: int      # tab index within window (1-based)
    tty: str          # /dev/ttysXXX — stable identifier
    busy: bool        # is a process running?
    process: str      # current process name
    title: str        # custom title set on the tab
    preview: str = "" # last lines of terminal history


# ─── AppleScript helpers ─────────────────────────────────────────────────────

def osascript(script: str) -> str:
    r = subprocess.run(["osascript", "-e", script],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


_LIST_SCRIPT = """\
tell application "Terminal"
    set out to {}
    set winIdx to 0
    repeat with w in windows
        set winIdx to winIdx + 1
        set tabIdx to 0
        repeat with t in tabs of w
            set tabIdx to tabIdx + 1
            set isBusy to busy of t
            set procs to processes of t
            set proc to "-zsh"
            if (count of procs) > 1 then
                set proc to item 2 of procs
            else if (count of procs) > 0 then
                set proc to item 1 of procs
            end if
            set ttl to ""
            try
                set ttl to custom title of t
            end try
            set ttyDev to tty of t
            set end of out to (winIdx as string) & "\\t" & (tabIdx as string) & "\\t" & ¬
                ttyDev & "\\t" & (isBusy as string) & "\\t" & proc & "\\t" & ttl
        end repeat
    end repeat
    set AppleScript's text item delimiters to "\\n"
    return out as string
end tell
"""

_HISTORY_SCRIPT = """\
tell application "Terminal"
    repeat with w in windows
        repeat with t in tabs of w
            if tty of t = "{tty}" then
                return history of t
            end if
        end repeat
    end repeat
    return ""
end tell
"""

_SWITCH_SCRIPT = """\
tell application "Terminal"
    activate
    repeat with w in windows
        repeat with t in tabs of w
            if tty of t = "{tty}" then
                set index of w to 1
                try
                    set selected tab of w to t
                end try
                return true
            end if
        end repeat
    end repeat
end tell
"""


def get_tabs() -> List[Tab]:
    raw = osascript(_LIST_SCRIPT)
    tabs = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        win_idx, tab_idx, tty, busy, proc, title = (
            parts[0], parts[1], parts[2], parts[3], parts[4],
            "\t".join(parts[5:])
        )
        tabs.append(Tab(
            win_idx=int(win_idx),
            tab_idx=int(tab_idx),
            tty=tty,
            busy=(busy == "true"),
            process=proc,
            title=title,
        ))
    return tabs


def fetch_preview(tty: str, lines: int = 10) -> str:
    history = osascript(_HISTORY_SCRIPT.replace("{tty}", tty))
    if not history:
        return ""
    # Strip ANSI / box-drawing clutter
    ansi = re.compile(r"\x1b\[[0-9;]*[mKHJABCDfhlsu]|\x1b\([A-Z]|\r")
    clean = ansi.sub("", history)
    # Keep last N non-empty lines
    meaningful = [l for l in clean.splitlines() if l.strip()][-lines:]
    return "\n".join(meaningful)


def switch_to(tty: str):
    osascript(_SWITCH_SCRIPT.replace("{tty}", tty))


# ─── Preview background loader ───────────────────────────────────────────────

class PreviewLoader(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.cache: dict = {}
        self._lock = threading.Lock()
        self._queue: Optional[str] = None
        self._event = threading.Event()
        self._stop = False

    def request(self, tty: str):
        self._queue = tty
        self._event.set()

    def get(self, tty: str) -> str:
        with self._lock:
            return self.cache.get(tty, "")

    def stop(self):
        self._stop = True
        self._event.set()

    def run(self):
        while not self._stop:
            self._event.wait(timeout=3.0)
            self._event.clear()
            if self._stop:
                break
            tty = self._queue
            if tty:
                try:
                    preview = fetch_preview(tty)
                    with self._lock:
                        self.cache[tty] = preview
                except Exception:
                    pass


# ─── Tab categorisation & state ──────────────────────────────────────────────

_SHELL_PROCS = {"-zsh", "zsh", "bash", "-bash", "fish", "-fish",
                "sh", "-sh", "dash", "tcsh", "-tcsh"}

# (keyword_set, color_pair, 4-char label)  — first match wins
_CATEGORIES: List[Tuple] = [
    ({"claude", "anthropic"},                                           11, "AI  "),
    ({"pytest", "jest", "vitest", "mocha", "rspec", "test", "spec"},    1,  "TEST"),
    ({"git", "gh", "tig", "lazygit"},                                   12, "GIT "),
    ({"vim", "nvim", "nano", "emacs", "helix", "hx"},                   8,  "EDIT"),
    ({"node", "npm", "yarn", "bun", "tsx", "ts-node", "deno"},          3,  "JS  "),
    ({"python", "python3", "pip", "uv", "ipython", "jupyter"},          2,  "PY  "),
    ({"docker", "kubectl", "k9s", "helm", "terraform"},                 6,  "OPS "),
    ({"cargo", "rustc"},                                                12,  "RS  "),
]


def tab_category(tab: Tab) -> Tuple[int, str]:
    """Return (color_pair, 4-char label) based on title/process keywords."""
    haystack = (tab.title + " " + tab.process).lower()
    tokens = set(re.split(r"[\s/\-_.]", haystack))
    for keywords, cp, label in _CATEGORIES:
        if tokens & keywords:
            return cp, label
    return 4, "    "


def tab_state(tab: Tab) -> Tuple[str, str, int]:
    """Return (indicator_char, state_text, color_pair)."""
    if tab.busy:
        proc = tab.process.lstrip("-")
        if proc not in _SHELL_PROCS:
            return "▶", proc, 1      # named process running — green
        return "▶", "running", 1     # shell is busy — green
    return "─", "idle", 8            # waiting for prompt — dim


# ─── Drawing ─────────────────────────────────────────────────────────────────

def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1,  curses.COLOR_GREEN,   -1)   # busy / running
    curses.init_pair(2,  curses.COLOR_CYAN,    -1)   # window header
    curses.init_pair(3,  curses.COLOR_YELLOW,  -1)   # hint / label / JS
    curses.init_pair(4,  curses.COLOR_WHITE,   -1)   # normal
    curses.init_pair(5,  curses.COLOR_BLACK,   curses.COLOR_CYAN)  # selected
    curses.init_pair(6,  curses.COLOR_RED,     -1)   # error / git / ops
    curses.init_pair(7,  curses.COLOR_WHITE,   curses.COLOR_BLACK) # header/footer
    curses.init_pair(8,  curses.COLOR_BLUE,    -1)   # dim / idle / editor
    curses.init_pair(9,  curses.COLOR_WHITE,   -1)   # preview
    curses.init_pair(10, curses.COLOR_BLACK,   curses.COLOR_GREEN) # selected+busy
    curses.init_pair(11, curses.COLOR_MAGENTA, -1)   # AI / claude category
    curses.init_pair(12, curses.COLOR_RED,     -1)   # git / ops / rust category


def safe(stdscr, y, x, text, attr=0):
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w:
        return
    avail = w - x - 1
    if avail <= 0:
        return
    try:
        stdscr.addstr(y, x, text[:avail], attr)
    except curses.error:
        pass


def draw(stdscr, tabs: List[Tab], selected: int, loader: PreviewLoader,
         message: str, h: int, w: int):
    stdscr.erase()

    # ── Header ──────────────────────────────────────────────────────────────
    header = "  terminal-manager  "
    bar = header.center(w, "─")
    safe(stdscr, 0, 0, bar[:w - 1], curses.color_pair(7) | curses.A_BOLD)

    # ── Layout ──────────────────────────────────────────────────────────────
    list_w = min(48, max(28, w // 3))
    div_x  = list_w
    det_x  = div_x + 1

    # ── Left: tab list ───────────────────────────────────────────────────────
    for i, tab in enumerate(tabs):
        y = 1 + i
        if y >= h - 1:
            break
        is_sel  = (i == selected)
        is_busy = tab.busy

        cat_cp, cat_label = tab_category(tab)
        ind, _state_text, state_cp = tab_state(tab)

        # Fixed-width badge on the right edge: "[TEST]" = 6 chars
        badge = f"[{cat_label}]"

        # Middle: "Win X [Tab Y]  title"
        info = f"Win {tab.win_idx}"
        if tab.tab_idx > 1:
            info += f" Tab {tab.tab_idx}"
        mid_w = list_w - 8   # 2 for indicator, 6 for badge
        title = tab.title if tab.title else tab.process
        title_space = mid_w - len(info) - 2
        if title_space > 0 and len(title) > title_space:
            title = title[:title_space - 1] + "…"
        elif title_space <= 0:
            title = ""
        mid = f"{info}  {title}".ljust(mid_w)[:mid_w]

        if is_sel:
            row_attr  = curses.color_pair(5) | curses.A_BOLD
            badge_attr = curses.color_pair(5) | curses.A_BOLD
        elif is_busy:
            row_attr  = curses.color_pair(cat_cp) | curses.A_BOLD
            badge_attr = curses.color_pair(cat_cp) | curses.A_BOLD
        else:
            row_attr  = curses.color_pair(cat_cp)
            badge_attr = curses.color_pair(cat_cp) | curses.A_BOLD

        ind_attr = curses.color_pair(state_cp) | (curses.A_BOLD if is_busy else 0)

        safe(stdscr, y, 0,          f"{ind} ",  ind_attr)
        safe(stdscr, y, 2,          mid,         row_attr)
        safe(stdscr, y, list_w - 6, badge,       badge_attr)

    # Fill rest of left column
    for y in range(1 + len(tabs), h - 1):
        safe(stdscr, y, 0, " " * list_w, curses.color_pair(4))

    # ── Divider ──────────────────────────────────────────────────────────────
    for y in range(1, h - 1):
        try:
            stdscr.addch(y, div_x, curses.ACS_VLINE, curses.color_pair(4))
        except curses.error:
            pass

    # ── Right: detail panel ──────────────────────────────────────────────────
    if tabs and 0 <= selected < len(tabs):
        tab = tabs[selected]
        det_w = w - det_x - 1
        y = 1

        def put(text, color=4, bold=False):
            nonlocal y
            if y >= h - 2:
                return
            attr = curses.color_pair(color) | (curses.A_BOLD if bold else 0)
            safe(stdscr, y, det_x + 1, text[:det_w], attr)
            y += 1

        cat_cp, cat_label = tab_category(tab)
        ind, state_text, state_cp = tab_state(tab)

        put(f"Window  {tab.win_idx}" +
            (f"  Tab {tab.tab_idx}" if tab.tab_idx > 1 else ""), 2, True)
        put("")
        put(f"  {ind}  {state_text.upper()}", state_cp, bold=True)
        proc = tab.process.lstrip("-")
        if tab.busy and proc not in _SHELL_PROCS:
            put(f"       {proc}", cat_cp, bold=True)
        put("")
        if cat_label.strip():
            put(f"Category  [{cat_label.strip()}]", cat_cp, bold=True)
        put(f"TTY       {tab.tty}", 8)
        put("")

        if tab.title:
            put("─── Title " + "─" * max(0, det_w - 11), 8)
            # word-wrap the title
            words = tab.title.split()
            line = ""
            for word in words:
                if len(line) + len(word) + 1 > det_w:
                    put("  " + line, 3)
                    line = word
                else:
                    line = (line + " " + word).strip()
            if line:
                put("  " + line, 3)
            put("")

        # Preview
        preview = loader.get(tab.tty)
        put("─── Last output " + "─" * max(0, det_w - 17), 8)
        if preview:
            for line in preview.splitlines():
                if y >= h - 2:
                    break
                safe(stdscr, y, det_x + 1, line[:det_w], curses.color_pair(9))
                y += 1
        else:
            put("  (loading…)", 8)

        # Hint
        hint = "  Enter → switch to this terminal"
        safe(stdscr, h - 2, det_x + 1, hint[:det_w], curses.color_pair(3))

    # ── Footer ───────────────────────────────────────────────────────────────
    if message:
        foot = f"  {message}"
    else:
        foot = "  ↑↓/jk navigate   Enter switch   R refresh   q quit"
    safe(stdscr, h - 1, 0, foot.ljust(w)[:w - 1], curses.color_pair(7))

    stdscr.refresh()


# ─── Prompt ──────────────────────────────────────────────────────────────────

def prompt(stdscr, h, w, text: str) -> str:
    curses.echo()
    curses.curs_set(1)
    label = f"  {text}: "
    safe(stdscr, h - 1, 0, (label + " " * w)[:w - 1],
         curses.color_pair(3) | curses.A_BOLD)
    stdscr.refresh()
    value = ""
    x = len(label)
    while True:
        try:
            ch = stdscr.getch(h - 1, min(x + len(value), w - 2))
        except Exception:
            break
        if ch in (10, 13, curses.KEY_ENTER):
            break
        elif ch == 27:
            value = ""
            break
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            value = value[:-1]
            safe(stdscr, h - 1, x, value + "  ", curses.color_pair(3))
        elif 32 <= ch < 127:
            value += chr(ch)
            safe(stdscr, h - 1, x, value, curses.color_pair(3))
        stdscr.refresh()
    curses.noecho()
    curses.curs_set(0)
    return value


# ─── Main loop ───────────────────────────────────────────────────────────────

def main(stdscr):
    init_colors()
    curses.curs_set(0)
    stdscr.keypad(True)
    stdscr.timeout(3000)   # auto-refresh every 3s

    selected = 0
    message  = ""
    msg_until = 0.0
    tabs: List[Tab] = []
    last_refresh = 0.0

    loader = PreviewLoader()
    loader.start()

    try:
        while True:
            now = time.time()

            # Refresh tab list
            if now - last_refresh > 3.0:
                tabs = get_tabs()
                last_refresh = now

            selected = max(0, min(selected, max(0, len(tabs) - 1)))

            # Request preview for selected tab
            if tabs and selected < len(tabs):
                loader.request(tabs[selected].tty)

            h, w = stdscr.getmaxyx()
            msg = message if now < msg_until else ""
            draw(stdscr, tabs, selected, loader, msg, h, w)

            key = stdscr.getch()
            if key == -1:
                continue

            if key in (ord("q"), 27):
                break

            elif key in (curses.KEY_UP, ord("k")):
                selected = max(0, selected - 1)

            elif key in (curses.KEY_DOWN, ord("j")):
                selected = min(len(tabs) - 1, selected + 1)

            elif key in (10, 13, curses.KEY_ENTER):
                if tabs:
                    tab = tabs[selected]
                    loader.stop()
                    switch_to(tab.tty)
                    return

            elif key in (ord("R"), curses.KEY_F5):
                tabs = get_tabs()
                last_refresh = time.time()
                message = "Refreshed"
                msg_until = time.time() + 1.5

            elif key == curses.KEY_RESIZE:
                pass

    finally:
        loader.stop()


# ─── Entry point ─────────────────────────────────────────────────────────────

def check_terminal_app() -> bool:
    out = osascript('tell application "System Events" to (name of processes) contains "Terminal"')
    return out == "true"


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        sys.exit(0)

    if not check_terminal_app():
        print("Terminal.app doesn't appear to be running.")
        print("Open Terminal.app first, then run tm again.")
        sys.exit(1)

    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
