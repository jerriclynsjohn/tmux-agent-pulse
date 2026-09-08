#!/usr/bin/env python3
"""AgentPulse sidebar — narrow vim-style tree of Claude and Codex panes.

Lives in an optional pane on the left of every window (24 columns by default).
Reads the ticker's tmux pane options every 200ms while visible and once per
second while hidden. The highlighted row reflects
the currently-active pane across the user's tmux client — when the
user uses tmux's own pane-nav (prefix+arrows etc.) the highlight
follows automatically.

Keys (active only while the sidebar pane itself is focused):
    j / Down / Ctrl+Down   — switch tmux to the next pane in the tree
    k / Up   / Ctrl+Up     — switch tmux to the previous pane
    Enter / Right / l      — re-fire switch to the selected pane (no-op
                             when the highlight already follows the
                             active pane)
    q                       — quit (closes this sidebar pane)
"""
import curses
import os
import subprocess
import sys
import time

from agent_pulse import FIELD_SEPARATOR, process_snapshot, tmux

SEPARATOR = FIELD_SEPARATOR
VISIBLE_POLL_MS = 200
HIDDEN_POLL_MS = 1000
ASCII = False
COLOR_SUPPORT = False
HIGH_COLOR = False

ICONS = {
    "waiting":   "⏳",
    "working":   "●",
    "cancelled": "✗",
    "idle":      "✓",
    "unknown":   "○",
}
ASCII_ICONS = {"waiting": "?", "working": "*", "cancelled": "x", "idle": "+", "unknown": "-"}

# Terminal palette; no external theme or patched font is required.
# Each fg has three pair slots:
#   - default-bg            (normal row)
#   - SEL_BG (passive)      — used to mark the currently-active pane
#                              when the user isn't focused on the sidebar
#   - NAV_BG (nav cursor)   — used while the user is keyboard-navigating
#                              the sidebar, so it's visually distinct from
#                              the passive active marker
SEL_BG = 237  # passive "your focus is here" tint
NAV_BG = 24   # active "you're picking this" tint
P_WAIT,    P_WAIT_S,    P_WAIT_N    = 1,  11,  21
P_WORK,    P_WORK_S,    P_WORK_N    = 2,  12,  22
P_CANCEL,  P_CANCEL_S,  P_CANCEL_N  = 3,  13,  23
P_IDLE,    P_IDLE_S,    P_IDLE_N    = 4,  14,  24
P_SESSION, P_SESSION_S, P_SESSION_N = 5,  15,  25
P_WINDOW,  P_WINDOW_S,  P_WINDOW_N  = 6,  16,  26
P_DIM,     P_DIM_S,     P_DIM_N     = 7,  17,  27
P_FILL,    P_FILL_NAV               = 18, 28
P_CHEV,    P_CHEV_S,    P_CHEV_N    = 9,  19,  29

ICON_PAIR = {
    # state -> (default, sel_bg, nav_bg)
    "waiting":   (P_WAIT,   P_WAIT_S,   P_WAIT_N),
    "working":   (P_WORK,   P_WORK_S,   P_WORK_N),
    "cancelled": (P_CANCEL, P_CANCEL_S, P_CANCEL_N),
    "idle":      (P_IDLE,   P_IDLE_S,   P_IDLE_N),
    "unknown":   (P_DIM,    P_DIM_S,    P_DIM_N),
}


def _hostname():
    try:
        return os.uname().nodename
    except AttributeError:
        return ""


HOST = _hostname()


def _set_pane_title():
    """OSC 2 sequence — tells the host terminal (and tmux, which captures
    it as pane_title) what this pane is."""
    sys.stdout.write("\033]2;AgentPulse\007")
    sys.stdout.flush()


def collect_sidebar_snapshot(my_session=None, my_pane=None):
    """Read the tree and the active pane in this sidebar's own window.

    Other clients/windows cannot change this sidebar's focus. The pane ID
    also keeps the tree in the right session after a session is renamed.
    """
    try:
        out = tmux(["list-panes", "-a", "-F", SEPARATOR.join((
            "#{pane_id}", "#{session_name}", "#{window_id}", "#{window_index}",
            "#{window_name}", "#{pane_index}", "#{pane_current_path}",
            "#{pane_title}", "#{pane_current_command}", "#{@agent-pulse-name}",
            "#{session_attached}", "#{window_active}", "#{pane_active}",
            "#{@agent-pulse-sidebar}", "#{@agent-pulse-state}", "#{@agent-pulse-provider}",
        ))], timeout=1)
    except Exception:
        return [], None, False, None, False

    records = [line.split(SEPARATOR) for line in out.splitlines()]
    records = [fields for fields in records if len(fields) == 16]
    anchor = next((fields for fields in records if fields[0] == my_pane), None)
    my_window = anchor[2] if anchor else None
    if my_pane and anchor is None:
        return [], None, False, None, False
    if anchor:
        my_session = anchor[1]
    # A window can be linked into more than one session. Any attached session
    # that currently displays this window keeps its sidebar responsive.
    visible = any(fields[2] == my_window and fields[10].isdigit() and
                  int(fields[10]) > 0 and fields[11] == "1" for fields in records)

    active_pid = None
    active_is_sidebar = False
    panes = []
    active_candidates = []
    for parts in records:
        pid, session, winid, winidx_s, winname, paneidx_s, cwd, panetitle, cmd, \
            user_name, sess_attach, win_active, pane_active, is_sidebar, state, provider = parts
        if my_session and session != my_session:
            continue
        if pane_active == "1" and (
                (my_window and winid == my_window) or
                (not my_pane and sess_attach.isdigit() and int(sess_attach) > 0 and win_active == "1")):
            active_candidates.append((pid, is_sidebar == "1"))
        # The ticker has already checked process ownership and stale records.
        if is_sidebar == "1" or state not in ICONS or provider not in ("claude", "codex"):
            continue
        panes.append({
            "session": session,
            "window_id": winid,
            "winidx": int(winidx_s or 0),
            "winname": winname,
            "paneidx": int(paneidx_s or 0),
            "cwd": cwd,
            "panetitle": panetitle,
            "cmd": cmd,
            "user_name": user_name,
            "pane_id": pid,
            "state": state,
            "provider": provider,
        })
    panes.sort(key=lambda p: (p["session"], p["winidx"], p["paneidx"]))

    rows = []
    last_window = None
    for p in panes:
        win_key = (p["session"], p["window_id"])
        if win_key != last_window:
            if last_window is not None:
                rows.append({"kind": "separator"})
            rows.append({
                "kind": "window",
                "winidx": p["winidx"],
                "label": p["winname"],
            })
            last_window = win_key

        # Title priority — same as the pane-border-format:
        #   user_name set        → "<user_name> · <agent task>"  (or just
        #                            <user_name> when the title is the
        #                            default hostname)
        #   user_name not set    → agent task / window name (existing
        #                            fallback)
        agent_title = p["panetitle"]
        if not agent_title or agent_title == HOST or agent_title == p["cmd"]:
            agent_title = p["winname"]
        if p["provider"] == "claude":
            agent_title = agent_title.lstrip("✳ " + "".join(map(chr, range(0x2800, 0x2900)))).strip()
        if p["user_name"]:
            title = (
                f"{p['user_name']} · {agent_title}"
                if agent_title and agent_title != p["winname"]
                else p["user_name"]
            )
        else:
            title = agent_title

        dirname = os.path.basename(p["cwd"]) or p["cwd"]

        rows.append({
            "kind": "pane",
            "session": p["session"],
            "pane_id": p["pane_id"],
            "window_id": p["window_id"],
            "state": p["state"],
            "provider": p["provider"],
            "paneidx": p["paneidx"],
            "winidx": p["winidx"],
            "title": title or "pane",
            "dirname": dirname,
        })
    # A shared tree without a sidebar anchor must not pick an arbitrary client.
    if len(set(active_candidates)) == 1:
        active_pid, active_is_sidebar = active_candidates[0]
    return rows, active_pid, active_is_sidebar, my_window, visible


def collect_tree_and_active(my_session=None, my_pane=None):
    rows, active, is_sidebar, _, _ = collect_sidebar_snapshot(my_session, my_pane)
    return rows, active, is_sidebar


def collect_tree(my_session=None):
    """Back-compat wrapper for ad-hoc inspection / tests."""
    rows, _, _ = collect_tree_and_active(my_session)
    return rows


def get_my_session(my_pane=None):
    """Session the sidebar pane itself lives in — used to scope the tree."""
    try:
        if not my_pane:
            return None
        return tmux(["display-message", "-p", "-t", my_pane, "#{session_name}"], timeout=1)
    except Exception:
        return None


def get_my_pane():
    """Identify our actual terminal/process, rather than inherited TMUX_PANE."""
    try:
        snapshot = tmux(["list-panes", "-a", "-F", SEPARATOR.join((
            "#{pane_id}", "#{pane_pid}", "#{pane_tty}"))], timeout=1)
        panes = [line.split(SEPARATOR) for line in snapshot.splitlines()]
        panes = [fields for fields in panes if len(fields) == 3]
        try:
            terminal = os.ttyname(sys.stdin.fileno())
        except (OSError, ValueError, AttributeError):
            terminal = None
        matches = {pane for pane, _, tty in panes if terminal and tty == terminal}
        if len(matches) == 1:
            return matches.pop()
        processes = process_snapshot()
        pid, ancestors = os.getpid(), set()
        while pid and pid not in ancestors:
            ancestors.add(pid)
            pid = processes.get(pid, {}).get("ppid", 0)
        matches = {pane for pane, root, _ in panes if root.isdigit() and int(root) in ancestors}
        return matches.pop() if len(matches) == 1 else None
    except Exception:
        return None


def get_previous_pane(window_id):
    """Read tmux's actual previous pane when keyboard focus enters the sidebar."""
    if not window_id:
        return None
    try:
        snapshot = tmux(["list-panes", "-t", window_id, "-F",
                         "#{pane_id}" + SEPARATOR + "#{pane_last}"], timeout=1)
        return next((line.split(SEPARATOR)[0] for line in snapshot.splitlines()
                     if line.endswith(SEPARATOR + "1")), None)
    except Exception:
        return None


def _load_options():
    global ASCII
    try:
        ASCII = tmux(["show-options", "-g", "-v", "@agent-pulse-ascii"]) in ("on", "1", "yes", "true")
    except subprocess.SubprocessError:
        ASCII = False


def _init_colors():
    global COLOR_SUPPORT, HIGH_COLOR
    COLOR_SUPPORT = HIGH_COLOR = False
    if not curses.has_colors():
        return
    curses.start_color()
    try:
        curses.use_default_colors()
        default_bg = -1
    except curses.error:
        default_bg = curses.COLOR_BLACK
    # (fg, default-bg-pair, selected-bg-pair)
    base = [
        # (fg, default-pair, sel-pair, nav-pair)
        (220, P_WAIT,    P_WAIT_S,    P_WAIT_N),     # yellow
        (45,  P_WORK,    P_WORK_S,    P_WORK_N),     # cyan
        (203, P_CANCEL,  P_CANCEL_S,  P_CANCEL_N),   # red
        (114, P_IDLE,    P_IDLE_S,    P_IDLE_N),     # green
        (75, P_SESSION, P_SESSION_S, P_SESSION_N),  # blue
        (252, P_WINDOW,  P_WINDOW_S,  P_WINDOW_N),   # text
        (245, P_DIM,     P_DIM_S,     P_DIM_N),      # dim
        (75, P_CHEV,    P_CHEV_S,    P_CHEV_N),     # blue
    ]
    if curses.COLORS >= 256:
        for fg, p_norm, p_sel, p_nav in base:
            curses.init_pair(p_norm, fg, default_bg)
            curses.init_pair(p_sel,  fg, SEL_BG)
            curses.init_pair(p_nav,  fg, NAV_BG)
        curses.init_pair(P_FILL,     curses.COLOR_WHITE, SEL_BG)
        curses.init_pair(P_FILL_NAV, curses.COLOR_WHITE, NAV_BG)
        HIGH_COLOR = True
    else:
        # 8-color fallback — collapse both bg variants to the default
        # and let A_REVERSE handle the highlight.
        fb = {
            P_WAIT:    curses.COLOR_YELLOW,
            P_WORK:    curses.COLOR_CYAN,
            P_CANCEL:  curses.COLOR_RED,
            P_IDLE:    curses.COLOR_GREEN,
            P_SESSION: curses.COLOR_BLUE,
            P_WINDOW:  curses.COLOR_WHITE,
            P_DIM:     curses.COLOR_WHITE,
            P_CHEV:    curses.COLOR_MAGENTA,
        }
        for pair, color in fb.items():
            curses.init_pair(pair,      color, default_bg)
            curses.init_pair(pair + 10, color, default_bg)  # _S
            curses.init_pair(pair + 20, color, default_bg)  # _N
        curses.init_pair(P_FILL,     curses.COLOR_WHITE, default_bg)
        curses.init_pair(P_FILL_NAV, curses.COLOR_WHITE, default_bg)
    COLOR_SUPPORT = True


def _color(pair):
    return curses.color_pair(pair) if COLOR_SUPPORT else 0


def _trunc(text, max_w):
    if max_w <= 0:
        return ""
    if len(text) <= max_w:
        return text
    if max_w <= 1:
        return text[:max_w]
    return text[: max_w - 1] + ("~" if ASCII else "…")


def pane_detail(row, max_w):
    """Keep the provider visible beside the state when the sidebar has room."""
    provider = row.get("provider", "")
    detail = row["dirname"]
    if provider and len(provider) <= max_w:
        if max_w < len(provider) + 5:
            return provider
        detail = f"{provider} {'|' if ASCII else '·'} {detail}"
    return _trunc(detail, max_w)


def _safe_addstr(win, y, x, text, attr=0):
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


def _paint_row(stdscr, y, w, pair=None):
    """Fill the row with one of the two highlight bgs (sel or nav). The
    text overlays then use the matching *_S / *_N color pair so the bg
    stays continuous across the row."""
    if pair is None:
        pair = P_FILL
    try:
        stdscr.addstr(y, 0, " " * max(0, w - 1), _color(pair) | (0 if HIGH_COLOR else curses.A_REVERSE))
    except curses.error:
        pass


def _has_italic():
    return hasattr(curses, "A_ITALIC")


def _row_height(row):
    return 2 if row["kind"] == "pane" else 1


def _viewport_start(rows, highlight_idx, height):
    """Keep the selected pane's title and detail visible in long trees."""
    if not 0 <= highlight_idx < len(rows):
        return 0
    used = _row_height(rows[highlight_idx])
    start = highlight_idx
    while start > 0 and used + _row_height(rows[start - 1]) <= height:
        start -= 1
        used += _row_height(rows[start])
    return start


def _render(stdscr, rows, highlight_idx, highlight_kind):
    """Two-line-per-pane layout with the dim ` · ` separator.

        project                      ← window (bold blue)
          ✓ · Example task…          ← icon · pane title (bold)
          idle · project             ← state · cwd (dim)
          ● · Another task…
          working · project
        ────────────                  ← thin separator between windows
        other-project
          ✓ · General task…
          idle · other-project

    Exactly one row gets a background tint. `highlight_kind` decides
    which one:
      - "active": passive marker showing the currently-focused content
        pane. Subtle SEL_BG.
      - "nav":    the user is keyboard-navigating the sidebar; this is
        the cursor row they're hovering. NAV_BG (tinted indigo) so it
        reads as a distinct "I'm selecting this" indicator.
    """
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    if not rows:
        _safe_addstr(stdscr, 0, 1, "no active agents",
                     _color(P_DIM))
        stdscr.refresh()
        return 0

    italic = curses.A_ITALIC if _has_italic() else 0

    first_visible = _viewport_start(rows, highlight_idx, h)
    y = 0
    for i, row in enumerate(rows[first_visible:], start=first_visible):
        if y >= h:
            break

        if row["kind"] == "separator":
            # Dim thin rule the width of the pane (minus a 1-col edge).
            _safe_addstr(stdscr, y, 1, ("-" if ASCII else "─") * max(0, w - 2),
                         _color(P_DIM))
            y += 1
            continue

        if row["kind"] == "window":
            # Bold blue — the previous "session" treatment, repurposed
            # since the sidebar is already session-scoped.
            _safe_addstr(stdscr, y, 0,
                         _trunc(row["label"], w - 1),
                         _color(P_SESSION) | curses.A_BOLD)
            y += 1
            continue

        # -------- pane row: TWO lines, ` · ` separated --------
        is_hl = (i == highlight_idx)
        state = row["state"] or "unknown"
        icon = (ASCII_ICONS if ASCII else ICONS).get(state, "-" if ASCII else "·")
        selected_attr = curses.A_REVERSE if is_hl and not HIGH_COLOR else 0
        separator = " | " if ASCII else " · "
        norm_pair, sel_pair, nav_pair = ICON_PAIR.get(state, (P_DIM, P_DIM_S, P_DIM_N))

        if is_hl and highlight_kind == "nav":
            fill_pair   = P_FILL_NAV
            title_pair  = P_WINDOW_N
            dim_pair    = P_DIM_N
            status_pair = nav_pair
        elif is_hl:
            fill_pair   = P_FILL
            title_pair  = P_WINDOW_S
            dim_pair    = P_DIM_S
            status_pair = sel_pair
        else:
            fill_pair   = None
            title_pair  = P_WINDOW
            dim_pair    = P_DIM
            status_pair = norm_pair

        # ---- Line 1: <icon> · <pane title> ----
        if is_hl:
            _paint_row(stdscr, y, w, fill_pair)
        _safe_addstr(stdscr, y, 2, icon,
                     _color(status_pair) | curses.A_BOLD | selected_attr)
        _safe_addstr(stdscr, y, 4, separator,
                     _color(dim_pair) | selected_attr)
        _safe_addstr(stdscr, y, 7,
                     _trunc(row["title"], max(0, w - 8)),
                     _color(title_pair) | curses.A_BOLD | selected_attr)
        y += 1
        if y >= h:
            continue

        # ---- Line 2: <state> · <provider> · <dirname> ----
        if is_hl:
            _paint_row(stdscr, y, w, fill_pair)
        _safe_addstr(stdscr, y, 2, state,
                     _color(status_pair) | selected_attr)
        sep_pos = 2 + len(state)
        _safe_addstr(stdscr, y, sep_pos, separator,
                     _color(dim_pair) | selected_attr)
        name_pos = sep_pos + 3
        _safe_addstr(stdscr, y, name_pos,
                     pane_detail(row, max(0, w - name_pos - 1)),
                     _color(dim_pair) | italic | selected_attr)
        y += 1

    stdscr.refresh()
    return first_visible


def _first_pane(rows):
    for i, r in enumerate(rows):
        if r["kind"] == "pane":
            return i
    return -1


def _index_for_pane(rows, pane_id):
    if not pane_id:
        return -1
    for i, r in enumerate(rows):
        if r["kind"] == "pane" and r.get("pane_id") == pane_id:
            return i
    return -1


def _nav_pane(rows, from_idx, direction):
    """Walk to the next pane row from `from_idx` and return its pane_id
    (so the caller can `tmux switch-client -t` it). Wraps top<->bottom."""
    if not rows:
        return None
    n = len(rows)
    i = from_idx
    seen = 0
    while seen < n + 1:
        i += direction
        if i < 0:
            i = n - 1
        elif i >= n:
            i = 0
        if rows[i]["kind"] == "pane":
            return rows[i]["pane_id"]
        seen += 1
    return None


def _read_escape_sequence(stdscr):
    stdscr.nodelay(True)
    buf = []
    for _ in range(8):
        c = stdscr.getch()
        if c == -1:
            break
        buf.append(c)
    stdscr.nodelay(False)
    return "".join(chr(c) for c in buf if 0 <= c < 256)


def _step_cursor(rows, current_pane_id, direction):
    """Walk from current_pane_id to the next/previous pane row in the
    tree, wrapping at the ends. Returns the new pane_id (or the old
    one if nothing to step to). Working in pane_id space means inserts
    elsewhere in the tree don't desync the cursor."""
    if not rows:
        return current_pane_id
    pane_ids = [r["pane_id"] for r in rows if r["kind"] == "pane"]
    if not pane_ids:
        return current_pane_id
    try:
        i = pane_ids.index(current_pane_id) if current_pane_id else -1
    except ValueError:
        i = -1
    i += direction
    if i < 0:
        i = len(pane_ids) - 1
    if i >= len(pane_ids):
        i = 0
    return pane_ids[i]


def _y_to_row_idx(y, rows):
    """Map a screen y-coordinate back to a rows[] index. Clicks on a
    window header or separator snap to the nearest pane row (next, then
    previous), so an accidental click between items doesn't trap focus
    in the sidebar without navigating anywhere.
    Returns None only if there are no pane rows at all."""
    # Build layout: (y_start, y_end_inclusive, kind, idx). Pane rows
    # span two lines; everything else spans one.
    layout = []
    cur_y = 0
    for i, row in enumerate(rows):
        if row["kind"] == "pane":
            layout.append((cur_y, cur_y + 1, "pane", i))
            cur_y += 2
        else:
            layout.append((cur_y, cur_y, row["kind"], i))
            cur_y += 1

    # Direct hit
    hit = None
    for start, end, kind, idx in layout:
        if start <= y <= end:
            hit = (kind, idx, start)
            break

    if hit and hit[0] == "pane":
        return hit[1]

    # Non-pane click — snap to nearest pane row by y distance.
    pane_layout = [(s, e, idx) for (s, e, k, idx) in layout if k == "pane"]
    if not pane_layout:
        return None
    def _dist(item):
        s, e, _ = item
        if s <= y <= e:
            return 0
        return min(abs(y - s), abs(y - e))
    return min(pane_layout, key=_dist)[2]


class SidebarFocus:
    """Keep the keyboard cursor separate from the actual active-pane marker."""

    def __init__(self):
        self.cursor_pane_id = None
        self.last_content_pane_id = None
        self.was_focused = False

    def update(self, rows, active, my_pane, window_id, previous=None):
        focused = bool(my_pane) and active == my_pane
        if not focused:
            # Never keep a prior agent highlighted while a shell/other pane has focus.
            self.last_content_pane_id = active if _index_for_pane(rows, active) >= 0 else None
            self.cursor_pane_id = None
            self.was_focused = False
            return _index_for_pane(rows, active), "active"

        local_panes = [r["pane_id"] for r in rows
                       if r["kind"] == "pane" and r["window_id"] == window_id]
        if not self.was_focused:
            # pane_last handles startup/reload and fast switches between polls.
            candidates = [previous, self.last_content_pane_id]
            self.cursor_pane_id = next((pid for pid in candidates if pid in local_panes), None)
        if _index_for_pane(rows, self.cursor_pane_id) < 0:
            self.cursor_pane_id = (local_panes[0] if local_panes else
                                   next((r["pane_id"] for r in rows if r["kind"] == "pane"), None))
        self.was_focused = True
        return _index_for_pane(rows, self.cursor_pane_id), "nav"


def main(stdscr):
    curses.curs_set(0)
    stdscr.keypad(True)
    stdscr.timeout(200)  # 5Hz redraw so the active-pane marker stays in sync
    _init_colors()
    _load_options()
    # Enable mouse — single-button click events. The sidebar will trap
    # them, identify the target pane row, and switch focus.
    try:
        curses.mousemask(curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED |
                         curses.BUTTON1_RELEASED)
        # Don't introduce a click-interval delay so single clicks land fast.
        curses.mouseinterval(0)
    except curses.error:
        pass

    my_pane = get_my_pane()
    my_session = get_my_session(my_pane)
    focus = SidebarFocus()

    while True:
        rows, active, active_is_sidebar, window_id, visible = collect_sidebar_snapshot(my_session, my_pane)
        # getch still wakes immediately for keyboard/mouse input. A hidden
        # window otherwise rechecks visibility within one second after a switch.
        stdscr.timeout(VISIBLE_POLL_MS if visible else HIDDEN_POLL_MS)
        previous = None
        if my_pane and active == my_pane and not focus.was_focused:
            previous = get_previous_pane(window_id)
        highlight_idx, highlight_kind = focus.update(
            rows, active, my_pane, window_id, previous)
        first_visible = _render(stdscr, rows, highlight_idx, highlight_kind)
        displayed_rows = rows[first_visible:] if isinstance(first_visible, int) else rows

        try:
            key = stdscr.getch()
        except KeyboardInterrupt:
            return
        if key == -1:
            continue

        # Focus can enter while getch waits. Resolve it before the first key,
        # so the next redraw does not reset a newly moved keyboard cursor.
        rows, active, _, window_id, _ = collect_sidebar_snapshot(my_session, my_pane)
        previous = get_previous_pane(window_id) if (
            my_pane and active == my_pane and not focus.was_focused) else None
        focus.update(rows, active, my_pane, window_id, previous)

        commit = False
        if key in (curses.KEY_UP, ord("k")):
            focus.cursor_pane_id = _step_cursor(rows, focus.cursor_pane_id, -1)
        elif key in (curses.KEY_DOWN, ord("j")):
            focus.cursor_pane_id = _step_cursor(rows, focus.cursor_pane_id, +1)
        elif key in (curses.KEY_RIGHT, ord("l"), curses.KEY_ENTER, 10, 13):
            commit = True
        elif key == 27:
            seq = _read_escape_sequence(stdscr)
            if seq in ("[1;5A", "[1;2A"):
                focus.cursor_pane_id = _step_cursor(rows, focus.cursor_pane_id, -1)
            elif seq in ("[1;5B", "[1;2B"):
                focus.cursor_pane_id = _step_cursor(rows, focus.cursor_pane_id, +1)
            elif seq in ("[1;5C", "[1;2C"):
                commit = True
        elif key == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bstate = curses.getmouse()
            except curses.error:
                continue
            if bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED |
                         curses.BUTTON1_RELEASED):
                # Map the click to what was actually painted. A hook can add
                # or remove agents before the post-getch snapshot completes.
                clicked = _y_to_row_idx(my, displayed_rows)
                if clicked is not None and displayed_rows[clicked]["kind"] == "pane":
                    focus.cursor_pane_id = displayed_rows[clicked]["pane_id"]
                    commit = True
        elif key == ord("q"):
            return

        if commit and focus.cursor_pane_id:
            target = next(
                (r for r in rows
                 if r["kind"] == "pane" and r["pane_id"] == focus.cursor_pane_id),
                None,
            )
            if target:
                # Target the tree's session explicitly, not another client's
                # most recently active session. Socket selection is shared
                # with the ticker and hook process.
                try:
                    tmux(["select-window", "-t", f"={target['session']}:{target['window_id']}", ";",
                          "select-pane", "-t", target["pane_id"]])
                except subprocess.SubprocessError:
                    pass  # The chosen pane may have exited since the snapshot.


if __name__ == "__main__":
    _set_pane_title()
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        sys.exit(0)
