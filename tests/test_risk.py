"""trader-risk tests (NSE India, Kite orders). Orders come from the real trader-budget on the SYNTHETIC
RELIANCE morning: BUY 59 at 1255.20, stop 1250.25, target 1270.05, max loss about Rs 365 with charges."""

import copy
import json
import os
import tempfile
import unittest

import _synth as s
from _load import load

check = load("trader-risk", "check")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.orders, self.cfg, self.plan, _ = s.make_orders(self.tmp)
        with open(self.orders) as fh:
            self.doc = json.load(fh)

    def run_check(self, now="2026-10-30 09:51", mutate=None, cfg=None, state=None, portfolio=None):
        doc = copy.deepcopy(self.doc)
        if mutate:
            mutate(doc["orders"])
        orders = os.path.join(self.tmp, "o.json")
        s.write_json(orders, doc)
        cfgp = s.make_config(self.tmp, **(cfg or {}))
        statep = os.path.join(self.tmp, "state.json")
        s.write_json(statep, state or {"orders": []})
        pf = os.path.join(self.tmp, "pf.json")
        if portfolio is not None:
            s.write_json(pf, portfolio)
        code, out, err = s.quiet(check.main, [
            "--orders", orders, "--config", cfgp, "--now", now, "--state", statep, "--json",
            "--portfolio", pf if portfolio is not None else os.path.join(self.tmp, "none.json"),
            "--out", os.path.join(self.tmp, "risk.json")])
        res = json.loads(out)
        return code, res, res["results"][0]

    def reasons(self, r):
        return " | ".join(r["reasons"]).lower()


class Verdicts(Base):
    def test_the_sized_order_passes(self):
        code, res, r = self.run_check()
        self.assertEqual((code, r["verdict"]), (0, "PASS"))
        self.assertEqual((res["pass"], res["block"]), (1, 0))
        self.assertLessEqual(r["max_loss"], 500)

    def test_records_verdicts_with_a_fingerprint_and_the_date(self):
        self.run_check()
        with open(os.path.join(self.tmp, "risk.json")) as fh:
            rec = json.load(fh)
        self.assertEqual(rec["date"], "2026-10-30")
        self.assertEqual(len(rec["results"][0]["fingerprint"]), 16)

    def test_fingerprint_matches_the_broker_script(self):
        broker = load("trader-broker", "ticket")
        o = self.doc["orders"][0]
        self.assertEqual(check.fingerprint(o), broker.fingerprint(o))

    def test_fingerprint_changes_with_the_price_but_not_with_our_notes(self):
        o = self.doc["orders"][0]
        base = check.fingerprint(o)
        o2 = copy.deepcopy(o)
        o2["why"], o2["plan"]["setup"] = "different words", "other"
        self.assertEqual(check.fingerprint(o2), base)
        o3 = copy.deepcopy(o)
        o3["price"] = 1255.25
        self.assertNotEqual(check.fingerprint(o3), base)


class Window(Base):
    def test_blocked_after_the_entry_window(self):
        code, _, r = self.run_check(now="2026-10-30 14:45")
        self.assertEqual((code, r["verdict"]), (1, "BLOCK"))
        self.assertIn("14:30", self.reasons(r))

    def test_blocked_before_the_range_is_set(self):
        _, _, r = self.run_check(now="2026-10-30 09:20")
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("09:30", self.reasons(r))

    def test_blocked_on_a_weekend(self):
        _, _, r = self.run_check(now="2026-10-31 10:00")
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("weekend", self.reasons(r))

    def test_blocked_on_an_nse_holiday(self):
        _, _, r = self.run_check(now="2026-11-10 10:00")
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("holiday", self.reasons(r))


class Limits(Base):
    def test_loss_over_the_per_trade_limit(self):
        _, _, r = self.run_check(cfg={"max_loss_per_trade_inr": 100})
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("loss", self.reasons(r))

    def test_net_reward_risk_below_the_minimum(self):
        _, _, r = self.run_check(cfg={"min_reward_risk": 2.5})        # 2.2 after charges
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("reward", self.reasons(r))

    def test_daily_loss_limit_counts_what_already_went_out(self):
        placed = {"orders": [{"status": "placed", "date": "2026-10-30", "symbol": "TCS", "side": "buy",
                              "kind": "entry", "max_loss": 1400.0, "at": "2026-10-30T09:35:00+05:30"}]}
        _, _, r = self.run_check(state=placed)                       # 1400 + 365 > 1500
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("daily", self.reasons(r))

    def test_trades_per_day(self):
        three = {"orders": [{"status": "placed", "date": "2026-10-30", "symbol": s_, "side": "buy",
                             "kind": "entry", "max_loss": 10.0, "at": "2026-10-30T09:35:00+05:30"}
                            for s_ in ("TCS", "INFY", "ITC")]}
        _, _, r = self.run_check(state=three)
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("trade", self.reasons(r))

    def test_blocked_ticker(self):
        _, _, r = self.run_check(cfg={"blocked": ["RELIANCE"]})
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("blocked", self.reasons(r))

    def test_order_larger_than_the_cash(self):
        _, _, r = self.run_check(cfg={"budget": 20000})
        self.assertEqual(r["verdict"], "BLOCK")


class Format(Base):
    def test_off_tick_price_is_blocked(self):
        def m(orders):
            orders[0]["price"] = 1255.22
        _, _, r = self.run_check(mutate=m)
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("tick", self.reasons(r))

    def test_no_stop_at_all_is_blocked(self):
        def m(orders):
            orders[0].pop("exit_orders")
            orders[0]["plan"].pop("stop")
        _, _, r = self.run_check(mutate=m)
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("stop", self.reasons(r))

    def test_a_plan_stop_without_a_stop_order_passes_with_a_warning(self):
        def m(orders):
            orders[0].pop("exit_orders")
        _, _, r = self.run_check(mutate=m)
        self.assertEqual(r["verdict"], "PASS")
        self.assertIn("nothing at Kite protects", " ".join(r["warnings"]))

    def test_limit_order_without_a_price_is_blocked(self):
        def m(orders):
            orders[0].pop("price")
        _, _, r = self.run_check(mutate=m)
        self.assertEqual(r["verdict"], "BLOCK")

    def test_margin_products_are_blocked_by_default(self):
        def m(orders):
            orders[0]["product"] = "NRML"
        _, _, r = self.run_check(mutate=m)
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("margin", self.reasons(r))

    def test_futures_and_options_are_blocked(self):
        def m(orders):
            orders[0]["exchange"], orders[0]["tradingsymbol"] = "NFO", "RELIANCE26NOVFUT"
        _, _, r = self.run_check(mutate=m)
        self.assertEqual(r["verdict"], "BLOCK")

    def test_only_regular_variety(self):
        def m(orders):
            orders[0]["variety"] = "amo"
        _, _, r = self.run_check(mutate=m)
        self.assertEqual(r["verdict"], "BLOCK")

    def test_a_short_entry_is_blocked_without_allow_short(self):
        def m(orders):
            orders[0]["transaction_type"] = "SELL"
            orders[0].pop("exit_orders", None)
        _, _, r = self.run_check(mutate=m)
        self.assertEqual(r["verdict"], "BLOCK")

    def test_stop_outside_the_circuit_band_is_blocked(self):
        def m(orders):
            orders[0]["plan"]["lower_circuit"] = 1252.0               # the stop 1250.25 could never trigger
        _, _, r = self.run_check(mutate=m)
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("circuit", self.reasons(r))


class GettingOut(Base):
    def test_a_sell_that_closes_what_you_hold_is_never_blocked_by_the_window(self):
        flat = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
                "transaction_type": "SELL", "quantity": 59, "product": "MIS", "order_type": "MARKET",
                "validity": "DAY", "tag": "FRELIANCE20261030"}
        pf = {"cash": 25000.0, "positions": [{"ticker": "RELIANCE", "shares": 59,
                                              "lots": [{"id": "RELIANCE-1", "date": "2026-10-30",
                                                        "shares": 59, "cost": 74056.8}]}]}
        _, _, r = self.run_check(now="2026-10-30 15:05", mutate=lambda o: o.__setitem__(0, flat), portfolio=pf,
                                 state={"orders": [{"status": "placed", "date": "2026-10-30", "symbol": s_,
                                                    "side": "buy", "kind": "entry", "max_loss": 1500.0,
                                                    "at": "2026-10-30T09:35:00+05:30"} for s_ in ("TCS", "INFY", "ITC")]})
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual(r.get("kind"), "exit")

    def test_selling_more_than_you_hold_is_blocked(self):
        flat = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
                "transaction_type": "SELL", "quantity": 100, "product": "MIS", "order_type": "MARKET",
                "validity": "DAY"}
        pf = {"cash": 25000.0, "positions": [{"ticker": "RELIANCE", "shares": 59, "lots": []}]}
        _, _, r = self.run_check(mutate=lambda o: o.__setitem__(0, flat), portfolio=pf)
        self.assertEqual(r["verdict"], "BLOCK")


class Output(Base):
    def test_rupees_not_dollars_and_the_disclaimer(self):
        out = s.quiet(check.main, ["--orders", self.orders, "--config", self.cfg, "--now", "2026-10-30 09:51",
                                   "--no-write", "--portfolio", os.path.join(self.tmp, "none.json"),
                                   "--state", os.path.join(self.tmp, "nostate.json")])[1]
        self.assertIn("₹", out)
        self.assertNotIn("$", out)
        self.assertIn("not financial advice", out.lower())
        self.assertIn("IST", out)


if __name__ == "__main__":
    unittest.main()
