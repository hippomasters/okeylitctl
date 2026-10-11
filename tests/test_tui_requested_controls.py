"""Regression tests for the hardware-tested TUI control contract."""

import curses
import unittest
from unittest import mock

from okeylitctl.tui import CursesTui
from okeylitctl.effects import EffectKind
from okeylitctl.tui_compositor import (
    compose_editor, compose_exact_hex_modal, compose_profiles,
    compose_responsive_profiles,
)
from test_tui import FakeBackend


class Screen:
    def __init__(self, keys, *, height=30, width=110):
        self.keys = iter(keys)
        self.height = height
        self.width = width
        self.lines = []
        self.frames = []

    def keypad(self, _value):
        pass

    def timeout(self, _value):
        pass

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.lines = []

    def addnstr(self, _y, _x, text, _limit, _attr):
        self.lines.append(text)

    def insstr(self, _y, _x, text, _attr):
        self.lines.append(text)

    def refresh(self):
        self.frames.append("".join(self.lines))

    def getch(self):
        return next(self.keys)


class RequestedControlsTests(unittest.TestCase):
 def test_tab_activates_selected_effect_and_stop_restores_once(self):
    backend = FakeBackend()
    app = CursesTui(backend)
    screen = Screen([9, ord("s"), ord("q")])
    original_getch = screen.getch
    running_at_stop = []

    def observe_stop():
        key = original_getch()
        if key == ord("s"):
            running_at_stop.append(app.state.effect_running)
        return key

    screen.getch = observe_stop
    with mock.patch("okeylitctl.tui.initialize_colors", return_value=False):
        app._main(screen)
    assert app.state.effect_kind is EffectKind.BLINK
    assert running_at_stop == [True]
    assert app.effect_runtime is None


 def test_o_restores_without_consuming_confirmation_key(self):
    backend = FakeBackend()
    app = CursesTui(backend)
    app.state.set_selected_color("ABCDEF")
    screen = Screen([ord("o"), ord("q")])
    with mock.patch("okeylitctl.tui.initialize_colors", return_value=False):
        app._main(screen)
    assert backend.restores == 1
    assert not app.state.dirty


 def test_undersized_resize_warning_appears_on_first_shrink(self):
    backend = FakeBackend()
    app = CursesTui(backend)
    screen = Screen([curses.KEY_RESIZE, ord("a"), ord("q")], height=30, width=110)
    original_getch = screen.getch

    def shrink_on_resize():
        key = original_getch()
        if key == curses.KEY_RESIZE:
            screen.width = 70
        return key

    screen.getch = shrink_on_resize
    with mock.patch("okeylitctl.tui.initialize_colors", return_value=False):
        app._main(screen)
    assert any("needs at least" in frame for frame in screen.frames[1:])
    assert backend.writes == []

 def test_resize_event_forces_physical_repaint_before_next_input(self):
    app = CursesTui(FakeBackend())
    screen = Screen([curses.KEY_RESIZE, ord("q")])
    repaints = []
    screen.clearok = repaints.append
    with mock.patch("okeylitctl.tui.initialize_colors", return_value=False):
        app._main(screen)
    assert repaints == [True]

 def test_tab_cannot_activate_effect_while_device_is_off(self):
    backend = FakeBackend()
    backend.status_value["state"] = "off"
    app = CursesTui(backend)
    screen = Screen([9, ord("q")])
    original_getch = screen.getch
    messages = []

    def observe_next_key():
        key = original_getch()
        if key == ord("q"):
            messages.append(app.state.message)
        return key

    screen.getch = observe_next_key
    with mock.patch("okeylitctl.tui.initialize_colors", return_value=False):
        app._main(screen)
    assert app.effect_runtime is None
    assert backend.writes == []
    assert "power off" in messages[0].lower()

 def test_tab_does_not_start_new_effect_after_ownership_was_lost(self):
    backend = FakeBackend()
    app = CursesTui(backend)
    app.state.effect_ownership_lost = True
    app.state.live_last_observed = True
    screen = Screen([9, ord("q")])
    with mock.patch("okeylitctl.tui.initialize_colors", return_value=False):
        app._main(screen)
    assert app.effect_runtime is None
    assert backend.writes == []

 def test_tab_returning_to_static_applies_dirty_draft_once(self):
    backend = FakeBackend()
    app = CursesTui(backend)
    app.state.effect_index = len(EffectKind) - 1
    app.state.set_selected_color("ABCDEF")
    screen = Screen([9, ord("q")])
    with mock.patch("okeylitctl.tui.initialize_colors", return_value=False):
        app._main(screen)
    assert app.state.effect_kind is EffectKind.STATIC
    assert backend.writes == [app.state.draft.to_wire()]
    assert not app.state.dirty


 def test_help_and_exact_modal_fill_never_reverse_terminal_default_colors(self):
    app = CursesTui(FakeBackend())
    for role in ("modal_fill", "modal_muted", "dimmed_text"):
        assert not app._reference_role_attr(role) & curses.A_REVERSE
    assert app._reference_role_attr("profile_selected") & curses.A_REVERSE
    assert not app._reference_role_attr("profile_delete_prompt") & curses.A_REVERSE

 def test_help_and_hex_modal_final_fill_cells_have_no_reverse_attribute(self):
    app = CursesTui(FakeBackend())
    screen = Screen([], height=55, width=160)
    cells = {}

    def addnstr(y, x, text, limit, attr):
        for offset, glyph in enumerate(text[:limit]):
            cells[y, x + offset] = glyph, attr

    screen.addnstr = addnstr
    screen.insstr = lambda y, x, text, attr: addnstr(y, x, text, 1, attr)
    app.state.help_visible = True
    for composed in (
        compose_editor(app.state, power_state="on"),
        compose_exact_hex_modal(app.state, power_state="on", input_text="AB", error=""),
    ):
        cells.clear()
        assert app._draw_composed_screen(screen, composed, 55, 160)
        fill = [(y, x) for y, row in enumerate(composed.roles)
                for x, role in enumerate(row) if role == "modal_fill"]
        assert fill
        assert all(cells[y, x][0] == " " and
                   not cells[y, x][1] & curses.A_REVERSE for y, x in fill)

 def test_delete_prompt_is_not_styled_as_error_at_reference_or_responsive_size(self):
    app = CursesTui(FakeBackend())
    for composed in (
        compose_profiles(app.state, names=("Saved",), selected=0,
                         preview_layout=None, pending_delete="Saved", power_state="on"),
        compose_responsive_profiles(app.state, width=110, height=30,
                                    names=("Saved",), selected=0,
                                    preview_layout=None, pending_delete="Saved",
                                    power_state="on"),
    ):
        for y, row in enumerate(composed.lines):
            if "Delete profile?" in row:
                x = row.index("Delete profile?")
                assert composed.roles[y][x] == "profile_delete_prompt"
