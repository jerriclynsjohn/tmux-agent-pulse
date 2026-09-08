"""Regressions for sidebar focus across windows, agents, and keyboard navigation."""
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(BIN))
spec = importlib.util.spec_from_file_location("sidebar_focus_subject", BIN / "tmux-agent-sidebar.py")
sidebar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sidebar)


def snapshot_row(pane, window, *, active=False, sidebar_pane=False, session="work", window_active=False):
    return sidebar.SEPARATOR.join((
        pane, session, window, "1", window, "1", "/same/cwd", "same title", "codex", "",
        "1", "1" if window_active else "0", "1" if active else "0",
        "1" if sidebar_pane else "", "" if sidebar_pane else "working",
        "" if sidebar_pane else "codex",
    ))


def rows():
    return [{"kind": "pane", "pane_id": pane, "window_id": window}
            for pane, window in (("%first", "@primary"), ("%second", "@primary"), ("%other", "@other"))]


class SidebarFocusTests(unittest.TestCase):
    def test_own_window_focus_wins_over_another_clients_current_window(self):
        snapshot = "\n".join((
            snapshot_row("%sidebar", "@primary", sidebar_pane=True),
            snapshot_row("%first", "@primary"),
            snapshot_row("%second", "@primary", active=True),
            snapshot_row("%other", "@other", active=True, window_active=True),
        ))
        with patch.object(sidebar, "tmux", return_value=snapshot):
            _, active, is_sidebar, window, visible = sidebar.collect_sidebar_snapshot("work", "%sidebar")
        self.assertEqual((active, is_sidebar, window), ("%second", False, "@primary"))
        self.assertFalse(visible)

    def test_session_rename_does_not_change_sidebar_scope(self):
        snapshot = "\n".join((
            snapshot_row("%sidebar", "@primary", sidebar_pane=True, session="renamed"),
            snapshot_row("%second", "@primary", active=True, session="renamed"),
            snapshot_row("%other", "@other", active=True, session="work", window_active=True),
        ))
        with patch.object(sidebar, "tmux", return_value=snapshot):
            collected, active, _, _, _ = sidebar.collect_sidebar_snapshot("work", "%sidebar")
        self.assertEqual(active, "%second")
        self.assertEqual([r["pane_id"] for r in collected if r["kind"] == "pane"], ["%second"])

    def test_passive_marker_follows_exact_agent_id_even_with_same_window_and_names(self):
        focus = sidebar.SidebarFocus()
        self.assertEqual(focus.update(rows(), "%first", "%sidebar", "@primary"), (0, "active"))
        self.assertEqual(focus.update(rows(), "%second", "%sidebar", "@primary"), (1, "active"))
        self.assertIsNone(focus.cursor_pane_id)

    def test_keyboard_entry_prefers_tmux_previous_pane_and_preserves_navigation(self):
        focus = sidebar.SidebarFocus()
        focus.last_content_pane_id = "%other"  # Reproduces the old cross-window cached selection.
        self.assertEqual(focus.update(rows(), "%sidebar", "%sidebar", "@primary", "%second"), (1, "nav"))
        focus.cursor_pane_id = "%other"  # User deliberately navigates to another window's row.
        reordered = list(reversed(rows()))
        self.assertEqual(focus.update(reordered, "%sidebar", "%sidebar", "@primary"), (0, "nav"))
        self.assertEqual(focus.cursor_pane_id, "%other")
        self.assertEqual(focus.update(rows(), "%first", "%sidebar", "@primary"), (0, "active"))
        self.assertIsNone(focus.cursor_pane_id)

    def test_another_sidebar_does_not_enable_this_sidebars_keyboard_cursor(self):
        focus = sidebar.SidebarFocus()
        focus.update(rows(), "%sidebar", "%sidebar", "@primary", "%first")
        self.assertEqual(focus.update(rows(), "%other-sidebar", "%sidebar", "@primary"), (-1, "active"))
        self.assertFalse(focus.was_focused)
        self.assertIsNone(focus.cursor_pane_id)

    def test_shell_or_missing_focus_does_not_retain_an_agent_highlight(self):
        focus = sidebar.SidebarFocus()
        focus.update(rows(), "%second", "%sidebar", "@primary")
        self.assertEqual(focus.update(rows(), "%shell", "%sidebar", "@primary"), (-1, "active"))
        self.assertIsNone(focus.last_content_pane_id)
        self.assertEqual(focus.update(rows(), None, "%sidebar", "@primary"), (-1, "active"))

    def test_missing_cursor_after_agent_exit_falls_back_inside_own_window(self):
        focus = sidebar.SidebarFocus()
        focus.update(rows(), "%sidebar", "%sidebar", "@primary", "%second")
        remaining = [r for r in rows() if r["pane_id"] != "%second"]
        self.assertEqual(focus.update(remaining, "%sidebar", "%sidebar", "@primary"), (0, "nav"))
        self.assertEqual(focus.cursor_pane_id, "%first")

    def test_sidebar_identity_uses_actual_terminal_instead_of_stale_environment(self):
        snapshot = sidebar.SEPARATOR.join(("%actual", "101", "/dev/sidebar"))
        with patch.dict(os.environ, {"TMUX_PANE": "%wrong"}), \
                patch.object(sidebar, "tmux", return_value=snapshot), \
                patch.object(sidebar.os, "ttyname", return_value="/dev/sidebar"):
            self.assertEqual(sidebar.get_my_pane(), "%actual")

    def test_identity_falls_back_to_process_ancestry_not_current_client(self):
        snapshot = "\n".join((sidebar.SEPARATOR.join(("%actual", "101", "/dev/sidebar")),
                               sidebar.SEPARATOR.join(("%wrong", "202", "/dev/other"))))
        with patch.dict(os.environ, {"TMUX_PANE": "%wrong"}), \
                patch.object(sidebar, "tmux", return_value=snapshot), \
                patch.object(sidebar.os, "ttyname", side_effect=OSError), \
                patch.object(sidebar.os, "getpid", return_value=102), \
                patch.object(sidebar, "process_snapshot", return_value={102: {"ppid": 101}, 101: {"ppid": 1}}):
            self.assertEqual(sidebar.get_my_pane(), "%actual")

    def test_window_visible_in_another_linked_session_keeps_fast_polling(self):
        snapshot = "\n".join((
            snapshot_row("%sidebar", "@primary", sidebar_pane=True),
            snapshot_row("%second", "@primary", active=True),
            snapshot_row("%sidebar", "@primary", sidebar_pane=True, session="linked", window_active=True),
        ))
        with patch.object(sidebar, "tmux", return_value=snapshot):
            _, active, _, _, visible = sidebar.collect_sidebar_snapshot("work", "%sidebar")
        self.assertEqual(active, "%second")
        self.assertTrue(visible)

    def test_hidden_polling_wakes_for_first_key_and_switches_to_visible_cadence(self):
        class Screen:
            def __init__(self):
                self.timeouts = []
                self.keys = iter((ord("j"), ord("q")))

            def keypad(self, enabled):
                pass

            def timeout(self, value):
                self.timeouts.append(value)

            def getch(self):
                return next(self.keys)

        screen = Screen()
        hidden = (rows(), "%first", False, "@primary", False)
        focused = (rows(), "%sidebar", True, "@primary", True)
        with patch.object(sidebar.curses, "curs_set"), \
                patch.object(sidebar.curses, "mousemask"), \
                patch.object(sidebar.curses, "mouseinterval"), \
                patch.object(sidebar, "_init_colors"), \
                patch.object(sidebar, "_load_options"), \
                patch.object(sidebar, "get_my_pane", return_value="%sidebar"), \
                patch.object(sidebar, "get_my_session", return_value="work"), \
                patch.object(sidebar, "get_previous_pane", return_value="%first"), \
                patch.object(sidebar, "collect_sidebar_snapshot", side_effect=(hidden, focused, focused, focused)), \
                patch.object(sidebar, "_render") as render:
            sidebar.main(screen)
        self.assertEqual(screen.timeouts[-2:], [sidebar.HIDDEN_POLL_MS, sidebar.VISIBLE_POLL_MS])
        # The first j after the hidden wait advances from the correct previous
        # pane and survives the next redraw, rather than resetting on focus entry.
        self.assertEqual(render.call_args_list[1].args[2:], (1, "nav"))

    def test_ascii_mode_uses_ascii_decorations(self):
        with patch.object(sidebar, "ASCII", True):
            self.assertEqual(sidebar._trunc("a long name", 5), "a lo~")
            self.assertEqual(sidebar.pane_detail({"provider": "codex", "dirname": "repo"}, 20), "codex | repo")
            self.assertTrue(all(icon.isascii() for icon in sidebar.ASCII_ICONS.values()))

    def test_missing_state_is_not_displayed_as_idle(self):
        snapshot = snapshot_row("%1", "@1", active=True).split(sidebar.SEPARATOR)
        snapshot[-2] = ""
        with patch.object(sidebar, "tmux", return_value=sidebar.SEPARATOR.join(snapshot)):
            collected, _, _ = sidebar.collect_tree_and_active("work")
        self.assertFalse(collected)

    def test_viewport_keeps_selected_title_and_detail_visible(self):
        tree = []
        for index in range(30):
            tree.extend([{"kind": "window", "label": str(index)},
                         {"kind": "pane", "pane_id": f"%{index}", "window_id": f"@{index}"},
                         {"kind": "separator"}])
        for index, row in enumerate(tree):
            if row["kind"] != "pane":
                continue
            with self.subTest(pane=row["pane_id"]):
                start = sidebar._viewport_start(tree, index, 15)
                self.assertLessEqual(start, index)
                self.assertLessEqual(sum(sidebar._row_height(r) for r in tree[start:index + 1]), 15)
        self.assertEqual(sidebar._viewport_start(tree, 1, 15), 0)
        self.assertGreater(sidebar._viewport_start(tree, 88, 15), 0)
        self.assertEqual(sidebar._viewport_start(tree, -1, 15), 0)

    def test_scrolled_mouse_coordinates_resolve_to_displayed_pane(self):
        tree = [{"kind": "pane", "pane_id": f"%{index}"} for index in range(20)]
        start = sidebar._viewport_start(tree, 18, 6)
        visible = tree[start:]
        clicked = sidebar._y_to_row_idx(4, visible)
        self.assertEqual(visible[clicked]["pane_id"], "%18")

    def test_tiny_viewport_still_renders_selected_title(self):
        tree = [{"kind": "pane", "pane_id": f"%{index}"} for index in range(20)]
        self.assertEqual(sidebar._viewport_start(tree, 18, 1), 18)

    def test_render_draws_offscreen_selected_agent_after_scrolling(self):
        class Screen:
            def __init__(self):
                self.text = []

            def erase(self):
                pass

            def getmaxyx(self):
                return 6, 60

            def addstr(self, y, x, text, attr=0):
                self.text.append((y, text))

            def refresh(self):
                pass

        tree = [{"kind": "pane", "pane_id": f"%{index}", "state": "working", "provider": "codex",
                 "title": f"task {index}", "dirname": f"repo{index}"} for index in range(20)]
        screen = Screen()
        with patch.object(sidebar, "COLOR_SUPPORT", False), patch.object(sidebar, "HIGH_COLOR", False):
            start = sidebar._render(screen, tree, 18, "nav")
        self.assertGreater(start, 0)
        self.assertIn((4, "task 18"), screen.text)
        self.assertTrue(any(y == 5 and "repo18" in text for y, text in screen.text))
        self.assertTrue(all(0 <= y < 6 for y, _ in screen.text))


if __name__ == "__main__":
    unittest.main()
