from __future__ import annotations

from math import inf, nan
import sys
from unittest import mock
import unittest

from okeylitctl.effects import EffectKind, EffectSpec
from okeylitctl.effect_runtime import EffectRuntime
from okeylitctl.ipc import ConflictError
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


class CASBackend:
    """Broker-like token fencing, including writes by a second client."""

    def __init__(self):
        self.state = "on"
        self.colors = BASE.to_wire()
        self.revision = 0
        self.writes: list[str] = []
        self.cas_calls: list[tuple[str, str, str, str]] = []
        self.race_color: str | None = None
        self.lose_ack = False

    @property
    def token(self) -> str:
        return f"0000000000000001:{self.revision:016X}"

    def snapshot(self) -> dict[str, object]:
        return {
            "token": self.token,
            "state": self.state,
            "colors": self.colors.split(","),
            "original": BASE.to_wire().split(","),
        }

    def external_write(self, colors: str) -> None:
        self.revision += 1
        self.colors = colors

    def compare_and_write(self, token: str, state: str, expected_wire: str, desired_wire: str) -> str:
        self.cas_calls.append((token, state, expected_wire, desired_wire))
        if self.race_color is not None:
            self.external_write(self.race_color)
            self.race_color = None
        if token != self.token or state != self.state or expected_wire != self.colors:
            raise ConflictError("broker snapshot conflict")
        self.revision += 1
        self.colors = desired_wire
        self.writes.append(desired_wire)
        if self.lose_ack:
            self.lose_ack = False
            raise BackendIOError("mutation outcome uncertain")
        return self.token


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

    def test_cas_tick_owns_acknowledged_token_and_stop_conditionally_restores(self):
        backend = CASBackend()
        runtime = EffectRuntime(backend, EffectSpec(kind=EffectKind.CYCLE, base=BASE))
        runtime.start(now=0.0)
        self.assertTrue(runtime.tick(now=0.0))
        frame = backend.colors
        self.assertNotEqual(frame, BASE.to_wire())
        self.assertEqual(runtime.owned_token, backend.token)
        self.assertEqual(backend.cas_calls[0][2], BASE.to_wire())
        self.assertTrue(runtime.stop())
        self.assertEqual(backend.cas_calls[-1][2:], (frame, BASE.to_wire()))
        self.assertEqual(runtime.owned_token, backend.token)
        self.assertEqual(runtime.last_written, BASE)
        self.assertFalse(runtime.stop())
        self.assertEqual(len(backend.writes), 2)

    def test_cross_client_write_before_stop_blocks_restore_even_with_same_colors(self):
        backend = CASBackend()
        runtime = EffectRuntime(backend, EffectSpec(kind=EffectKind.CYCLE, base=BASE))
        runtime.start(now=0.0)
        self.assertTrue(runtime.tick(now=0.0))
        frame = backend.colors
        owned_token = runtime.owned_token
        backend.external_write(frame)  # ABA: colors match, ownership does not.
        self.assertFalse(runtime.stop())
        self.assertTrue(runtime.ownership_lost)
        self.assertFalse(runtime.active)
        self.assertNotEqual(runtime.owned_token, backend.token)
        self.assertNotEqual(runtime.last_written, BASE)
        self.assertFalse(runtime.stop())
        self.assertEqual(len(backend.cas_calls), 1)
        self.assertEqual(backend.writes, [frame])
        self.assertNotEqual(owned_token, backend.token)

    def test_cross_client_write_during_effect_stops_without_next_frame(self):
        backend = CASBackend()
        runtime = EffectRuntime(backend, EffectSpec(kind=EffectKind.CYCLE, base=BASE))
        runtime.start(now=0.0)
        self.assertTrue(runtime.tick(now=0.0))
        backend.external_write(BASE.to_wire())
        with self.assertRaises(ConflictError):
            runtime.tick(now=runtime.next_due)
        self.assertTrue(runtime.ownership_lost)
        self.assertFalse(runtime.active)
        self.assertFalse(runtime.stop())
        self.assertNotEqual(runtime.last_written, BASE)
        self.assertEqual(len(backend.cas_calls), 1)

    def test_cross_client_race_between_snapshot_and_restore_cas(self):
        backend = CASBackend()
        runtime = EffectRuntime(backend, EffectSpec(kind=EffectKind.CYCLE, base=BASE))
        runtime.start(now=0.0)
        self.assertTrue(runtime.tick(now=0.0))
        backend.race_color = BASE.to_wire()
        self.assertFalse(runtime.stop())
        self.assertTrue(runtime.ownership_lost)
        self.assertFalse(runtime.stop())
        self.assertEqual(len(backend.cas_calls), 2)
        self.assertEqual(len(backend.writes), 1)
        self.assertNotEqual(runtime.last_written, BASE)

    def test_lost_ack_does_not_infer_ownership_from_attempted_frame(self):
        backend = CASBackend()
        backend.lose_ack = True
        runtime = EffectRuntime(backend, EffectSpec(kind=EffectKind.CYCLE, base=BASE))
        runtime.start(now=0.0)
        with self.assertRaises(BackendIOError):
            runtime.tick(now=0.0)
        self.assertTrue(runtime.write_state_uncertain)
        self.assertIsNone(runtime.owned_token)
        self.assertEqual(backend.colors, runtime.attempted_frame.to_wire())
        self.assertFalse(runtime.stop())
        self.assertFalse(runtime.stop())
        self.assertTrue(runtime.write_state_uncertain)
        self.assertIsNone(runtime.last_written)
        self.assertEqual(len(backend.cas_calls), 1)

    def test_power_off_does_not_restore_after_cas_frame(self):
        backend = CASBackend()
        runtime = EffectRuntime(backend, EffectSpec(kind=EffectKind.CYCLE, base=BASE))
        runtime.start(now=0.0)
        self.assertTrue(runtime.tick(now=0.0))
        backend.state = "off"
        self.assertFalse(runtime.stop())
        self.assertTrue(runtime.observed_power_off)
        self.assertFalse(runtime.stop())
        self.assertEqual(len(backend.cas_calls), 1)
        self.assertNotEqual(runtime.last_written, BASE)

    def test_base_frame_is_verified_before_claiming_it_is_still_active(self):
        backend = CASBackend()
        runtime = EffectRuntime(backend, EffectSpec(kind=EffectKind.STATIC, base=BASE))
        runtime.start(now=0.0)
        self.assertTrue(runtime.tick(now=0.0))
        self.assertEqual(runtime.last_written, BASE)
        backend.external_write(BASE.to_wire())
        self.assertFalse(runtime.stop())
        self.assertTrue(runtime.ownership_lost)
        self.assertIsNone(runtime.last_written)

    def test_race_during_frame_cas_does_not_claim_or_restore_frame(self):
        backend = CASBackend()
        backend.race_color = BASE.to_wire()
        runtime = EffectRuntime(backend, EffectSpec(kind=EffectKind.CYCLE, base=BASE))
        runtime.start(now=0.0)
        with self.assertRaises(ConflictError):
            runtime.tick(now=0.0)
        self.assertTrue(runtime.ownership_lost)
        self.assertIsNone(runtime.last_written)
        self.assertIsNone(runtime.owned_token)
        self.assertFalse(runtime.stop())
        self.assertEqual(backend.writes, [])
        self.assertEqual(len(backend.cas_calls), 1)

    def test_lost_restore_ack_never_claims_base_or_retries(self):
        backend = CASBackend()
        runtime = EffectRuntime(backend, EffectSpec(kind=EffectKind.CYCLE, base=BASE))
        runtime.start(now=0.0)
        self.assertTrue(runtime.tick(now=0.0))
        frame = runtime.last_written
        owned_token = runtime.owned_token
        backend.lose_ack = True
        with self.assertRaises(BackendIOError):
            runtime.stop()
        self.assertTrue(runtime.write_state_uncertain)
        self.assertEqual(runtime.last_written, frame)
        self.assertEqual(runtime.owned_token, owned_token)
        self.assertEqual(backend.colors, BASE.to_wire())
        self.assertFalse(runtime.stop())
        self.assertEqual(len(backend.cas_calls), 2)
        self.assertNotEqual(runtime.last_written, BASE)

    def test_unaccepted_interrupted_restore_can_retry_only_after_matching_token_and_frame(self):
        class InterruptedBeforeAcceptance(CASBackend):
            interrupt = True
            def compare_and_write(self, token, state, expected_wire, desired_wire):
                if self.interrupt and desired_wire == BASE.to_wire():
                    self.interrupt = False
                    self.cas_calls.append((token, state, expected_wire, desired_wire))
                    raise KeyboardInterrupt
                return super().compare_and_write(token, state, expected_wire, desired_wire)

        backend = InterruptedBeforeAcceptance()
        runtime = EffectRuntime(backend, EffectSpec(kind=EffectKind.CYCLE, base=BASE))
        runtime.start(now=0.0)
        self.assertTrue(runtime.tick(now=0.0))
        frame, owned_token = runtime.last_written, runtime.owned_token
        with self.assertRaises(KeyboardInterrupt):
            runtime.stop()
        self.assertTrue(runtime.write_state_uncertain)
        self.assertEqual((runtime.last_written, runtime.owned_token), (frame, owned_token))
        self.assertEqual(backend.colors, frame.to_wire())
        self.assertFalse(runtime.stop())  # Read-only: proves this CAS was not accepted.
        self.assertFalse(runtime.write_state_uncertain)
        self.assertEqual(len(backend.cas_calls), 2)
        self.assertTrue(runtime.stop())  # Only this acknowledged CAS restores base.
        self.assertEqual(backend.writes.count(BASE.to_wire()), 1)
        self.assertEqual(runtime.last_written, BASE)

    def test_interrupted_restore_with_changed_token_never_retries_even_for_matching_colors(self):
        for accepted in (True, False):
            with self.subTest(accepted=accepted):
                class Interrupted(CASBackend):
                    interrupt = True
                    def compare_and_write(self, token, state, expected_wire, desired_wire):
                        if self.interrupt and desired_wire == BASE.to_wire():
                            self.interrupt = False
                            if accepted:
                                super().compare_and_write(token, state, expected_wire, desired_wire)
                            else:
                                self.external_write(expected_wire)  # ABA by another client.
                            raise KeyboardInterrupt
                        return super().compare_and_write(token, state, expected_wire, desired_wire)

                backend = Interrupted()
                runtime = EffectRuntime(backend, EffectSpec(kind=EffectKind.CYCLE, base=BASE))
                runtime.start(now=0.0)
                runtime.tick(now=0.0)
                with self.assertRaises(KeyboardInterrupt):
                    runtime.stop()
                self.assertFalse(runtime.stop())
                self.assertTrue(runtime.ownership_lost)
                self.assertNotEqual(runtime.last_written, BASE)
                self.assertFalse(runtime.stop())
                self.assertEqual(len(backend.writes), 2 if accepted else 1)

    def test_interrupted_restore_power_off_or_snapshot_failure_keeps_unknown_without_write(self):
        class Interrupted(CASBackend):
            interrupt = True
            fail_snapshot = False
            def compare_and_write(self, token, state, expected_wire, desired_wire):
                if self.interrupt and desired_wire == BASE.to_wire():
                    self.interrupt = False
                    raise KeyboardInterrupt
                return super().compare_and_write(token, state, expected_wire, desired_wire)
            def snapshot(self):
                if self.fail_snapshot:
                    raise BackendIOError("status unavailable")
                return super().snapshot()

        backend = Interrupted()
        runtime = EffectRuntime(backend, EffectSpec(kind=EffectKind.CYCLE, base=BASE))
        runtime.start(now=0.0)
        runtime.tick(now=0.0)
        with self.assertRaises(KeyboardInterrupt):
            runtime.stop()
        backend.fail_snapshot = True
        with self.assertRaises(BackendIOError):
            runtime.stop()
        self.assertTrue(runtime.write_state_uncertain)
        backend.fail_snapshot = False
        backend.state = "off"
        self.assertFalse(runtime.stop())
        self.assertTrue(runtime.observed_power_off)
        self.assertTrue(runtime.write_state_uncertain)
        self.assertEqual(len(backend.writes), 1)

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
