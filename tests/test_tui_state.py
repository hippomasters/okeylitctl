import unittest

from okeylitctl.models import ColorLayout, Zone
from okeylitctl.effects import EffectKind
from okeylitctl.tui_state import TuiState


CURRENT = ColorLayout.from_wire("110000,002200,000033,444444")
ORIGINAL = ColorLayout.from_wire("580BC3,D00FEF,4D0998,AF0AA1")


class TuiStateTests(unittest.TestCase):
    def test_cycle_controls_change_the_effect_spec_without_changing_draft(self):
        state = TuiState(current=CURRENT, original=ORIGINAL)
        state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        state.selected_channel = 0
        state.adjust_cycle_control(1)
        self.assertEqual(state.effect_spec.cycle_from_deg, 5)
        state.selected_channel = 1
        state.adjust_cycle_control(-1)
        self.assertEqual(state.effect_spec.cycle_to_deg, 355)
        state.selected_channel = 2
        state.adjust_cycle_control(-1)
        self.assertEqual(state.effect_spec.cycle_saturation, 0.85)
        self.assertEqual(state.current, CURRENT)
        self.assertEqual(state.draft, CURRENT)

    def test_cycle_hue_endpoints_and_saturation_remain_valid_at_limits(self):
        state = TuiState(current=CURRENT, original=ORIGINAL)
        state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        state.selected_channel = 0
        state.adjust_cycle_control(100)
        self.assertEqual(state.cycle_from_deg, 355)
        state.selected_channel = 1
        state.adjust_cycle_control(-100)
        self.assertEqual(state.cycle_to_deg, 360)
        state.selected_channel = 2
        state.adjust_cycle_control(-100)
        self.assertEqual(state.cycle_saturation_percent, 0)
        state.adjust_cycle_control(100)
        self.assertEqual(state.cycle_saturation_percent, 100)
        self.assertLess(state.effect_spec.cycle_from_deg, state.effect_spec.cycle_to_deg)

    def test_edits_are_local_until_marked_applied(self):
        state = TuiState(current=CURRENT, original=ORIGINAL)

        state.set_selected_color("abcdef")

        self.assertEqual(state.current, CURRENT)
        self.assertEqual(state.draft.right, "ABCDEF")
        self.assertTrue(state.dirty)

        state.mark_applied()

        self.assertEqual(state.current.right, "ABCDEF")
        self.assertFalse(state.dirty)

    def test_zone_navigation_uses_explicit_firmware_order(self):
        state = TuiState(current=CURRENT, original=ORIGINAL)

        self.assertEqual(state.selected_zone, Zone.RIGHT)
        state.select_next_zone()
        self.assertEqual(state.selected_zone, Zone.CENTER)
        state.select_next_zone()
        self.assertEqual(state.selected_zone, Zone.LEFT)
        state.select_next_zone()
        self.assertEqual(state.selected_zone, Zone.WASD)
        state.select_next_zone()
        self.assertEqual(state.selected_zone, Zone.RIGHT)
        state.select_previous_zone()
        self.assertEqual(state.selected_zone, Zone.WASD)

    def test_channel_adjustment_is_clamped_and_never_writes_firmware(self):
        state = TuiState(current=CURRENT, original=ORIGINAL)
        state.selected_channel = 0

        state.adjust_selected_channel(-255)
        self.assertEqual(state.draft.right, "000000")
        state.adjust_selected_channel(999)
        self.assertEqual(state.draft.right, "FF0000")

    def test_restore_result_resets_current_and_draft(self):
        state = TuiState(current=CURRENT, original=ORIGINAL)
        state.set_selected_color("FFFFFF")

        state.mark_restored()

        self.assertEqual(state.current, ORIGINAL)
        self.assertEqual(state.draft, ORIGINAL)
        self.assertFalse(state.dirty)

    def test_escape_discards_local_draft(self):
        state = TuiState(current=CURRENT, original=ORIGINAL)
        state.set_selected_color("FFFFFF")

        state.discard_draft()

        self.assertEqual(state.draft, CURRENT)
        self.assertFalse(state.dirty)

    def test_manual_and_reset_actions_clear_named_preset_metadata(self):
        state = TuiState(current=CURRENT, original=ORIGINAL)
        state.preset_index = 2

        state.set_selected_color("FFFFFF")
        self.assertEqual(state.preset_index, -1)

        state.preset_index = 2
        state.discard_draft()
        self.assertEqual(state.preset_index, -1)

        state.preset_index = 2
        state.mark_restored()
        self.assertEqual(state.preset_index, -1)


if __name__ == "__main__":
    unittest.main()
