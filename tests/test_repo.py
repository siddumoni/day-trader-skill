import json
import os
import re
import unittest

from _load import ROOT

SKILLS = ["trader-scanner", "trader-news", "trader-entry", "trader-exit", "trader-timemachine",
          "trader-risk", "trader-budget", "trader-broker", "trader-alerts", "trader-analyst",
          "trader-insider"]
ABOUT = ("11 Claude skills for intraday trading on the NSE: scan charts, track NSE announcements, "
         "time every entry and exit, replay every outcome net of Indian charges, and send nothing to "
         "Zerodha Kite without your yes.")
# skills on the way to an order carry the safety block
ORDER_PATH = ["trader-scanner", "trader-entry", "trader-exit", "trader-timemachine", "trader-budget",
              "trader-risk", "trader-broker", "trader-alerts"]
US_LEFTOVERS = ["EDGAR", "Alpaca", "NYSE", "Nasdaq Trader", "Pelosi", "House Clerk", "America/New_York"]


def frontmatter(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return (m.group(1) if m else ""), text


def every_text_file():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for f in files:
            if f.endswith((".md", ".py", ".json", ".csv", ".txt", ".xml")) or f == "LICENSE":
                yield os.path.join(base, f)


class Repo(unittest.TestCase):
    def test_eleven_skill_folders_at_the_root(self):
        found = sorted(d for d in os.listdir(ROOT)
                       if os.path.isfile(os.path.join(ROOT, d, "SKILL.md")))
        self.assertEqual(found, sorted(SKILLS))

    def test_root_files(self):
        root = sorted(d for d in os.listdir(ROOT) if not d.startswith(".git") and d != "__pycache__")
        self.assertEqual(root, sorted([".claude-plugin", "LICENSE", "README.md", "nse_market.py", "tests"] + SKILLS))

    def test_frontmatter_name_and_description(self):
        for s in SKILLS:
            fm, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertIn("name: %s\n" % s, fm + "\n", s)
            self.assertRegex(fm, r"description: ", s)
            self.assertGreater(len(fm), 200, "%s description is too thin" % s)
            self.assertGreater(len(text.splitlines()), 60, s)

    def test_order_path_skills_carry_the_safety_rule(self):
        for s in ORDER_PATH:
            _, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            low = " ".join(text.lower().split())
            self.assertIn("not financial advice", low, s)
            self.assertIn("yes", low, s)
            self.assertIn("lose money", low, s)

    def test_every_skill_says_what_is_verified(self):
        for s in SKILLS:
            _, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertIn("Verified vs not", text, s)
            self.assertIn("${CLAUDE_SKILL_DIR}", text, s)

    def test_broker_never_allows_always_allow(self):
        _, text = frontmatter(os.path.join(ROOT, "trader-broker", "SKILL.md"))
        self.assertIn("Needs approval", text)
        self.assertIn("never \"Always allow\"", text)

    def test_nse_market_is_identical_in_every_skill(self):
        with open(os.path.join(ROOT, "nse_market.py"), "rb") as fh:
            root_copy = fh.read()
        for s in SKILLS:
            with open(os.path.join(ROOT, s, "nse_market.py"), "rb") as fh:
                self.assertEqual(fh.read(), root_copy, s)

    def test_plugin_manifest_points_at_the_root_skills(self):
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json")) as fh:
            plugin = json.load(fh)
        self.assertEqual(plugin["name"], "day-trader-india")
        self.assertEqual(plugin["skills"], "./")
        self.assertEqual(plugin["description"], ABOUT)
        with open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")) as fh:
            market = json.load(fh)
        self.assertEqual(market["name"], "day-trader-india")
        self.assertEqual(market["plugins"][0]["name"], "day-trader-india")

    def test_readme(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = " ".join(fh.read().split())
        self.assertTrue(readme.startswith("# The Day Trader skill, India edition"))
        self.assertIn(ABOUT.split(":")[0], readme)
        for part in ("Not financial advice", "Verified vs not", "Closing auction", "## Fine print"):
            self.assertIn(part, readme)
        for s in SKILLS:
            self.assertIn("/" + s, readme, s)

    def test_no_em_dashes(self):
        for path in every_text_file():
            rel = os.path.relpath(path, ROOT)
            if rel.startswith(os.path.join("tests", "fixtures")):
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                self.assertNotIn(chr(0x2014), fh.read(), rel)

    def test_no_us_leftovers_in_skills_or_tools(self):
        for s in SKILLS:
            for f in os.listdir(os.path.join(ROOT, s)):
                if not f.endswith((".md", ".py", ".json")):
                    continue
                with open(os.path.join(ROOT, s, f), encoding="utf-8") as fh:
                    text = fh.read()
                for bad in US_LEFTOVERS:
                    self.assertNotIn(bad, text, "%s/%s" % (s, f))

    def test_every_tool_runs_on_python_3_8(self):
        for s in SKILLS + ["."]:
            folder = os.path.join(ROOT, s)
            for f in os.listdir(folder):
                if not f.endswith(".py"):
                    continue
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    src = fh.read()
                for bad in ("import zoneinfo", "from zoneinfo", ".removeprefix(", ".removesuffix("):
                    self.assertNotIn(bad, src, "%s/%s" % (s, f))
                self.assertNotRegex(src, r"(?m)^\s+match \w+.*:$", "%s/%s" % (s, f))
                self.assertNotRegex(src, r"(?<![\w.])(list|dict|tuple)\[", "%s/%s" % (s, f))

    def test_india_limits_ship_in_the_example_config(self):
        with open(os.path.join(ROOT, "trader-budget", "config.example.json")) as fh:
            cfg = json.load(fh)
        for k in ("max_loss_per_trade_inr", "max_daily_loss_inr", "max_trades_per_day",
                  "min_reward_risk", "require_stop", "no_entries_after", "flat_by", "tick_size"):
            self.assertIn(k, cfg, k)
        self.assertIs(cfg["require_stop"], True)
        self.assertEqual(cfg["tick_size"], 0.05)
        self.assertEqual(cfg["product"], "MIS")
        self.assertIs(cfg.get("allow_short", False), False)
        self.assertIs(cfg.get("allow_margin", False), False)
        self.assertIs(cfg.get("allow_options", False), False)

    def test_advisory_is_the_default(self):
        with open(os.path.join(ROOT, "trader-budget", "config.example.json")) as fh:
            cfg = json.load(fh)
        self.assertIs(cfg["paper"], True)
        self.assertEqual(cfg["broker"], "kite")


if __name__ == "__main__":
    unittest.main()
