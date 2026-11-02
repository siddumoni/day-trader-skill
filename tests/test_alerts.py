"""trader-alerts tests (NSE India). Fixtures in tests/fixtures/trader-alerts/ are SYNTHETIC, not market data:

yahoo-RELIANCE|TCS|ITC.json   one-day Yahoo chart answers in rupees (RELIANCE 1266.40, prev close 1244.30)
yahoo-ZZZZ.json               Yahoo's "No data found" 404 body
kite-quotes.json              a Kite get_quotes result: RELIANCE, and SUZLON sitting at its lower circuit limit
plan-reliance.json            the trader/plan.json trader-exit writes for the synthetic RELIANCE morning
"""

import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

alerts = load("trader-alerts", "alerts")

QDIR = os.path.join(FIX, "trader-alerts")
PLAN = os.path.join(QDIR, "plan-reliance.json")
DT = datetime.datetime
UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
DAY = "2026-10-30"


def utc(text):
    return alerts.parse_now(text)


def q(price, prev=None, **kw):
    return dict({"price": price, "prev_close": prev, "source": "yahoo"}, **kw)


def ev(rules, quotes, now, state=None, cas=False):
    return alerts.evaluate(rules, quotes, alerts.market_status(utc(now), cas), state, utc(now))


class Calendar(unittest.TestCase):
    def test_parse_now_is_india_time(self):
        self.assertEqual(utc("2026-10-30 09:15"), DT(2026, 10, 30, 3, 45))
        self.assertEqual(utc("2026-10-30T09:15:00+05:30"), DT(2026, 10, 30, 3, 45))
        self.assertEqual(utc("2026-10-30T03:45:00Z"), DT(2026, 10, 30, 3, 45))
        with self.assertRaises(alerts.AlertError):
            utc("half past nine")

    def test_market_status(self):
        s = alerts.market_status(utc("2026-10-30 10:15"))
        self.assertEqual((s["open"], s["reason"], s["opens"], s["closes"]), (True, "regular session", "09:15", "15:30"))
        self.assertEqual(s["now_ist"], "2026-10-30 10:15")
        s = alerts.market_status(utc("2026-10-30 09:10"))
        self.assertEqual((s["open"], s["reason"]), (False, "before the open"))
        s = alerts.market_status(utc("2026-10-30 15:35"))
        self.assertEqual((s["open"], s["reason"], s["next_open"]), (False, "after the close", "2026-11-02 09:15"))

    def test_f_and_o_stocks_close_at_15_15(self):
        self.assertTrue(alerts.market_status(utc("2026-10-30 15:20"))["open"])
        s = alerts.market_status(utc("2026-10-30 15:20"), cas=True)
        self.assertEqual((s["open"], s["closes"], s["cas"]), (False, "15:15", True))

    def test_holidays_and_weekends(self):
        s = alerts.market_status(utc("2026-11-10 10:00"))
        self.assertFalse(s["open"])
        self.assertIn("Diwali", s["reason"])
        self.assertEqual(s["next_open"], "2026-11-11 09:15")
        self.assertEqual(alerts.market_status(utc("2026-10-31 10:00"))["reason"], "weekend")
        self.assertFalse(alerts.market_status(utc("2026-01-26 10:00"))["open"])        # Republic Day
        self.assertIn("Muhurat", alerts.market_status(utc("2026-11-08 18:00"))["reason"])

    def test_the_holiday_list_covers_2026_only(self):
        self.assertTrue(alerts.market_status(utc("2026-10-30 10:00"))["calendar_known"])
        self.assertFalse(alerts.market_status(utc("2027-01-05 10:00"))["calendar_known"])


class Quotes(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(QDIR, name)) as fh:
            return fh.read()

    def test_yahoo_fixtures_in_rupees(self):
        r = alerts.parse_yahoo(self.read("yahoo-RELIANCE.json"), "RELIANCE")
        self.assertEqual((r["symbol"], r["price"], r["prev_close"], r["open"], r["currency"], r["source"]),
                         ("RELIANCE", 1266.40, 1244.30, 1249.0, "INR", "yahoo"))
        self.assertEqual(alerts.parse_yahoo(self.read("yahoo-ITC.json"), "ITC")["price"], 410.35)

    def test_an_unknown_symbol_is_yahoos_answer(self):
        with self.assertRaises(alerts.AlertError) as cm:
            alerts.parse_yahoo(self.read("yahoo-ZZZZ.json"), "ZZZZ")
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(alerts.AlertError):
            alerts.parse_yahoo("<html>", "X")

    def test_kite_quotes_carry_the_circuit_limits(self):
        text = self.read("kite-quotes.json")
        r = alerts.parse_kite(text, "RELIANCE")
        self.assertEqual((r["price"], r["prev_close"], r["upper_circuit"], r["lower_circuit"], r["source"]),
                         (1262.0, 1244.3, 1368.7, 1119.9, "kite"))
        self.assertEqual(alerts.parse_kite(text, "SUZLON")["lower_circuit"], 50.35)
        with self.assertRaises(alerts.AlertError):
            alerts.parse_kite(text, "TCS")
        ltp = json.dumps({"data": {"NSE:INFY": {"instrument_token": 408065, "last_price": 1500.5}}})   # get_ltp shape
        self.assertEqual(alerts.parse_kite(ltp, "INFY")["price"], 1500.5)

    def test_urls(self):
        self.assertEqual(alerts.yahoo_url("RELIANCE"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=1d")
        self.assertIn("/chart/M%26M.NS?", alerts.yahoo_url("M&M"))
        self.assertIn("/chart/^NSEI?", alerts.yahoo_url("NIFTY"))
        self.assertEqual(alerts.norm_symbol("nse:reliance"), "RELIANCE")
        with self.assertRaises(alerts.AlertError):
            alerts.norm_symbol("not a symbol")

    def test_offline_quotes_and_errors(self):
        qs, errs = alerts.get_quotes(["reliance.ns", "ZZZZ", "TCS", "NOFILE"], offline=QDIR)
        self.assertEqual(sorted(qs), ["RELIANCE", "TCS"])
        self.assertEqual(len(errs), 2)
        self.assertTrue(any("No data found" in e for e in errs))
        self.assertTrue(any("no offline quote file" in e for e in errs))

    def test_offline_kite_combined_file(self):
        qs, errs = alerts.get_quotes(["RELIANCE", "SUZLON"], source="kite", offline=QDIR)
        self.assertEqual((sorted(qs), errs), (["RELIANCE", "SUZLON"], []))
        _, errs = alerts.get_quotes(["RELIANCE"], source="kite")
        self.assertIn("--offline DIR", errs[0])

    def test_offline_gz(self):
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(QDIR, "yahoo-TCS.json"), "rb") as fh, \
                    gzip.open(os.path.join(tmp, "yahoo-TCS.json.gz"), "wb") as out:
                out.write(fh.read())
            self.assertEqual(alerts.get_quotes(["TCS"], offline=tmp)[0]["TCS"]["price"], 4100.0)
        finally:
            shutil.rmtree(tmp)


class FakeNet(object):
    """Stands in for urlopen and sleep: answers is a list of (HTTP code, body)."""

    class Resp(object):
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body.encode()

    def __init__(self, answers):
        self.answers, self.urls, self.agents, self.sleeps = list(answers), [], [], []

    def urlopen(self, req, timeout=20):
        self.urls.append(req.full_url)
        self.agents.append(req.get_header("User-agent"))
        code, body = self.answers.pop(0)
        if code == 200:
            return FakeNet.Resp(body)
        raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(body.encode()))


class Network(unittest.TestCase):
    def setUp(self):
        self._open, self._sleep = alerts._urlopen, alerts._sleep
        self.addCleanup(self.restore)
        with open(os.path.join(QDIR, "yahoo-RELIANCE.json")) as fh:
            self.good = fh.read()

    def restore(self):
        alerts._urlopen, alerts._sleep = self._open, self._sleep

    def net(self, answers):
        n = FakeNet(answers)
        alerts._urlopen, alerts._sleep = n.urlopen, n.sleeps.append
        return n

    def test_query2_first_with_the_user_agent(self):
        n = self.net([(200, self.good)])
        qs, errs = alerts.get_quotes(["RELIANCE"])
        self.assertEqual((qs["RELIANCE"]["price"], errs), (1266.40, []))
        self.assertIn("query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS", n.urls[0])
        self.assertEqual(n.agents, [UA])

    def test_one_retry_with_a_pause_on_429(self):
        n = self.net([(429, ""), (200, self.good)])
        self.assertEqual(alerts.get_quotes(["RELIANCE"])[0]["RELIANCE"]["price"], 1266.40)
        self.assertEqual(n.sleeps, [alerts.BACKOFF])
        self.assertEqual(len(n.urls), 2)

    def test_falls_back_to_query1(self):
        n = self.net([(503, ""), (200, self.good)])
        self.assertEqual(alerts.get_quotes(["RELIANCE"])[0]["RELIANCE"]["price"], 1266.40)
        self.assertIn("query1.finance.yahoo.com", n.urls[1])

    def test_a_404_is_yahoos_answer_and_a_403_is_not_retried(self):
        n = self.net([(404, json.dumps({"chart": {"result": None, "error": {"description": "No data found"}}}))])
        qs, errs = alerts.get_quotes(["ZZZZ"])
        self.assertEqual(qs, {})
        self.assertIn("No data found", errs[0])
        n = self.net([(403, "")])
        qs, errs = alerts.get_quotes(["RELIANCE"])
        self.assertEqual((qs, len(n.urls)), ({}, 1))
        self.assertIn("HTTP 403", errs[0])

    def test_a_pause_between_symbols(self):
        n = self.net([(200, self.good), (200, self.good)])
        alerts.get_quotes(["RELIANCE", "TCS"], pause=0.5)
        self.assertEqual(n.sleeps, [0.5])


class Rules(unittest.TestCase):
    TARGET = {"id": "t", "symbol": "RELIANCE", "type": "target", "price": 1265.10}
    STOP = {"id": "s", "symbol": "RELIANCE", "type": "stop", "price": 1250.25}

    def test_a_target_rings_once_in_rupees(self):
        rings, state = ev([self.TARGET], {"RELIANCE": q(1266.40)}, DAY + " 10:00")
        self.assertEqual(len(rings), 1)
        self.assertIn("RELIANCE is ₹1,266.40: at or above your target ₹1,265.10", rings[0]["message"])
        self.assertEqual(ev([self.TARGET], {"RELIANCE": q(1270)}, DAY + " 10:05", state)[0], [])
        self.assertEqual(ev([self.TARGET], {"RELIANCE": q(1260)}, DAY + " 10:00")[0], [])

    def test_stop_and_rearm(self):
        rings, state = ev([self.STOP], {"RELIANCE": q(1249.9)}, DAY + " 10:00")
        self.assertIn("at or below your stop ₹1,250.25", rings[0]["message"])
        self.assertEqual(ev([self.STOP], {"RELIANCE": q(1252)}, DAY + " 10:05", state)[0], [])
        rule = dict(self.STOP, rearm=True)
        _, s1 = ev([rule], {"RELIANCE": q(1249)}, DAY + " 10:00")
        _, s2 = ev([rule], {"RELIANCE": q(1252)}, DAY + " 10:05", s1)           # crossed back: re-armed
        self.assertEqual(len(ev([rule], {"RELIANCE": q(1249)}, DAY + " 10:10", s2)[0]), 1)

    def test_a_stock_locked_at_its_circuit_says_so(self):
        rule = {"id": "z", "symbol": "SUZLON", "type": "below", "price": 51.0}
        rings, _ = ev([rule], {"SUZLON": q(50.35, 55.95, lower_circuit=50.35, upper_circuit=61.55)}, DAY + " 10:00")
        self.assertIn("LOWER circuit limit (₹50.35)", rings[0]["message"])
        self.assertIn("a sell or a stop may not fill", rings[0]["message"])
        rule = {"id": "u", "symbol": "SUZLON", "type": "above", "price": 60.0}
        rings, _ = ev([rule], {"SUZLON": q(61.55, 55.95, lower_circuit=50.35, upper_circuit=61.55)}, DAY + " 10:00")
        self.assertIn("UPPER circuit limit", rings[0]["message"])
        rings, _ = ev([self.TARGET], {"RELIANCE": q(1266, lower_circuit=1119.9, upper_circuit=1368.7)}, DAY + " 10:00")
        self.assertNotIn("circuit", rings[0]["message"])

    def test_pct_move_from_the_previous_close(self):
        rule = {"id": "m", "symbol": "ITC", "type": "pct_move", "pct": 2}
        rings, state = ev([rule], {"ITC": q(410.35, 420.0)}, DAY + " 10:00")
        self.assertEqual(rings[0]["message"], "ITC moved -2.30% to ₹410.35 (from ₹420.00).")
        self.assertEqual(ev([rule], {"ITC": q(410.35, 420.0)}, DAY + " 11:00", state)[0], [])      # once a day
        self.assertEqual(len(ev([rule], {"ITC": q(410.35, 420.0)}, "2026-11-02 10:00", state)[0]), 1)
        self.assertEqual(ev([dict(rule, direction="up")], {"ITC": q(410.35, 420.0)}, DAY + " 10:00")[0], [])
        self.assertEqual(ev([rule], {"ITC": q(415.0, 420.0)}, DAY + " 10:00")[0], [])

    def test_opening_bell_is_the_nse_open_once_a_day(self):
        rule = {"id": "bell", "type": "opening_bell"}
        self.assertEqual(ev([rule], {}, DAY + " 09:10")[0], [])
        rings, state = ev([rule], {}, DAY + " 09:15")
        self.assertEqual(rings[0]["message"], "Opening bell: the NSE is open (2026-10-30 09:15 IST).")
        self.assertEqual(ev([rule], {}, DAY + " 09:20", state)[0], [])
        self.assertEqual(len(ev([rule], {}, "2026-11-02 09:15", state)[0]), 1)       # Monday
        self.assertEqual(ev([rule], {}, "2026-11-10 09:15")[0], [])                  # Diwali-Balipratipada
        self.assertEqual(ev([rule], {}, "2026-10-31 09:15")[0], [])                  # Saturday

    def test_the_plan_gives_a_stop_a_target_and_a_flat_by(self):
        with open(PLAN) as fh:
            plan = json.load(fh)
        rules = alerts.rules_from_plan(plan)
        self.assertEqual([r["id"] for r in rules], ["reliance-plan-stop", "reliance-plan-target", "reliance-flat-by"])
        self.assertEqual((rules[0]["type"], rules[0]["price"]), ("stop", 1250.25))
        self.assertEqual((rules[1]["type"], rules[1]["price"]), ("target", 1265.10))
        self.assertEqual((rules[2]["type"], rules[2]["at"], rules[2]["label"]), ("time", "15:00", "flat-by exit"))
        self.assertTrue(all(r["date"] == DAY for r in rules))
        self.assertFalse(any("before_close" in r for r in rules))
        self.assertEqual(alerts.rules_from_plan(dict(plan, flat_by=None), "14:45")[2]["at"], "14:45")
        self.assertEqual(alerts.rules_from_plan(dict(plan, flat_by=None))[2]["at"], "15:00")
        self.assertTrue(all(r["cas"] for r in alerts.rules_from_plan(dict(plan, cas=True))))
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan({"symbol": "RELIANCE", "target": 1265.1})

    def test_the_flat_by_rings_once_per_trading_day(self):
        rule = {"id": "f", "symbol": "RELIANCE", "type": "time", "at": "15:00", "label": "flat-by exit"}
        self.assertEqual(ev([rule], {}, DAY + " 14:59")[0], [])
        rings, state = ev([rule], {}, DAY + " 15:00")
        m = rings[0]["message"]
        self.assertIn("Flat-by exit: it is 15:00 IST (set for 15:00)", m)
        self.assertIn("the market closes at 15:30", m)
        self.assertIn("Zerodha squares off MIS at 15:25 and charges Rs 50 + GST", m)
        self.assertEqual(ev([rule], {}, DAY + " 15:10", state)[0], [])
        self.assertEqual(len(ev([rule], {}, "2026-11-02 15:00", state)[0]), 1)
        self.assertEqual(ev([rule], {}, "2026-11-10 15:00")[0], [])                 # a holiday

    def test_an_f_and_o_flat_by_uses_the_earlier_square_off(self):
        rule = {"id": "f", "symbol": "RELIANCE", "type": "time", "at": "15:00", "label": "flat-by exit", "cas": True}
        m = ev([rule], {}, DAY + " 15:00", cas=True)[0][0]["message"]
        self.assertIn("the market closes at 15:15", m)
        self.assertIn("squares off MIS at 15:12", m)
        self.assertIn("the market has closed", ev([rule], {}, DAY + " 15:20", cas=True)[0][0]["message"])

    def test_the_example_rules_are_valid(self):
        with open(os.path.join(ROOT, "trader-alerts", "alerts.example.json")) as fh:
            doc = json.load(fh)
        for r in doc["rules"]:
            alerts.validate(r)
        self.assertEqual({r["type"] for r in doc["rules"]}, {"opening_bell", "stop", "target", "time", "pct_move"})

    def test_bad_rules(self):
        for bad in ({"id": "a", "type": "nonsense"},
                    {"id": "b", "type": "above", "price": 10},
                    {"id": "c", "symbol": "TCS", "type": "above", "price": "ten"},
                    {"id": "d", "symbol": "TCS", "type": "pct_move"},
                    {"id": "e", "type": "time", "at": "25:00"},
                    {"id": "f", "type": "time", "at": "15:00", "date": "30-10-2026"}):
            with self.assertRaises(alerts.AlertError, msg=bad):
                alerts.validate(bad)
        with self.assertRaises(alerts.AlertError) as cm:
            alerts.validate({"id": "e", "type": "time", "at": "25:00"})
        self.assertIn("India time", str(cm.exception))


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.rules = os.path.join(self.tmp, "alerts.json")
        self.state = os.path.join(self.tmp, "state.json")
        self.prices = os.path.join(self.tmp, "prices.json")

    def run_a(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = alerts.main(argv)
        return code, out.getvalue(), err.getvalue()

    def paths(self):
        return ["--rules", self.rules, "--state", self.state]

    def check(self, now, *extra):
        return self.run_a(["check"] + self.paths() + ["--prices-out", self.prices, "--offline", QDIR, "--now", now] + list(extra))

    def test_the_trade_from_the_plan(self):
        code, out, _ = self.run_a(["add", "--from-plan", "--plan", PLAN, "--config", os.path.join(self.tmp, "c.json")] + self.paths())
        self.assertEqual(code, 0)
        with open(self.rules) as fh:
            self.assertEqual(len(json.load(fh)["rules"]), 3)
        code, out, _ = self.check(DAY + " 10:00")
        self.assertEqual(code, 0)
        self.assertIn("NSE OPEN (2026-10-30 10:00 IST, regular session)", out)
        self.assertIn("ALERT RELIANCE is ₹1,266.40: at or above your target ₹1,265.10", out)
        self.assertIn("An alert is information, not an order", out)
        with open(self.prices) as fh:
            p = json.load(fh)
        self.assertEqual((p["prices"]["RELIANCE"], p["currency"], p["exchange"]), (1266.40, "INR", "NSE"))
        code, out, _ = self.check(DAY + " 10:05")                       # already rang
        self.assertEqual(code, 1)
        self.assertIn("Nothing hit. 3 rule(s) watched today, RELIANCE ₹1,266.40", out)
        code, out, _ = self.check(DAY + " 15:00")                       # the flat-by
        self.assertEqual(code, 0)
        self.assertIn("Flat-by exit: it is 15:00 IST", out)

    def test_a_plan_for_another_day_is_not_watched_today(self):
        self.run_a(["add", "--from-plan", "--plan", PLAN, "--config", os.path.join(self.tmp, "c.json")] + self.paths())
        code, out, _ = self.check("2026-11-02 10:00")
        self.assertEqual(code, 1)
        self.assertIn("0 rule(s) watched today", out)

    def test_check_with_an_f_and_o_plan_uses_the_15_15_close(self):
        with open(PLAN) as fh:
            plan = dict(json.load(fh), cas=True)
        pf = os.path.join(self.tmp, "plan.json")
        with open(pf, "w") as fh:
            json.dump(plan, fh)
        self.run_a(["add", "--from-plan", "--plan", pf, "--config", os.path.join(self.tmp, "c.json")] + self.paths())
        code, out, _ = self.check(DAY + " 15:20")
        self.assertIn("NSE closed (2026-10-30 15:20 IST, after the close)", out)

    def test_check_with_kite_quotes_and_the_circuit(self):
        self.run_a(["add", "--symbol", "SUZLON", "--below", "51"] + self.paths())
        code, out, _ = self.check(DAY + " 10:00", "--source", "kite")
        self.assertEqual(code, 0)
        self.assertIn("LOWER circuit limit", out)

    def test_clock_exit_codes_and_words(self):
        code, out, _ = self.run_a(["clock", "--now", DAY + " 10:15"])
        self.assertEqual(code, 0)
        self.assertIn("India 2026-10-30 10:15 IST (Friday): NSE OPEN, regular session.", out)
        self.assertIn("Today's session: 09:15 to 15:30.", out)
        self.assertIn("Zerodha MIS auto square-off: 15:25", out)
        code, out, _ = self.run_a(["clock", "--now", "2026-11-10 10:00"])
        self.assertEqual(code, 1)
        self.assertIn("NSE holiday: Diwali-Balipratipada", out)
        self.assertIn("Next open: 2026-11-11 09:15 IST.", out)
        code, out, _ = self.run_a(["clock", "--now", DAY + " 15:10", "--cas"])
        self.assertEqual(code, 0)
        self.assertIn("15:12", out)
        code, out, _ = self.run_a(["clock", "--now", "2027-01-05 10:00"])
        self.assertIn("nseindia.com", out)

    def test_add_rules(self):
        self.assertEqual(self.run_a(["add", "--at", "15:00", "--label", "flat by", "--symbol", "reliance.ns"] + self.paths())[0], 0)
        self.assertEqual(self.run_a(["add", "--symbol", "TCS", "--above", "4200"] + self.paths())[0], 0)
        self.assertEqual(self.run_a(["add", "--symbol", "INFY", "--stop", "1480", "--cas"] + self.paths())[0], 0)
        self.assertEqual(self.run_a(["add", "--opening-bell"] + self.paths())[0], 0)
        code, _, err = self.run_a(["add", "--symbol", "TCS", "--above", "4200"] + self.paths())
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        code, _, err = self.run_a(["add", "--at", "3pm"] + self.paths())
        self.assertIn("India time like 15:00", err)
        code, out, _ = self.run_a(["list"] + self.paths())
        self.assertEqual(code, 0)
        self.assertIn('"symbol": "RELIANCE"', out)
        with open(self.rules) as fh:
            rules = {r["id"]: r for r in json.load(fh)["rules"]}
        self.assertTrue(rules["infy-stop-1480"]["cas"])
        self.assertNotIn("cas", rules["tcs-above-4200"])

    def test_reset(self):
        self.run_a(["add", "--symbol", "RELIANCE", "--target", "1265.10"] + self.paths())
        self.check(DAY + " 10:00")
        code, out, _ = self.run_a(["reset", "reliance-target-1265.10", "--state", self.state])
        self.assertEqual(code, 0)
        self.assertIn("Re-armed", out)
        self.assertEqual(self.run_a(["reset", "nothing", "--state", self.state])[0], 1)

    def test_quote_in_rupees_and_merge_into_prices_json(self):
        code, out, _ = self.run_a(["quote", "RELIANCE", "--offline", QDIR, "--write", self.prices])
        self.assertEqual(code, 0)
        self.assertIn("RELIANCE     ₹1,266.40 (+1.78% vs prev close ₹1,244.30)", out)
        self.assertIn("Quotes are free, delayed and unofficial", out)
        self.run_a(["quote", "TCS", "--offline", QDIR, "--write", self.prices])
        with open(self.prices) as fh:
            p = json.load(fh)
        self.assertEqual(sorted(p["prices"]), ["RELIANCE", "TCS"])
        self.assertEqual(p["detail"]["TCS"]["currency"], "INR")

    def test_quote_from_saved_kite_quotes(self):
        code, out, _ = self.run_a(["quote", "SUZLON", "RELIANCE", "--source", "kite", "--offline", QDIR])
        self.assertEqual(code, 0)
        self.assertIn("₹50.35", out)
        self.assertIn("[kite]", out)

    def test_quote_holdings_from_portfolio_and_plan(self):
        pf = os.path.join(self.tmp, "portfolio.json")
        with open(pf, "w") as fh:
            json.dump({"positions": [{"ticker": "TCS"}, {"symbol": "ITC"}]}, fh)
        code, out, _ = self.run_a(["quote", "--holdings", "--portfolio", pf, "--plan", PLAN, "--offline", QDIR, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(json.loads(out)["quotes"]), ["ITC", "RELIANCE", "TCS"])

    def test_check_without_rules_and_a_symbol_error(self):
        code, _, err = self.run_a(["check"] + self.paths() + ["--offline", QDIR])
        self.assertEqual(code, 1)
        self.assertIn("no rules", err)
        code, _, err = self.run_a(["quote", "bad symbol!", "--offline", QDIR])
        self.assertEqual(code, 1)

    def test_the_old_us_pieces_are_gone(self):
        self.assertFalse(hasattr(alerts, "NYSE_HOLIDAYS"))
        self.assertFalse(hasattr(alerts, "stooq_url"))
        with open(os.path.join(ROOT, "trader-alerts", "alerts.py"), encoding="utf-8") as fh:
            src = fh.read()
        for bad in ("New York", "NYSE", "eastern", "before_close"):
            self.assertNotIn(bad, src)
        with open(os.path.join(ROOT, "nse_market.py"), "rb") as a, open(os.path.join(ROOT, "trader-alerts", "nse_market.py"), "rb") as b:
            self.assertEqual(a.read(), b.read())


if __name__ == "__main__":
    unittest.main()
