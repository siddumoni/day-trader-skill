"""SYNTHETIC NSE test data, built on the fly. Nothing here is market data.

Charts come out in the Yahoo v8 shape the skills read with --offline
(currency INR, exchangeTimezoneName Asia/Kolkata, timestamps in UTC seconds).
"""

import datetime
import json
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import nse_market as nm  # noqa: E402

DAY = datetime.date(2026, 10, 30)          # a Friday and a normal NSE trading day


def session_bars(day=DAY, base=1250.0, breakout=True, bars=75, vol=100000):
    """One 09:15-15:30 session of 5-minute candles. The first three bars (the 15-minute opening
    range) wiggle around `base`; bar 4 (the 09:30 bar) closes 6.5 above it when `breakout`."""
    out, p = [], base
    for i in range(bars):
        t = datetime.datetime.combine(day, datetime.time(9, 15)) + datetime.timedelta(minutes=5 * i)
        o = p
        if i < 3:
            cl = o + (1.0 if i % 2 else -0.8)
        elif i == 3:
            cl = o + (6.5 if breakout else -0.2)
        else:
            cl = o + 0.3
        h, l = max(o, cl) + 0.5, min(o, cl) - 0.5
        out.append([t.isoformat(), round(o, 2), round(h, 2), round(l, 2), round(cl, 2), vol + i * 500])
        p = cl
    return out


def chart(symbol="RELIANCE", candles=None, prev_close=1248.6, interval="5m"):
    ch = nm.kite_candles_to_chart(symbol, candles or session_bars(), interval, prev_close=prev_close)
    ch["chart"]["result"][0]["meta"]["note"] = "SYNTHETIC test bars, not market data"
    return ch


def history(symbol="RELIANCE", days=59, seed=5, end=datetime.date(2026, 10, 29)):
    """`days` earlier NSE trading days of random-walk 5-minute bars (weekends and holidays skipped)."""
    rnd = random.Random(seed)
    day, ds = end, []
    while len(ds) < days:
        if nm.trading_day(day)[0]:
            ds.append(day)
        day -= datetime.timedelta(days=1)
    rows, p = [], 1240.0
    for d in sorted(ds):
        base = datetime.datetime.combine(d, datetime.time(9, 15))
        p *= 1 + rnd.gauss(0, 0.003)
        for i in range(75):
            t = base + datetime.timedelta(minutes=5 * i)
            o = p
            cl = o * (1 + rnd.gauss(0.00002, 0.0011))
            h = max(o, cl) * (1 + abs(rnd.gauss(0, 0.0004)))
            l = min(o, cl) * (1 - abs(rnd.gauss(0, 0.0004)))
            rows.append([t.isoformat(), round(o, 2), round(h, 2), round(l, 2), round(cl, 2),
                         rnd.randint(40000, 160000)])
            p = cl
    return rows


def write_json(path, doc):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return path


def merge(history_rows, today_rows):
    return history_rows + today_rows


# ---------------------------------------------------------------- the pipeline, built with the real scripts

import contextlib  # noqa: E402
import io  # noqa: E402
from _load import FIX, load  # noqa: E402,F401


def quiet(fn, argv):
    """Run a script's main(argv) with its output captured: (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fn(argv)
    return code, out.getvalue(), err.getvalue()


def make_config(tmp, **over):
    with open(os.path.join(ROOT, "trader-budget", "config.example.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg.update(over)
    path = os.path.join(tmp, "config.json")
    write_json(path, cfg)
    return path


def make_plan(tmp, target="3R", qty=40, now="2026-10-30 09:50", entry="entry-triggered.json", stop="or-mid"):
    """trader-exit's real plan.json for the synthetic RELIANCE morning (entry 1255.20, range 1246.50-1254.00)."""
    exit_ = load("trader-exit", "exit")
    plan = os.path.join(tmp, "plan.json")
    code, out, err = quiet(exit_.main, ["plan", "--from", os.path.join(FIX, "trader-exit", entry), "--stop", stop,
                                        "--target", target, "--qty", str(qty), "--now", now, "--write", plan])
    if code != 0:
        raise RuntimeError("make_plan failed: %s %s" % (out, err))
    return plan


def make_orders(tmp, cfg=None, plan=None, now="2026-10-30 09:51", extra=()):
    """plan -> trader-budget -> orders.json. Returns (orders_path, config_path, plan_path, (code, out, err))."""
    cfgp = make_config(tmp, **(cfg or {}))
    planp = plan or make_plan(tmp)
    size = load("trader-budget", "size")
    orders = os.path.join(tmp, "orders.json")
    res = quiet(size.main, ["--plan", planp, "--config", cfgp, "--now", now, "--write", orders,
                            "--portfolio", os.path.join(tmp, "none.json")] + list(extra))
    return orders, cfgp, planp, res
