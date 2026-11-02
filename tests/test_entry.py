"""trader-entry tests (NSE India). Fixtures in tests/fixtures/trader-entry/ are SYNTHETIC, not market data:

RELIANCE-2026-10-30-5m.json  five-minute bars for Fri 2026-10-30 09:15-09:55 IST in Yahoo's chart JSON v8 shape
                             (currency INR, Asia/Kolkata), plus a live tick at 09:57:45. Opening range 09:15-09:30:
                             high 1254.00, low 1246.50. The 09:35 bar wicks to 1256.50 but closes 1253.90 (not a
                             trigger); the 09:40 bar closes 1255.20 (the trigger, bar 5, closing at 9:45 AM).
kite-RELIANCE-5m.json        the same bars as Kite get_historical_data candles ([ISO IST date, o, h, l, c, v]).
DEMO-1m-synthetic.json       one-minute bars: a wick above the range high, a close AT the high, a null bar, a live tick.
"""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

entry = load("trader-entry", "entry")

EDIR = os.path.join(FIX, "trader-entry")
REL = os.path.join(EDIR, "RELIANCE-2026-10-30-5m.json")
KITE = os.path.join(EDIR, "kite-RELIANCE-5m.json")
DEMO = os.path.join(EDIR, "DEMO-1m-synthetic.json")
DAY = "2026-10-30"
DT = datetime.datetime


def ist(text):
    return entry.parse_now(text)


def at(hhmm, day=DAY):
    return ist("%s %s" % (day, hhmm))


def chart(path=REL, sym="RELIANCE", interval="5m"):
    return entry.get_chart(sym, interval, offline=path)


def ev(hhmm, day=DAY, path=REL, sym="RELIANCE", interval="5m", **kw):
    return entry.evaluate(chart(path, sym, interval), at(hhmm, day), interval=interval, **kw)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = entry.main(argv)
    return code, out.getvalue(), err.getvalue()


class Calendar(unittest.TestCase):
    def test_ist_is_utc_plus_530_all_year(self):
        self.assertEqual(ist("2026-10-30 09:15"), DT(2026, 10, 30, 3, 45))
        self.assertEqual(ist("2026-10-30T09:15:00+05:30"), DT(2026, 10, 30, 3, 45))
        self.assertEqual(ist("2026-10-30T03:45:00Z"), DT(2026, 10, 30, 3, 45))
        self.assertEqual(entry.to_ist(DT(2026, 6, 15, 4, 0)).hour, 9)       # no daylight saving
        self.assertEqual(entry.to_ist(DT(2026, 12, 15, 4, 0)).hour, 9)
        with self.assertRaises(entry.EntryError):
            ist("tomorrow morning")

    def test_holiday_weekend_and_special_session(self):
        self.assertEqual(entry.trading_day(datetime.date(2026, 10, 30)), (True, ""))
        ok, why = entry.trading_day(datetime.date(2026, 11, 10))
        self.assertFalse(ok)
        self.assertEqual(why, "NSE holiday: Diwali-Balipratipada")
        self.assertEqual(entry.trading_day(datetime.date(2026, 10, 31)), (False, "weekend"))
        ok, why = entry.trading_day(datetime.date(2026, 11, 8))             # Muhurat trading, a Sunday
        self.assertFalse(ok)
        self.assertIn("Muhurat", why)

    def test_session_hours_and_the_closing_auction_for_fo_stocks(self):
        d = datetime.date(2026, 10, 30)
        self.assertEqual(entry.session(d), (datetime.time(9, 15), datetime.time(15, 30)))
        self.assertEqual(entry.session(d, True), (datetime.time(9, 15), datetime.time(15, 15)))
        self.assertEqual(entry.session(datetime.date(2026, 7, 31), True)[1], datetime.time(15, 30))  # before 3 Aug 2026
        self.assertIsNone(entry.session(datetime.date(2026, 11, 10)))

    def test_next_open_skips_weekends_and_holidays(self):
        self.assertEqual(entry.next_open(datetime.date(2026, 10, 30)), "2026-11-02 09:15")
        self.assertEqual(entry.next_open(datetime.date(2026, 11, 9)), "2026-11-11 09:15")


class Symbols(unittest.TestCase):
    def test_normalising_and_yahoo_names(self):
        self.assertEqual(entry.norm_symbol("reliance.ns"), "RELIANCE")
        self.assertEqual(entry.norm_symbol("NSE:TCS"), "TCS")
        self.assertEqual(entry.yahoo_symbol("M&M"), "M%26M.NS")
        self.assertEqual(entry.chart_symbol("M&M"), "M&M.NS")
        self.assertEqual(entry.chart_symbol("BAJAJ-AUTO"), "BAJAJ-AUTO.NS")
        self.assertEqual(entry.chart_path("RELIANCE", "1d", "5m"), "/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m")
        with self.assertRaises(entry.EntryError):
            entry.norm_symbol("REL IANCE!")

    def test_offline_folder_lookup_with_an_ampersand(self):
        tmp = tempfile.mkdtemp()
        try:
            with open(REL) as fh:
                d = json.load(fh)
            d["chart"]["result"][0]["meta"]["symbol"] = "M&M.NS"
            with open(os.path.join(tmp, "M&M.json"), "w") as fh:
                json.dump(d, fh)
            self.assertEqual(entry.get_chart("M&M", "5m", offline=tmp)["symbol"], "M&M")
            with self.assertRaises(entry.EntryError):
                entry.get_chart("TCS", "5m", offline=tmp)
        finally:
            shutil.rmtree(tmp)

    def test_a_chart_for_another_stock_is_refused(self):
        with self.assertRaises(entry.EntryError) as cm:
            entry.get_chart("TCS", "5m", offline=REL)
        self.assertIn("holds RELIANCE.NS", str(cm.exception))

    def test_bar_size_must_match(self):
        with self.assertRaises(entry.EntryError):
            entry.get_chart("RELIANCE", "1m", offline=REL)


class OpeningRange(unittest.TestCase):
    def test_before_the_open(self):
        r = ev("09:00")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "pre-open"))
        self.assertIn("the NSE opens at 9:15", r["reason"])
        self.assertIn("range is set at 9:30", r["reason"])

    def test_range_forming(self):
        r = ev("09:20")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "forming"))
        self.assertIn("1 of 3 range bars closed", r["reason"])
        r = ev("09:28")
        self.assertEqual(r["opening_range"]["complete"], False)

    def test_range_set_at_9_30(self):
        r = ev("09:30")
        self.assertEqual(r["opening_range"], {"high": 1254.0, "low": 1246.5, "minutes": 15, "bars": 3, "of": 3,
                                              "complete": True, "avg_volume": 113333})
        self.assertEqual(r["level"], 1254.0)
        self.assertEqual(r["status"], "waiting")

    def test_a_wick_above_the_high_is_not_a_trigger(self):
        r = ev("09:40")                  # the 09:35 bar high 1256.50, close 1253.90
        self.assertEqual(r["status"], "waiting")
        self.assertIsNone(r["trigger"])
        self.assertIn("under the range high", r["reason"])
        self.assertEqual(r["last"]["close"], 1253.9)

    def test_triggered_on_the_first_close_above_the_high(self):
        r = ev("09:45")
        self.assertEqual(r["status"], "triggered")
        t = r["trigger"]
        self.assertEqual((t["time"], t["close_time"], t["bar_start"], t["bar_index"]), ("9:45 AM", "09:45", "09:40", 5))
        self.assertEqual(t["price"], 1255.2)                 # the bar's close, never rounded or moved
        self.assertEqual(t["pct_over"], 0.1)
        self.assertEqual(t["volume_x"], 1.32)
        self.assertEqual(ev("09:55")["trigger"]["bar_index"], 5)      # later bars do not move it

    def test_the_bar_does_not_count_until_it_closes(self):
        r = ev("09:44:30")
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["forming"]["bar_start"], "09:40")
        self.assertEqual(r["forming"]["closes"], "9:45 AM")

    def test_the_live_tick_is_not_a_bar(self):
        r = ev("09:57:45")
        self.assertEqual(r["bars_closed"], 8)                # 09:15..09:50; the 09:55 bar is still forming
        self.assertEqual(r["forming"]["bar_start"], "09:55")

    def test_late_entry_is_called_out(self):
        r = ev("10:30")
        self.assertEqual(r["minutes_since"], 45)
        text = "\n".join(entry.describe(r))
        self.assertIn("45 minutes ago", text)
        self.assertIn("A late entry is a different trade", text)
        self.assertNotIn("moment to get in", text)

    def test_no_entries_before_from_the_config(self):
        cfg = dict(entry.DEFAULTS, no_entries_before="09:50")
        r = ev("09:45", cfg=cfg)
        self.assertEqual(r["status"], "waiting")
        self.assertIn("no entries before 9:50", r["reason"])
        r = ev("09:50", cfg=cfg)                             # the next bar closes above: that one triggers
        self.assertEqual((r["status"], r["trigger"]["bar_index"], r["trigger"]["price"]), ("triggered", 6, 1257.1))

    def test_defaults_are_the_nse_windows(self):
        self.assertEqual(entry.DEFAULTS, {"no_entries_before": "09:30", "no_entries_after": "14:30"})
        self.assertEqual(entry.read_config("/nonexistent/config.json"), entry.DEFAULTS)
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "config.json")
            with open(p, "w") as fh:
                json.dump({"no_entries_after": "25:00"}, fh)
            with self.assertRaises(entry.EntryError):
                entry.read_config(p)
        finally:
            shutil.rmtree(tmp)

    def test_volume_filter(self):
        r = ev("09:45", min_volume_x=2.0)
        self.assertEqual(r["status"], "waiting")
        self.assertIn("under your 2.00x", r["reason"])
        self.assertEqual(ev("09:45", min_volume_x=1.0)["status"], "triggered")

    def test_level_setup(self):
        r = ev("09:45", setup="level", level=1255.0)
        self.assertEqual((r["status"], r["trigger"]["bar_index"], r["level"]), ("triggered", 5, 1255.0))
        r = ev("09:50", setup="level", level=1256.0)         # first close above 1256 is the 09:45 bar
        self.assertEqual(r["trigger"]["bar_index"], 6)
        with self.assertRaises(entry.EntryError):
            ev("09:45", setup="level")


class Expiry(unittest.TestCase):
    def test_waiting_before_the_cutoff_expired_after(self):
        r = ev("14:00", setup="level", level=1300.0)
        self.assertEqual(r["status"], "waiting")
        r = ev("14:35", setup="level", level=1300.0)
        self.assertEqual((r["status"], r["phase"]), ("expired", "after-cutoff"))
        self.assertIn("14:30", r["reason"])
        self.assertEqual(r["no_entries_after"], "14:30")

    def test_fo_stocks_stop_at_15_15(self):
        r = ev("15:20", setup="level", level=1300.0, cas=True)
        self.assertEqual(r["phase"], "after-close")
        self.assertEqual(r["closes"], "15:15")
        self.assertEqual(ev("15:20", setup="level", level=1300.0)["phase"], "after-cutoff")
        cfg = dict(entry.DEFAULTS, no_entries_after="15:20")
        self.assertEqual(ev("14:00", setup="level", level=1300.0, cas=True, cfg=cfg)["no_entries_after"], "15:15")
        self.assertIn("F&O stock (CAS)", "\n".join(entry.describe(ev("14:00", setup="level", level=1300.0, cas=True))))

    def test_holiday_weekend_and_unknown_calendar(self):
        r = ev("10:00", day="2026-11-10")
        self.assertEqual((r["status"], r["phase"]), ("no-data", "closed"))
        self.assertEqual(r["reason"], "the NSE is closed today (NSE holiday: Diwali-Balipratipada)")
        self.assertEqual(r["next_open"], "2026-11-11 09:15")
        r = ev("11:00", day="2026-10-31")
        self.assertIn("weekend", r["reason"])
        r = ev("10:00", day="2027-01-05")
        self.assertFalse(r["calendar_known"])
        self.assertIn("nseindia.com", "\n".join(entry.describe(r)))


class OneMinuteBars(unittest.TestCase):
    def test_close_at_the_high_and_a_wick_do_not_trigger(self):
        r = ev("09:45", path=DEMO, sym="DEMO", interval="1m")
        self.assertEqual(r["opening_range"]["high"], 100.0)
        self.assertEqual(r["status"], "triggered")
        t = r["trigger"]
        self.assertEqual((t["bar_index"], t["price"], t["time"]), (18, 100.05, "9:34 AM"))
        self.assertEqual(r["bars_closed"], 18)               # 19 slots less the null bar; the live tick is dropped
        r = ev("09:32", path=DEMO, sym="DEMO", interval="1m")
        self.assertEqual(r["status"], "waiting")             # the 09:31 close sits AT 100.00

    def test_range_must_be_whole_bars(self):
        with self.assertRaises(entry.EntryError):
            ev("09:45", or_minutes=12)


class KiteCandles(unittest.TestCase):
    def test_saved_kite_candles_trigger_like_yahoo(self):
        c = chart(KITE)
        self.assertEqual(c["source"], "kite")
        r = entry.evaluate(c, at("09:50"))
        self.assertEqual((r["status"], r["trigger"]["price"], r["source"]), ("triggered", 1255.2, "kite"))
        self.assertIn("Kite candles", entry.describe(r)[0])
        self.assertEqual(entry.entry_doc(r, at("09:50"))["source"], "kite")

    def test_a_bare_candle_list_and_a_bad_file(self):
        tmp = tempfile.mkdtemp()
        try:
            with open(KITE) as fh:
                candles = json.load(fh)["candles"]
            p = os.path.join(tmp, "RELIANCE.json")
            with open(p, "w") as fh:
                json.dump(candles, fh)
            self.assertEqual(entry.evaluate(chart(tmp), at("09:50"))["status"], "triggered")
            with open(p, "w") as fh:
                json.dump([["not a date", 1, 2]], fh)
            with self.assertRaises(entry.EntryError):
                chart(tmp)
        finally:
            shutil.rmtree(tmp)


class Words(unittest.TestCase):
    def test_lines_are_in_rupees_and_ist(self):
        text = "\n".join(entry.describe(ev("09:50")))
        self.assertIn("IST", text)
        self.assertIn("₹1255.20", text)
        self.assertIn("₹1254.00", text)
        self.assertIn("TRIGGERED 9:45 AM", text)
        for bad in ("$", "New York", " ET", "NYSE", "cent"):
            self.assertNotIn(bad, text)

    def test_entry_json_contract(self):
        r = ev("09:50")
        d = entry.entry_doc(r, at("09:50"))
        self.assertEqual((d["symbol"], d["status"], d["price"], d["time"], d["bar_index"], d["side"]),
                         ("RELIANCE", "triggered", 1255.2, "9:45 AM", 5, "buy"))
        self.assertEqual((d["exchange"], d["currency"], d["source"], d["cas"]), ("NSE", "INR", "yahoo", False))
        self.assertEqual(d["opening_range"], {"high": 1254.0, "low": 1246.5, "minutes": 15})
        self.assertEqual(d["checked_at"], "2026-10-30T04:20:00Z")


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_check_writes_entry_json(self):
        out = os.path.join(self.tmp, "entry.json")
        code, text, _ = run(["check", "RELIANCE", "--offline", REL, "--now", "2026-10-30 09:50", "--write", out,
                             "--config", os.path.join(self.tmp, "none.json")])
        self.assertEqual(code, 0)
        self.assertIn("TRIGGERED 9:45 AM at ₹1255.20", text)
        with open(out) as fh:
            self.assertEqual(json.load(fh)["price"], 1255.2)
        code, text, _ = run(["check", "RELIANCE", "--offline", REL, "--now", "2026-10-30 09:40"])
        self.assertEqual(code, 1)
        self.assertIn("WAITING", text)

    def test_check_on_a_holiday_and_with_a_lowercase_suffix_symbol(self):
        code, text, _ = run(["check", "reliance.ns", "--offline", REL, "--now", "2026-11-10 10:00"])
        self.assertEqual(code, 1)
        self.assertIn("MARKET CLOSED", text)
        self.assertIn("Next open: 2026-11-11 09:15 IST", text)

    def test_longs_only_and_flag_mistakes(self):
        code, _, err = run(["check", "RELIANCE", "--offline", REL, "--side", "short"])
        self.assertEqual(code, 1)
        self.assertIn("longs only", err)
        code, _, err = run(["check", "RELIANCE", "--offline", REL, "--level", "1260"])
        self.assertEqual(code, 1)
        self.assertIn("--level is for --setup level", err)

    def test_the_cas_flag_reaches_the_json(self):
        code, text, _ = run(["check", "RELIANCE", "--offline", REL, "--now", "2026-10-30 15:20", "--cas",
                             "--setup", "level", "--level", "1300", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(text)["closes"], "15:15")

    def test_watch_replays_the_morning_and_stops_on_the_trigger(self):
        out = os.path.join(self.tmp, "entry.json")
        code, text, _ = run(["watch", "RELIANCE", "--offline", REL, "--now", "2026-10-30 09:20", "--out", out,
                             "--config", os.path.join(self.tmp, "none.json")])
        self.assertEqual(code, 0)
        self.assertIn("IST", text)
        self.assertIn("opening range set: high ₹1254.00, low ₹1246.50", text)
        self.assertIn("TRIGGERED", text)
        with open(out) as fh:
            self.assertEqual(json.load(fh)["bar_index"], 5)

    def test_watch_on_a_holiday(self):
        code, text, _ = run(["watch", "RELIANCE", "--offline", REL, "--now", "2026-11-10 09:20"])
        self.assertEqual(code, 1)
        self.assertIn("market closed today", text)

    def test_watchlist_table(self):
        d = os.path.join(self.tmp, "bars")
        os.makedirs(d)
        shutil.copy(REL, os.path.join(d, "RELIANCE.json"))
        wl = os.path.join(self.tmp, "watchlist.json")
        with open(wl, "w") as fh:
            json.dump({"hits": [{"symbol": "RELIANCE"}, {"symbol": "NOSUCH"}]}, fh)
        code, text, _ = run(["watchlist", "--watchlist", wl, "--offline", d, "--now", "2026-10-30 09:50",
                             "--config", os.path.join(self.tmp, "none.json")])
        self.assertEqual(code, 0)
        self.assertIn("RELIANCE", text)
        self.assertIn("triggered", text)
        self.assertIn("₹1255.20", text)
        self.assertIn("NOSUCH", text)
        self.assertIn("1 of 2 triggered", text)


if __name__ == "__main__":
    unittest.main()
