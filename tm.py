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


# ─── Drawing ─────────────────────────────────────────────────────────────────

def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1,  curses.COLOR_GREEN,   -1)   # busy / active
    curses.init_pair(2,  curses.COLOR_CYAN,    -1)   # window header
    curses.init_pair(3,  curses.COLOR_YELLOW,  -1)   # hint / label
    curses.init_pair(4,  curses.COLOR_WHITE,   -1)   # normal
    curses.init_pair(5,  curses.COLOR_BLACK,   curses.COLOR_CYAN)  # selected
    curses.init_pair(6,  curses.COLOR_RED,     -1)   # error
    curses.init_pair(7,  curses.COLOR_WHITE,   curses.COLOR_BLACK) # header/footer
    curses.init_pair(8,  curses.COLOR_BLUE,    -1)   # dim path / idle
    curses.init_pair(9,  curses.COLOR_WHITE,   -1)   # preview
    curses.init_pair(10, curses.COLOR_BLACK,   curses.COLOR_GREEN) # selected+busy


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

        indicator = "● " if is_busy else "○ "
        label = f" {indicator}Win {tab.win_idx}"
        if tab.tab_idx > 1:
            label += f" Tab {tab.tab_idx}"

        # Truncate title to fit
        title_space = list_w - len(label) - 2
        title = tab.title if tab.title else tab.process
        if len(title) > title_space:
            title = title[:title_space - 1] + "…"
        row = f"{label}  {title}"

        if is_sel and is_busy:
            attr = curses.color_pair(10) | curses.A_BOLD
        elif is_sel:
            attr = curses.color_pair(5) | curses.A_BOLD
        elif is_busy:
            attr = curses.color_pair(1)
        else:
            attr = curses.color_pair(8)

        safe(stdscr, y, 0, row.ljust(list_w)[:list_w], attr)

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

        put(f"Window  {tab.win_idx}" +
            (f"  Tab {tab.tab_idx}" if tab.tab_idx > 1 else ""), 2, True)
        put(f"Status  {'running' if tab.busy else 'idle'}",
            1 if tab.busy else 8)
        put(f"Process {tab.process}", 4)
        put(f"TTY     {tab.tty}", 8)
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
                    curses.endwin()
                    switch_to(tab.tty)
                    sys.exit(0)

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
    r = subprocess.run(["pgrep", "-x", "Terminal"], capture_output=True)
    return r.returncode == 0


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        sys.exit(0)

    if not check_terminal_app():
        print("Terminal.app doesn't appear to be running.")
        sys.exit(1)

    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
