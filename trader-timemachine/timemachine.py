#!/usr/bin/env python3
"""Time Machine for trader-timemachine (India / NSE). Standard library only.

Replays a planned intraday trade before you risk a rupee: 10,000 simulated runs
of the rest of the session from your entry, plus the same rule on every past
session in the data, all NET of Indian intraday charges.

    python3 timemachine.py run                          # trader/plan.json, 10,000 replays, seed 19
    python3 timemachine.py run --write                  # also save trader/timemachine.json
    python3 timemachine.py run --until 15:00            # stop each replay at your flat-by time
    python3 timemachine.py run --qty 40 --cas           # 40 shares of an F&O (CAS) stock
    python3 timemachine.py run --seed 7 --replays 20000 --json
    python3 timemachine.py run --symbol RELIANCE --date 2026-10-19 --entry 1262.4 \\
        --stop 1255.1 --target 1269.7 --entry-time 10:15 --qty 40
    python3 timemachine.py chart                        # ASCII fan chart of the replays
    python3 timemachine.py chart --from trader/timemachine.json

How the replays work (the same algorithm and the same random draws as the original):
  1. Fetch 60 days of 5-minute bars (Yahoo v8 chart for SYMBOL.NS, range=60d
     interval=5m; one request). Keep only FULL sessions before the plan's date:
     75 bars, 09:15 to 15:30 IST. For an F&O stock (--cas) on a day from
     2026-08-03 the session ends at 15:15 (the closing auction session starts),
     so the first 72 bars are the session, and older sessions are cut to the
     same 72 bars so the pool is consistent. Gaps, the short Muhurat evening
     session and today are left out. (NSE has no half days.)
  2. The pool: for every bar slot 1..N-1 of the day, each past session's bar
     as ratios to the previous bar's close: (open, high, low, close) / prev close.
  3. One replay starts at the entry price and walks the rest of the session.
     For each bar i it picks a slot j = i + randint(-window, window), kept in
     1..N-1, then one past bar from pool[j] (random.choice), and scales it to
     the current price, snapped to the exchange tick (0.05, --tick). The stop is
     checked before the target in every bar, so a bar that touches both counts
     as the stop (the conservative call).
  4. Outcomes: "target" or "stop" (whichever was touched first), or "neither"
     when the trade is still open at --until (default the close: 15:30, or 15:15
     for an F&O stock). "green" = target first, or above the entry at --until
     (below for a short). R = the target's R at the target, -1 at the stop,
     otherwise (price at --until - entry) / risk. A gap through the stop still
     counts -1R here; a real fill can be worse.
  5. Charges. Every replay is also priced net: --qty shares (or the plan's
     qty / quantity / shares; default 1), a Zerodha-style intraday (MIS)
     schedule from nse_market.py (brokerage 0.03 % or Rs 20 a leg, STT 0.025 %
     on the sell leg, NSE and SEBI fees, stamp duty on the buy leg, 18 % GST).
     avg_R_net = avg_R - charges / (risk x qty). "breakeven_per_share" is what
     the price must move, per share, just to pay the round trip.
  6. The price at --until: median and the 5th to 95th percentile of all
     replays. The fan chart uses a second run of 2,000 paths with the same seed.
  7. History: the plan's own rule (an opening-range breakout with the same
     opening range, stop rule and target R) re-run on every one of those past
     full sessions, first close beyond the range = entry, same stop-first rule,
     also net of charges.

The verdict line looks at the replays that hit one side (target or stop) and
compares the target's share of them, target / (target + stop), with the share
that breaks even AFTER CHARGES at the plan's target R: (1 + c) / (1 + R), where
c is the round-trip charge in units of the risk (so 50 % at 1R and 33 % at 2R
when charges are zero). The gap, in percentage points:
  less than 5 points    "Close to a coin flip" (1R) / "Close to break-even" (other R)
  5 to 15 points        "Leans toward the target" / "Leans toward the stop"
  15 points or more     "Strongly toward the target" / "Strongly toward the stop"
Below it: a note when more than 40 % of the replays hit neither, the charges
line, and the average R when its sign goes against the lean.

5-minute bars only: Yahoo keeps 60 days of 5-min bars but only about 7 days of
1-min bars, too few sessions for a fair pool. The run refuses with a plain
message when there are fewer than 20 full sessions, when the plan's date is not
an NSE trading day, or when the entry bar is not in the data. Same plan, same
data and same --seed give the same numbers on any machine. A simulation built
from the stock's own recent bars is not a prediction: results, news, circuit
limits and halts can do what the last 60 days never did.

Data: https://query2.finance.yahoo.com/v8/finance/chart/<SYMBOL>.NS?range=60d&interval=5m
(unofficial and free, can lag; falls back to query1 on 429 or 5xx).
--offline FILE reads a saved chart JSON (.json or .json.gz) instead; Kite candles
(get_historical_data) convert with `python3 nse_market.py kite-to-chart`.
Times are India Standard Time (IST); --now "2026-10-19 10:15" pins the clock.
"""

import argparse
import datetime
import gzip
import json
import math
import os
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_market as nm  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36 day-trader-india/1.0")
PAUSE = 0.5          # seconds between requests: be polite to free endpoints
BACKOFF = 5.0        # seconds to wait once after a 429
HOSTS = ("query2", "query1")
INTERVAL = 300       # 5-minute bars, in seconds
SESSION_BARS = 75    # 09:15 to 15:30 in 5-minute bars
CAS_BARS = 72        # 09:15 to 15:15: an F&O stock from 2026-08-03
OPEN_MIN = nm.OPEN.hour * 60 + nm.OPEN.minute          # 09:15
CLOSE_MIN = nm.CLOSE.hour * 60 + nm.CLOSE.minute       # 15:30
CAS_MIN = nm.CTS_END_FO.hour * 60 + nm.CTS_END_FO.minute  # 15:15
TICK = 0.05
MIN_SESSIONS = 20
SAMPLE_PATHS = 160
BAND_PATHS = 2000
BAND_Q = (0.05, 0.25, 0.5, 0.75, 0.95)
HIST_BINS = 24
COIN_FLIP = 0.05     # verdict thresholds, see the docstring
STRONG = 0.15
MANY_NEITHER = 0.40
EPOCH = datetime.datetime(1970, 1, 1)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class TMError(Exception):
    pass


# ---------------------------------------------------------------- time (IST, from nse_market)

def parse_now(text):
    """--now -> naive UTC. A time without an offset is India time."""
    try:
        return nm.parse_now(text)
    except nm.MarketError as e:
        raise TMError(str(e))


def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


def parse_clock(text, what):
    """'10:15', '10:15 AM', '3:15 pm' or '15:15' (a bare 1 to 8 o'clock means p.m.) -> minutes after midnight."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*(?:([AaPp])\.?\s*[Mm]\.?)?\s*(?:IST)?\s*$", str(text or ""))
    if not m:
        raise TMError("%s wants an India time like 10:15 or 15:00, not %r" % (what, text))
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if mi > 59 or h > 23 or (ap and not 1 <= h <= 12):
        raise TMError("%s %r is not a clock time" % (what, text))
    if ap == "p" and h != 12:
        h += 12
    elif ap == "a" and h == 12:
        h = 0
    elif not ap and 1 <= h <= 8:
        h += 12
    return h * 60 + mi


def clock24(minutes):
    return "%02d:%02d" % divmod(minutes, 60)


def session_end_min(date_iso, cas):
    """Minute of day the continuous session ends: 15:30, or 15:15 for an F&O (CAS) stock from 2026-08-03."""
    d = datetime.date.fromisoformat(date_iso)
    return CAS_MIN if (cas and d >= nm.CAS_START) else CLOSE_MIN


def bars_in(end_min):
    return (end_min - OPEN_MIN) // 5


# ---------------------------------------------------------------- price grid (fast, float)

def tick_round(x, tick=TICK):
    """Nearest tick, as a float: the same grid as nse_market.round_to_tick (a test checks it)."""
    return round(round(x / tick) * tick, 2)


def tick_away(x, long, tick=TICK):
    """Snap a stop to the grid, away from the entry: down for a buy, up for a short."""
    q = x / tick
    n = math.floor(q + 1e-9) if long else math.ceil(q - 1e-9)
    return round(n * tick, 2)


def tick_far(x, long, tick=TICK):
    """Snap a target to the grid, further from the entry: up for a buy, down for a short."""
    return tick_away(x, not long, tick)


# ---------------------------------------------------------------- data

def chart_url(sym, host="query2"):
    return "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=60d&interval=5m" % (
        host, nm.yahoo_symbol(sym))


def _sleep(seconds):
    time.sleep(seconds)


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_chart(sym):
    """One request for 60 days of 5-min bars. query2 first, one retry after a 429,
    then query1 on 429 or 5xx."""
    last = None
    for n, host in enumerate(HOSTS):
        if n:
            _sleep(PAUSE)
        for attempt in (0, 1):
            url = chart_url(sym, host)
            try:
                return _get(url)
            except urllib.error.HTTPError as e:
                last = e.code
                try:
                    e.close()
                except Exception:
                    pass
                if e.code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if e.code == 429 or e.code >= 500:
                    break
                if e.code == 404:
                    raise TMError("Yahoo has no chart for %s.NS (HTTP 404): check the NSE symbol" % sym.upper())
                raise TMError("Yahoo answered HTTP %d for %s" % (e.code, sym.upper()))
            except urllib.error.URLError as e:
                raise TMError("could not reach Yahoo: %s (pull candles with the Kite connector and use --offline)"
                              % e.reason)
            except OSError as e:
                raise TMError("could not reach Yahoo: %s" % e)
    if last == 429:
        raise TMError("Yahoo is rate limiting (HTTP 429). Wait a minute and run it again.")
    raise TMError("Yahoo answered HTTP %s for %s on both hosts. Try again later." % (last, sym.upper()))


def read_offline(path, sym):
    """A saved Yahoo chart file (.json or .json.gz), or a folder holding <SYM>-60d5m.json(.gz)."""
    if os.path.isdir(path):
        for name in ("%s-60d5m.json" % sym.upper(), "%s-60d5m.json.gz" % sym.upper()):
            if os.path.exists(os.path.join(path, name)):
                path = os.path.join(path, name)
                break
        else:
            raise TMError("no %s-60d5m.json(.gz) in %s" % (sym.upper(), path))
    if not os.path.exists(path):
        raise TMError("no offline chart file %s" % path)
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    except OSError as e:
        raise TMError("could not read %s: %s" % (path, e))


def chart_result(text, sym):
    try:
        data = json.loads(text)
    except ValueError:
        raise TMError("Yahoo returned something that is not JSON for %s" % sym.upper())
    chart = data.get("chart") if isinstance(data, dict) else None
    if not isinstance(chart, dict):
        raise TMError("Yahoo returned something unexpected for %s" % sym.upper())
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise TMError("Yahoo has no data for %s: %s" % (sym.upper(), err.get("description") or "unknown ticker"))
    return res[0]


def parse_bars(result, now_utc):
    """Yahoo chart result -> (meta, bars, dropped). Regular-session 5-min bars only,
    oldest first. Dropped: bars with a missing price, bars off the 5-minute grid
    (the live tick, e.g. 10:49:45), bars not complete yet (now < start + 5 min,
    where now is the earlier of --now and the data's own regularMarketTime), and
    bars outside 09:15 to 15:30 India time."""
    meta = result.get("meta") or {}
    stamps = result.get("timestamp") or []
    q = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
    as_of = now_utc
    rmt = meta.get("regularMarketTime")
    if isinstance(rmt, (int, float)) and utc_of(rmt) < as_of:
        as_of = utc_of(rmt)
    dropped = {"no_price": 0, "off_grid": 0, "not_complete": 0, "outside_hours": 0}
    bars = {}
    for i, ts in enumerate(stamps):
        o, h, l, c, v = [col[i] if i < len(col) else None for col in cols]
        if ts is None or None in (o, h, l, c):
            dropped["no_price"] += 1
            continue
        ts = int(ts)
        if ts % INTERVAL:
            dropped["off_grid"] += 1
            continue
        start = utc_of(ts)
        local = nm.to_ist(start)
        mins = local.hour * 60 + local.minute - OPEN_MIN
        if mins < 0 or mins >= SESSION_BARS * 5:
            dropped["outside_hours"] += 1
            continue
        if start + datetime.timedelta(seconds=INTERVAL) > as_of:
            dropped["not_complete"] += 1
            continue
        bars[ts] = {"date": local.date().isoformat(), "slot": mins // 5, "start": local.strftime("%H:%M"),
                    "o": o, "h": h, "l": l, "c": c, "v": v or 0}
    return meta, [bars[t] for t in sorted(bars)], dropped


def split_sessions(bars):
    """{date: {slot: bar}}."""
    days = {}
    for b in bars:
        days.setdefault(b["date"], {})[b["slot"]] = b
    return days


def full_sessions(days, before, nbars=SESSION_BARS):
    """Full sessions (all `nbars` bars, slots 0..nbars-1) strictly before the plan's date, oldest
    first, and the short ones that were left out (gaps, the Muhurat evening session). Extra bars
    after slot nbars-1 are ignored, so an F&O stock's older 15:30 sessions are cut to 15:15."""
    full, short = [], []
    for d in sorted(days):
        if d >= before:
            continue
        s = days[d]
        if all(i in s for i in range(nbars)):
            full.append((d, [s[i] for i in range(nbars)]))
        else:
            short.append({"date": d, "bars": len(s)})
    return full, short


# ---------------------------------------------------------------- charges

def trip_cost(entry, exit_price, qty, long=True, costs=None):
    """Round-trip charges in rupees for qty shares, entry then exit. A long buys then sells
    (nse_market.round_trip); a short sells then buys back."""
    try:
        if long:
            return nm.round_trip(entry, exit_price, qty, costs)["charges"]
        return round(nm.charges(entry, qty, "SELL", costs)["total"] + nm.charges(exit_price, qty, "BUY", costs)["total"], 2)
    except nm.MarketError as e:
        raise TMError(str(e))


def breakeven_per_share(entry, qty, long=True, costs=None):
    """What the price must move per share, from the entry, just to pay the charges (a flat trade)."""
    return round(trip_cost(entry, entry, qty, long, costs) / qty, 2)


# ---------------------------------------------------------------- the simulation

def build_pool(sessions, nbars=SESSION_BARS):
    """pool[i] = every past session's bar i as (o, h, l, c) / bar i-1's close, in date order."""
    pool = [[] for _ in range(nbars)]
    for _, D in sessions:
        for i in range(1, nbars):
            pc = D[i - 1]["c"]
            pool[i].append((D[i]["o"] / pc, D[i]["h"] / pc, D[i]["l"] / pc, D[i]["c"] / pc))
    return pool


def pct(a, q):
    return a[min(len(a) - 1, int(q * len(a)))]


def simulate(pool, entry, stop, target, k, last, replays=10000, seed=19, window=2, side="buy",
             qty=1, tick=TICK, costs=None):
    """Monte Carlo replays of bars k+1..last. Same draws and order as the original sim.py; prices
    are snapped to the tick; results are gross and net of charges."""
    long = side == "buy"
    nbars = len(pool)
    risk = (entry - stop) if long else (stop - entry)
    r_target = round(((target - entry) if long else (entry - target)) / risk, 4)

    def slot(rng, i):
        return i if window == 0 else min(nbars - 1, max(1, i + rng.randint(-window, window)))

    rng = random.Random(seed)
    choice = rng.choice
    outcomes, closes, paths = [], [], []
    for n in range(replays):
        p = entry
        outcome = None
        keep = n < SAMPLE_PATHS
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            hi = round(round(p * h / tick) * tick, 2)
            lo = round(round(p * l / tick) * tick, 2)
            cl = round(round(p * c / tick) * tick, 2)
            if outcome is None:
                if long:
                    if lo <= stop:
                        outcome = "stop"        # both inside one bar: count the loss (conservative)
                    elif hi >= target:
                        outcome = "target"
                else:
                    if hi >= stop:
                        outcome = "stop"
                    elif lo <= target:
                        outcome = "target"
            p = cl
            if keep:
                path.append(round(p, 2))
        outcomes.append(outcome or "close")
        closes.append(p)
        if keep:
            paths.append(path)
    cnt = {x: outcomes.count(x) for x in ("target", "stop", "close")}
    green = sum(1 for o, p in zip(outcomes, closes)
                if o == "target" or (o == "close" and (p > entry if long else p < entry)))
    Rs = [r_target if o == "target" else -1.0 if o == "stop" else ((p - entry) if long else (entry - p)) / risk
          for o, p in zip(outcomes, closes)]
    # net of charges: each replay's exit price is the target, the stop, or the tick-rounded price at --until
    cache = {}

    def cost(exit_p):
        c = cache.get(exit_p)
        if c is None:
            c = cache[exit_p] = trip_cost(entry, exit_p, qty, long, costs)
        return c
    risk_rs = risk * qty
    Rn = [R - cost(target if o == "target" else stop if o == "stop" else p) / risk_rs
          for R, o, p in zip(Rs, outcomes, closes)]
    green_net = sum(1 for R in Rn if R > 0)
    flat = trip_cost(entry, entry, qty, long, costs)
    srt = sorted(closes)
    # percentile bands per bar for the fan chart: a second run of 2,000 paths, same seed
    rng = random.Random(seed)
    choice = rng.choice
    allp = []
    for n in range(BAND_PATHS):
        p = entry
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            p = round(round(p * c / tick) * tick, 2)
            path.append(p)
        allp.append(path)
    bands = []
    for j in range(last - k + 1):
        col = sorted(pp[j] for pp in allp)
        bands.append([round(pct(col, q), 2) for q in BAND_Q])
    lo_, hi_ = pct(srt, 0.01), pct(srt, 0.99)
    W = (hi_ - lo_) / HIST_BINS
    bins = [0] * HIST_BINS
    for c in srt:
        b = int((c - lo_) / W) if W > 0 else 0
        if 0 <= b < HIST_BINS:
            bins[b] += 1
    return {
        "replays": replays, "seed": seed, "window": window, "bars_left": last - k, "tick": tick,
        "p_target": round(cnt["target"] / replays, 4), "p_stop": round(cnt["stop"] / replays, 4),
        "p_neither": round(cnt["close"] / replays, 4), "p_green": round(green / replays, 4),
        "p_green_net": round(green_net / replays, 4),
        "median_close": round(pct(srt, 0.5), 2), "p5_close": round(pct(srt, 0.05), 2),
        "p95_close": round(pct(srt, 0.95), 2), "avg_R": round(statistics.mean(Rs), 3),
        "avg_R_net": round(statistics.mean(Rn), 3), "r_at_target": r_target,
        "qty": qty, "charges_flat": round(flat, 2), "cost_r": round(flat / risk_rs, 4),
        "breakeven_per_share": round(flat / qty, 2),
        "charges_at_target": round(cost(target), 2), "charges_at_stop": round(cost(stop), 2),
        "band_paths": BAND_PATHS, "bands": bands, "sample_paths": paths,
        "hist": {"lo": round(lo_, 2), "hi": round(hi_, 2), "bins": bins},
    }


# ---------------------------------------------------------------- stop rules and the history replay

def opening_range(D, n):
    """(high, low) of the first n bars of a session given as {slot: bar} or a list."""
    rows = [D[i] for i in range(n)]
    return max(b["h"] for b in rows), min(b["l"] for b in rows)


def rule_stop(rule, oh, ol, entry, long, tick=TICK):
    r = rule["stop_rule"]
    if r == "or_mid":
        return tick_away((oh + ol) / 2, long, tick)
    if r in ("or_low", "or_high"):
        return tick_away(ol if long else oh, long, tick)
    frac = rule["stop_pct"] / 100.0
    return tick_away(entry * (1 - frac) if long else entry * (1 + frac), long, tick)


def history_replay(sessions, rule, last, side="buy", qty=1, tick=TICK, costs=None):
    """The plan's rule on every past full session: entry = the first bar after the
    opening range to close beyond it (up to the --until bar), then stop first, then
    target, bar by bar, else the close at --until. Gross R and R net of charges."""
    long = side == "buy"
    n = rule["or_minutes"] // 5
    trades = []
    for d, D in sessions:
        oh, ol = opening_range(D, n)
        kk = next((i for i in range(n, last + 1) if (D[i]["c"] > oh if long else D[i]["c"] < ol)), None)
        if kk is None:
            continue
        e = tick_round(D[kk]["c"], tick)
        s = rule_stop(rule, oh, ol, e, long, tick)
        risk = (e - s) if long else (s - e)
        if risk <= 0:
            continue
        t = tick_far(e + rule["target_r"] * risk if long else e - rule["target_r"] * risk, long, tick)
        oc = None
        for b in D[kk + 1:last + 1]:
            if (b["l"] <= s) if long else (b["h"] >= s):
                oc = "stop"
                break
            if (b["h"] >= t) if long else (b["l"] <= t):
                oc = "target"
                break
        cl = D[last]["c"]
        exit_p = t if oc == "target" else s if oc == "stop" else cl
        R = ((t - e) if long else (e - t)) / risk if oc == "target" else -1.0 if oc == "stop" else \
            ((cl - e) if long else (e - cl)) / risk
        ch = trip_cost(e, exit_p, qty, long, costs)
        Rn = R - ch / (risk * qty)
        trades.append({"date": d, "time": clock24(OPEN_MIN + 5 * (kk + 1)), "entry": e, "stop": s, "target": t,
                       "outcome": oc or "close", "R": round(R, 2), "R_net": round(Rn, 2), "charges": round(ch, 2)})
    return {
        "sessions": len(sessions), "triggered": len(trades),
        "target": sum(1 for x in trades if x["outcome"] == "target"),
        "stop": sum(1 for x in trades if x["outcome"] == "stop"),
        "close": sum(1 for x in trades if x["outcome"] == "close"),
        "green": sum(1 for x in trades if x["R"] > 0),
        "green_net": sum(1 for x in trades if x["R_net"] > 0),
        "avg_R": round(statistics.mean(x["R"] for x in trades), 3) if trades else None,
        "avg_R_net": round(statistics.mean(x["R_net"] for x in trades), 3) if trades else None,
        "rule": rule, "trades": trades,
    }


# ---------------------------------------------------------------- the plan

def _num(v, what):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise TMError("the plan needs a number for %s, got %r" % (what, v))
    if not x > 0 or math.isinf(x):
        raise TMError("the plan's %s must be a positive price, got %r" % (what, v))
    return x


def load_plan(a, now_utc):
    """The plan from --plan (trader/plan.json) or from the flags, checked and normalized."""
    if a.symbol:
        missing = [f for f in ("entry", "stop", "target", "entry_time") if getattr(a, f) is None]
        if missing:
            raise TMError("with --symbol also give %s" % ", ".join("--" + m.replace("_", "-") for m in missing))
        raw = {"symbol": a.symbol, "side": a.side, "date": a.date or nm.to_ist(now_utc).date().isoformat(),
               "entry": a.entry, "stop": a.stop, "target": a.target, "entry_time": a.entry_time,
               "setup": a.setup, "opening_range": {"minutes": a.or_minutes}, "stop_rule": a.stop_rule,
               "interval": "5m"}
        src = "flags"
    else:
        if not os.path.exists(a.plan):
            raise TMError("no plan at %s: run /trader-exit first, or pass --symbol --date --entry --stop "
                          "--target --entry-time" % a.plan)
        try:
            with open(a.plan, encoding="utf-8") as fh:
                raw = json.load(fh)
        except ValueError as e:
            raise TMError("%s is not valid JSON: %s" % (a.plan, e))
        if not isinstance(raw, dict):
            raise TMError("%s is not a plan" % a.plan)
        src = a.plan
    if getattr(a, "qty", None):
        raw = dict(raw, qty=a.qty)
    if getattr(a, "tick", None):
        raw = dict(raw, tick_size=a.tick)
    return normalize_plan(raw, src, cas=getattr(a, "cas", False))


def normalize_plan(raw, src="flags", cas=False):
    """A plan dict (trader/plan.json shape) -> the checked plan the time machine runs."""
    try:
        sym = nm.normalize_symbol(raw.get("symbol") or "")
    except nm.MarketError:
        raise TMError("the plan needs an NSE symbol like RELIANCE, got %r" % raw.get("symbol"))
    side = str(raw.get("side") or "buy").lower()
    if side not in ("buy", "sell"):
        raise TMError("side must be buy or sell, got %r" % raw.get("side"))
    interval = str(raw.get("interval") or "5m")
    if interval != "5m":
        raise TMError("5-minute bars only (the plan says %s): Yahoo keeps 60 days of 5-min bars but only about "
                      "7 days of 1-min bars, too few sessions for a fair replay pool" % interval)
    try:
        d = datetime.date.fromisoformat(str(raw.get("date") or ""))
    except ValueError:
        raise TMError("the plan needs a date like 2026-10-19, got %r" % raw.get("date"))
    ok, why = nm.trading_day(d)
    if not ok:
        raise TMError("the NSE is closed on %s (%s): plan the trade for a trading day" % (d.isoformat(), why))
    date = d.isoformat()
    entry, stop, target = (_num(raw.get(k), k) for k in ("entry", "stop", "target"))
    long = side == "buy"
    if long and not stop < entry < target:
        raise TMError("a buy needs stop < entry < target (got stop %s, entry %s, target %s)" % (stop, entry, target))
    if not long and not target < entry < stop:
        raise TMError("a sell needs target < entry < stop (got target %s, entry %s, stop %s)" % (target, entry, stop))
    tick = TICK
    if raw.get("tick_size") is not None:
        tick = _num(raw["tick_size"], "tick_size")
    warn = [("%s %.2f is not on the %g tick: Kite rejects it" % (n, v, tick))
            for n, v in (("entry", entry), ("stop", stop), ("target", target))
            if abs(tick_round(v, tick) - v) > 1e-6]
    qty = 1
    for key in ("qty", "quantity", "shares"):
        if raw.get(key) is not None:
            try:
                qty = int(raw[key])
            except (TypeError, ValueError):
                raise TMError("the plan's %s must be a whole number of shares, got %r" % (key, raw[key]))
            break
    if qty < 1:
        raise TMError("quantity must be at least 1 share")
    end_min = session_end_min(date, cas)
    nbars = bars_in(end_min)
    k = None
    if raw.get("entry_time"):
        close_min = parse_clock(raw["entry_time"], "entry_time")
        if (close_min - OPEN_MIN) % 5:
            raise TMError("entry_time %s is not on the 5-minute grid from 09:15 (use the bar's close, e.g. 10:15)"
                          % raw["entry_time"])
        k = (close_min - OPEN_MIN) // 5 - 1
    if raw.get("entry_bar") is not None:
        try:
            kb = int(raw["entry_bar"])
        except (TypeError, ValueError):
            raise TMError("entry_bar must be a bar number, got %r" % raw["entry_bar"])
        if k is not None and kb != k:
            raise TMError("the plan disagrees with itself: entry_bar %d but entry_time %s is bar %d"
                          % (kb, raw["entry_time"], k))
        k = kb
    if k is None:
        raise TMError("the plan needs entry_time (the entry bar's close, e.g. 10:15) or entry_bar")
    if not 0 <= k <= nbars - 2:
        raise TMError("the entry must close between %s and %s to leave bars to replay (got %s)"
                      % (clock24(OPEN_MIN + 5), clock24(end_min - 5), clock24(OPEN_MIN + 5 * (k + 1))))
    orng = raw.get("opening_range") or {}
    or_min = orng.get("minutes", 15) if isinstance(orng, dict) else 15
    try:
        or_min = int(or_min)
    except (TypeError, ValueError):
        raise TMError("opening_range.minutes must be a number")
    if or_min < 5 or or_min % 5 or or_min >= 375:
        raise TMError("opening_range.minutes must be a multiple of 5 (got %s)" % or_min)
    risk = (entry - stop) if long else (stop - entry)
    return {
        "symbol": sym, "side": side, "date": date, "setup": str(raw.get("setup") or "orb"),
        "entry": entry, "entry_time": clock24(OPEN_MIN + 5 * (k + 1)), "entry_bar": k,
        "stop": stop, "target": target, "risk_per_share": round(risk, 2),
        "target_r": round(((target - entry) if long else (entry - target)) / risk, 2),
        "qty": qty, "tick": tick, "cas": bool(cas),
        "opening_range": dict(orng, minutes=or_min) if isinstance(orng, dict) else {"minutes": or_min},
        "stop_rule": str(raw.get("stop_rule") or "auto"), "interval": "5m",
        "flat_by": raw.get("flat_by"), "from": src, "tick_warnings": warn,
    }


def resolve_rule(plan, entry_day):
    """The rule the history replay uses: orb, the plan's opening range, its stop rule
    (or_mid, or_low / or_high, or 'pct' = the plan's stop distance as a percent of
    the entry), and the plan's target R. 'auto' works the stop rule out from the
    plan day's own opening range. None when the setup is not orb."""
    if plan["setup"].lower() != "orb":
        return None
    long = plan["side"] == "buy"
    tick = plan["tick"]
    n = plan["opening_range"]["minutes"] // 5
    rule = {"setup": "orb", "or_minutes": plan["opening_range"]["minutes"], "stop_rule": plan["stop_rule"],
            "target_r": plan["target_r"],
            "stop_pct": round(100.0 * plan["risk_per_share"] / plan["entry"], 4)}
    far = "or_low" if long else "or_high"
    if rule["stop_rule"] == "auto":
        rule["stop_rule"] = "pct"
        if all(i in entry_day for i in range(n)):
            oh, ol = opening_range(entry_day, n)
            if abs(tick_away((oh + ol) / 2, long, tick) - plan["stop"]) < 0.001:
                rule["stop_rule"] = "or_mid"
            elif abs(tick_away(ol if long else oh, long, tick) - plan["stop"]) < 0.001:
                rule["stop_rule"] = far
    elif rule["stop_rule"] in ("or_low", "or_high") and rule["stop_rule"] != far:
        rule["stop_rule"] = "pct"
    elif rule["stop_rule"] not in ("or_mid", far):
        rule["stop_rule"] = "pct"
    if rule["stop_rule"] != "pct":
        del rule["stop_pct"]
    return rule


# ---------------------------------------------------------------- the run

def _until(mc):
    return mc.get("until") or "15:30"


def lean(mc):
    """(gap, share, break_even): the target's share of the replays that hit one side,
    minus the share that breaks even after charges at the target's R. None when nothing hit a side."""
    pt, ps = mc["p_target"], mc["p_stop"]
    if pt + ps <= 0:
        return None
    r = mc.get("r_at_target") or 1.0
    c = mc.get("cost_r") or 0.0
    share, even = pt / (pt + ps), (1.0 + c) / (1.0 + r)
    return share - even, share, even


def verdict(mc):
    """One line from the numbers, see the docstring for the thresholds."""
    pt, ps = mc["p_target"], mc["p_stop"]
    lv = lean(mc)
    if lv is None:
        return "No replay reached the target or the stop by %s: the price at %s decides every one." % (
            _until(mc), _until(mc))
    d, share, even = lv
    r = mc.get("r_at_target") or 1.0
    one_r = abs(r - 1.0) < 0.005 and not mc.get("cost_r")
    if abs(d) < COIN_FLIP:
        head = "Close to a coin flip" if one_r else "Close to break-even"
    elif abs(d) < STRONG:
        head = "Leans toward the target" if d > 0 else "Leans toward the stop"
    else:
        head = "Strongly toward the target" if d > 0 else "Strongly toward the stop"
    odds = "target first %.1f %% vs stop first %.1f %%" % (100 * pt, 100 * ps)
    if one_r:
        return "%s: %s." % (head, odds)
    return ("%s: at %gR and after charges the target has to come first in %.0f %% of the replays that hit "
            "one side; it did in %.0f %% (%s)." % (head, round(r, 2), 100 * even, 100 * share, odds))


def verdict_notes(mc):
    """The lines printed right below the verdict, so nothing reads as one-sided."""
    notes = []
    pn = mc.get("p_neither", 0)
    if pn > MANY_NEITHER and lean(mc) is not None:
        notes.append("%.1f %% of the replays hit neither by %s; the price at %s decides those."
                     % (100 * pn, _until(mc), _until(mc)))
    if mc.get("avg_R_net") is not None and mc.get("qty"):
        notes.append("Net of charges (%s a round trip on %d share%s, %s a share to break even) the average R "
                     "is %+.3f; before charges %+.3f."
                     % (nm.inr(mc["charges_flat"]), mc["qty"], "" if mc["qty"] == 1 else "s",
                        nm.inr(mc["breakeven_per_share"]), mc["avg_R_net"], mc["avg_R"]))
    lv = lean(mc)
    avg = mc.get("avg_R_net")
    if lv and abs(lv[0]) >= COIN_FLIP and avg is not None and avg != 0 and (avg > 0) != (lv[0] > 0):
        notes.append("Average R is %+.3f all the same: the replays that hit neither pull it the other way." % avg)
    return notes


def time_machine(text, plan, now_utc, replays=10000, seed=19, window=2, until=None, source="yahoo", costs=None):
    """Chart JSON text + a normalized plan -> the timemachine.json document."""
    if not 1 <= replays <= 1000000:
        raise TMError("--replays must be between 1 and 1,000,000")
    if not 0 <= window <= 10:
        raise TMError("--window must be between 0 and 10 bars")
    sym = plan["symbol"]
    result = chart_result(text, sym)
    meta, bars, dropped = parse_bars(result, now_utc)
    msym = str(meta.get("symbol") or "").upper()
    if msym:
        try:
            same = nm.normalize_symbol(urllib.parse.unquote(msym)) == sym
        except nm.MarketError:
            same = False
        if not same:
            raise TMError("the data is for %s but the plan is for %s" % (msym, sym))
    gran = meta.get("dataGranularity")
    if gran and gran != "5m":
        raise TMError("the data has %s bars; the time machine needs 5-minute bars" % gran)
    if not bars:
        raise TMError("no complete 5-minute bars for %s in the data" % sym)
    k = plan["entry_bar"]
    cas = bool(plan.get("cas"))
    end_min = session_end_min(plan["date"], cas)
    nbars = bars_in(end_min)
    until_min = end_min if until is None else parse_clock(until, "--until")
    if (until_min - OPEN_MIN) % 5 or until_min > end_min:
        raise TMError("--until must be on the 5-minute grid and no later than %s%s"
                      % (clock24(end_min), " (an F&O stock's continuous session ends at 15:15)" if end_min == CAS_MIN else ""))
    last = (until_min - OPEN_MIN) // 5 - 1
    if last <= k:
        raise TMError("--until %s leaves nothing to replay after the %s entry" % (clock24(until_min),
                                                                             plan["entry_time"]))
    days = split_sessions(bars)
    entry_day = days.get(plan["date"], {})
    if k not in entry_day:
        reason = ("no bars for that day" if not entry_day else
                  "the data stops at the %s bar" % entry_day[max(entry_day)]["start"])
        raise TMError("the entry is not in the data: no complete %s bar (closing %s) on %s for %s (%s)"
                      % (clock24(OPEN_MIN + 5 * k), plan["entry_time"], plan["date"], sym, reason))
    eb = entry_day[k]
    tick = plan.get("tick", TICK)
    warn = list(plan.get("tick_warnings") or [])
    if not eb["l"] - tick <= plan["entry"] <= eb["h"] + tick:
        warn.append("the plan's entry %.2f is outside that bar's range %.2f to %.2f"
                    % (plan["entry"], eb["l"], eb["h"]))
    sessions, short = full_sessions(days, plan["date"], nbars)
    if len(sessions) < MIN_SESSIONS:
        raise TMError("only %d full session(s) of %s before %s in the data; the time machine needs at least %d"
                      % (len(sessions), sym, plan["date"], MIN_SESSIONS))
    qty = plan.get("qty", 1)
    pool = build_pool(sessions, nbars)
    mc = simulate(pool, plan["entry"], plan["stop"], plan["target"], k, last, replays, seed, window, plan["side"],
                  qty, tick, costs)
    mc = dict(mc, until=clock24(until_min), pool_sessions=len(sessions), pool_from=sessions[0][0],
              pool_to=sessions[-1][0], session_end=clock24(end_min), cas=cas,
              band_times=[clock24(OPEN_MIN + 5 * (j + 1)) for j in range(k, last + 1)])
    rule = resolve_rule(plan, entry_day)
    plan_out = {kk: v for kk, v in plan.items() if v is not None}
    if rule:
        plan_out["stop_rule"] = rule["stop_rule"]
        history = history_replay(sessions, rule, last, plan["side"], qty, tick, costs)
    else:
        history = {"sessions": len(sessions), "triggered": 0, "rule": None, "trades": [],
                   "note": "skipped: the history replay knows the opening-range breakout (orb) only, "
                           "this plan's setup is %r" % plan["setup"]}
    last_bar = bars[-1]
    return {
        "created_by": "trader-timemachine",
        "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "currency": "INR",
        "data": {"symbol": msym or sym, "interval": "5m", "range": "60d",
                 "last_bar": "%s %s IST" % (last_bar["date"], last_bar["start"]),
                 "dropped": dropped, "short_sessions_left_out": short},
        "plan": plan_out,
        "mc": mc,
        "history": history,
        "verdict": verdict(mc),
        "verdict_notes": verdict_notes(mc),
        "warnings": warn,
        "note": ("A simulation built from %s's own last %d full sessions, net of Zerodha-style intraday charges "
                 "(an estimate: check your contract note). It is not a prediction: results, news, circuit "
                 "limits and halts can do things those sessions never did. Same seed, same numbers."
                 % (sym, len(sessions))),
    }


# ---------------------------------------------------------------- output

def pc(x):
    return "%.1f %%" % (100.0 * x)


def report(doc):
    p, m, h = doc["plan"], doc["mc"], doc["history"]
    long = p["side"] == "buy"
    until = m["until"]
    wd = WEEKDAYS[datetime.date.fromisoformat(p["date"]).weekday()]
    r = p["target_r"]
    lines = [
        "%s %s, %s %s: entry %s at %s IST (bar %d), stop %s, target %s (%sR, risk %s a share), %d share%s."
        % (p["symbol"], p["side"], wd, p["date"], nm.inr(p["entry"]), p["entry_time"], p["entry_bar"],
           nm.inr(p["stop"]), nm.inr(p["target"]), ("%g" % r), nm.inr(p["risk_per_share"]), p["qty"],
           "" if p["qty"] == 1 else "s"),
        "%s replays of the rest of the session, %s to %s IST (%d five-minute bars), seed %d%s."
        % ("{:,}".format(m["replays"]), p["entry_time"], until, m["bars_left"], m["seed"],
           ", F&O stock: continuous session ends 15:15" if (m.get("cas") and m.get("session_end") == "15:15") else ""),
        "Each bar is drawn whole from %s's own 5-min bars at the same time of day (+-%d bars) over its last "
        "%d full sessions (%s to %s), prices on the %g tick." % (p["symbol"], m["window"], m["pool_sessions"],
                                                                 m["pool_from"], m["pool_to"], m["tick"]),
        "",
        "  target first   %s" % pc(m["p_target"]),
        "  stop first     %s" % pc(m["p_stop"]),
        "  neither        %-8s still open at %s" % (pc(m["p_neither"]), until),
        "  green          %-8s target first, or %s the entry at %s (before charges; %s after)" % (
            pc(m["p_green"]), "above" if long else "below", until, pc(m["p_green_net"])),
        "",
        "  Price at %s: median %s, 90 %% of replays between %s and %s."
        % (until, nm.inr(m["median_close"]), nm.inr(m["p5_close"]), nm.inr(m["p95_close"])),
        "  Average R: %+.3f gross, %+.3f net of charges (R = the %s risk a share: %sR at the target, -1R at the stop)."
        % (m["avg_R"], m["avg_R_net"], nm.inr(p["risk_per_share"]), ("+%g" % m["r_at_target"])),
        "  Charges: %s a round trip at the entry price, %s a share to break even (%.2fR)."
        % (nm.inr(m["charges_flat"]), nm.inr(m["breakeven_per_share"]), m["cost_r"]),
        "",
    ]
    if h.get("rule"):
        ru = h["rule"]
        stop_words = {"or_mid": "opening-range midpoint", "or_low": "opening-range low",
                      "or_high": "opening-range high"}.get(ru["stop_rule"],
                                                           "%.2f %% from the entry" % ru.get("stop_pct", 0))
        if h["triggered"]:
            lines.append("History: the same rule (%d-min opening range, stop at the %s, %gR target) on each of "
                         "those %d real sessions fired %d times: %d target, %d stop, %d neither. Average R %+.3f "
                         "gross, %+.3f net of charges."
                         % (ru["or_minutes"], stop_words, ru["target_r"], h["sessions"], h["triggered"],
                            h["target"], h["stop"], h["close"], h["avg_R"], h["avg_R_net"]))
        else:
            lines.append("History: the same rule never fired in those %d sessions." % h["sessions"])
    else:
        lines.append("History: " + h.get("note", "skipped."))
    lines += ["", doc["verdict"]] + list(doc.get("verdict_notes") or []) + [""]
    for w in doc.get("warnings") or []:
        lines.append("Note: " + w + ".")
    if doc["data"]["short_sessions_left_out"]:
        lines.append("Left out %d short session(s) (gaps or the Muhurat evening session): %s."
                     % (len(doc["data"]["short_sessions_left_out"]),
                        ", ".join(x["date"] for x in doc["data"]["short_sessions_left_out"])))
    lines.append(doc["note"] + " Repeat this run with --seed %d." % m["seed"])
    return lines


def render_chart(doc, height=17):
    """ASCII fan chart of the bands: one column per 5-min bar."""
    p, m = doc["plan"], doc["mc"]
    bands, times = m["bands"], m["band_times"]
    lo = min([b[0] for b in bands] + [p["stop"], p["target"]])
    hi = max([b[4] for b in bands] + [p["stop"], p["target"]])
    pad = (hi - lo) * 0.04 or 0.01
    lo, hi = lo - pad, hi + pad
    step = (hi - lo) / (height - 1)

    def row(price):
        return max(0, min(height - 1, int(round((hi - price) / step))))
    grid = [[" "] * len(bands) for _ in range(height)]
    for j, b in enumerate(bands):
        r5, r25, r50, r75, r95 = [row(x) for x in b]
        for r in range(r95, r5 + 1):
            grid[r][j] = "."
        for r in range(r75, r25 + 1):
            grid[r][j] = ":"
        grid[r50][j] = "*"
    marks = {row(p["target"]): "target", row(p["stop"]): "stop"}
    if row(p["entry"]) not in marks:
        marks[row(p["entry"])] = "entry"
    for r in (row(p["target"]), row(p["stop"])):
        grid[r] = ["-" if ch == " " else ch for ch in grid[r]]
    out = ["%s %s, %s replay paths from the %s IST entry (seed %d)" % (
        p["symbol"], p["date"], "{:,}".format(m.get("band_paths", BAND_PATHS)), p["entry_time"], m["seed"])]
    for r in range(height):
        label = marks.get(r)
        price = {"target": p["target"], "stop": p["stop"], "entry": p["entry"]}.get(label, hi - r * step)
        left = ("%9.2f |" % price) if (label or r in (0, height - 1)) else "          |"
        out.append(left + "".join(grid[r]) + ("  " + label if label else ""))
    axis = [" "] * len(bands)
    for j in sorted({0, len(bands) // 2, len(bands) - 1}):
        lab = times[j]
        s = max(0, min(j, len(bands) - len(lab)))
        axis[s:s + len(lab)] = list(lab)
    out.append("          +" + "-" * len(bands))
    out.append("           " + "".join(axis))
    out.append("* median   : middle half of the paths   . 90 % of the paths   - stop and target")
    return out


# ---------------------------------------------------------------- commands

def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")


def compute(a):
    now = parse_now(a.now)
    plan = load_plan(a, now)
    if a.offline:
        text, source = read_offline(a.offline, plan["symbol"]), "offline"
    else:
        text, source = fetch_chart(plan["symbol"]), "yahoo"
    return time_machine(text, plan, now, a.replays, a.seed, a.window, a.until, source)


def cmd_run(a):
    doc = compute(a)
    if a.write:
        _write(a.write, doc)
    if a.json:
        slim = dict(doc, mc={k: v for k, v in doc["mc"].items() if k != "sample_paths"})
        print(json.dumps(slim, indent=2))
    else:
        print("\n".join(report(doc)))
        if a.write:
            print("Wrote %s." % a.write)
        print("Next: /trader-budget sizes it to your budget and your max loss per trade.")
    return 0


def cmd_chart(a):
    if a.from_file:
        if not os.path.exists(a.from_file):
            raise TMError("no %s: run timemachine.py run --write first" % a.from_file)
        try:
            with open(a.from_file, encoding="utf-8") as fh:
                doc = json.load(fh)
            doc["mc"]["bands"]
        except (ValueError, KeyError, TypeError):
            raise TMError("%s is not a time machine result" % a.from_file)
    else:
        doc = compute(a)
    print("\n".join(render_chart(doc)))
    print("\n".join([doc.get("verdict", "")] + list(doc.get("verdict_notes") or [])))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Time Machine: replay a planned intraday trade on the NSE (trader-timemachine).")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--plan", default="trader/plan.json", help="the plan from trader-exit")
        p.add_argument("--symbol", help="or give the plan as flags: NSE symbol, e.g. RELIANCE")
        p.add_argument("--side", default="buy", choices=["buy", "sell"])
        p.add_argument("--date", help="the plan's day, YYYY-MM-DD (default today, India)")
        p.add_argument("--entry", type=float)
        p.add_argument("--stop", type=float)
        p.add_argument("--target", type=float)
        p.add_argument("--entry-time", help='the entry bar\'s close, e.g. "10:15"')
        p.add_argument("--setup", default="orb")
        p.add_argument("--or-minutes", type=int, default=15)
        p.add_argument("--stop-rule", default="auto", choices=["auto", "or_mid", "or_low", "or_high", "pct"])
        p.add_argument("--qty", type=int, help="shares, for the charges (default: the plan's qty, else 1)")
        p.add_argument("--cas", action="store_true",
                       help="an F&O (CAS) stock: continuous session ends 15:15 from 2026-08-03")
        p.add_argument("--tick", type=float, help="tick size in rupees (default 0.05)")
        p.add_argument("--replays", type=int, default=10000)
        p.add_argument("--seed", type=int, default=19)
        p.add_argument("--window", type=int, default=2, help="draw from the same time of day +- this many bars")
        p.add_argument("--until", help="end each replay at this time (default the close: 15:30, or 15:15 with --cas)")
        p.add_argument("--offline", metavar="FILE", help="a saved Yahoo chart JSON (.json or .json.gz), no network")
        p.add_argument("--now", help='pretend it is this time ("2026-10-19 10:15" IST, or ISO with Z)')

    p = sub.add_parser("run", help="replay the plan: 10,000 simulated sessions plus the history")
    common(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--write", nargs="?", const="trader/timemachine.json", metavar="PATH",
                   help="save the result (default trader/timemachine.json)")
    p = sub.add_parser("chart", help="ASCII fan chart of the replays")
    common(p)
    p.add_argument("--from", dest="from_file", nargs="?", const="trader/timemachine.json", metavar="PATH",
                   help="draw a saved result instead of running again")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"run": cmd_run, "chart": cmd_chart}[a.cmd](a)
    except TMError as e:
        print("timemachine.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
