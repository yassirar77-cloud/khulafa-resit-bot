import unittest
from unittest import mock

import director_feed as df


def _env(**values):
    return mock.patch.dict("os.environ", values, clear=False)


class ModeTests(unittest.TestCase):
    def test_default_is_focus(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(df.mode(), df.FOCUS)
            self.assertFalse(df.wants_receipt_feed())

    def test_full_restores_receipt_feed(self):
        with _env(DIRECTOR_FEED=" Full "):
            self.assertEqual(df.mode(), df.FULL)
            self.assertTrue(df.wants_receipt_feed())

    def test_unknown_value_falls_back_to_focus(self):
        with _env(DIRECTOR_FEED="everything"):
            self.assertEqual(df.mode(), df.FOCUS)


class SpikeTests(unittest.TestCase):
    def test_small_spike_is_held_back_in_focus(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(df.wants_spike({"percent_increase": 12.0}))

    def test_big_spike_goes_live(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertTrue(df.wants_spike({"percent_increase": 20.0}))
            self.assertTrue(df.wants_spike({"percent_increase": 55.0}))

    def test_threshold_is_configurable(self):
        with _env(DIRECTOR_FEED="focus", DIRECTOR_SPIKE_MIN_PCT="30"):
            self.assertFalse(df.wants_spike({"percent_increase": 25.0}))
            self.assertTrue(df.wants_spike({"percent_increase": 30.0}))

    def test_bad_threshold_uses_default(self):
        for raw in ("abc", "-5", ""):
            with _env(DIRECTOR_SPIKE_MIN_PCT=raw):
                self.assertEqual(df.spike_min_pct(), df.DEFAULT_SPIKE_MIN_PCT)

    def test_full_mode_sends_every_spike(self):
        with _env(DIRECTOR_FEED="full"):
            self.assertTrue(df.wants_spike({"percent_increase": 11.0}))

    def test_malformed_spike_is_let_through(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertTrue(df.wants_spike({}))
            self.assertTrue(df.wants_spike(None))


if __name__ == "__main__":
    unittest.main()
