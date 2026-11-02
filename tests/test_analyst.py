import contextlib
import datetime
import io
import json
import os
import unittest

from _load import FIX, load

facts = load("trader-analyst", "facts")
F = os.path.join(FIX, "trader-analyst")


def jload(name):
    with open(os.path.join(F, name), encoding="utf-8") as fh:
        return json.load(fh)


def cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = facts.main(argv)
    return code, out.getvalue(), err.getvalue()


class Maths(unittest.TestCase):
    def setUp(self):
        self.res = facts.analyse(jload("results-RELIANCE.json"), quarters=8)
        self.rows = {r["fiscal"]: r for r in self.res["rows"]}

    def test_fiscal_quarters_follow_the_indian_year(self):
        self.assertEqual(sorted(self.rows), sorted(["Q2 FY27", "Q1 FY27", "Q4 FY26", "Q3 FY26",
                                                    "Q2 FY26", "Q1 FY26", "Q4 FY25", "Q3 FY25"]))

    def test_yoy_growth(self):
        q = self.rows["Q2 FY27"]
        self.assertEqual(q["revenue_yoy"], 20.0)          # 1560 vs 1300
        self.assertEqual(q["net_profit_yoy"], 20.0)       # 240 vs 200
        self.assertEqual(q["eps_yoy"], 20.0)              # 12.0 vs 10.0

    def test_operating_profit_is_revenue_minus_expenses_not_ebitda(self):
        q = self.rows["Q2 FY27"]
        self.assertEqual(q["operating_profit"], 310.0)    # 1560 - 1250
        self.assertEqual(q["op_margin"], 19.9)

    def test_no_yoy_without_the_prior_year_quarter(self):
        self.assertIsNone(self.rows["Q4 FY25"]["revenue_yoy"])

    def test_trailing_twelve_months(self):
        t = self.res["ttm"]
        self.assertTrue(t["available"])
        self.assertEqual((t["from"], t["to"]), ("2025-12-31", "2026-09-30"))
        self.assertEqual(t["revenue"], 5660.0)            # 1250 + 1350 + 1500 + 1560
        self.assertEqual(t["net_profit"], 885.0)          # 195 + 215 + 235 + 240
        self.assertEqual(t["eps"], 44.2)                  # 9.8 + 10.7 + 11.7 + 12.0

    def test_basis_and_unit_are_stated(self):
        self.assertEqual(self.res["basis"], "consolidated")
        self.assertEqual(self.res["unit_read_as"], "crore")

    def test_unit_conversion_lakh_to_crore(self):
        row = {"from_date": "2026-07-01", "to_date": "2026-09-30", "re_net_sale": 15600000,
               "re_total_expenses": 12500000}
        q = facts.normalise(row, "lakh")
        self.assertEqual(q["revenue"], 156000.0)          # 1,56,00,000 lakh = 1,56,000 crore

    def test_half_year_and_annual_rows_are_skipped(self):
        data = jload("results-RELIANCE.json")
        data["data"].append({"from_date": "2026-04-01", "to_date": "2026-09-30", "re_net_sale": 3060,
                             "re_total_expenses": 2450})
        res = facts.analyse(data, quarters=8)
        self.assertEqual(len(res["rows"]), 8)


class MissingData(unittest.TestCase):
    def test_missing_fields_are_listed_never_guessed(self):
        res = facts.analyse(jload("results-TINY.json"), quarters=8)
        latest = res["rows"][0]
        self.assertIsNone(latest["eps"])
        self.assertIn("eps", latest["missing"])
        self.assertIn("eps", " ".join(res["warnings"]) + " ".join(res["absent"]))
        self.assertFalse(res["ttm"].get("eps"))           # a TTM needs the field in all four quarters

    def test_unusable_rows_are_dropped(self):
        self.assertIsNone(facts.normalise({"re_net_sale": 1}, "crore"))

    def test_rupee_formatting_is_indian(self):
        self.assertEqual(facts.cr(5660.0), "₹5,660.00")


class Calendar(unittest.TestCase):
    def setUp(self):
        self.meetings = facts.parse_meetings(jload("meetings-RELIANCE.json"))

    def test_results_meeting_two_trading_days_away(self):
        c = facts.calendar_status(self.meetings, datetime.date(2026, 10, 30))
        self.assertEqual((c["next_results"], c["trading_days_until"], c["flag"]), ("2026-11-03", 2, "soon"))
        self.assertIn("intraday only", c["advice"])

    def test_far_away_results_do_not_flag(self):
        c = facts.calendar_status(self.meetings, datetime.date(2026, 11, 20))
        self.assertEqual(c["flag"], "later")
        self.assertEqual(c["next_results"], "2027-01-15")

    def test_the_diwali_holiday_is_not_a_trading_day(self):
        # Mon 9 Nov -> Wed 11 Nov skips Tuesday 10 Nov (Diwali-Balipratipada)
        self.assertEqual(facts.trading_days_between(datetime.date(2026, 11, 9), datetime.date(2026, 11, 11)), 1)


class Cli(unittest.TestCase):
    def test_table_and_results_warning(self):
        code, out, _ = cli(["RELIANCE", "--offline", F, "--now", "2026-10-30 09:00"])
        self.assertEqual(code, 0)
        self.assertIn("Q2 FY27", out)
        self.assertIn("₹5,660.00 Cr", out)
        self.assertIn("board meeting on 2026-11-03", out)
        self.assertIn("not EBITDA", out)
        self.assertNotIn("SEC", out)

    def test_json_mode(self):
        code, out, _ = cli(["RELIANCE", "--offline", F, "--now", "2026-10-30 09:00", "--json"])
        self.assertEqual(code, 0)
        self.assertIn("results", json.loads(out))

    def test_missing_offline_file_is_an_error_not_a_crash(self):
        code, out, err = cli(["NOSUCH", "--offline", F, "--now", "2026-10-30 09:00"])
        self.assertNotEqual(code, 0)
        self.assertIn("not found", out + err)

    def test_never_says_buy(self):
        _, out, _ = cli(["RELIANCE", "--offline", F, "--now", "2026-10-30 09:00"])
        self.assertNotIn("you should buy", out.lower())


if __name__ == "__main__":
    unittest.main()
