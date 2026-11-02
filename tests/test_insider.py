import contextlib
import io
import json
import os
import tempfile
import unittest

from _load import FIX, load

ins = load("trader-insider", "insider")
F = os.path.join(FIX, "trader-insider")


def read(name):
    with open(os.path.join(F, name), "rb") as fh:
        return fh.read()


def cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ins.main(argv)
    return code, out.getvalue(), err.getvalue()


NONE = ["--watchlist", "/none", "--plan", "/none", "--portfolio", "/none"]


class Pit(unittest.TestCase):
    def setUp(self):
        self.rows = {r["person"] or "unnamed": r for r in ins.parse_pit(read("pit-RELIANCE.json"))}

    def test_promoter_buy_in_crore(self):
        r = self.rows["Synthetic Promoter Holdings Pvt Ltd"]
        self.assertEqual((r["side"], r["quantity"], r["value_crore"]), ("BUY", 250000.0, 32.15))
        self.assertEqual(r["trade_date"], "2026-10-27")

    def test_days_to_disclose_use_the_nse_calendar(self):
        self.assertEqual(self.rows["Synthetic Promoter Holdings Pvt Ltd"]["days_to_disclose"], 2)
        # 21 Oct -> 30 Oct: 7 trading days (weekend skipped)
        self.assertEqual(self.rows["Synthetic Director A"]["days_to_disclose"], 7)

    def test_holiday_is_not_counted(self):
        import datetime
        # Monday 9 Nov -> Wednesday 11 Nov: Tuesday 10 Nov is a holiday, so only 1 trading day
        self.assertEqual(ins.trading_days_between(datetime.date(2026, 11, 9), datetime.date(2026, 11, 11)), 1)

    def test_missing_fields_are_reported_not_invented(self):
        r = self.rows["unnamed"]
        self.assertEqual(r["side"], "PLEDGE")
        self.assertIsNone(r["quantity"])
        self.assertIsNone(r["trade_date"])
        self.assertEqual(set(r["missing"]), {"person", "quantity", "trade_date"})

    def test_block_page_is_an_error(self):
        with self.assertRaises(ins.InsiderError):
            ins.parse_pit(b"<html>Access Denied</html>")


class Deals(unittest.TestCase):
    def test_bulk_rows_and_values(self):
        rows = ins.parse_deals(read("bulk.csv"), "bulk")
        self.assertEqual(len(rows), 3)
        rel = rows[0]
        self.assertEqual((rel["symbol"], rel["side"], rel["quantity"]), ("RELIANCE", "BUY", 4500000.0))
        self.assertAlmostEqual(rel["value_crore"], 568.06, places=2)    # 45 lakh x 1,262.35
        self.assertEqual(rel["trade_date"], "2026-10-30")

    def test_comma_prices_parse(self):
        self.assertEqual(ins.num("1,262.35"), 1262.35)
        self.assertEqual(ins.num("₹1,23,456.50"), 123456.5)
        self.assertIsNone(ins.num("-"))

    def test_file_without_a_symbol_header_is_refused(self):
        with self.assertRaises(ins.InsiderError):
            ins.parse_deals("a,b,c\n1,2,3\n", "bulk")

    def test_min_crore_filter(self):
        rows = ins.parse_deals(read("bulk.csv"), "bulk")
        kept = ins._filter(rows, set(), 10, None)
        self.assertEqual({r["symbol"] for r in kept}, {"RELIANCE", "TCS"})     # SMALLCO is about Rs 0.06 crore


class FiiDii(unittest.TestCase):
    def test_groups_and_net(self):
        rows = ins.parse_fiidii(read("fiidii.json"))
        by = {r["group"]: r for r in rows}
        self.assertEqual(by["DII"]["net_crore"], 3150.45)
        self.assertEqual(by["FII"]["net_crore"], -2850.75)

    def test_negative_money_format(self):
        self.assertEqual(ins._cr(-2850.75), "-₹2,850.75 Cr")


class Watch(unittest.TestCase):
    def watch(self, state, extra=()):
        return cli(["watch", "--offline", F, "--symbols", "RELIANCE", "--state", state, "--today", "2026-10-30"]
                   + list(extra) + NONE)

    def test_each_filing_alerts_once(self):
        state = os.path.join(tempfile.mkdtemp(), "insider.json")
        code, out, _ = self.watch(state)
        self.assertEqual(code, 0)
        self.assertIn("Synthetic Promoter Holdings", out)
        self.assertIn("SYNTHETIC MUTUAL FUND", out)
        code, out, _ = self.watch(state)
        self.assertEqual(code, 1)                                    # nothing new the second time
        self.assertIn("Nothing to show", out)

    def test_only_watched_symbols(self):
        state = os.path.join(tempfile.mkdtemp(), "insider.json")
        _, out, _ = self.watch(state)
        self.assertNotIn("TCS", out)
        self.assertNotIn("SMALLCO", out)

    def test_watchlist_flag_marks_the_rows(self):
        d = tempfile.mkdtemp()
        wl = os.path.join(d, "watchlist.json")
        with open(wl, "w") as fh:
            json.dump({"hits": [{"symbol": "RELIANCE"}]}, fh)
        code, out, _ = cli(["pit", "--offline", F, "--watchlist", wl, "--plan", "/none", "--portfolio", "/none"])
        self.assertEqual(code, 0)
        self.assertTrue(all(l.startswith("* ") for l in out.splitlines() if "PIT" in l))

    def test_footer_says_public_not_a_tip(self):
        _, out, _ = cli(["fiidii", "--offline", F])
        self.assertIn("not tips", out.lower().replace("public disclosures, ", ""))
        self.assertIn("Not financial advice", out)

    def test_blocked_nse_is_a_clear_error(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ins.main(["fiidii", "--offline", "/nonexistent-dir"])
        self.assertEqual(code, 2)
        self.assertIn("offline: no file", err.getvalue())

    def test_bad_symbol_refused(self):
        code, _, err = cli(["pit", "--offline", F, "--symbols", "bad symbol!"] + NONE)
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
