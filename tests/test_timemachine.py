"""trader-timemachine tests (NSE India). The history is SYNTHETIC random-walk 5-minute bars (59 NSE trading
days, weekends and holidays skipped) so the numbers mean nothing; what is tested is the mechanics: the
09:15-15:30 session, the F&O closing-auction cutoff, charges, reproducibility and honest wording."""

import json
import os
import tempfile
import unittest

import _synth as s
from _load import ROOT, load

tm = load("trader-timemachine", "timemachine")
entry = load("trader-entry", "entry")
exit_ = load("trader-exit", "exit")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.chart = s.write_json(os.path.join(cls.tmp, "RELIANCE-60d5m.json"),
                                 s.chart("RELIANCE", s.history() + s.session_bars()))
        code, out, err = s.quiet(entry.main, ["check", "RELIANCE", "--offline", cls.chart, "--now",
                                              "2026-10-30 09:41", "--write", os.path.join(cls.tmp, "entry.json")])
        assert code == 0, out + err
        cls.plan = os.path.join(cls.tmp, "plan.json")
        code, out, err = s.quiet(exit_.main, ["plan", "--from", os.path.join(cls.tmp, "entry.json"), "--stop",
                                              "or-mid", "--target", "3R", "--qty", "59", "--now",
                                              "2026-10-30 09:42", "--write", cls.plan])
        assert code == 0, out + err

    def run_tm(self, *extra, plan=None, chart=None):
        code, out, err = s.quiet(tm.main, ["run", "--plan", plan or self.plan, "--offline", chart or self.chart,
                                           "--now", "2026-10-30 09:43"] + list(extra))
        return code, out, err

    def doc(self, *extra, replays="1000"):
        code, out, err = self.run_tm("--json", "--replays", replays, *extra)
        self.assertEqual(code, 0, err)
        return json.loads(out)


class Run(Base):
    def test_the_report_is_in_ist_and_rupees(self):
        code, out, _ = self.run_tm("--qty", "59")
        self.assertEqual(code, 0)
        self.assertIn("10,000 replays", out)
        self.assertIn("09:35 to 15:30 IST (71 five-minute bars)", out)
        self.assertIn("₹", out)
        for us in ("New York", " ET", "NYSE", "$"):
            self.assertNotIn(us, out, us)

    def test_the_nse_session_has_75_bars_and_the_replay_is_the_rest_of_the_day(self):
        d = self.doc()
        self.assertEqual(d["mc"]["bars_left"], 71)                 # 75 bars minus the 4 up to the 09:35 entry
        self.assertEqual(d["mc"]["replays"], 1000)
        self.assertEqual(self.doc(replays="10000")["mc"]["replays"], 10000)

    def test_outcomes_add_up_to_everything(self):
        mc = self.doc()["mc"]
        self.assertAlmostEqual(mc["p_target"] + mc["p_stop"] + mc["p_neither"], 1.0, places=3)

    def test_prices_are_on_the_five_paisa_tick(self):
        self.assertEqual(self.doc()["mc"]["tick"], 0.05)

    def test_same_seed_same_numbers_different_seed_different_numbers(self):
        a, b, c = self.doc("--seed", "19"), self.doc("--seed", "19"), self.doc("--seed", "7")
        self.assertEqual(a["mc"], b["mc"])
        self.assertNotEqual(a["mc"]["p_stop"], c["mc"]["p_stop"])

    def test_history_uses_59_real_sessions_with_no_holidays(self):
        h = self.doc()["history"]
        self.assertEqual(h["sessions"], 59)
        for t in h["trades"]:
            self.assertTrue(s.nm.trading_day(__import__("datetime").date.fromisoformat(t["date"]))[0], t["date"])


class Charges(Base):
    def test_net_r_is_below_gross_r_because_of_charges(self):
        mc = self.doc("--qty", "59")["mc"]
        self.assertLess(mc["avg_R_net"], mc["avg_R"])
        self.assertAlmostEqual(mc["avg_R"] - mc["avg_R_net"], mc["cost_r"], delta=0.05)
        self.assertGreater(mc["charges_flat"], 50)                 # about Rs 73 for 59 shares near Rs 1,255
        self.assertGreater(mc["breakeven_per_share"], 1.0)

    def test_history_is_also_priced_net(self):
        h = self.doc("--qty", "59")["history"]
        self.assertLess(h["avg_R_net"], h["avg_R"])
        for t in h["trades"][:5]:
            self.assertLess(t["R_net"], t["R"])

    def test_charges_scale_with_the_quantity(self):
        a, b = self.doc("--qty", "10")["mc"], self.doc("--qty", "100")["mc"]
        self.assertGreater(b["charges_flat"], a["charges_flat"])

    def test_the_report_says_net_of_charges(self):
        _, out, _ = self.run_tm("--qty", "59")
        self.assertIn("net of charges", out)
        self.assertIn("a share to break even", out)


class ClosingAuction(Base):
    def test_an_f_and_o_stock_stops_at_1515(self):
        d = self.doc("--cas")
        self.assertEqual(d["mc"]["bars_left"], 68)
        _, out, _ = self.run_tm("--cas", "--replays", "500")
        self.assertIn("09:35 to 15:15 IST (68 five-minute bars)", out)
        self.assertIn("continuous session ends 15:15", out)

    def test_until_shortens_the_replay(self):
        d = self.doc("--until", "14:00")
        self.assertLess(d["mc"]["bars_left"], 71)


class Honesty(Base):
    def test_it_says_it_is_a_simulation_not_a_prediction(self):
        _, out, _ = self.run_tm()
        self.assertIn("not a prediction", out)
        self.assertIn("--seed", out)

    def test_it_gives_one_verdict(self):
        self.assertTrue(self.doc()["verdict"])

    def test_the_chart_subcommand_draws_a_fan(self):
        out_json = os.path.join(self.tmp, "tm.json")
        self.assertEqual(s.quiet(tm.main, ["run", "--plan", self.plan, "--offline", self.chart, "--now",
                                           "2026-10-30 09:43", "--write", out_json])[0], 0)
        code, out, err = s.quiet(tm.main, ["chart", "--from", out_json])
        self.assertEqual(code, 0, err)
        self.assertTrue(out.strip())


class Refusals(Base):
    def test_no_entry_bar_in_the_data_is_an_error_not_a_guess(self):
        only_history = s.write_json(os.path.join(self.tmp, "hist-only.json"), s.chart("RELIANCE", s.history()))
        code, out, err = self.run_tm(chart=only_history)
        self.assertNotEqual(code, 0)
        self.assertIn("entry is not in the data", out + err)

    def test_a_plan_with_the_stop_above_the_entry_is_refused(self):
        with open(self.plan) as fh:
            p = json.load(fh)
        p["stop"] = p["entry"] + 1
        bad = s.write_json(os.path.join(self.tmp, "bad-plan.json"), p)
        code, out, err = self.run_tm(plan=bad)
        self.assertNotEqual(code, 0)

    def test_missing_offline_file(self):
        code, out, err = self.run_tm(chart="/nonexistent.json")
        self.assertNotEqual(code, 0)
        self.assertIn("offline", out + err)


class NoUsMachinery(unittest.TestCase):
    def test_source_has_no_us_market_left(self):
        with open(os.path.join(ROOT, "trader-timemachine", "timemachine.py"), encoding="utf-8") as fh:
            src = fh.read()
        for word in ("NYSE", "New York", "eastern_offset", "America/New_York"):
            self.assertNotIn(word, src, word)


if __name__ == "__main__":
    unittest.main()
