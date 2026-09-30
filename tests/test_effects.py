import unittest

from okeylitctl.effects import EffectEvent, EffectKind, EffectSpec, frame_at
from okeylitctl.models import ColorLayout, Zone
from okeylitctl.tui_visual_contract import EFFECT_NAMES


BASE = ColorLayout.from_wire("7B2CFF,00CFFF,FF2E88,E8F7FF")


class EffectFrameTests(unittest.TestCase):
    def test_effect_catalog_matches_the_approved_mockup_order(self):
        self.assertEqual(tuple(kind.value for kind in EffectKind), EFFECT_NAMES)

    def test_static_returns_the_complete_base_layout_at_any_time(self):
        spec = EffectSpec(kind=EffectKind.STATIC, base=BASE)
        self.assertEqual(frame_at(spec, 0.0), BASE)
        self.assertEqual(frame_at(spec, 123.456), BASE)

    def test_blink_is_periodic_and_never_emits_an_all_black_layout(self):
        spec = EffectSpec(kind=EffectKind.BLINK, base=BASE, speed=0.5, light=1.0)
        bright = frame_at(spec, 0.0)
        dim = frame_at(spec, 1.1)
        repeated = frame_at(spec, 4.0)

        self.assertEqual(bright, BASE)
        self.assertNotEqual(dim, BASE)
        self.assertEqual(repeated, bright)
        self.assertNotEqual(dim.to_wire(), "000000,000000,000000,000000")

    def test_breathe_is_smooth_periodic_deterministic_and_bounded(self):
        spec = EffectSpec(kind=EffectKind.BREATHE, base=BASE, speed=0.5, light=0.8)
        start = frame_at(spec, 0.0)
        quarter = frame_at(spec, 1.0)
        repeated = frame_at(spec, 4.0)

        self.assertEqual(start, repeated)
        self.assertNotEqual(start, quarter)
        self.assertEqual(quarter, frame_at(spec, 1.0))
        for color in quarter.to_wire().split(","):
            self.assertRegex(color, r"^[0-9A-F]{6}$")
            self.assertTrue(any(int(color[offset : offset + 2], 16) > 0 for offset in (0, 2, 4)))

    def test_effect_spec_rejects_unsafe_or_nonfinite_controls(self):
        for kwargs in (
            {"speed": 0.0},
            {"speed": 1.01},
            {"light": -0.01},
            {"light": 1.01},
            {"speed": float("nan")},
            {"light": float("inf")},
            {"direction": 0},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    EffectSpec(kind=EffectKind.BREATHE, base=BASE, **kwargs)

    def test_every_time_driven_effect_is_complete_deterministic_and_animated(self):
        animated = tuple(
            kind
            for kind in EffectKind
            if kind not in (EffectKind.STATIC, EffectKind.REACTIVE, EffectKind.RIPPLE)
        )
        for kind in animated:
            with self.subTest(kind=kind.value):
                spec = EffectSpec(kind=kind, base=BASE, speed=0.6, light=0.85, seed=17)
                frames = tuple(frame_at(spec, elapsed) for elapsed in (0.0, 0.73, 1.41))
                self.assertEqual(frames, tuple(frame_at(spec, elapsed) for elapsed in (0.0, 0.73, 1.41)))
                self.assertGreater(len(set(frames)), 1)
                for frame in frames:
                    self.assertEqual(len(frame.to_wire().split(",")), 4)
                    for color in frame.to_wire().split(","):
                        self.assertRegex(color, r"^[0-9A-F]{6}$")

    def test_reactive_event_brightens_only_the_triggered_zone_then_expires(self):
        uniform = ColorLayout.from_wire("808080,808080,808080,808080")
        spec = EffectSpec(kind=EffectKind.REACTIVE, base=uniform, light=1.0)
        event = EffectEvent(zone=Zone.WASD, elapsed=1.0)

        active = frame_at(spec, 1.0, event=event)
        repeated = frame_at(spec, 1.0, event=event)
        expired = frame_at(spec, 2.0, event=event)

        def brightness(color: str) -> int:
            return sum(int(color[offset : offset + 2], 16) for offset in (0, 2, 4))

        self.assertEqual(active, repeated)
        self.assertNotEqual(active, uniform)
        self.assertGreater(brightness(active.wasd), brightness(active.left))
        self.assertGreater(brightness(active.wasd), brightness(active.center))
        self.assertGreater(brightness(active.wasd), brightness(active.right))
        self.assertEqual(expired, uniform)

    def test_ripple_event_propagates_across_physical_zones_then_expires(self):
        uniform = ColorLayout.from_wire("808080,808080,808080,808080")
        spec = EffectSpec(kind=EffectKind.RIPPLE, base=uniform, speed=1.0, light=1.0)
        event = EffectEvent(zone=Zone.LEFT, elapsed=1.0)

        at_left = frame_at(spec, 1.0, event=event)
        at_center = frame_at(spec, 1.22, event=event)
        at_right = frame_at(spec, 1.44, event=event)
        expired = frame_at(spec, 2.0, event=event)

        def brightness(color: str) -> int:
            return sum(int(color[offset : offset + 2], 16) for offset in (0, 2, 4))

        self.assertGreater(brightness(at_left.left), brightness(at_left.center))
        self.assertGreater(brightness(at_center.center), brightness(at_center.left))
        self.assertGreater(brightness(at_center.center), brightness(at_center.right))
        self.assertGreater(brightness(at_right.right), brightness(at_right.center))
        self.assertEqual(expired, uniform)

    def test_reactive_and_ripple_are_idle_without_input_events(self):
        for kind in (EffectKind.REACTIVE, EffectKind.RIPPLE):
            with self.subTest(kind=kind.value):
                spec = EffectSpec(kind=kind, base=BASE)
                self.assertEqual(frame_at(spec, 0.0), BASE)
                self.assertEqual(frame_at(spec, 99.0), BASE)

    def test_direction_reverses_directional_effect_progression(self):
        for kind in (EffectKind.WAVE, EffectKind.COMET, EffectKind.SCANNER):
            with self.subTest(kind=kind.value):
                forward = EffectSpec(kind=kind, base=BASE, speed=0.7, direction=1)
                reverse = EffectSpec(kind=kind, base=BASE, speed=0.7, direction=-1)
                self.assertNotEqual(frame_at(forward, 0.63), frame_at(reverse, 0.63))

    def test_seed_controls_deterministic_rain_fire_and_sparkle_variation(self):
        for kind in (EffectKind.RAIN, EffectKind.FIRE, EffectKind.SPARKLE):
            with self.subTest(kind=kind.value):
                first = EffectSpec(kind=kind, base=BASE, seed=1)
                second = EffectSpec(kind=kind, base=BASE, seed=2)
                self.assertNotEqual(frame_at(first, 1.25), frame_at(second, 1.25))


if __name__ == "__main__":
    unittest.main()
