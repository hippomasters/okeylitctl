from __future__ import annotations

from math import inf, nan
import sys
from unittest import mock
import unittest

from okeylitctl.effects import EffectKind, EffectSpec
from okeylitctl.effect_runtime import EffectRuntime
from okeylitctl.models import ColorLayout, Zone
from okeylitctl.sysfs import BackendIOError


BASE = ColorLayout.from_wire("112233,445566,778899,AABBCC")


class RecordingBackend:
    def __init__(
        self,
        *,
        state: str = "on",
        fail_on_write: int | None = None,
        fail_status: bool = False,
    ):
        self.state = state
        self.fail_on_write = fail_on_write
        self.fail_status = fail_status
        self.writes: list[str] = []
        self.write_attempts = 0
        self.status_calls = 0

    def status(self) -> dict[str, object]:
        self.status_calls += 1
        if self.fail_status:
            raise BackendIOError("injected firmware status failure")
        return {
            "state": self.state,
            "colors": BASE.to_wire().split(","),
            "original": BASE.to_wire().split(","),
        }

    def write_colors(self, canonical_colors: str) -> None:
        self.write_attempts += 1
        if self.write_attempts == self.fail_on_write:
            raise BackendIOError("injected firmware write failure")
        self.writes.append(canonical_colors)


class EffectRuntimeSchedulingTests(unittest.TestCase):
    def test_next_deadline_is_measured_from_completed_write(self):
        backend = RecordingBackend()
        runtime = EffectRuntime(
            backend=backend,
            spec=EffectSpec(kind=EffectKind.CYCLE, base=BASE),
            rate_hz=2.0,
        )

        runtime.start(now=0.0)
        with mock.patch("okeylitctl.effect_runtime.monotonic", side_effect=(10.0, 10.2)):
            self.assertTrue(runtime.tick(now=0.0))

        self.assertAlmostEqual(runtime.next_due, 0.7)
        self.assertFalse(runtime.tick(now=0.69))

    def test_rapid_reactive_events_never_advance_the_write_deadline(self):
        backend = RecordingBackend()
        runtime = EffectRuntime(
            backend=backend,
            spec=EffectSpec(kind=EffectKind.REACTIVE, base=BASE),
            rate_hz=2.0,
        )

        runtime.start(now=0.0)
        runtime.trigger(Zone.WASD, now=0.0)
        self.assertTrue(runtime.tick(now=0.0))
        first_deadline = runtime.next_due
        runtime.trigger(Zone.LEFT, now=0.1)
        self.assertFalse(runtime.tick(now=0.1))
        runtime.trigger(Zone.CENTER, now=0.2)
        self.assertFalse(runtime.tick(now=first_deadline - 0.01))
        self.assertTrue(runtime.tick(now=first_deadline))
        self.assertEqual(len(backend.writes), 2)

    def test_reactive_runtime_waits_for_input_then_writes_immediately(self):
        uniform = ColorLayout.from_wire("808080,808080,808080,808080")
        backend = RecordingBackend()
        runtime = EffectRuntime(
            backend=backend,
            spec=EffectSpec(kind=EffectKind.REACTIVE, base=uniform),
            rate_hz=2.0,
        )

        runtime.start(now=0.0)
        self.assertFalse(runtime.tick(now=0.0))
        self.assertEqual(backend.writes, [])

        self.assertTrue(runtime.trigger(Zone.WASD, now=0.1))
        self.assertTrue(runtime.tick(now=0.1))
        frame = ColorLayout.from_wire(backend.writes[-1])

        def brightness(color: str) -> int:
            return sum(int(color[offset : offset + 2], 16) for offset in (0, 2, 4))

        self.assertGreater(brightness(frame.wasd), brightness(frame.left))
        self.assertGreater(brightness(frame.wasd), brightness(frame.center))
        self.assertGreater(brightness(frame.wasd), brightness(frame.right))

    def test_due_ticks_write_once_and_late_ticks_do_not_catch_up(self):
        backend = RecordingBackend()
        runtime = EffectRuntime(
            backend=backend,
            spec=EffectSpec(kind=EffectKind.CYCLE, base=BASE),
            rate_hz=2.0,
        )

        runtime.start(now=0.0)
        self.assertTrue(runtime.tick(now=0.0))
        first_deadline = runtime.next_due
        self.assertFalse(runtime.tick(now=0.10))
        self.assertFalse(runtime.tick(now=first_deadline - 0.01))
        self.assertTrue(runtime.tick(now=first_deadline))

        writes_before_jump = len(backend.writes)
        self.assertTrue(runtime.tick(now=10.0))
        self.assertEqual(len(backend.writes), writes_before_jump + 1)
        late_deadline = runtime.next_due
        self.assertFalse(runtime.tick(now=10.0))
        self.assertFalse(runtime.tick(now=late_deadline - 0.01))
        self.assertTrue(runtime.tick(now=late_deadline))

        self.assertEqual(backend.status_calls, len(backend.writes))
        self.assertTrue(all(len(payload.split(",")) == 4 for payload in backend.writes))

    def test_unauthorized_render_state_defers_tick_without_device_access(self):
        backend = RecordingBackend()
        runtime = EffectRuntime(
            backend=backend,
            spec=EffectSpec(kind=EffectKind.CYCLE, base=BASE),
            rate_hz=2.0,
        )

        runtime.start(now=0.0)
        self.assertFalse(runtime.tick(now=0.0, authorized=False))
        self.assertEqual(backend.status_calls, 0)
        self.assertEqual(backend.write_attempts, 0)
        self.assertTrue(runtime.active)

        self.assertTrue(runtime.tick(now=0.25, authorized=True))
        self.assertEqual(backend.status_calls, 1)
        self.assertEqual(backend.write_attempts, 1)

    def test_status_failure_after_success_preserves_pending_restore_target(self):
        backend = RecordingBackend()
        runtime = EffectRuntime(
            backend=backend,
            spec=EffectSpec(kind=EffectKind.CYCLE, base=BASE),
            rate_hz=2.0,
        )

        runtime.start(now=0.0)
        self.assertTrue(runtime.tick(now=0.0))
        successful_frame = runtime.last_written
        backend.fail_status = True

        with self.assertRaisesRegex(BackendIOError, "status"):
            runtime.tick(now=runtime.next_due)

        self.assertFalse(runtime.active)
        self.assertEqual(runtime.last_written, successful_frame)
        self.assertNotEqual(runtime.last_written, runtime.spec.base)

    def test_interrupt_during_write_marks_attempted_frame_uncertain(self):
        class InterruptAfterWriteBackend(RecordingBackend):
            def write_colors(self, canonical_colors: str) -> None:
                self.write_attempts += 1
                self.writes.append(canonical_colors)
                raise KeyboardInterrupt

        backend = InterruptAfterWriteBackend()
        runtime = EffectRuntime(
            backend=backend,
            spec=EffectSpec(kind=EffectKind.CYCLE, base=BASE),
            rate_hz=2.0,
        )
        runtime.start(now=0.0)

        with self.assertRaises(KeyboardInterrupt):
            runtime.tick(now=0.0)

        self.assertFalse(runtime.active)
        self.assertTrue(runtime.write_state_uncertain)
        self.assertIsNotNone(runtime.attempted_frame)
        self.assertEqual(runtime.attempted_frame.to_wire(), backend.writes[-1])

    def test_interrupt_after_effect_write_returns_still_marks_state_uncertain(self):
        armed = False

        class InterruptGapBackend(RecordingBackend):
            def __init__(self):
                super().__init__()
                self.live = BASE.to_wire().split(",")

            def status(self):
                self.status_calls += 1
                return {
                    "state": "on",
                    "colors": list(self.live),
                    "original": BASE.to_wire().split(","),
                }

            def write_colors(self, canonical_colors: str) -> None:
                nonlocal armed
                self.write_attempts += 1
                self.writes.append(canonical_colors)
                self.live = canonical_colors.split(",")
                armed = True

        backend = InterruptGapBackend()
        runtime = EffectRuntime(
            backend=backend,
            spec=EffectSpec(kind=EffectKind.CYCLE, base=BASE),
            rate_hz=2.0,
        )
        runtime.start(now=0.0)

        def interrupt_after_return(frame, event, _arg):
            nonlocal armed
            if armed and event == "line" and frame.f_code is EffectRuntime.tick.__code__:
                armed = False
                raise KeyboardInterrupt
            return interrupt_after_return

        sys.settrace(interrupt_after_return)
        try:
            with self.assertRaises(KeyboardInterrupt):
                runtime.tick(now=0.0)
        finally:
            sys.settrace(None)

        self.assertTrue(runtime.write_state_uncertain)
        self.assertIsNotNone(runtime.attempted_frame)
        attempted = runtime.attempted_frame
        self.assertFalse(runtime.stop())
        self.assertFalse(runtime.write_state_uncertain)
        self.assertEqual(runtime.last_written, attempted)
        self.assertNotEqual(runtime.last_written, BASE)

    def test_interrupt_after_restore_write_returns_never_duplicates_base_write(self):
        armed = False

        class InterruptGapBackend(RecordingBackend):
            def __init__(self):
                super().__init__()
                self.live = BASE.to_wire().split(",")

            def status(self):
                self.status_calls += 1
                return {
                    "state": "on",
                    "colors": list(self.live),
                    "original": BASE.to_wire().split(","),
                }

            def write_colors(self, canonical_colors: str) -> None:
                nonlocal armed
                self.write_attempts += 1
                self.writes.append(canonical_colors)
                self.live = canonical_colors.split(",")
                if canonical_colors == BASE.to_wire():
                    armed = True

        backend = InterruptGapBackend()
        runtime = EffectRuntime(
            backend=backend,
            spec=EffectSpec(kind=EffectKind.CYCLE, base=BASE),
            rate_hz=2.0,
        )
        runtime.start(now=0.0)
        self.assertTrue(runtime.tick(now=0.0))

        def interrupt_after_return(frame, event, _arg):
            nonlocal armed
            if armed and event == "line" and frame.f_code is EffectRuntime.stop.__code__:
                armed = False
                raise KeyboardInterrupt
            return interrupt_after_return

        sys.settrace(interrupt_after_return)
        try:
            with self.assertRaises(KeyboardInterrupt):
                runtime.stop()
        finally:
            sys.settrace(None)

        self.assertTrue(runtime.write_state_uncertain)
        self.assertEqual(runtime.attempted_frame, BASE)
        self.assertEqual(backend.writes.count(BASE.to_wire()), 1)
        self.assertFalse(runtime.stop())
        self.assertFalse(runtime.write_state_uncertain)
        self.assertEqual(runtime.last_written, BASE)
        self.assertEqual(backend.writes.count(BASE.to_wire()), 1)

    def test_interrupt_during_restore_write_marks_base_attempt_uncertain(self):
        class InterruptAfterRestoreBackend(RecordingBackend):
            def __init__(self):
                super().__init__()
                self.live = BASE.to_wire().split(",")
                self.interrupted_restore = False

            def status(self):
                self.status_calls += 1
                return {
                    "state": self.state,
                    "colors": list(self.live),
                    "original": BASE.to_wire().split(","),
                }

            def write_colors(self, canonical_colors: str) -> None:
                self.write_attempts += 1
                self.writes.append(canonical_colors)
                self.live = canonical_colors.split(",")
                if canonical_colors == BASE.to_wire() and not self.interrupted_restore:
                    self.interrupted_restore = True
                    raise KeyboardInterrupt

        backend = InterruptAfterRestoreBackend()
        runtime = EffectRuntime(
            backend=backend,
            spec=EffectSpec(kind=EffectKind.CYCLE, base=BASE),
            rate_hz=2.0,
        )
        runtime.start(now=0.0)
        self.assertTrue(runtime.tick(now=0.0))
        effect_frame = runtime.last_written

        with self.assertRaises(KeyboardInterrupt):
            runtime.stop()

        self.assertTrue(runtime.write_state_uncertain)
        self.assertEqual(runtime.last_written, effect_frame)
        self.assertEqual(runtime.attempted_frame, BASE)
        self.assertEqual(backend.writes.count(BASE.to_wire()), 1)

        self.assertFalse(runtime.stop())
        self.assertFalse(runtime.write_state_uncertain)
        self.assertEqual(runtime.last_written, BASE)
        self.assertEqual(backend.writes.count(BASE.to_wire()), 1)

    def test_first_status_failure_stops_without_writing(self):
        backend = RecordingBackend(fail_status=True)
        runtime = EffectRuntime(
            backend=backend,
            spec=EffectSpec(kind=EffectKind.CYCLE, base=BASE),
            rate_hz=2.0,
        )

        runtime.start(now=0.0)
        with self.assertRaisesRegex(BackendIOError, "status"):
            runtime.tick(now=0.0)

        self.assertFalse(runtime.active)
        self.assertEqual(backend.status_calls, 1)
        self.assertEqual(backend.write_attempts, 0)

    def test_first_write_failure_stops_without_attempting_restore(self):
        backend = RecordingBackend(fail_on_write=1)
        runtime = EffectRuntime(
            backend=backend,
            spec=EffectSpec(kind=EffectKind.CYCLE, base=BASE),
            rate_hz=2.0,
        )

        runtime.start(now=0.0)
        with self.assertRaisesRegex(BackendIOError, "injected"):
            runtime.tick(now=0.0)

        self.assertFalse(runtime.active)
        self.assertEqual(backend.write_attempts, 1)
        self.assertTrue(runtime.write_state_uncertain)
        self.assertFalse(runtime.stop())
        self.assertFalse(runtime.write_state_uncertain)
        self.assertEqual(runtime.last_written, BASE)
        self.assertEqual(backend.write_attempts, 1)

    def test_failed_later_write_never_restores_in_same_stop_that_resolves_state(self):
        backend = RecordingBackend(fail_on_write=2)
        runtime = EffectRuntime(
            backend=backend,
            spec=EffectSpec(kind=EffectKind.CYCLE, base=BASE),
            rate_hz=2.0,
        )

        runtime.start(now=0.0)
        self.assertTrue(runtime.tick(now=0.0))
        with self.assertRaisesRegex(BackendIOError, "injected"):
            runtime.tick(now=runtime.next_due)

        attempts_after_failure = backend.write_attempts
        self.assertTrue(runtime.write_state_uncertain)
        self.assertFalse(runtime.stop())
        self.assertEqual(backend.write_attempts, attempts_after_failure)
        self.assertFalse(runtime.write_state_uncertain)
        self.assertEqual(runtime.last_written, BASE)

    def test_stop_restores_base_once_after_an_effect_frame(self):
        backend = RecordingBackend()
        runtime = EffectRuntime(
            backend=backend,
            spec=EffectSpec(kind=EffectKind.CYCLE, base=BASE),
            rate_hz=2.0,
        )

        runtime.start(now=0.0)
        self.assertTrue(runtime.tick(now=0.0))
        self.assertNotEqual(backend.writes[-1], BASE.to_wire())

        self.assertTrue(runtime.stop())
        self.assertEqual(backend.writes[-1], BASE.to_wire())
        writes_after_restore = list(backend.writes)
        self.assertFalse(runtime.stop())
        self.assertEqual(backend.writes, writes_after_restore)
        self.assertFalse(runtime.active)

    def test_power_off_after_frame_defers_restore_without_another_write(self):
        backend = RecordingBackend()
        runtime = EffectRuntime(
            backend=backend,
            spec=EffectSpec(kind=EffectKind.CYCLE, base=BASE),
            rate_hz=2.0,
        )

        runtime.start(now=0.0)
        self.assertTrue(runtime.tick(now=0.0))
        backend.state = "off"
        self.assertFalse(runtime.tick(now=runtime.next_due))
        self.assertFalse(runtime.active)

        attempts_before_stop = backend.write_attempts
        self.assertFalse(runtime.stop())
        self.assertEqual(backend.write_attempts, attempts_before_stop)

    def test_power_off_stops_without_writing(self):
        backend = RecordingBackend(state="off")
        runtime = EffectRuntime(
            backend=backend,
            spec=EffectSpec(kind=EffectKind.CYCLE, base=BASE),
            rate_hz=2.0,
        )

        runtime.start(now=0.0)
        self.assertFalse(runtime.tick(now=0.0))

        self.assertFalse(runtime.active)
        self.assertEqual(backend.writes, [])
        self.assertEqual(backend.status_calls, 1)

    def test_rate_must_be_finite_and_within_safe_hardware_limit(self):
        for rate in (0.0, -1.0, 5.01, inf, nan):
            with self.subTest(rate=rate):
                with self.assertRaisesRegex(ValueError, "rate_hz"):
                    EffectRuntime(
                        backend=RecordingBackend(),
                        spec=EffectSpec(kind=EffectKind.CYCLE, base=BASE),
                        rate_hz=rate,
                    )


if __name__ == "__main__":
    unittest.main()
