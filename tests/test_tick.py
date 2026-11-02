"""A stock on a Rs 0.10 tick (Kite's search_instruments tick_size; RELIANCE was one on 2026-11-02, found
with live Kite data) must get stop, target and order prices on that tick, end to end. SYNTHETIC session."""

import json
import os
import tempfile
import unittest
from decimal import Decimal

import _synth as s
from _load import FIX, load

exit_ = load("trader-exit", "exit")
size = load("trader-budget", "size")
check = load("trader-risk", "check")


def on_grid(x, tick):
    return (Decimal(repr(x)) / Decimal(repr(tick))) % 1 == 0


class TenPaiseTick(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.plan = os.path.join(self.tmp, "plan.json")
        code, out, err = s.quiet(exit_.main, [
            "plan", "--from", os.path.join(FIX, "trader-exit", "entry-triggered.json"), "--stop", "or-mid",
            "--target", "3R", "--qty", "40", "--tick", "0.1", "--now", "2026-10-30 09:50", "--write", self.plan])
        self.assertEqual(code, 0, err)
        self.cfg = s.make_config(self.tmp)            # config still says 0.05: the plan's tick must win
        self.orders = os.path.join(self.tmp, "orders.json")
        code, out, err = s.quiet(size.main, ["--plan", self.plan, "--config", self.cfg, "--now", "2026-10-30 09:51",
                                             "--write", self.orders])
        self.assertEqual(code, 0, err)
        self.doc = json.load(open(self.orders))
        self.o = self.doc["orders"][0] if isinstance(self.doc, dict) and "orders" in self.doc else self.doc[0]

    def test_plan_carries_the_tick(self):
        self.assertEqual(json.load(open(self.plan))["tick_size"], 0.1)

    def test_every_price_is_on_the_tenth(self):
        for k in ("price",):
            if self.o.get(k):
                self.assertTrue(on_grid(self.o[k], 0.1), (k, self.o[k]))
        for leg in self.o["exit_orders"]:
            for k in ("price", "trigger_price"):
                if leg.get(k):
                    self.assertTrue(on_grid(leg[k], 0.1), (leg["role"], k, leg[k]))

    def test_risk_check_uses_the_orders_tick(self):
        code, out, err = s.quiet(check.main, ["--orders", self.orders, "--config", self.cfg,
                                              "--now", "2026-10-30 09:52", "--no-write"])
        self.assertIn("PASS", out + err)

    def test_off_tick_price_is_blocked_on_a_tenth_tick_order(self):
        doc = json.load(open(self.orders))
        o = doc["orders"][0] if isinstance(doc, dict) and "orders" in doc else doc[0]
        o["exit_orders"][1]["price"] = round(o["exit_orders"][1]["price"] + 0.05, 2)   # now on a 0.05, off a 0.10
        bad = os.path.join(self.tmp, "bad.json")
        s.write_json(bad, doc)
        code, out, err = s.quiet(check.main, ["--orders", bad, "--config", self.cfg,
                                              "--now", "2026-10-30 09:52", "--no-write"])
        self.assertEqual(code, 1)
        self.assertIn("tick", out + err)


if __name__ == "__main__":
    unittest.main()
