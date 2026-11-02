"""trader-budget tests (NSE India, Kite order shape). The plan comes from the real trader-exit on the
SYNTHETIC RELIANCE morning (entry 1255.20, opening range 1246.50-1254.00, stop at the midpoint 1250.25)."""

import json
import os
import tempfile
import unittest

import _synth as s
from _load import load

size = load("trader-budget", "size")
nm = size.nm if hasattr(size, "nm") else size.nse_market


def build(**kw):
    tmp = tempfile.mkdtemp()
    orders, cfg, plan, res = s.make_orders(tmp, **kw)
    doc = json.load(open(orders)) if os.path.exists(orders) else None
    return doc, res, tmp


class Sizing(unittest.TestCase):
    def setUp(self):
        self.doc, (self.code, self.out, self.err), _ = build()
        self.o = self.doc["orders"][0]

    def test_one_entry_order_in_the_kite_place_order_shape(self):
        self.assertEqual(self.code, 0)
        self.assertEqual(len(self.doc["orders"]), 1)
        for k, v in (("variety", "regular"), ("exchange", "NSE"), ("tradingsymbol", "RELIANCE"),
                     ("transaction_type", "BUY"), ("product", "MIS"), ("order_type", "LIMIT"),
                     ("validity", "DAY")):
            self.assertEqual(self.o[k], v, k)
        self.assertEqual(self.o["price"], 1255.2)
        self.assertIsInstance(self.o["quantity"], int)

    def test_the_position_cap_binds_here(self):
        # 75% of Rs 1,00,000 = Rs 75,000 / 1255.20 = 59 whole shares
        self.assertEqual(self.o["quantity"], 59)
        self.assertLessEqual(self.o["quantity"] * 1255.2, 75000)
        self.assertIn("position cap", self.o["why"])
        self.assertIn("position cap", self.out)

    def test_max_loss_includes_charges(self):
        plan = self.o["plan"]
        self.assertAlmostEqual(plan["gross_loss"], 59 * 4.95, places=2)
        self.assertGreater(plan["charges_at_stop"], 50)           # brokerage, STT, stamp, GST...
        self.assertAlmostEqual(plan["max_loss"], plan["gross_loss"] + plan["charges_at_stop"], places=1)
        self.assertLessEqual(plan["max_loss"], 500)

    def test_stop_and_target_are_separate_kite_orders(self):
        stop, target = self.o["exit_orders"]
        self.assertEqual((stop["order_type"], stop["transaction_type"], stop["role"]), ("SL", "SELL", "stop"))
        self.assertEqual(stop["trigger_price"], 1250.25)
        self.assertLess(stop["price"], stop["trigger_price"])     # a SELL stop-loss limit sits under its trigger
        self.assertEqual((target["order_type"], target["transaction_type"], target["role"]),
                         ("LIMIT", "SELL", "target"))
        self.assertEqual(target["price"], 1270.05)                # 1255.20 + 3 x 4.95
        self.assertEqual({stop["quantity"], target["quantity"]}, {59})

    def test_every_price_is_on_the_five_paisa_tick(self):
        for o in [self.o] + self.o["exit_orders"]:
            for k in ("price", "trigger_price"):
                if k in o:
                    self.assertTrue(nm.on_tick(o[k]), "%s %s" % (k, o[k]))

    def test_report_is_in_rupees_not_dollars(self):
        self.assertIn("₹", self.out)
        self.assertNotIn("$", self.out)

    def test_nothing_is_placed(self):
        self.assertIn("not financial advice", self.out.lower())
        self.assertEqual(self.doc["status"], "proposed")


class Binding(unittest.TestCase):
    def test_a_small_max_loss_binds_and_shrinks_the_size(self):
        doc, (code, out, err), _ = build(cfg={"max_loss_per_trade_inr": 150})
        q = doc["orders"][0]["quantity"]
        self.assertLess(q, 59)
        self.assertIn("max loss", doc["orders"][0]["why"])
        self.assertLessEqual(doc["orders"][0]["plan"]["max_loss"], 150)

    def test_a_stop_too_far_for_the_max_loss_is_a_refusal_not_a_smaller_order(self):
        # one share risks Rs 4.95 plus about Rs 1.3 of charges each way, so Rs 5 fits not even one
        doc, (code, out, err), _ = build(cfg={"max_loss_per_trade_inr": 5})
        self.assertIsNone(doc)
        self.assertNotEqual(code, 0)
        self.assertIn("No order", out)

    def test_cash_buffer_caps_the_size(self):
        doc, _, _ = build(cfg={"max_position_pct": 100, "max_order_inr": 100000, "cash_buffer_pct": 50,
                               "max_loss_per_trade_inr": 5000})
        self.assertLessEqual(doc["orders"][0]["quantity"] * 1255.2, 50000)
        self.assertIn("cash", doc["orders"][0]["why"])


class Costs(unittest.TestCase):
    def test_a_target_that_does_not_clear_charges_gets_no_order(self):
        tmp = tempfile.mkdtemp()
        plan = s.make_plan(tmp, target="1R")
        orders, cfg, _, (code, out, err) = s.make_orders(tmp, plan=plan)
        self.assertFalse(os.path.exists(orders))
        self.assertIn("after charges", out)
        self.assertIn("1.5", out)                                 # the configured minimum reward:risk

    def test_net_reward_risk_is_below_gross(self):
        doc, _, _ = build()
        self.assertLess(doc["orders"][0]["plan"]["reward_risk"], 3.0)


class Guards(unittest.TestCase):
    def test_circuit_limits_are_carried_to_the_risk_check(self):
        # sizing does not judge the band: it records it in the order's plan, and trader-risk BLOCKs on it
        tmp = tempfile.mkdtemp()
        orders, cfg, plan, (code, out, err) = s.make_orders(
            tmp, extra=["--lower-circuit", "1252.00", "--upper-circuit", "1380.00"])
        p = json.load(open(orders))["orders"][0]["plan"]
        self.assertEqual((p["lower_circuit"], p["upper_circuit"]), (1252.0, 1380.0))

    def test_a_plan_from_another_day_is_refused(self):
        tmp = tempfile.mkdtemp()
        orders, cfg, plan, (code, out, err) = s.make_orders(tmp, now="2026-11-02 10:00")
        self.assertFalse(os.path.exists(orders))
        self.assertNotEqual(code, 0)
        self.assertIn("2026-10-30", out + err)

    def test_market_order_option(self):
        tmp = tempfile.mkdtemp()
        orders, cfg, plan, (code, out, err) = s.make_orders(tmp, extra=["--market"])
        self.assertEqual(json.load(open(orders))["orders"][0]["order_type"], "MARKET")
        self.assertNotIn("price", json.load(open(orders))["orders"][0])

    def test_margin_is_off_by_default(self):
        doc, _, _ = build()
        self.assertEqual(doc["orders"][0]["product"], "MIS")
        self.assertNotIn("NRML", json.dumps(doc))


if __name__ == "__main__":
    unittest.main()
