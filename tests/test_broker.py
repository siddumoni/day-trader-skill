"""trader-broker tests (Zerodha Kite, advisory tickets). Orders come from the real trader-budget and the real
trader-risk on the SYNTHETIC RELIANCE morning. Nothing here talks to a broker: ticket.py cannot."""

import copy
import json
import os
import re
import tempfile
import unittest

import _synth as s
from _load import ROOT, load

ticket = load("trader-broker", "ticket")
check = load("trader-risk", "check")
NOW = "2026-10-30 09:51"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.orders, self.cfg, self.plan, _ = s.make_orders(self.tmp)
        self.risk = os.path.join(self.tmp, "risk.json")
        self.state = os.path.join(self.tmp, "broker.json")
        self.pf = os.path.join(self.tmp, "portfolio.json")
        self.run_risk()

    def run_risk(self, now=NOW):
        s.quiet(check.main, ["--orders", self.orders, "--config", self.cfg, "--now", now, "--out", self.risk,
                             "--portfolio", os.path.join(self.tmp, "none.json"),
                             "--state", os.path.join(self.tmp, "nostate.json")])

    def tk(self, cmd, *args, now=NOW, cfg=None):
        if cmd == "log":                       # log only takes the state file and the clock
            return s.quiet(ticket.main, ["log", "--state", self.state, "--now", now] + list(args))
        base = ["--orders", self.orders, "--config", cfg or self.cfg, "--risk", self.risk,
                "--state", self.state, "--now", now]
        extra = {"result": ["--portfolio", self.pf]}.get(cmd, [])
        return s.quiet(ticket.main, [cmd] + base + extra + list(args))

    def say_yes(self, reply="yes"):
        return self.tk("approve", "--index", "1", "--reply", reply)

    def state_rows(self):
        with open(self.state) as fh:
            return json.load(fh)["orders"]


class Show(Base):
    def test_ready_after_risk_pass_with_the_exact_place_order_json(self):
        code, out, _ = self.tk("show")
        self.assertEqual(code, 0)
        self.assertIn("READY: trader-risk PASS", out)
        self.assertIn("ADVISORY (paper)", out)
        self.assertIn("BUY 59 RELIANCE (NSE, MIS), limit at ₹1,255.20", out)
        self.assertIn('"tradingsymbol": "RELIANCE"', out)
        self.assertIn('"order_type": "SL"', out)
        self.assertIn("Kite does not link the stop and the target", out)
        self.assertNotIn("$", out)

    def test_our_notes_are_never_in_the_tool_input(self):
        _, out, _ = self.tk("show", "--index", "1", "--json")
        t = json.loads(out)
        for k in ("why", "plan", "role", "exit_orders"):
            self.assertNotIn(k, t["tool_input"])
        self.assertEqual(t["tool"], "place_order")
        self.assertEqual(len(t["exit_tool_inputs"]), 2)
        self.assertTrue(t["ready"])

    def test_not_ready_without_a_risk_check(self):
        os.remove(self.risk)
        code, out, _ = self.tk("show")
        self.assertEqual(code, 1)
        self.assertIn("NOT READY: trader-risk has not checked", out)

    def test_blocked_orders_are_not_ready(self):
        self.run_risk(now="2026-10-30 14:45")
        code, out, _ = self.tk("show")
        self.assertEqual(code, 1)
        self.assertIn("BLOCKED by trader-risk", out)

    def test_a_risk_check_from_another_day_is_stale(self):
        _, out, _ = self.tk("show", "--today", "2026-10-31")
        self.assertIn("is from 2026-10-30, not today", out)

    def test_an_order_changed_after_the_check_is_not_ready(self):
        with open(self.orders) as fh:
            doc = json.load(fh)
        doc["orders"][0]["price"] = 1255.25
        s.write_json(self.orders, doc)
        code, out, _ = self.tk("show")
        self.assertEqual(code, 1)
        self.assertIn("not in the last risk check", out)

    def test_live_mode_says_so_and_asks_for_the_card(self):
        cfg = s.make_config(self.tmp, paper=False)
        _, out, _ = self.tk("show", cfg=cfg)
        self.assertIn("LIVE ACCOUNT, REAL MONEY", out)
        self.assertIn("Allow once", out)


class Yes(Base):
    def test_only_an_explicit_yes_for_this_order_counts(self):
        for reply, why in (("ok go ahead", "starts with"), ("sure", "starts with"), ("yes to all", "batch"),
                           ("yes but wait", "doubt"), ("yes buy 100 shares", "100"), ("yes?", "question"),
                           ("", "empty"), ("no", "starts with")):
            code, out, _ = self.say_yes(reply)
            self.assertEqual(code, 1, reply)
            self.assertIn("Not recorded", out, reply)
            self.assertIn(why, out, reply)
        self.assertFalse(os.path.exists(self.state), "a refused yes must record nothing")

    def test_a_yes_that_names_a_different_stock_is_refused(self):
        order = json.load(open(self.orders))["orders"][0]
        ok, why = ticket.parse_yes("yes TCS", order, ["RELIANCE", "TCS"])
        self.assertFalse(ok)
        self.assertIn("TCS", why)

    def test_a_yes_that_repeats_the_right_numbers_is_fine(self):
        self.assertEqual(self.say_yes("Yes, 59 RELIANCE")[0], 0)

    def test_the_yes_is_recorded_word_for_word_with_the_fingerprint(self):
        code, out, _ = self.say_yes("yes")
        self.assertEqual(code, 0)
        rec = self.state_rows()[0]
        self.assertEqual((rec["status"], rec["reply"], rec["symbol"], rec["side"], rec["kind"]),
                         ("approved", "yes", "RELIANCE", "buy", "entry"))
        self.assertEqual(len(rec["fingerprint"]), 16)
        self.assertEqual(len(rec["exit_fingerprints"]), 2)

    def test_advisory_mode_does_not_ask_claude_to_place_it(self):
        _, out, _ = self.say_yes()
        self.assertIn("Claude does NOT place this", out)
        self.assertNotIn("Now call place_order", out)

    def test_live_mode_hands_over_the_exact_call(self):
        cfg = s.make_config(self.tmp, paper=False)
        code, out, _ = s.quiet(ticket.main, ["approve", "--orders", self.orders, "--config", cfg, "--risk", self.risk,
                                             "--state", self.state, "--now", NOW, "--index", "1", "--reply", "yes"])
        self.assertEqual(code, 0)
        self.assertIn("Now call place_order with exactly", out)
        self.assertIn("Allow once / Deny", out)

    def test_one_yes_per_order_a_second_is_refused(self):
        self.say_yes()
        code, _, err = self.say_yes()
        self.assertEqual(code, 1)
        self.assertIn("not ready", err)

    def test_nothing_is_approved_unless_ready(self):
        self.run_risk(now="2026-10-30 14:45")
        code, _, err = self.say_yes()
        self.assertEqual(code, 1)
        self.assertIn("not ready", err)
        self.assertFalse(os.path.exists(self.state))

    def test_decline_records_a_no_and_sends_nothing(self):
        code, out, _ = self.tk("decline", "--index", "1", "--reply", "no thanks")
        self.assertEqual(code, 0)
        self.assertEqual(self.state_rows()[0]["status"], "declined")
        self.assertIn("Nothing was sent", out)


class AfterTheYes(Base):
    def test_a_result_needs_an_approval_first(self):
        code, _, err = self.tk("result", "--index", "1", "--status", "placed")
        self.assertEqual(code, 1)
        self.assertIn("no yes recorded", err)

    def test_placed_with_a_fill_updates_cash_shares_and_charges(self):
        s.write_json(self.pf, {"cash": 100000.0, "positions": []})
        self.say_yes()
        code, out, _ = self.tk("result", "--index", "1", "--status", "placed", "--broker-id", "2610300000123",
                               "--filled-qty", "59", "--filled-price", "1255.20", "--charges", "36.50")
        self.assertEqual(code, 0)
        pf = json.load(open(self.pf))
        self.assertEqual(pf["positions"][0]["shares"], 59)
        self.assertAlmostEqual(pf["cash"], 100000 - 59 * 1255.2 - 36.5, places=2)
        rows = self.state_rows()
        self.assertEqual([r["status"] for r in rows], ["approved", "placed", "filled"])
        self.assertEqual(rows[1]["broker_order_id"], "2610300000123")
        self.assertEqual(rows[1]["kind"], "entry")
        self.assertEqual(rows[2]["charges"], 36.5)

    def test_placed_records_feed_the_risk_check_the_way_it_reads_them(self):
        self.say_yes()
        self.tk("result", "--index", "1", "--status", "placed")
        placed = check.placed_today(json.load(open(self.state)), "2026-10-30")
        self.assertEqual(len(placed), 1)
        self.assertTrue(check.rec_is_entry(placed[0]))
        self.assertGreater(placed[0]["max_loss"], 0)

    def test_protect_waits_for_a_placed_entry_then_shows_the_stop_first(self):
        self.say_yes()
        code, _, err = self.tk("protect", "--index", "1")
        self.assertEqual(code, 1)
        self.assertIn("not recorded as placed", err)
        self.tk("result", "--index", "1", "--status", "placed")
        code, out, _ = self.tk("protect", "--index", "1")
        self.assertEqual(code, 0)
        self.assertLess(out.index("STOP:"), out.index("TARGET:"))
        self.assertIn('"trigger_price": 1250.25', out)
        self.assertIn("cancel the other", out)
        self.assertIn("Be out by 15:00", out)

    def test_protect_refuses_a_leg_changed_after_the_yes(self):
        self.say_yes()
        self.tk("result", "--index", "1", "--status", "placed")
        doc = json.load(open(self.orders))
        doc["orders"][0]["exit_orders"][0]["trigger_price"] = 1240.0
        s.write_json(self.orders, doc)
        code, _, err = self.tk("protect", "--index", "1", "--which", "stop")
        self.assertEqual(code, 1)
        self.assertIn("changed since you said yes", err)

    def test_a_denied_card_is_recorded_not_placed(self):
        self.say_yes()
        self.tk("result", "--index", "1", "--status", "denied")
        self.assertEqual(self.state_rows()[-1]["status"], "denied")
        self.assertEqual(check.placed_today(json.load(open(self.state)), "2026-10-30"), [])


class Fills(Base):
    def test_fill_estimates_zerodha_charges_when_none_is_given(self):
        s.write_json(self.pf, {"cash": 100000.0, "positions": []})
        code, out, _ = s.quiet(ticket.main, ["fill", "--symbol", "RELIANCE", "--side", "buy", "--qty", "59",
                                             "--price", "1255.20", "--portfolio", self.pf, "--state", self.state,
                                             "--config", self.cfg, "--now", NOW])
        self.assertEqual(code, 0)
        pf = json.load(open(self.pf))
        self.assertLess(pf["cash"], 100000 - 59 * 1255.2)             # charges came off as well
        self.assertIn("charges", out)

    def test_selling_more_than_held_is_refused(self):
        s.write_json(self.pf, {"cash": 1000.0, "positions": [{"ticker": "RELIANCE", "shares": 10, "lots": [
            {"id": "RELIANCE-1", "date": "2026-10-30", "shares": 10, "cost": 12552.0}]}]})
        code, _, err = s.quiet(ticket.main, ["fill", "--symbol", "RELIANCE", "--side", "sell", "--qty", "59",
                                             "--price", "1260", "--portfolio", self.pf, "--state", self.state,
                                             "--config", self.cfg, "--now", NOW])
        self.assertEqual(code, 1)
        self.assertIn("holds", err)


class Flatten(Base):
    def flat(self, *args):
        return s.quiet(ticket.main, ["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--config", self.cfg,
                                     "--plan", self.plan, "--portfolio", self.pf, "--now", "2026-10-30 14:55"]
                       + list(args))

    def test_builds_a_sell_mis_ticket_for_the_held_quantity(self):
        s.write_json(self.pf, {"cash": 1.0, "positions": [{"ticker": "RELIANCE", "shares": 59, "lots": []}]})
        code, out, _ = self.flat()
        self.assertEqual(code, 0)
        o = json.load(open(self.orders))["orders"][0]
        self.assertEqual((o["transaction_type"], o["quantity"], o["product"], o["order_type"], o["exchange"]),
                         ("SELL", 59, "MIS", "MARKET", "NSE"))
        self.assertLessEqual(len(o["tag"]), 20)
        self.assertIn("flat by 15:00 India time", out)
        self.assertIn("Rs 50 + GST", out)                               # Zerodha's auto square-off fee

    def test_an_explicit_quantity_must_be_whole_shares(self):
        code, _, err = self.flat("--qty", "12.5")
        self.assertEqual(code, 1)
        self.assertIn("whole number", err)

    def test_nothing_to_flatten(self):
        s.write_json(self.pf, {"cash": 1.0, "positions": []})
        code, _, err = self.flat()
        self.assertEqual(code, 1)
        self.assertIn("nothing to flatten", err)

    def test_a_bad_symbol_is_refused(self):
        code, _, err = s.quiet(ticket.main, ["flatten", "--symbol", "not valid!", "--qty", "5",
                                             "--orders", self.orders])
        self.assertEqual(code, 1)

    def test_the_flatten_ticket_passes_the_risk_check_when_you_hold_it(self):
        pf = {"cash": 25000.0, "positions": [{"ticker": "RELIANCE", "shares": 59, "lots": [
            {"id": "RELIANCE-1", "date": "2026-10-30", "shares": 59, "cost": 74056.8}]}]}
        s.write_json(self.pf, pf)
        self.flat()
        code, out, _ = s.quiet(check.main, ["--orders", self.orders, "--config", self.cfg, "--now", "2026-10-30 15:05",
                                            "--portfolio", self.pf, "--state", os.path.join(self.tmp, "n.json"),
                                            "--out", self.risk, "--json"])
        self.assertEqual(json.loads(out)["results"][0]["verdict"], "PASS")


class Log(Base):
    def test_log_lists_the_days_decisions(self):
        self.say_yes()
        code, out, _ = self.tk("log")
        self.assertEqual(code, 0)
        self.assertIn("approved", out)
        self.assertIn('reply "yes"', out)
        self.assertIn("+05:30", out)                                    # India time stamps

    def test_empty_log(self):
        code, out, _ = self.tk("log")
        self.assertEqual(code, 1)
        self.assertIn("No decisions", out)


class NoBrokerCode(unittest.TestCase):
    def test_the_script_has_no_way_to_reach_a_broker_or_the_network(self):
        with open(os.path.join(ROOT, "trader-broker", "ticket.py"), encoding="utf-8") as fh:
            src = fh.read()
        for banned in ("urllib", "requests", "http.client", "socket", "subprocess", "os.system"):
            self.assertNotIn("import " + banned, src, banned)
        self.assertFalse(re.search(r"^def (place|send|submit|execute)", src, re.M))

    def test_us_machinery_is_gone(self):
        for name in ("ALPACA_FIELDS", "eastern_offset", "to_eastern", "ny_iso"):
            self.assertFalse(hasattr(ticket, name), name)


if __name__ == "__main__":
    unittest.main()
