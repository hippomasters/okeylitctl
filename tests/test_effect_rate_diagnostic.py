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

    def test_unbudgeted_diagnostics_are_disabled_before_backend_access(self):
        for rate in (4, 5):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                CursesTui(CasEffectBackend(), profile_store=FakeProfileStore(),
                          test_rate_hz=rate)

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
