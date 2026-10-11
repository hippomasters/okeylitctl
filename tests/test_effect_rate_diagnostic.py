"""Device-free checks for provisional, explicit rate validation."""

import unittest
from unittest import mock

from okeylitctl.effects import EffectKind
from okeylitctl.tui import CursesTui
from okeylitctl.sysfs import BackendIOError
from test_tui import CasEffectBackend, FakeProfileStore


class RateDiagnosticTests(unittest.TestCase):
    def test_unbudgeted_diagnostics_are_disabled_before_backend_access(self):
        for rate in (4, 5):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                CursesTui(CasEffectBackend(), profile_store=FakeProfileStore(),
                          test_rate_hz=rate)

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
                                side_effect=(10.0, 10.1, 10.4, 10.5)):
                    self.assertTrue(app._tick_effect(now=0.0, authorized=True))
                    self.assertIn("I/O 100ms", app.state.message)
                    self.assertIn("spacing pending", app.state.message)
                    self.assertAlmostEqual(runtime.next_due, 0.1 + 1 / rate)
                    self.assertFalse(app._tick_effect(now=runtime.next_due - 0.01,
                                                      authorized=True))
                    self.assertTrue(app._tick_effect(now=runtime.next_due,
                                                     authorized=True))
                self.assertIn("spacing 400ms", app.state.message)
                self.assertAlmostEqual(runtime.next_due, 0.2 + 2 / rate)
                self.assertEqual(len(backend.cas_writes), 2)
                self.assertTrue(app._stop_effect())
                self.assertEqual(len(backend.cas_writes), 3)
                self.assertEqual(backend.cas_writes[-1], runtime.spec.base.to_wire())

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
