import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import group_reports as gr

GROUP = -5043287182
DIRECTOR = -1009999
MANAGER_DM = 4242


class GroupReportsTests(unittest.TestCase):
    def test_default_keeps_every_money_report_out_of_groups(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            for report in gr.MONEY_REPORTS:
                self.assertTrue(gr.blocked(report, GROUP, DIRECTOR), report)

    def test_director_chat_is_never_blocked_even_though_it_is_a_group(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(gr.blocked(gr.FOOD_COST, DIRECTOR, DIRECTOR))

    def test_manager_dm_still_gets_reports(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(gr.blocked(gr.BILL_ANALYSIS, MANAGER_DM, DIRECTOR))

    def test_setting_allows_listed_reports(self):
        with mock.patch.dict("os.environ", {"GROUP_MONEY_REPORTS": " Praise, food_cost ,junk"}):
            self.assertEqual(gr.allowed_in_groups(), {gr.PRAISE, gr.FOOD_COST})
            self.assertFalse(gr.blocked(gr.PRAISE, GROUP, DIRECTOR))
            self.assertTrue(gr.blocked(gr.OVERBUY, GROUP, DIRECTOR))

    def test_all_allows_everything(self):
        with mock.patch.dict("os.environ", {"GROUP_MONEY_REPORTS": "all"}):
            for report in gr.MONEY_REPORTS:
                self.assertFalse(gr.blocked(report, GROUP, DIRECTOR))

    def test_bad_chat_ids_are_not_groups(self):
        self.assertFalse(gr.is_group_chat(None))
        self.assertFalse(gr.is_group_chat("abc"))


if __name__ == "__main__":
    unittest.main()
