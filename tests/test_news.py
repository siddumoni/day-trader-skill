import contextlib
import datetime
import io
import json
import os
import tempfile
import unittest

from _load import FIX, load

news = load("trader-news", "news")
F = os.path.join(FIX, "trader-news")


def run(extra=(), state=None):
    state = state or os.path.join(tempfile.mkdtemp(), "news.json")
    argv = ["--tickers", "RELIANCE", "--offline", F, "--now", "2026-10-30 15:00", "--state", state] + list(extra)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = news.main(argv)
    return code, out.getvalue(), err.getvalue(), state


class Parsing(unittest.TestCase):
    def test_rss_items(self):
        with open(os.path.join(F, "yahoo-RELIANCE.xml"), "rb") as fh:
            items = news.parse_rss(fh.read(), "RELIANCE")
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["source"], "Yahoo Finance")

    def test_nse_announcements_skip_empty_rows_and_read_india_time(self):
        with open(os.path.join(F, "nse-RELIANCE.json"), "rb") as fh:
            items = news.parse_nse_announcements(fh.read(), "RELIANCE")
        self.assertEqual(len(items), 2)                       # the row with no text is dropped
        self.assertEqual(items[0]["time"].strftime("%H:%M:%S"), "08:35:33")   # 14:05:33 IST
        self.assertEqual(news.to_ist(items[0]["time"]).strftime("%H:%M"), "14:05")

    def test_nse_accepts_wrapped_data_and_rejects_a_block_page(self):
        rows = {"data": [{"symbol": "TCS", "desc": "Dividend", "an_dt": "30-Oct-2026 10:00:00", "seq_id": "1"}]}
        self.assertEqual(len(news.parse_nse_announcements(json.dumps(rows), "TCS")), 1)
        with self.assertRaises(news.NewsError):
            news.parse_nse_announcements(b"<html>Access Denied</html>", "TCS")

    def test_missing_time_is_kept_as_unknown(self):
        row = [{"symbol": "ITC", "desc": "Board meeting intimation", "seq_id": "7"}]
        self.assertIsNone(news.parse_nse_announcements(json.dumps(row), "ITC")[0]["time"])

    def test_chart_move_is_in_rupees_and_percent(self):
        with open(os.path.join(F, "chart-RELIANCE.json"), "rb") as fh:
            m = news.parse_chart(fh.read(), "RELIANCE")
        self.assertEqual(m["price"], 1285.5)
        self.assertEqual(m["pct"], 2.96)


class Tagging(unittest.TestCase):
    def tag(self, title, body=""):
        it = {"id": "x", "ticker": "RELIANCE", "kind": "filing", "title": title, "summary": body,
              "link": "", "time": None}
        return news.tag([it], {"RELIANCE": "Reliance Industries"})[0]

    def test_results_dividend_board(self):
        it = self.tag("Outcome of Board Meeting", "financial results and interim dividend, record date")
        self.assertTrue({"results", "board meeting", "dividend"} <= set(it["tags"]))
        self.assertTrue(it["important"])

    def test_pledge_and_sebi(self):
        it = self.tag("Disclosure under SEBI regulations", "pledge of shares by promoter")
        self.assertIn("pledge", it["tags"])
        self.assertIn("regulatory", it["tags"])

    def test_order_win_and_rating(self):
        self.assertIn("order win", self.tag("Company bags order worth Rs 500 crore")["tags"])
        self.assertIn("rating", self.tag("CRISIL upgrades long-term rating")["tags"])

    def test_nse_prefix_and_dot_ns_mentions_touch_the_symbol(self):
        it = {"id": "y", "ticker": "TCS", "kind": "headline", "title": "Brokers like NSE:RELIANCE and INFY.NS",
              "summary": "", "link": "", "time": None}
        out = news.tag([it], {"RELIANCE": None, "INFY": None, "TCS": None})[0]
        self.assertEqual(out["touches"], ["INFY", "RELIANCE", "TCS"])

    def test_us_dollar_style_mentions_are_gone(self):
        it = {"id": "z", "ticker": "TCS", "kind": "headline", "title": "$RELIANCE rallies", "summary": "",
              "link": "", "time": None}
        self.assertEqual(news.tag([it], {"RELIANCE": None, "TCS": None})[0]["touches"], ["TCS"])


class Symbols(unittest.TestCase):
    def test_ampersand_symbol_is_url_safe(self):
        self.assertEqual(news.nse_market.yahoo_symbol("M&M"), "M%26M.NS")

    def test_bad_symbol_is_refused(self):
        with self.assertRaises(news.NewsError):
            news._norm("not a symbol!")


class Run(unittest.TestCase):
    def test_new_items_in_window_exit_zero(self):
        code, _, _, _ = run(["--json", "--no-moves"])
        self.assertEqual(code, 0)

    def test_output_is_ist_and_rupees_newest_first(self):
        _, out, _, _ = run()
        self.assertIn("IST", out)
        self.assertIn("₹1,285.50", out)
        self.assertIn("MOVING" if news.MOVE_PCT <= 2.96 else "vs yesterday", out)
        self.assertNotIn(" ET", out)
        self.assertNotIn("Old story from last week", out)              # outside the 48 hour window
        self.assertLess(out.index("14:05 IST"), out.index("09:40 IST"))

    def test_seen_items_do_not_repeat(self):
        code, _, _, state = run(["--no-moves"])
        self.assertEqual(code, 0)
        code, out, _, _ = run(["--no-moves"], state=state)
        self.assertEqual(code, 1)                                       # nothing new the second time
        self.assertIn("Nothing new", out)

    def test_no_symbols_is_an_error(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = news.main(["--offline", F, "--watchlist", "/none", "--plan", "/none", "--portfolio", "/none"])
        self.assertEqual(code, 2)
        self.assertIn("no symbols", err.getvalue())

    def test_blocked_nse_is_a_note_not_a_crash(self):
        class Blocked(news.Fetcher):
            def get(self, url, name, nse=False):
                if nse:
                    raise news.NewsError("HTTP 403 for https://www.nseindia.com/api/corporate-announcements")
                return super().get(url, name, nse)
        errors = []
        items = news.collect(Blocked(offline=F), {"RELIANCE": None}, nse=True, errors=errors)
        self.assertTrue(items)                                          # headlines still ran
        self.assertTrue(any("NSE announcements" in e and "403" in e for e in errors))

    def test_us_machinery_is_gone(self):
        self.assertFalse(hasattr(news, "eastern_offset"))
        self.assertFalse(hasattr(news, "cik_map"))


if __name__ == "__main__":
    unittest.main()
