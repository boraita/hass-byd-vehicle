"""Unit tests for the pure helpers in custom_components/byd_vehicle/_logic.py.

Loads the module by file path so it does NOT trigger the package __init__
(which imports Home Assistant) — these run with plain `python3 -m unittest`,
no HA / pyBYD / pytest needed.
"""

import importlib.util
import pathlib
import unittest

_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "custom_components"
    / "byd_vehicle"
    / "_logic.py"
)
_spec = importlib.util.spec_from_file_location("byd_logic", _PATH)
logic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(logic)


class TestSocToKwh(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(logic.soc_to_kwh(4.0), 3.3)  # 4% * 82.5 / 100

    def test_zero(self):
        self.assertEqual(logic.soc_to_kwh(0), 0.0)

    def test_negative_preserved(self):
        # net regen / charged-while-on yields negative energy (surfaced)
        self.assertEqual(logic.soc_to_kwh(-2.0), -1.65)

    def test_custom_pack(self):
        self.assertEqual(logic.soc_to_kwh(10.0, pack_kwh=60.0), 6.0)

    def test_non_numeric(self):
        self.assertIsNone(logic.soc_to_kwh(None))
        self.assertIsNone(logic.soc_to_kwh("x"))


class TestEfficiency(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(logic.efficiency_per_100km(3.3, 16.0), 20.6)

    def test_requires_positive_energy(self):
        self.assertIsNone(logic.efficiency_per_100km(0, 16.0))
        self.assertIsNone(logic.efficiency_per_100km(-1.0, 16.0))

    def test_requires_positive_distance(self):
        self.assertIsNone(logic.efficiency_per_100km(3.3, 0))
        self.assertIsNone(logic.efficiency_per_100km(3.3, None))

    def test_none_energy(self):
        self.assertIsNone(logic.efficiency_per_100km(None, 16.0))


class TestTrendFsm(unittest.TestCase):
    def test_enter_improving(self):
        self.assertEqual(logic.next_trend_state(None, 0.80), "improving")
        self.assertEqual(logic.next_trend_state("steady", 0.89), "improving")

    def test_enter_worsening(self):
        self.assertEqual(logic.next_trend_state("steady", 1.20), "worsening")

    def test_stay_steady_in_band(self):
        self.assertEqual(logic.next_trend_state(None, 1.0), "steady")
        self.assertEqual(logic.next_trend_state("steady", 1.05), "steady")

    def test_hysteresis_holds_improving(self):
        # already improving, ratio still below the exit band -> stays improving
        self.assertEqual(logic.next_trend_state("improving", 0.94), "improving")

    def test_hysteresis_exits_improving(self):
        self.assertEqual(logic.next_trend_state("improving", 0.96), "steady")

    def test_hysteresis_holds_worsening(self):
        self.assertEqual(logic.next_trend_state("worsening", 1.06), "worsening")

    def test_hysteresis_exits_worsening(self):
        self.assertEqual(logic.next_trend_state("worsening", 1.04), "steady")


class TestCommandError(unittest.TestCase):
    def test_remote_unconfirmed(self):
        base, mode = logic.command_error("remote_control", "lock", "timeout", "boom")
        self.assertIn("didn't confirm", base)
        self.assertEqual(mode, logic.HINT_APPEND_OR_RETRY)

    def test_remote_rejected(self):
        base, mode = logic.command_error("remote_control", "lock", "9001", "rejected")
        self.assertIn("rejected the command (rejected)", base)
        self.assertEqual(mode, logic.HINT_APPEND)

    def test_password_known_code(self):
        base, mode = logic.command_error("password", "lock", "5005", "x")
        self.assertEqual(base, "Command PIN is wrong — reconfigure the integration")
        self.assertEqual(mode, logic.HINT_NONE)

    def test_password_unknown_code(self):
        base, mode = logic.command_error("password", "lock", "9999", "boom")
        self.assertEqual(base, "Command PIN error: boom")
        self.assertEqual(mode, logic.HINT_NONE)

    def test_unsupported(self):
        base, mode = logic.command_error("unsupported", "lock", None, "x")
        self.assertIn("not supported", base)
        self.assertEqual(mode, logic.HINT_NONE)

    def test_generic(self):
        base, mode = logic.command_error("generic", "lock", None, "weird")
        self.assertEqual(base, "lock: weird")
        self.assertEqual(mode, logic.HINT_APPEND)


if __name__ == "__main__":
    unittest.main()
