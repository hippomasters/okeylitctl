"""Device-free checks for provisional, explicit rate validation."""

import unittest
from unittest import mock

from okeylitctl.effects import EffectKind
from okeylitctl.tui import CursesTui
from okeylitctl.tui_compositor import compose_narrow_editor
from okeylitctl.sysfs import BackendIOError
from test_tui import CasEffectBackend, FakeProfileStore


class RateDiagnosticTests(unittest.TestCase):
    class BurstBackend(CasEffectBackend):
        started_rate = None

        def start_rate_experiment(self, rate):
            self.started_rate = rate

        def start_long_rate_experiment(self, rate):
            self.long_started_rate = rate

    def long_app(self, backend=None):
        backend = backend or self.BurstBackend()
        return CursesTui(backend, profile_store=FakeProfileStore(),
                         test_rate_hz=4, rate_experiment=True,
                         test_effect=EffectKind.WAVE, test_duration_seconds=12)

    def test_long_session_rejects_wrong_opt_in_before_status(self):
        for options in ({"test_rate_hz": 4, "rate_experiment": True},
                        {"test_rate_hz": 5, "rate_experiment": True,
                         "test_effect": EffectKind.WAVE},
                        {"test_rate_hz": 4, "rate_experiment": True,
                         "test_effect": EffectKind.STATIC},
                        {"test_rate_hz": 4, "rate_experiment": True,
                         "test_effect": EffectKind.WAVE, "test_duration_seconds": 11}):
            backend = self.BurstBackend()
            backend.status = lambda: self.fail("invalid long session read status")
            duration = options.get("test_duration_seconds", 12)
            with self.subTest(options=options), self.assertRaises(ValueError):
                CursesTui(backend, profile_store=FakeProfileStore(),
                          test_duration_seconds=duration,
                          **{key: value for key, value in options.items()
                             if key != "test_duration_seconds"})

    def test_direct_runner_rejects_invalid_duration_before_backend_construction(self):
        from okeylitctl.tui import run_tui
        with mock.patch("okeylitctl.tui.IPCBackend") as backend:
            with self.assertRaises(ValueError):
                run_tui(test_duration_seconds=12)
            backend.assert_not_called()

    def test_long_session_start_needs_separate_broker_ack(self):
        backend = self.BurstBackend()
        backend.start_long_rate_experiment = None
        app = self.long_app(backend)
        self.assertFalse(app._start_effect(now=0.0))
        self.assertIsNone(app.effect_runtime)
        self.assertEqual(backend.cas_writes, [])
        self.assertIsNone(backend.started_rate)

    def test_long_session_displays_manual_controls_and_bounds(self):
        app = self.long_app()
        self.assertIn("A Start", app.state.message)
        self.assertIn("S Stop", app.state.message)
        self.assertIn("12s/40 frames", app.state.message)
        self.assertTrue(app._start_effect(now=0.0))
        self.assertIn("S Stop", app.state.message)
        self.assertIn("12s/40 frames", app.state.message)

    def test_long_session_uses_separate_lease_and_stops_at_40_frames(self):
        backend = self.BurstBackend()
        app = self.long_app(backend)
        self.assertTrue(app._start_effect(now=0.0))
        self.assertEqual(backend.long_started_rate, 4)
        self.assertIsNone(backend.started_rate)
        for frame in range(40):
            with mock.patch("okeylitctl.effect_runtime.monotonic",
                            side_effect=(100 + frame, 100.005 + frame,
                                         100.007 + frame, 100.01 + frame)), \
                 mock.patch("okeylitctl.tui.monotonic", return_value=frame * 0.26 + 0.01):
                app._tick_effect(now=frame * 0.26, authorized=True)
        self.assertIsNone(app.effect_runtime)
        self.assertEqual(len(backend.cas_writes), 41)
        self.assertEqual(backend.cas_writes[-1], app.state.current.to_wire())

    def test_long_session_stops_at_12_seconds_without_late_frame(self):
        backend = self.BurstBackend()
        app = self.long_app(backend)
        self.assertTrue(app._start_effect(now=0.0))
        self.assertFalse(app._tick_effect(now=12.0, authorized=True))
        self.assertIsNone(app.effect_runtime)
        self.assertEqual(backend.cas_writes, [])

    def test_long_session_stop_failure_retains_lock_and_prevents_later_frames(self):
        class DenyStop(self.BurstBackend):
            def stop_snapshot(self):
                raise BackendIOError("stop unavailable")
        backend = DenyStop()
        app = self.long_app(backend)
        self.assertTrue(app._start_effect(now=0.0))
        self.assertTrue(app._tick_effect(now=0.0, authorized=True))
        self.assertFalse(app._tick_effect(now=12.0, authorized=True))
        self.assertTrue(app.state.effect_restore_pending)
        self.assertIsNotNone(app.effect_runtime)
        self.assertFalse(app._tick_effect(now=12.5, authorized=True))
        self.assertEqual(len(backend.cas_writes), 1)

    def test_long_session_tab_cannot_start_or_stop_named_effect(self):
        backend = self.BurstBackend()
        app = self.long_app(backend)

        class Screen:
            def __init__(self): self.keys = iter((9, ord("a"), 9, ord("s")))
            def keypad(self, _enabled): pass
            def timeout(self, _value): pass
            def getmaxyx(self): return (30, 110)
            def erase(self): pass
            def addnstr(self, *_args): pass
            def refresh(self): pass
            def getch(self):
                key = next(self.keys)
                if key == ord("a"):
                    self_outer.assertIsNone(backend.__dict__.get("long_started_rate"))
                if key == ord("s"):
                    self_outer.assertIsNotNone(app.effect_runtime)
                return key

        self_outer = self
        with mock.patch("okeylitctl.tui.initialize_colors", return_value=False):
            app._main(Screen())
        self.assertEqual(backend.long_started_rate, 4)
        self.assertIsNone(app.effect_runtime)
        self.assertEqual(app.state.effect_kind, EffectKind.WAVE)
        if backend.cas_writes:
            self.assertEqual(backend.cas_writes[-1], app.state.current.to_wire())

    def test_long_session_cannot_rearm_after_successful_stop(self):
        backend = self.BurstBackend()
        app = self.long_app(backend)
        self.assertTrue(app._start_effect(now=0.0))
        self.assertTrue(app._stop_effect())
        self.assertFalse(app._start_effect(now=1.0))
        self.assertIsNone(app.effect_runtime)

    def test_long_session_slow_readback_fails_closed_with_base_ownership_retained(self):
        class SlowRead(self.BurstBackend):
            fail_reads = False
            def snapshot(self):
                if self.fail_reads:
                    raise BackendIOError("slow readback timeout")
                return super().snapshot()
            def stop_snapshot(self):
                return self.snapshot()

        backend = SlowRead()
        app = self.long_app(backend)
        self.assertTrue(app._start_effect(now=0.0))
        with mock.patch("okeylitctl.tui.monotonic", return_value=0.01):
            self.assertTrue(app._tick_effect(now=0.0, authorized=True))
        backend.fail_reads = True
        self.assertFalse(app._tick_effect(now=0.3, authorized=True))
        self.assertTrue(app.state.effect_restore_pending)
        self.assertIsNotNone(app.effect_runtime)
        self.assertFalse(app._tick_effect(now=12.0, authorized=True))
        self.assertEqual(len(backend.cas_writes), 1)

    def test_long_session_power_off_does_not_write_base_blindly(self):
        backend = self.BurstBackend()
        app = self.long_app(backend)
        self.assertTrue(app._start_effect(now=0.0))
        with mock.patch("okeylitctl.tui.monotonic", return_value=0.01):
            self.assertTrue(app._tick_effect(now=0.0, authorized=True))
        backend.status_value["state"] = "off"
        self.assertFalse(app._tick_effect(now=0.3, authorized=True))
        self.assertEqual(app.power_state, "off")
        self.assertEqual(len(backend.cas_writes), 1)
        self.assertIsNotNone(app.effect_runtime)

    def test_long_session_external_writer_preserves_baseline_ownership(self):
        backend = self.BurstBackend()
        app = self.long_app(backend)
        self.assertTrue(app._start_effect(now=0.0))
        with mock.patch("okeylitctl.tui.monotonic", return_value=0.01):
            self.assertTrue(app._tick_effect(now=0.0, authorized=True))
        backend.revision += 1
        backend.status_value["colors"] = ["ABCDEF"] * 4
        self.assertFalse(app._tick_effect(now=0.3, authorized=True))
        self.assertTrue(app.state.live_last_observed)
        self.assertEqual(len(backend.cas_writes), 1)
        self.assertEqual(backend.status_value["colors"], ["ABCDEF"] * 4)
        self.assertIsNone(app.effect_runtime)

    def test_long_session_crossed_deadline_after_readback_autostops(self):
        backend = self.BurstBackend()
        app = self.long_app(backend)
        self.assertTrue(app._start_effect(now=0.0))
        with mock.patch("okeylitctl.tui.monotonic", return_value=12.1):
            self.assertTrue(app._tick_effect(now=0.0, authorized=True))
        self.assertIsNone(app.effect_runtime)
        self.assertEqual(len(backend.cas_writes), 2)

    def test_unbudgeted_diagnostics_are_disabled_before_backend_access(self):
        for rate in (4, 5):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                CursesTui(CasEffectBackend(), profile_store=FakeProfileStore(),
                          test_rate_hz=rate)

    def test_named_effect_requires_experiment_before_status_read(self):
        for effect in (EffectKind.STATIC, "Wave", EffectKind.WAVE):
            with self.subTest(effect=effect):
                backend = self.BurstBackend()
                backend.status = lambda: self.fail("invalid effect accessed device")
                with self.assertRaises(ValueError):
                    CursesTui(backend, profile_store=FakeProfileStore(),
                              test_rate_hz=5, rate_experiment=effect is not EffectKind.WAVE,
                              test_effect=effect)

    def test_named_effect_is_local_until_deliberate_start(self):
        backend = self.BurstBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore(),
                        test_rate_hz=4, rate_experiment=True,
                        test_effect=EffectKind.WAVE)
        self.assertEqual(app.state.effect_kind, EffectKind.WAVE)
        self.assertIsNone(backend.started_rate)
        self.assertEqual(backend.cas_writes, [])
        self.assertTrue(app._start_effect(now=0.0))
        self.assertEqual(backend.started_rate, 4)
        self.assertTrue(app._stop_effect())
        self.assertIsNone(app.effect_runtime)

    def test_normal_tui_selection_remains_static(self):
        app = CursesTui(self.BurstBackend(), profile_store=FakeProfileStore())
        self.assertEqual(app.state.effect_kind, EffectKind.STATIC)

    def test_explicit_burst_ends_after_bounded_frames_and_restores_base(self):
        for rate in (4, 5):
            with self.subTest(rate=rate):
                backend = self.BurstBackend()
                app = CursesTui(backend, profile_store=FakeProfileStore(),
                                test_rate_hz=rate, rate_experiment=True)
                app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
                self.assertTrue(app._start_effect(now=0.0))
                self.assertEqual(backend.started_rate, rate)
                frames = rate * 2
                for frame in range(frames):
                    with mock.patch("okeylitctl.effect_runtime.monotonic",
                                    side_effect=(10.0 + frame, 10.005 + frame, 10.007 + frame, 10.01 + frame)):
                        self.assertTrue(app._tick_effect(now=frame * (1 / rate + 0.01),
                                                         authorized=True))
                if rate == 4:
                    self.assertFalse(app._tick_effect(now=2.0, authorized=True))
                self.assertFalse(app.state.effect_running)
                self.assertIsNone(app.effect_runtime)
                self.assertEqual(len(backend.cas_writes), frames + 1)
                self.assertEqual(backend.cas_writes[-1], app.state.current.to_wire())

    def test_burst_refuses_unsafe_backend_before_any_write(self):
        backend = CasEffectBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore(),
                        test_rate_hz=5, rate_experiment=True)
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        self.assertFalse(app._start_effect(now=0.0))
        self.assertEqual(backend.cas_writes, [])

    def test_burst_power_off_never_consumes_lease_or_writes(self):
        backend = self.BurstBackend()
        backend.status_value["state"] = "off"
        app = CursesTui(backend, profile_store=FakeProfileStore(),
                        test_rate_hz=5, rate_experiment=True)
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        self.assertFalse(app._start_effect(now=0.0))
        self.assertIsNone(backend.started_rate)
        self.assertEqual(backend.cas_writes, [])

    def test_burst_deadline_stops_before_broker_lease_expires_without_late_frame(self):
        backend = self.BurstBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore(),
                        test_rate_hz=5, rate_experiment=True)
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        self.assertTrue(app._start_effect(now=0.0))
        self.assertFalse(app._tick_effect(now=2.0, authorized=True))
        self.assertEqual(backend.cas_writes, [])
        self.assertIsNone(app.effect_runtime)

    def test_auto_stop_retries_one_definite_read_quota_failure_only(self):
        class BusyStopRead(self.BurstBackend):
            stops = 0

            def stop_snapshot(self):
                self.stops += 1
                if self.stops == 1:
                    raise BackendIOError("read quota busy")
                return self.snapshot()

        backend = BusyStopRead()
        app = CursesTui(backend, profile_store=FakeProfileStore(),
                        test_rate_hz=5, rate_experiment=True)
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        self.assertTrue(app._start_effect(now=0.0))
        self.assertTrue(app._tick_effect(now=0.0, authorized=True))
        self.assertFalse(app._tick_effect(now=3.0, authorized=True))
        self.assertEqual(backend.stops, 2)
        self.assertIsNone(app.effect_runtime)
        self.assertEqual(len(backend.cas_writes), 2)

    def test_failed_auto_stop_never_writes_another_frame_past_deadline(self):
        class BusyStopRead(self.BurstBackend):
            def stop_snapshot(self):
                raise BackendIOError("broker read quota busy")

        backend = BusyStopRead()
        app = CursesTui(backend, profile_store=FakeProfileStore(),
                        test_rate_hz=5, rate_experiment=True)
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        self.assertTrue(app._start_effect(now=0.0))
        self.assertTrue(app._tick_effect(now=0.0, authorized=True))
        self.assertFalse(app._tick_effect(now=2.0, authorized=True))
        self.assertTrue(app.state.effect_restore_pending)
        self.assertFalse(app._tick_effect(now=2.3, authorized=True))
        self.assertEqual(len(backend.cas_writes), 1)
        self.assertIsNotNone(app.effect_runtime)

    def test_burst_write_failure_keeps_unknown_locked(self):
        class UncertainBurst(self.BurstBackend):
            def compare_and_write(self, *args):
                raise BackendIOError("ACK lost")

        backend = UncertainBurst()
        app = CursesTui(backend, profile_store=FakeProfileStore(),
                        test_rate_hz=5, rate_experiment=True)
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        self.assertTrue(app._start_effect(now=0.0))
        self.assertFalse(app._tick_effect(now=0.0, authorized=True))
        self.assertTrue(app.state.effect_frame_uncertain)
        self.assertIsNotNone(app.effect_runtime)

    def test_auto_stop_does_not_resurrect_reconciled_ownership_loss(self):
        class ExternalWriteOnFinalFrame(self.BurstBackend):
            def compare_and_write(self, *args):
                token = super().compare_and_write(*args)
                if len(self.cas_writes) == 10:
                    self.revision += 1
                    self.status_value["colors"] = self.status_value["original"][:]
                return token

        backend = ExternalWriteOnFinalFrame()
        app = CursesTui(backend, profile_store=FakeProfileStore(),
                        test_rate_hz=5, rate_experiment=True)
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        self.assertTrue(app._start_effect(now=0.0))
        for frame in range(10):
            with mock.patch("okeylitctl.effect_runtime.monotonic",
                            side_effect=(10.0 + frame, 10.005 + frame, 10.007 + frame, 10.01 + frame)):
                app._tick_effect(now=frame * 0.21, authorized=True)
        self.assertIsNone(app.effect_runtime)
        self.assertFalse(app.state.effect_running)
        self.assertTrue(app.state.live_last_observed)
        self.assertEqual(len(backend.cas_writes), 10)

    def test_quit_aborts_burst_before_waiting_for_dirty_confirmation(self):
        backend = self.BurstBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore(),
                        test_rate_hz=5, rate_experiment=True)
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        app.state.set_selected_color("A1B2C3")
        self.assertTrue(app._start_effect())

        class Screen:
            keys = iter((ord("q"),))

            def keypad(self, _enabled): pass
            def timeout(self, _value): pass
            def getmaxyx(self): return (30, 110)
            def erase(self): pass
            def addnstr(self, *_args): pass
            def refresh(self): pass
            def getch(self): return next(self.keys)

        def confirm_after_stop(_screen, _prompt):
            self.assertIsNone(app.effect_runtime)
            self.assertFalse(app.state.effect_running)
            return False

        with mock.patch("okeylitctl.tui.initialize_colors", return_value=False), \
             mock.patch.object(app, "_confirm_action", side_effect=confirm_after_stop):
            app._main(Screen())
        self.assertIsNone(app.effect_runtime)
        self.assertFalse(app.state.effect_running)
        self.assertEqual(backend.cas_writes[-1], app.state.current.to_wire())

    def test_running_effect_polls_to_next_due_instead_of_fixed_50ms(self):
        backend = self.BurstBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore(),
                        test_rate_hz=5, rate_experiment=True)
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        self.assertTrue(app._start_effect(now=0.0))
        app.effect_runtime.next_due = 0.24

        class Screen:
            keys = iter((-1,))

            def __init__(self):
                self.timeouts = []

            def keypad(self, _enabled): pass
            def timeout(self, value): self.timeouts.append(value)
            def getmaxyx(self): return (30, 110)
            def erase(self): pass
            def addnstr(self, *_args): pass
            def refresh(self): pass
            def getch(self): return next(self.keys)

        screen = Screen()
        with mock.patch("okeylitctl.tui.monotonic", return_value=0.20), \
             mock.patch("okeylitctl.tui.initialize_colors", return_value=False):
            app._main(screen)
        self.assertTrue(any(0 < timeout < 50 for timeout in screen.timeouts),
                        screen.timeouts)
        self.assertEqual(backend.cas_writes, [])

    def test_poll_accounts_for_next_redraw_without_shortening_io_spacing(self):
        backend = self.BurstBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore(),
                        test_rate_hz=5, rate_experiment=True)
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        self.assertTrue(app._start_effect(now=0.0))
        app.effect_runtime.next_due = 0.26

        class Screen:
            def __init__(self): self.timeouts = []
            def keypad(self, _enabled): pass
            def timeout(self, value): self.timeouts.append(value)
            def getmaxyx(self): return (30, 110)
            def erase(self): pass
            def addnstr(self, *_args): pass
            def refresh(self): pass
            def getch(self): raise StopIteration

        screen = Screen()
        # Drawing consumed 20ms; wake early enough for the next redraw.
        runtime = app.effect_runtime
        with mock.patch("okeylitctl.tui.monotonic",
                        side_effect=(0.20, 0.20, 0.22, 0.22, 0.22, 0.22, 0.22)), \
             mock.patch("okeylitctl.tui.initialize_colors", return_value=False):
            app._main(screen)
        self.assertTrue(any(19 <= timeout <= 21 for timeout in screen.timeouts),
                        screen.timeouts)
        self.assertEqual(runtime.next_due, 0.26)
        self.assertEqual(backend.cas_writes, [])

    def test_poll_timeout_returns_to_50ms_after_stop(self):
        backend = self.BurstBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore(),
                        test_rate_hz=5, rate_experiment=True)
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        self.assertTrue(app._start_effect(now=0.0))
        app.effect_runtime.next_due = 0.24

        class Screen:
            keys = iter((ord("s"), -1))

            def __init__(self):
                self.timeouts = []

            def keypad(self, _enabled): pass
            def timeout(self, value): self.timeouts.append(value)
            def getmaxyx(self): return (30, 110)
            def erase(self): pass
            def addnstr(self, *_args): pass
            def refresh(self): pass
            def getch(self): return next(self.keys)

        screen = Screen()
        with mock.patch("okeylitctl.tui.monotonic", return_value=0.20), \
             mock.patch("okeylitctl.tui.initialize_colors", return_value=False):
            app._main(screen)
        self.assertTrue(any(0 < value < 50 for value in screen.timeouts))
        self.assertEqual(screen.timeouts[-1], 50)

    def test_opt_in_rate_reports_measured_io_and_spacing_without_advancing_deadline(self):
        for rate in (2,):
            with self.subTest(rate=rate):
                backend = CasEffectBackend()
                app = CursesTui(backend, profile_store=FakeProfileStore(), test_rate_hz=rate)
                app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
                self.assertTrue(app._start_effect(now=0.0))
                runtime = app.effect_runtime
                self.assertEqual(runtime.rate_hz, rate)
                with mock.patch("okeylitctl.effect_runtime.monotonic",
                                side_effect=(10.0, 10.04, 10.045, 10.1, 10.4, 10.44, 10.445, 10.5)):
                    self.assertTrue(app._tick_effect(now=0.0, authorized=True))
                    self.assertIn("I/O 100ms", app.state.message)
                    self.assertIn("spacing pending", app.state.message)
                    self.assertAlmostEqual(runtime.next_due, 0.1 + 1 / rate)
                    self.assertFalse(app._tick_effect(now=runtime.next_due - 0.01,
                                                      authorized=True))
                    self.assertTrue(app._tick_effect(now=runtime.next_due,
                                                     authorized=True))
                self.assertIn("spacing 400ms", app.state.message)
                self.assertIn("start 400ms", app.state.message)
                self.assertIn("snap 40ms", app.state.message)
                self.assertIn("CAS 55ms", app.state.message)
                self.assertAlmostEqual(runtime.next_due, 0.2 + 2 / rate)
                self.assertEqual(len(backend.cas_writes), 2)
                self.assertTrue(app._stop_effect())
                self.assertIn("2 verified frames", app.state.message)
                self.assertIn("2.5 verified/s", app.state.message)
                screen = compose_narrow_editor(app.state, width=78, height=24,
                                               power_state=app.power_state)
                self.assertIn("2.5 verified/s", screen.lines[2])
                self.assertEqual(len(backend.cas_writes), 3)
                self.assertEqual(backend.cas_writes[-1], runtime.spec.base.to_wire())

    def test_long_session_stop_summary_fits_78_columns(self):
        app = CursesTui(CasEffectBackend(), profile_store=FakeProfileStore(),
                        test_rate_hz=2)
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        self.assertTrue(app._start_effect(now=0.0))
        self.assertTrue(app._tick_effect(now=0.0, authorized=True))
        runtime = app.effect_runtime
        # Model a long verified session without issuing 999 extra fake writes.
        runtime.verified_frames = 1000
        runtime._first_write_completed_at = 0.0
        runtime._last_write_completed_at = 499.5
        with mock.patch("okeylitctl.tui.monotonic", side_effect=(10.0, 11.0)):
            self.assertTrue(app._stop_effect())
        screen = compose_narrow_editor(app.state, width=78, height=24,
                                       power_state=app.power_state)
        self.assertIn("base restored", screen.lines[2].lower())
        self.assertIn("Stop 1000ms", screen.lines[2])
        self.assertIn("1000 verified frames", screen.lines[2])
        self.assertIn("2.0 verified/s", screen.lines[2])

    def test_opt_in_tick_shows_complete_stop_guidance_at_78_columns(self):
        app = CursesTui(CasEffectBackend(), profile_store=FakeProfileStore(),
                        test_rate_hz=2)
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        self.assertTrue(app._start_effect(now=0.0))
        with mock.patch("okeylitctl.effect_runtime.monotonic",
                        side_effect=(10.0, 10.04, 10.045, 10.1,
                                     10.4, 10.44, 10.445, 10.5)):
            self.assertTrue(app._tick_effect(now=0.0, authorized=True))
            self.assertTrue(app._tick_effect(now=0.6, authorized=True))
        screen = compose_narrow_editor(app.state, width=78, height=24,
                                       power_state=app.power_state)
        self.assertIn("S Stop", screen.lines[2])
        self.assertIn("CAS 55ms", screen.lines[2])

    def test_opt_in_slow_tick_keeps_stop_visible_at_78_columns(self):
        app = CursesTui(CasEffectBackend(), profile_store=FakeProfileStore(),
                        test_rate_hz=2)
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        self.assertTrue(app._start_effect(now=0.0))
        with mock.patch("okeylitctl.effect_runtime.monotonic",
                        side_effect=(10.0, 10.04, 10.045, 11.1,
                                     12.4, 12.44, 12.445, 13.5)):
            self.assertTrue(app._tick_effect(now=0.0, authorized=True))
            self.assertTrue(app._tick_effect(now=2.0, authorized=True))
        screen = compose_narrow_editor(app.state, width=78, height=24,
                                       power_state=app.power_state)
        self.assertIn("CAS 1055ms", screen.lines[2])
        self.assertIn("S Stop", screen.lines[2])

    def test_opt_in_stop_reports_elapsed_restoration_time(self):
        backend = CasEffectBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore(), test_rate_hz=2)
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        app._start_effect(now=0.0)
        app._tick_effect(now=0.0, authorized=True)
        with mock.patch("okeylitctl.tui.monotonic", side_effect=(10.0, 10.24)):
            self.assertTrue(app._stop_effect())
        self.assertIn("Stop 240ms", app.state.message)

    def test_default_rate_remains_provisional_two_hz(self):
        app = CursesTui(CasEffectBackend(), profile_store=FakeProfileStore())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        self.assertTrue(app._start_effect(now=0.0))
        self.assertEqual(app.effect_runtime.rate_hz, 2)

    def test_quota_denied_stop_never_retries_mutation_until_explicit_stop(self):
        class BusyOnce(CasEffectBackend):
            deny_snapshot = False

            def snapshot(self):
                if self.deny_snapshot:
                    self.deny_snapshot = False
                    raise BackendIOError("broker read quota busy")
                return super().snapshot()

        backend = BusyOnce()
        app = CursesTui(backend, profile_store=FakeProfileStore(), test_rate_hz=2)
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        app._start_effect(now=0.0)
        app._tick_effect(now=0.0, authorized=True)
        backend.deny_snapshot = True
        with mock.patch("okeylitctl.tui.monotonic", side_effect=(10.0, 10.24)):
            self.assertFalse(app._stop_effect())
        self.assertTrue(app.state.effect_restore_pending)
        self.assertIn("Stop 240ms", app.state.message)
        self.assertEqual(len(backend.cas_writes), 1)
        self.assertTrue(app._stop_effect())
        self.assertEqual(len(backend.cas_writes), 2)
        self.assertEqual(backend.cas_writes[-1], app.state.current.to_wire())


if __name__ == "__main__":
    unittest.main()
