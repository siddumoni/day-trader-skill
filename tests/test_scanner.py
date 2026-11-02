"""trader-scanner tests (NSE India). All data is SYNTHETIC and built on the fly: an EQUITY_L.csv in NSE's shape,
5-minute sessions for RELIANCE (breaks its opening range at 09:30) and TCS (does not), and daily bars with a
20-day-high breakout (RELIANCE), a long slide (TCS) and a flat stock (ITC)."""

import datetime
import json
import os
import tempfile
import unittest

import _synth as s
from _load import ROOT, load

scan = load("trader-scanner", "scan")
nm = s.nm

EQUITY_L = """SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE
RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10,1,INE002A01018,10
TCS,Tata Consultancy Services Limited,EQ,25-AUG-2004,1,1,INE467B01029,1
M&M,Mahindra & Mahindra Limited,EQ,01-JAN-1996,5,1,INE101A01026,5
SOMEBE,Some BE Series Ltd,BE,01-JAN-2010,10,1,INE000000001,10
GOLDETF,Gold ETF,EQ,01-JAN-2010,10,1,INE000000002,10
"""


def trading_days(n, end=datetime.date(2026, 10, 29)):
    d, out = end, []
    while len(out) < n:
        if nm.trading_day(d)[0]:
            out.append(d)
        d -= datetime.timedelta(days=1)
    return sorted(out)


def daily(sym, closes, vol=2000000):
    rows = []
    for d, c in zip(trading_days(len(closes)), closes):
        t = datetime.datetime.combine(d, datetime.time(9, 15))
        rows.append([t.isoformat(), c, c * 1.004, c * 0.996, c, vol])
    return s.chart(sym, rows, prev_close=closes[-2], interval="1d")


def run(argv):
    code, out, err = s.quiet(scan.main, argv)
    return code, out, err


class Universe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "uni"))
        with open(os.path.join(self.tmp, "uni", "EQUITY_L.csv"), "w") as fh:
            fh.write(EQUITY_L)

    def test_only_equity_series_shares_survive(self):
        kept, counts = scan.filter_equity(EQUITY_L) if hasattr(scan, "filter_equity") else (None, None)
        code, out, err = run(["universe", "--offline", os.path.join(self.tmp, "uni"), "--no-write", "--json"])
        self.assertEqual(code, 0, err)
        d = json.loads(out)
        self.assertEqual(d["count"], 3)                       # RELIANCE, TCS, M&M
        self.assertEqual(d["dropped"]["series BE"], 1)
        self.assertEqual(d["dropped"]["ETF or fund (by name)"], 1)

    def test_universe_file_is_written_with_names(self):
        out_path = os.path.join(self.tmp, "universe.txt")
        code, _, err = run(["universe", "--offline", os.path.join(self.tmp, "uni"), "--out", out_path])
        self.assertEqual(code, 0, err)
        lines = open(out_path).read().splitlines()
        syms = [l.split("|")[0] for l in lines if l and not l.startswith("#")]
        self.assertEqual(syms, ["M&M", "RELIANCE", "TCS"])
        self.assertTrue(any("Mahindra" in l for l in lines))

    def test_no_nasdaq_machinery_is_left(self):
        for name in ("NASDAQ_URL", "NYSE_HOLIDAYS", "eastern_offset"):
            self.assertFalse(hasattr(scan, name), name)


class Orb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bundle = os.path.join(self.tmp, "bundle.json")
        s.write_json(self.bundle, {
            "RELIANCE": s.chart("RELIANCE", s.session_bars(breakout=True)),
            "TCS": s.chart("TCS", s.session_bars(base=3900.0, breakout=False), prev_close=3895)})

    def orb(self, *extra, now="2026-10-30 09:50"):
        code, out, err = run(["orb", "--offline", self.bundle, "--now", now, "--min-price", "0", "--no-write",
                              "--json"] + list(extra))
        return code, (json.loads(out) if out.strip().startswith("{") else None), out + err

    def test_finds_the_breakout_and_skips_the_stock_that_did_not_break(self):
        code, d, _ = self.orb()
        self.assertEqual(code, 0)
        self.assertEqual([h["symbol"] for h in d["hits"]], ["RELIANCE"])
        h = d["hits"][0]
        self.assertEqual((h["setups"], h["at"], h["bar_start"]), (["orb"], "09:35", "09:30"))
        self.assertEqual((h["or_high"], h["or_low"]), (1250.7, 1248.7))
        self.assertEqual((d["exchange"], d["currency"], d["as_of"]), ("NSE", "INR", "2026-10-30 09:50 IST"))

    def test_the_opening_range_runs_from_the_0915_open(self):
        _, d, _ = self.orb()
        self.assertEqual(d["or_minutes"], 15)
        self.assertEqual(d["hits"][0]["bar_index"], 3)          # bars 1-3 are 09:15, 09:20, 09:25

    def test_nothing_before_the_range_is_set(self):
        code, d, text = self.orb(now="2026-10-30 09:25")
        self.assertNotEqual(code, 0)
        self.assertIsNone(d)

    def test_refuses_on_an_nse_holiday_and_a_weekend(self):
        for now in ("2026-11-10 10:00", "2026-10-31 10:00"):
            code, d, text = self.orb(now=now)
            self.assertNotEqual(code, 0, now)

    def test_the_min_price_filter(self):
        code, d, _ = self.orb("--min-price", "2000")
        self.assertEqual(d["hits"], [])                         # RELIANCE at 1,255 is under Rs 2,000

    def test_f_and_o_stocks_are_marked_for_the_closing_auction(self):
        fno = os.path.join(self.tmp, "fno.txt")
        open(fno, "w").write("RELIANCE\n")
        _, d, _ = self.orb("--fno-list", fno)
        h = d["hits"][0]
        self.assertTrue(h.get("fno"))
        self.assertTrue(h.get("cas"))

    def test_the_watchlist_it_writes_feeds_the_other_skills(self):
        out = os.path.join(self.tmp, "watchlist.json")
        code, _, err = run(["orb", "--offline", self.bundle, "--now", "2026-10-30 09:50", "--min-price", "0",
                            "--out", out])
        self.assertEqual(code, 0, err)
        news = load("trader-news", "news")
        self.assertEqual(sorted(news.watch_from_files("/none", out, "/none")), ["RELIANCE"])


class Daily(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bundle = os.path.join(self.tmp, "daily.json")
        s.write_json(self.bundle, {
            "RELIANCE": daily("RELIANCE", [1200 + (i % 5) for i in range(59)] + [1240]),
            "TCS": daily("TCS", [4000 - 12 * i for i in range(60)]),
            "ITC": daily("ITC", [500 + (i % 3) for i in range(60)])})

    def daily(self, *extra):
        code, out, err = run(["daily", "--offline", self.bundle, "--now", "2026-10-30 08:30", "--min-price", "0",
                              "--no-write", "--json"] + list(extra))
        return code, json.loads(out), err

    def test_breakout_and_oversold_are_found_and_the_flat_stock_is_not(self):
        code, d, _ = self.daily("--min-value-cr", "0")
        by = {h["symbol"]: h for h in d["hits"]}
        self.assertEqual(by["RELIANCE"]["setups"], ["breakout_20d"])
        self.assertEqual(by["TCS"]["setups"], ["oversold"])
        self.assertNotIn("ITC", by)

    def test_traded_value_is_in_rupee_crore(self):
        _, d, _ = self.daily("--min-value-cr", "0")
        rel = next(h for h in d["hits"] if h["symbol"] == "RELIANCE")
        self.assertEqual(rel["value_basis"], "20-day average")
        self.assertAlmostEqual(rel["value_cr"], 240.76, delta=2.0)       # about 20 lakh shares x Rs 1,205

    def test_illiquid_stocks_are_dropped_by_the_traded_value_floor(self):
        s.write_json(self.bundle, {"RELIANCE": daily("RELIANCE", [1200 + (i % 5) for i in range(59)] + [1240],
                                                    vol=1000)})            # about Rs 0.12 crore a day
        _, d, _ = self.daily()                                             # default floor: Rs 10 crore
        self.assertEqual(d["hits"], [])
        self.assertEqual(d["counts"]["illiquid"], 1)

    def test_too_little_history_is_reported_not_guessed(self):
        s.write_json(self.bundle, {"RELIANCE": daily("RELIANCE", [1200, 1210, 1220])})
        _, d, _ = self.daily("--min-value-cr", "0")
        self.assertEqual(d["hits"], [])
        self.assertEqual(d["counts"]["short"], 1)


class Symbols(unittest.TestCase):
    def test_an_ampersand_symbol_is_url_safe(self):
        self.assertEqual(nm.yahoo_symbol("M&M"), "M%26M.NS")

    def test_the_scanner_text_has_no_us_market_left(self):
        with open(os.path.join(ROOT, "trader-scanner", "scan.py"), encoding="utf-8") as fh:
            src = fh.read()
        for word in ("nasdaqtrader", "NYSE", "New York", "ADR"):
            self.assertNotIn(word, src, word)


if __name__ == "__main__":
    unittest.main()
