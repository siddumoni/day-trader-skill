"""trader-exit tests (NSE India). Fixtures in tests/fixtures/trader-exit/ are SYNTHETIC, not market data:

entry-triggered.json   the trader/entry.json trader-entry writes for the synthetic RELIANCE morning: opening range
                       1246.50-1254.00, trigger at 9:45 AM, price 1255.20 (bar 5)
entry-waiting.json     the same stock before it triggered
RELIANCE-2026-10-30-5m.json / kite-RELIANCE-5m.json   the day's 5-minute bars (Yahoo shape / Kite candles), for ATR
"""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

from _load import load, ROOT, FIX

ex = load("trader-exit", "exit")
nse = ex.nse_market

XDIR = os.path.join(FIX, "trader-exit")
TRIG = os.path.join(XDIR, "entry-triggered.json")
WAIT = os.path.join(XDIR, "entry-waiting.json")
BARS = os.path.join(XDIR, "RELIANCE-2026-10-30-5m.json")
KITE = os.path.join(XDIR, "kite-RELIANCE-5m.json")
NOW = "2026-10-30 09:50"


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.cfg = os.path.join(self.tmp, "config.json")

    def config(self, **kw):
        with open(self.cfg, "w") as fh:
            json.dump(kw, fh)

    def run_ex(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ex.main(argv)
        return code, out.getvalue(), err.getvalue()

    def plan(self, *argv, **kw):
        """(code, plan-dict-with-warnings or None, stderr)."""
        args = ["plan", "--from", kw.get("src", TRIG), "--now", kw.get("now", NOW), "--config", self.cfg, "--json"]
        code, out, err = self.run_ex(args + list(argv))
        return code, (json.loads(out) if code == 0 else None), err

    def ok(self, *argv, **kw):
        code, p, err = self.plan(*argv, **kw)
        self.assertEqual(code, 0, err)
        return p

    def refused(self, *argv, **kw):
        code, p, err = self.plan(*argv, **kw)
        self.assertEqual(code, 1, p)
        return err

    # ------------------------------------------------------------ the plan

    def test_orb_default_stop_and_2r_target(self):
        p = self.ok("--qty", "40")
        self.assertEqual((p["symbol"], p["side"], p["date"]), ("RELIANCE", "buy", "2026-10-30"))
        self.assertEqual(p["entry"], 1255.2)                 # the observed close, never snapped
        self.assertEqual(p["stop"], 1250.25)                 # (1254.00 + 1246.50) / 2
        self.assertEqual(p["target"], 1265.10)               # entry + 2 x 4.95
        self.assertEqual(p["risk_per_share"], 4.95)
        self.assertEqual(p["reward_risk"], 2.0)
        self.assertEqual(p["stop_rule"], "or_mid")
        self.assertEqual(p["flat_by"], "15:00")
        self.assertEqual((p["exchange"], p["currency"], p["tick_size"], p["cas"]), ("NSE", "INR", 0.05, False))
        self.assertEqual(p["opening_range"], {"high": 1254.0, "low": 1246.5, "minutes": 15})

    def test_the_contract_keys_other_skills_read(self):
        p = self.ok("--qty", "40")
        for k in ("created_by", "symbol", "side", "date", "setup", "entry", "stop", "target", "target_r", "risk_per_share",
                  "reward_risk", "flat_by", "interval", "opening_range", "created_at", "note", "costs"):
            self.assertIn(k, p)

    def test_stop_rounds_down_to_the_tick(self):
        code, out, err = self.run_ex(["plan", "--symbol", "RELIANCE", "--entry", "1255.20", "--or-high", "1254.00",
                                      "--or-low", "1246.45", "--qty", "40", "--now", NOW, "--config", self.cfg, "--json"])
        self.assertEqual(code, 0, err)
        p = json.loads(out)
        self.assertEqual(p["stop"], 1250.20)                 # midpoint 1250.225 -> down to the Rs 0.05 tick
        self.assertIn("rounded down to the ₹0.05 tick", p["stop_detail"])

    def test_target_rounds_up_to_the_tick(self):
        p = self.ok("--target", "1.5R", "--qty", "40")
        self.assertEqual(p["target"], 1262.65)               # 1255.20 + 7.425 = 1262.625 -> up
        self.assertGreaterEqual(p["reward_risk"], 1.5)

    def test_a_custom_tick_from_the_config(self):
        self.config(tick_size=0.5)
        p = self.ok("--qty", "40")
        self.assertEqual(p["stop"], 1250.0)                  # 1250.25 down to a Rs 0.50 grid
        self.assertEqual(p["target"], 1266.0)                # 1255.20 + 2 x 5.20 = 1265.60 up
        self.assertEqual(p["tick_size"], 0.5)

    def test_an_off_tick_entry_is_kept_as_observed(self):
        code, out, err = self.run_ex(["plan", "--symbol", "RELIANCE", "--entry", "1255.23", "--or-high", "1254.00",
                                      "--or-low", "1246.50", "--qty", "10", "--now", NOW, "--config", self.cfg, "--json"])
        p = json.loads(out)
        self.assertEqual((p["entry"], p["stop"], p["risk_per_share"]), (1255.23, 1250.25, 4.98))

    def test_other_stops(self):
        self.assertEqual(self.ok("--stop", "or-low", "--qty", "40")["stop"], 1246.5)
        self.assertEqual(self.ok("--stop", "price:1249.90", "--qty", "40")["stop"], 1249.90)
        self.assertEqual(self.ok("--stop", "price:₹1,249.93", "--qty", "40")["stop"], 1249.90)   # snapped down
        p = self.ok("--stop", "pct:0.5", "--qty", "40")      # 1255.20 x 0.995 = 1248.924 -> 1248.90
        self.assertEqual(p["stop"], 1248.90)

    def test_atr_stop_from_the_session_bars(self):
        for src in (BARS, KITE):
            p = self.ok("--stop", "atr:2", "--offline", src, "--qty", "40")
            self.assertEqual((p["atr_bars"], p["atr"]), (6, 3.2833))
            self.assertEqual(p["stop"], 1248.60)             # 1255.20 - 2 x 3.2833 = 1248.633 -> down
            self.assertIn("fewer than 14", p["stop_detail"])
            self.assertIn("ATR(6)", p["stop_label"])
        err = self.refused("--stop", "atr:2", "--offline", BARS, "--now", "2026-10-30 09:25")
        self.assertIn("at least 3 closed", err)

    def test_level_setup_needs_a_stop(self):
        code, out, err = self.run_ex(["plan", "--symbol", "TCS", "--entry", "4125.00", "--setup", "level",
                                      "--now", NOW, "--config", self.cfg, "--json"])
        self.assertEqual(code, 1)
        self.assertIn("give --stop", err)
        code, out, err = self.run_ex(["plan", "--symbol", "TCS", "--entry", "4125.00", "--setup", "level", "--stop", "pct:0.5",
                                      "--qty", "10", "--now", NOW, "--config", self.cfg, "--json"])
        self.assertEqual(code, 0, err)
        p = json.loads(out)
        self.assertEqual((p["stop"], p["target"]), (4104.35, 4166.30))   # 4104.375 down; risk 20.65; 2R = +41.30

    # ------------------------------------------------------------ charges

    def test_costs_for_a_given_quantity(self):
        p = self.ok("--qty", "40")
        k = p["costs"]
        be = nse.round_trip(1255.2, 1255.2, 40)
        at_stop = nse.round_trip(1255.2, 1250.25, 40)
        at_target = nse.round_trip(1255.2, 1265.1, 40)
        self.assertEqual((k["qty"], k["qty_source"]), (40, "--qty"))
        self.assertEqual(k["breakeven_per_share"], be["breakeven_per_share"])
        self.assertGreater(k["breakeven_per_share"], 1.0)    # rupees a share, before you make anything
        self.assertEqual(k["charges_at_stop"], at_stop["charges"])
        self.assertEqual(k["net_loss_at_stop"], round(198.0 + at_stop["charges"], 2))
        self.assertEqual(k["gross_reward"], 396.0)
        self.assertEqual(k["net_reward_at_target"], at_target["net"])
        self.assertEqual(k["net_r"], round(at_target["net"] / k["net_loss_at_stop"], 2))
        self.assertLess(k["net_r"], p["reward_risk"])
        self.assertEqual(k["charges_pct_of_reward"], round(100 * at_target["charges"] / 396.0, 1))

    def test_quantity_defaults(self):
        p = self.ok()
        self.assertEqual(p["costs"]["qty"], 79)              # a Rs 1,00,000 reference position at 1255.20
        self.assertIn("reference position", p["costs"]["qty_source"])
        self.config(max_loss_per_trade_inr=500)
        p = self.ok()
        self.assertEqual(p["costs"]["qty"], 101)             # 500 / 4.95
        self.assertIn("max loss", p["costs"]["qty_source"])
        self.assertEqual(self.ok("--qty", "7")["costs"]["qty"], 7)

    def test_a_target_that_does_not_pay_the_charges_is_refused(self):
        code, out, err = self.run_ex(["plan", "--symbol", "ITC", "--entry", "100.00", "--setup", "level", "--stop", "price:99.95",
                                      "--target", "1R", "--qty", "10", "--now", NOW, "--config", self.cfg, "--json"])
        self.assertEqual(code, 1)
        self.assertIn("does not pay the charges", err)

    def test_thin_trades_are_warned(self):
        p = self.ok("--target", "1R", "--qty", "40")
        self.assertGreaterEqual(p["costs"]["charges_pct_of_reward"], 25)
        self.assertTrue(any("Charges take" in w for w in p["warnings"]))
        self.assertFalse(any("Charges take" in w for w in self.ok("--qty", "40")["warnings"]))

    def test_net_r_under_the_minimum_warns(self):
        self.config(min_reward_risk=1.9)
        p = self.ok("--qty", "40")                           # gross 2.0 clears 1.9, net R after charges does not
        self.assertTrue(any("After charges" in w for w in p["warnings"]), p["warnings"])

    def test_costs_can_be_overridden_in_the_config(self):
        self.config(costs={"brokerage_cap": 0, "brokerage_pct": 0})
        cheap = self.ok("--qty", "40")["costs"]["charges_at_stop"]
        self.config()
        self.assertLess(cheap, self.ok("--qty", "40")["costs"]["charges_at_stop"])

    # ------------------------------------------------------------ refusals and warnings

    def test_refusals(self):
        self.assertIn("at or above the entry", self.refused("--stop", "price:1256"))
        self.assertIn("at or below the entry", self.refused("--target", "price:1255.00"))
        self.assertIn("too tight", self.refused("--stop", "price:1255.15"))
        self.assertIn("not triggered", self.refused(src=WAIT))
        self.assertIn("longs only", self.refused("--side", "short"))
        self.assertIn("--stop is or-mid", self.refused("--stop", "trail:3"))
        self.assertIn("--target is", self.refused("--target", "big"))
        self.assertIn("--qty must be at least 1", self.refused("--qty", "0"))

    def test_waiting_entry_can_plan_ahead_with_a_price(self):
        p = self.ok("--entry", "1255.20", "--qty", "40", src=WAIT)
        self.assertEqual((p["entry"], p["stop"]), (1255.2, 1250.25))

    def test_flat_by(self):
        self.assertEqual(self.ok("--flat-by", "15:10")["flat_by"], "15:10")
        self.assertFalse(any("auto-squares-off" in w for w in self.ok("--flat-by", "15:10")["warnings"]))
        late = self.ok("--flat-by", "15:26")                 # after Zerodha's 15:25 MIS square-off
        self.assertTrue(any("15:25" in w and "Rs 50 + GST" in w for w in late["warnings"]), late["warnings"])
        self.assertIn("at or after the 3:30 PM close", self.refused("--flat-by", "15:35"))
        self.assertIn("is at or after your flat-by", self.refused("--time", "3:10 PM"))

    def test_f_and_o_stocks_square_off_earlier(self):
        p = self.ok("--cas", "--flat-by", "15:13")
        self.assertTrue(p["cas"])
        self.assertTrue(any("15:12" in w and "F&O" in w for w in p["warnings"]), p["warnings"])
        self.assertIn("at or after the 3:15 PM close", self.refused("--cas", "--flat-by", "15:20"))

    def test_holiday_and_unknown_calendar_dates(self):
        w = self.ok("--date", "2026-11-10")["warnings"]
        self.assertTrue(any("NSE is closed on 2026-11-10" in x and "Diwali" in x for x in w), w)
        w = self.ok("--date", "2027-01-05")["warnings"]
        self.assertTrue(any("nseindia.com" in x for x in w), w)
        w = self.ok("--date", "2026-10-29")["warnings"]
        self.assertTrue(any("not today" in x for x in w), w)

    # ------------------------------------------------------------ output

    def test_words_are_rupees_and_ist(self):
        code, out, err = self.run_ex(["plan", "--from", TRIG, "--now", NOW, "--config", self.cfg, "--qty", "40"])
        self.assertEqual(code, 0, err)
        for want in ("Exit plan: RELIANCE long", "₹1,255.20", "₹1,250.25", "₹1,265.10", "Net R", "break-even",
                     "IST", "Nothing was placed"):
            self.assertIn(want, out)
        for bad in ("$", "New York", " ET", "NYSE"):
            self.assertNotIn(bad, out)

    def test_write_and_show(self):
        path = os.path.join(self.tmp, "plan.json")
        code, out, err = self.run_ex(["plan", "--from", TRIG, "--now", NOW, "--config", self.cfg, "--qty", "40", "--write", path])
        self.assertEqual(code, 0, err)
        with open(path) as fh:
            self.assertEqual(json.load(fh)["target"], 1265.1)
        code, out, _ = self.run_ex(["show", "--plan", path, "--now", NOW])
        self.assertEqual(code, 0)
        self.assertIn("₹1,265.10", out)
        code, out, _ = self.run_ex(["show", "--plan", os.path.join(self.tmp, "none.json")])
        self.assertEqual(code, 1)


class Helpers(unittest.TestCase):
    def test_rupee_formatting_and_parsing(self):
        self.assertEqual(ex.money("1250.25"), "₹1,250.25")
        self.assertEqual(ex.money(125000.5), "₹1,25,000.50")       # lakh grouping
        self.assertEqual(ex.money(12345678), "₹1,23,45,678.00")     # crore grouping
        self.assertEqual(ex.D("₹1,250.25"), Decimal("1250.25"))
        self.assertEqual(ex.D("Rs. 99.5"), Decimal("99.5"))
        with self.assertRaises(ex.PlanError):
            ex.D("a lot")

    def test_tick_rounding(self):
        self.assertEqual(ex.floor_tick(Decimal("1250.224")), Decimal("1250.20"))
        self.assertEqual(ex.floor_tick(Decimal("1250.25")), Decimal("1250.25"))
        self.assertEqual(ex.ceil_tick(Decimal("1262.625")), Decimal("1262.65"))
        self.assertEqual(ex.ceil_tick(Decimal("1262.65")), Decimal("1262.65"))
        self.assertEqual(ex.ceil_tick(Decimal("1262.601")), Decimal("1262.65"))
        with self.assertRaises(ex.PlanError):
            ex.floor_tick(Decimal("10"), 0)

    def test_the_old_us_calendar_is_gone(self):
        self.assertFalse(hasattr(ex, "NYSE_HOLIDAYS"))
        self.assertFalse(hasattr(ex, "to_eastern"))
        with open(os.path.join(ROOT, "trader-exit", "exit.py"), encoding="utf-8") as fh:
            src = fh.read()
        for bad in ("New York", "NYSE", "America/New_York"):
            self.assertNotIn(bad, src)

    def test_the_shared_module_copy_is_identical(self):
        with open(os.path.join(ROOT, "nse_market.py"), "rb") as a, open(os.path.join(ROOT, "trader-exit", "nse_market.py"), "rb") as b:
            self.assertEqual(a.read(), b.read())


if __name__ == "__main__":
    unittest.main()
