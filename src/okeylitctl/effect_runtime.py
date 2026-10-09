"""Foreground-only, bounded scheduling for validated four-zone effects."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from time import monotonic
from typing import Protocol

from .effects import EffectEvent, EffectKind, EffectSpec, frame_at
from .ipc import ConflictError
from .models import ColorLayout, Zone
from .sysfs import SysfsError


class EffectBackend(Protocol):
    def status(self) -> dict[str, object]: ...

    def write_colors(self, canonical_colors: str) -> None: ...


@dataclass
class EffectRuntime:
    backend: EffectBackend
    spec: EffectSpec
    rate_hz: float = 2.0
    active: bool = field(default=False, init=False)
    started_at: float = field(default=0.0, init=False)
    next_due: float = field(default=0.0, init=False)
    last_written: ColorLayout | None = field(default=None, init=False)
    event: EffectEvent | None = field(default=None, init=False)
    observed_power_off: bool = field(default=False, init=False)
    write_state_uncertain: bool = field(default=False, init=False)
    attempted_frame: ColorLayout | None = field(default=None, init=False)
    # Only an acknowledged CAS response grants ownership. Never reconstruct it
    # from a matching layout after a lost acknowledgement or another client's write.
    owned_token: str | None = field(default=None, init=False)
    ownership_lost: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isfinite(self.rate_hz) or not 0.0 < self.rate_hz <= 5.0:
            raise ValueError("rate_hz must be finite and in the range 0 < rate_hz <= 5")

    @property
    def interval(self) -> float:
        return 1.0 / self.rate_hz

    def start(self, *, now: float) -> None:
        self.started_at = now
        self.next_due = now
        self.active = True
        self.last_written = None
        self.event = None
        self.observed_power_off = False
        self.write_state_uncertain = False
        self.attempted_frame = None
        self.owned_token = None
        self.ownership_lost = False

    @property
    def _uses_cas(self) -> bool:
        return (callable(getattr(self.backend, "snapshot", None))
                and callable(getattr(self.backend, "compare_and_write", None)))

    def _lose_ownership(self) -> None:
        self.ownership_lost = True
        self.active = False
        self.owned_token = None
        # A previously acknowledged frame is no longer ours to restore. In
        # particular, never claim that the base has been restored.
        self.last_written = None

    def _cas_stop(self) -> bool:
        if self.ownership_lost or self.write_state_uncertain:
            return False
        if self.owned_token is None or self.last_written is None:
            return False
        snapshot = self.backend.snapshot()
        if snapshot.get("state") != "on":
            self.observed_power_off = True
            self._lose_ownership()
            return False
        self.observed_power_off = False
        expected = self.last_written.to_wire()
        if (snapshot.get("token") != self.owned_token
                or snapshot.get("colors") != expected.split(",")):
            self._lose_ownership()
            return False
        if self.last_written == self.spec.base:
            return False
        self.attempted_frame = self.spec.base
        self.write_state_uncertain = True
        try:
            token = self.backend.compare_and_write(
                self.owned_token, "on", expected, self.spec.base.to_wire()
            )
        except ConflictError:
            self.write_state_uncertain = False
            self.attempted_frame = None
            self._lose_ownership()
            return False
        except BaseException:
            # An acknowledgement may have been lost after the broker applied
            # the write. Reading colors (even the base) cannot establish that.
            raise
        self.owned_token = token
        self.last_written = self.spec.base
        self.write_state_uncertain = False
        self.attempted_frame = None
        return True

    def _cas_tick(self, *, now: float) -> bool:
        snapshot = self.backend.snapshot()
        if snapshot.get("state") != "on":
            self.active = False
            self.observed_power_off = True
            self._lose_ownership()
            return False
        self.observed_power_off = False
        live_wire = ",".join(snapshot["colors"])
        if self.owned_token is not None and (
            snapshot.get("token") != self.owned_token
            or self.last_written is None
            or live_wire != self.last_written.to_wire()
        ):
            self._lose_ownership()
            raise ConflictError("effect lost broker CAS ownership")
        elapsed = now - self.started_at
        frame = frame_at(self.spec, elapsed, event=self.event)
        event_expired = (
            self.event is not None
            and elapsed > self.event.elapsed
            and frame == self.spec.base
        )
        self.attempted_frame = frame
        self.write_state_uncertain = True
        try:
            token = self.backend.compare_and_write(
                snapshot["token"], "on", live_wire, frame.to_wire()
            )
        except ConflictError:
            self.write_state_uncertain = False
            self.attempted_frame = None
            self._lose_ownership()
            raise
        except BaseException:
            self.active = False
            raise
        self.owned_token = token
        self.last_written = frame
        self.write_state_uncertain = False
        self.attempted_frame = None
        if event_expired:
            self.event = None
        return True

    def trigger(self, zone: Zone, *, now: float) -> bool:
        if not self.active or self.spec.kind not in (
            EffectKind.REACTIVE,
            EffectKind.RIPPLE,
        ):
            return False
        elapsed = now - self.started_at
        if not isfinite(elapsed) or elapsed < 0.0:
            raise ValueError("event time must not precede effect start")
        self.event = EffectEvent(zone=zone, elapsed=elapsed)
        return True

    def stop(self) -> bool:
        self.active = False
        if self._uses_cas:
            return self._cas_stop()
        if self.write_state_uncertain:
            status = self.backend.status()
            if status.get("state") != "on":
                self.observed_power_off = True
                return False
            self.observed_power_off = False
            live_colors = status.get("colors")
            base_colors = self.spec.base.to_wire().split(",")
            previous_colors = (
                None if self.last_written is None else self.last_written.to_wire().split(",")
            )
            attempted_colors = (
                None if self.attempted_frame is None else self.attempted_frame.to_wire().split(",")
            )
            if live_colors == base_colors:
                self.last_written = self.spec.base
                self.write_state_uncertain = False
                self.attempted_frame = None
            elif previous_colors is not None and live_colors == previous_colors:
                self.write_state_uncertain = False
                self.attempted_frame = None
            elif attempted_colors is not None and live_colors == attempted_colors:
                self.last_written = self.attempted_frame
                self.write_state_uncertain = False
                self.attempted_frame = None
            return False
        if self.last_written is None or self.last_written == self.spec.base:
            return False
        status = self.backend.status()
        if status.get("state") != "on":
            self.observed_power_off = True
            return False
        self.observed_power_off = False
        self.attempted_frame = self.spec.base
        self.write_state_uncertain = True
        try:
            self.backend.write_colors(self.spec.base.to_wire())
        except BaseException:
            self.write_state_uncertain = True
            raise
        self.last_written = self.spec.base
        self.write_state_uncertain = False
        self.attempted_frame = None
        return True

    def tick(self, *, now: float, authorized: bool = True) -> bool:
        if not authorized or not self.active or now + 1e-12 < self.next_due:
            return False
        if self.spec.kind in (EffectKind.REACTIVE, EffectKind.RIPPLE) and self.event is None:
            return False
        io_started = monotonic()
        if self._uses_cas:
            try:
                wrote = self._cas_tick(now=now)
            except SysfsError:
                self.active = False
                raise
            if wrote:
                io_completed = monotonic()
                self.next_due = now + max(0.0, io_completed - io_started) + self.interval
            return wrote
        try:
            status = self.backend.status()
        except SysfsError:
            self.active = False
            self.observed_power_off = False
            raise
        if status.get("state") != "on":
            self.active = False
            self.observed_power_off = True
            return False
        self.observed_power_off = False
        elapsed = now - self.started_at
        frame = frame_at(self.spec, elapsed, event=self.event)
        event_expired = (
            self.event is not None
            and elapsed > self.event.elapsed
            and frame == self.spec.base
        )
        self.attempted_frame = frame
        self.write_state_uncertain = True
        try:
            self.backend.write_colors(frame.to_wire())
        except BaseException:
            self.active = False
            self.write_state_uncertain = True
            raise
        self.last_written = frame
        self.write_state_uncertain = False
        self.attempted_frame = None
        if event_expired:
            self.event = None
        io_completed = monotonic()
        self.next_due = now + max(0.0, io_completed - io_started) + self.interval
        return True
