#!/usr/bin/env python3
"""
tm - terminal manager
Visual TUI for managing tmux sessions and windows.

Usage:
  python3 tm.py          # launch manager
  python3 tm.py --help   # show help

Controls:
  ↑/↓ or j/k  Navigate
  Enter        Switch to selected window
  n            New window in selected session
  d            Delete/kill selected window
  r            Rename selected window
  R            Refresh manually (auto-refreshes every 2s)
  q / Esc      Quit
"""

import curses
import subprocess
import sys
import os
import shutil
import time
import threading
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# ─── Data model ──────────────────────────────────────────────────────────────

@dataclass
class Pane:
    session: str
    window_index: int
    pane_index: int
    command: str
    path: str
    is_active: bool
    pid: str
    title: str
    preview: str = ""   # last few lines from capture-pane


@dataclass
class Window:
    session: str
    index: int
    name: str
    panes: List[Pane] = field(default_factory=list)
    is_active: bool = False


@dataclass
class Session:
    name: str
    windows: List[Window] = field(default_factory=list)
    is_attached: bool = False


# ─── tmux helpers ────────────────────────────────────────────────────────────

def run(cmd: List[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def check_tmux() -> Tuple[bool, str]:
    if not shutil.which("tmux"):
        return False, "notfound"
    out = run(["tmux", "list-sessions"])
    if not out:
        return False, "nosessions"
    return True, "ok"


def get_sessions() -> List[Session]:
    sessions: dict[str, Session] = {}

    # Sessions
    for line in run(["tmux", "list-sessions", "-F",
                      "#{session_name}\t#{session_attached}"]).splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            name, attached = parts
            sessions[name] = Session(name=name, is_attached=(attached == "1"))

    # Windows
    windows: dict[str, Window] = {}
    for line in run(["tmux", "list-windows", "-a", "-F",
                      "#{session_name}\t#{window_index}\t#{window_name}\t#{window_active}"]).splitlines():
        parts = line.split("\t")
        if len(parts) == 4:
            sess, idx, name, active = parts
            if sess in sessions:
                w = Window(session=sess, index=int(idx), name=name,
                           is_active=(active == "1"))
                sessions[sess].windows.append(w)
                windows[f"{sess}:{idx}"] = w

    # Panes
    for line in run(["tmux", "list-panes", "-a", "-F",
                      "#{session_name}\t#{window_index}\t#{pane_index}"
                      "\t#{pane_current_command}\t#{pane_current_path}"
                      "\t#{pane_active}\t#{pane_pid}\t#{pane_title}"]).splitlines():
        parts = line.split("\t")
        if len(parts) == 8:
            sess, widx, pidx, cmd, path, active, pid, title = parts
            key = f"{sess}:{widx}"
            if key in windows:
                pane = Pane(session=sess, window_index=int(widx),
                            pane_index=int(pidx), command=cmd,
                            path=path, is_active=(active == "1"),
                            pid=pid, title=title)
                windows[key].panes.append(pane)

    # Sort windows per session
    for s in sessions.values():
        s.windows.sort(key=lambda w: w.index)

    return list(sessions.values())


def capture_pane(session: str, window: int, pane: int, lines: int = 8) -> str:
    target = f"{session}:{window}.{pane}"
    out = run(["tmux", "capture-pane", "-p", "-t", target,
               "-J", f"-e"])  # -J joins wrapped lines, -e includes escape
    # Strip ANSI escape codes simply
    import re
    ansi = re.compile(r"\x1b\[[0-9;]*[mKHJABCDfhlsu]|\x1b\([A-Z]|\r")
    clean = ansi.sub("", out)
    # Return last N non-empty lines
    result_lines = [l for l in clean.splitlines() if l.strip()][-lines:]
    return "\n".join(result_lines)


def flat_list(sessions: List[Session]) -> List[Tuple[Session, Window]]:
    items = []
    for session in sessions:
        for window in session.windows:
            items.append((session, window))
    return items


def switch_to(session_name: str, window_index: int):
    target = f"{session_name}:{window_index}"
    if os.environ.get("TMUX"):
        run(["tmux", "switch-client", "-t", target])
    else:
        curses.endwin()
        os.execvp("tmux", ["tmux", "attach-session", "-t", session_name,
                            "-", "select-window", "-t", target])


def new_window(session_name: str):
    run(["tmux", "new-window", "-t", session_name])


def kill_window(session_name: str, window_index: int):
    run(["tmux", "kill-window", "-t", f"{session_name}:{window_index}"])


def rename_window(session_name: str, window_index: int, name: str):
    run(["tmux", "rename-window", "-t", f"{session_name}:{window_index}", name])


# ─── UI drawing ──────────────────────────────────────────────────────────────

COLORS = {}

def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1,  curses.COLOR_GREEN,   -1)   # active window
    curses.init_pair(2,  curses.COLOR_CYAN,    -1)   # session header
    curses.init_pair(3,  curses.COLOR_YELLOW,  -1)   # running cmd
    curses.init_pair(4,  curses.COLOR_WHITE,   -1)   # normal
    curses.init_pair(5,  curses.COLOR_BLACK,   curses.COLOR_CYAN)   # selected
    curses.init_pair(6,  curses.COLOR_RED,     -1)   # error
    curses.init_pair(7,  curses.COLOR_BLUE,    -1)   # path dim
    curses.init_pair(8,  curses.COLOR_BLACK,   curses.COLOR_WHITE)  # header/footer bar
    curses.init_pair(9,  curses.COLOR_WHITE,   -1)   # preview text
    curses.init_pair(10, curses.COLOR_MAGENTA, -1)   # attached indicator
    curses.init_pair(11, curses.COLOR_BLACK,   curses.COLOR_GREEN)  # active+selected


def safe_addstr(stdscr, y: int, x: int, text: str, attr=0):
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


def draw_header(stdscr, w: int):
    title = "  terminal-manager  "
    bar = title.center(w, "─")
    safe_addstr(stdscr, 0, 0, bar[:w - 1], curses.color_pair(8) | curses.A_BOLD)


def draw_footer(stdscr, h: int, w: int, message: str = ""):
    if message:
        text = f"  {message}"
    else:
        text = "  ↑↓/jk: navigate   Enter: switch   n: new   d: delete   r: rename   q: quit"
    bar = text.ljust(w)
    safe_addstr(stdscr, h - 1, 0, bar[:w - 1], curses.color_pair(8))


def draw_list(stdscr, sessions: List[Session], flat: List[Tuple[Session, Window]],
              selected: int, scroll: int, list_w: int, h: int):
    """Draw the left panel: session/window list."""
    y = 1
    visible_row = 0
    last_session = None

    for i, (session, window) in enumerate(flat):
        # Session group header
        if session.name != last_session:
            last_session = session.name
            if visible_row >= scroll and y < h - 1:
                attached = " ●" if session.is_attached else ""
                label = f" {session.name}{attached}"
                attr = curses.color_pair(2) | curses.A_BOLD
                safe_addstr(stdscr, y, 0, label.ljust(list_w)[:list_w], attr)
                y += 1
            visible_row += 1

        if visible_row < scroll:
            visible_row += 1
            continue
        if y >= h - 1:
            break

        # Window row
        cmd = window.panes[0].command if window.panes else "─"
        path = window.panes[0].path if window.panes else ""
        short_path = path.replace(os.path.expanduser("~"), "~") if path else ""

        is_sel = (i == selected)
        is_act = window.is_active

        # status indicator
        indicator = "▶ " if is_act else "  "
        idx_name = f"{window.index}:{window.name}"
        entry = f" {indicator}{idx_name:<16} {cmd}"

        if is_sel and is_act:
            attr = curses.color_pair(11) | curses.A_BOLD
        elif is_sel:
            attr = curses.color_pair(5) | curses.A_BOLD
        elif is_act:
            attr = curses.color_pair(1) | curses.A_BOLD
        else:
            attr = curses.color_pair(4)

        safe_addstr(stdscr, y, 0, entry.ljust(list_w)[:list_w], attr)
        y += 1
        visible_row += 1

    # Fill rest of left panel
    while y < h - 1:
        safe_addstr(stdscr, y, 0, " " * list_w, curses.color_pair(4))
        y += 1


def draw_divider(stdscr, h: int, x: int):
    for y in range(1, h - 1):
        try:
            stdscr.addch(y, x, curses.ACS_VLINE, curses.color_pair(4))
        except curses.error:
            pass


def draw_detail(stdscr, flat: List[Tuple[Session, Window]], selected: int,
                x: int, h: int, w: int, preview_cache: dict):
    """Draw the right detail panel."""
    if not flat or selected >= len(flat):
        return

    session, window = flat[selected]
    detail_w = w - x - 1
    y = 1

    def put(text, color=4, bold=False):
        nonlocal y
        if y >= h - 1:
            return
        attr = curses.color_pair(color)
        if bold:
            attr |= curses.A_BOLD
        safe_addstr(stdscr, y, x + 1, text[:detail_w], attr)
        y += 1

    put(f"Session : {session.name}", 2, True)
    attached_str = "attached" if session.is_attached else "detached"
    put(f"Status  : {attached_str}", 10 if session.is_attached else 4)
    put("")
    put(f"Window  : {window.index}: {window.name}", 4, True)
    act_str = "active" if window.is_active else "background"
    put(f"Status  : {act_str}", 1 if window.is_active else 7)
    put("")

    if window.panes:
        pane_label = "Pane" if len(window.panes) == 1 else "Panes"
        put(f"─── {pane_label} ({'─' * max(0, detail_w - 8)})", 7)
        for pane in window.panes:
            short = pane.path.replace(os.path.expanduser("~"), "~") if pane.path else ""
            act = " ◀" if pane.is_active else ""
            put(f"  [{pane.pane_index}] {pane.command}{act}", 1 if pane.is_active else 4, pane.is_active)
            put(f"      {short}", 7)
        put("")

    # Preview
    cache_key = f"{session.name}:{window.index}"
    preview = preview_cache.get(cache_key, "")
    if preview:
        put(f"─── Preview ({'─' * max(0, detail_w - 14)})", 7)
        for line in preview.splitlines():
            if y >= h - 2:
                break
            safe_addstr(stdscr, y, x + 1, line[:detail_w], curses.color_pair(9))
            y += 1
    else:
        put(f"─── Preview ({'─' * max(0, detail_w - 14)})", 7)
        put("  (loading...)", 7)

    # Hint at bottom of detail panel
    hint = "  Press Enter to switch to this window"
    safe_addstr(stdscr, h - 2, x + 1, hint[:detail_w], curses.color_pair(3))


# ─── Preview background loader ───────────────────────────────────────────────

class PreviewLoader(threading.Thread):
    """Background thread that refreshes pane previews."""

    def __init__(self):
        super().__init__(daemon=True)
        self.cache: dict = {}
        self._lock = threading.Lock()
        self._target: Optional[Tuple[str, int, int]] = None
        self._event = threading.Event()
        self._stop = False

    def request(self, session: str, window: int, pane: int):
        self._target = (session, window, pane)
        self._event.set()

    def get(self, key: str) -> str:
        with self._lock:
            return self.cache.get(key, "")

    def stop(self):
        self._stop = True
        self._event.set()

    def run(self):
        while not self._stop:
            self._event.wait(timeout=2.0)
            self._event.clear()
            if self._stop:
                break
            t = self._target
            if t:
                sess, widx, pidx = t
                key = f"{sess}:{widx}"
                try:
                    preview = capture_pane(sess, widx, pidx, lines=10)
                    with self._lock:
                        self.cache[key] = preview
                except Exception:
                    pass


# ─── Rename prompt ───────────────────────────────────────────────────────────

def prompt_rename(stdscr, h: int, w: int, current: str) -> str:
    label = f"  Rename '{current}' → "
    curses.echo()
    curses.curs_set(1)
    safe_addstr(stdscr, h - 1, 0, (label + " " * w)[:w - 1], curses.color_pair(3) | curses.A_BOLD)
    stdscr.refresh()
    name = ""
    x = len(label)
    while True:
        try:
            ch = stdscr.getch(h - 1, min(x + len(name), w - 2))
        except Exception:
            break
        if ch in (10, 13, curses.KEY_ENTER):
            break
        elif ch == 27:
            name = ""
            break
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            name = name[:-1]
        elif 32 <= ch < 127:
            name += chr(ch)
            safe_addstr(stdscr, h - 1, x, name + " ", curses.color_pair(3))
            stdscr.refresh()
    curses.noecho()
    curses.curs_set(0)
    return name


# ─── Main TUI loop ───────────────────────────────────────────────────────────

def main(stdscr):
    init_colors()
    curses.curs_set(0)
    stdscr.keypad(True)
    stdscr.timeout(2000)   # refresh every 2s for live updates

    selected = 0
    scroll = 0
    message = ""
    message_until = 0.0

    loader = PreviewLoader()
    loader.start()

    sessions: List[Session] = []
    flat: List[Tuple[Session, Window]] = []
    last_refresh = 0.0

    try:
        while True:
            now = time.time()

            # Refresh data every 2s or on first load
            if now - last_refresh > 2.0:
                sessions = get_sessions()
                flat = flat_list(sessions)
                last_refresh = now

            if not flat:
                stdscr.clear()
                safe_addstr(stdscr, 1, 2, "No tmux sessions found.", curses.color_pair(6) | curses.A_BOLD)
                safe_addstr(stdscr, 3, 2, "Start one with:  tmux new-session -s main", curses.color_pair(4))
                safe_addstr(stdscr, 4, 2, "Or just:         tmux", curses.color_pair(7))
                safe_addstr(stdscr, 6, 2, "Press q to quit.", curses.color_pair(7))
                stdscr.refresh()
                key = stdscr.getch()
                if key in (ord("q"), 27):
                    break
                continue

            # Clamp selection
            selected = max(0, min(selected, len(flat) - 1))

            # Request preview for selected pane
            sel_session, sel_window = flat[selected]
            if sel_window.panes:
                active_pane = next((p for p in sel_window.panes if p.is_active), sel_window.panes[0])
                loader.request(sel_session.name, sel_window.index, active_pane.pane_index)

            # Layout
            h, w = stdscr.getmaxyx()
            list_w = min(46, max(30, w // 3))
            div_x = list_w
            detail_x = div_x

            stdscr.erase()
            draw_header(stdscr, w)
            draw_list(stdscr, sessions, flat, selected, scroll, list_w, h)
            draw_divider(stdscr, h, div_x)
            draw_detail(stdscr, flat, selected, detail_x, h, w, loader.cache)

            # Footer message
            if message and now < message_until:
                draw_footer(stdscr, h, w, message)
            else:
                message = ""
                draw_footer(stdscr, h, w)

            stdscr.refresh()

            # Input
            key = stdscr.getch()
            if key == -1:
                continue  # timeout — just refresh

            if key in (ord("q"), 27):
                break

            elif key in (curses.KEY_UP, ord("k")):
                if selected > 0:
                    selected -= 1
                    # Scroll up if needed (rough: assume ~1.3 rows per window)
                    if scroll > 0 and selected < scroll:
                        scroll = max(0, scroll - 1)

            elif key in (curses.KEY_DOWN, ord("j")):
                if selected < len(flat) - 1:
                    selected += 1
                    # Scroll down if needed
                    max_visible = h - 3
                    if selected - scroll >= max_visible - 2:
                        scroll += 1

            elif key in (10, 13, curses.KEY_ENTER):
                sel_s, sel_w = flat[selected]
                if os.environ.get("TMUX"):
                    # We're inside tmux: switch client
                    target = f"{sel_s.name}:{sel_w.index}"
                    run(["tmux", "switch-client", "-t", target])
                    # Also select the window in that session
                    run(["tmux", "select-window", "-t", target])
                    loader.stop()
                    break   # exit manager after switching
                else:
                    # Outside tmux: exit curses, attach
                    loader.stop()
                    curses.endwin()
                    target = f"{sel_s.name}:{sel_w.index}"
                    os.execvp("tmux", ["tmux", "attach-session", "-t",
                                       sel_s.name])

            elif key == ord("n"):
                sel_s, _ = flat[selected]
                new_window(sel_s.name)
                sessions = get_sessions()
                flat = flat_list(sessions)
                last_refresh = now
                selected = len(flat) - 1
                message = f"Created new window in '{sel_s.name}'"
                message_until = time.time() + 2.0

            elif key == ord("d"):
                if len(flat) <= 1:
                    message = "Can't delete the last window!"
                    message_until = time.time() + 2.0
                else:
                    sel_s, sel_w = flat[selected]
                    kill_window(sel_s.name, sel_w.index)
                    sessions = get_sessions()
                    flat = flat_list(sessions)
                    last_refresh = now
                    selected = max(0, selected - 1)
                    message = f"Killed window {sel_w.index}:{sel_w.name}"
                    message_until = time.time() + 2.0

            elif key == ord("r"):
                sel_s, sel_w = flat[selected]
                new_name = prompt_rename(stdscr, h, w, sel_w.name)
                if new_name:
                    rename_window(sel_s.name, sel_w.index, new_name)
                    sessions = get_sessions()
                    flat = flat_list(sessions)
                    last_refresh = now
                    message = f"Renamed to '{new_name}'"
                    message_until = time.time() + 2.0

            elif key in (ord("R"), curses.KEY_F5):
                sessions = get_sessions()
                flat = flat_list(sessions)
                last_refresh = now
                message = "Refreshed"
                message_until = time.time() + 1.5

            elif key == curses.KEY_RESIZE:
                pass  # will redraw on next iteration

    finally:
        loader.stop()


# ─── Entry point ─────────────────────────────────────────────────────────────

def entrypoint():
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        sys.exit(0)

    ok, reason = check_tmux()
    if not ok:
        if reason == "notfound":
            print("Error: tmux is not installed.")
            print()
            print("Install it:")
            print("  macOS:  brew install tmux")
            print("  Ubuntu: sudo apt install tmux")
            sys.exit(1)
        elif reason == "nosessions":
            print("No tmux sessions running.")
            print()
            print("Start one first:")
            print("  tmux                          # new unnamed session")
            print("  tmux new-session -s main      # named session")
            sys.exit(1)

    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    entrypoint()
