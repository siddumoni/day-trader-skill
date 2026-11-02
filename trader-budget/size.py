#!/usr/bin/env python3
"""Position sizing for trader-budget (NSE, rupees). Standard library only, no network.

Default mode: sizes ONE intraday (MIS) day trade from trader/plan.json (written by
trader-exit) to your budget and your max loss per trade, then writes the proposed
order to trader/orders.json in the shape of the Kite connector's place_order tool.
It never places anything: trader-risk checks the order and trader-broker asks you
yes or no.

    python3 size.py                                  # trader/plan.json + trader/config.json
    python3 size.py --write                          # write trader/orders.json
    python3 size.py --limit 1250.50 --write          # a limit order at 1250.50 (default: the plan's entry)
    python3 size.py --market --write                 # a MARKET order instead of a limit
    python3 size.py --now "2026-10-29 10:15" --json  # pin the clock (India time)

Kite has no bracket order in the place_order tool, so the order file holds the ENTRY
order and, inside it, "exit_orders": a stop-loss (order_type SL, SELL) and a target
(order_type LIMIT, SELL). They are NOT placed with the entry: a SELL with no position
would open a short. trader-broker (ticket.py protect) turns them into tickets after the
entry fills, with the filled quantity. The two are not linked on Kite (not OCO): when one
fills, cancel the other. GTT does not cover MIS, so there is no server-side OCO.

How many shares. Whole shares only (cash equity, lot size 1).

    shares = floor of the smallest of
      max loss       the most shares whose loss at the stop, PLUS the round-trip
                     charges (brokerage, STT, exchange, GST, stamp), fits
                     max_loss_per_trade_inr
      position cap   budget x max_position_pct / entry
      max order      max_order_inr / entry
      cash           (budget minus the cash buffer) / entry; with "allow_margin": true
                     the budget is multiplied by "mis_leverage" first. The max-loss
                     limit still binds, so leverage never raises the risk you accept.
                     (and no more than the cash in trader/portfolio.json, if you have one)

Prices are snapped to the Rs 0.05 tick ("tick_size"): the entry to the nearest tick, the
stop and the target DOWN (a wider stop, a nearer target: the careful side).

Zero shares (the stop is too far away for your max loss) is a plain refusal: no order is
written. So is a reward:risk, net of charges, under min_reward_risk. Charges are an
estimate from Zerodha's published equity intraday schedule (nse_market.CHARGES).

Weights mode (kept from v1, no longer the headline): split a budget across a list of
stocks by weight, in whole shares, as CNC (delivery) market orders.

    python3 size.py --weights RELIANCE=40,TCS=30,INFY=30 --prices prices.json
    python3 size.py --targets my-weights.json --budget 100000 --write

Money that cannot be invested is reported as "left idle", never hidden.
"""

import argparse
import datetime
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_market as nm  # noqa: E402  (one shared copy of the India helpers, copied into this folder)

# the contract's config.example.json. Missing keys fall back to these.
DEFAULTS = {
    "budget": None,
    "cash_buffer_pct": 10,
    "max_position_pct": 75,
    "max_order_inr": 80000,
    "min_order_inr": 500,
    "max_loss_per_trade_inr": 500,
    "max_daily_loss_inr": 1500,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 1.5,
    "require_stop": True,
    "no_entries_before": "09:30",
    "no_entries_after": "14:30",
    "flat_by": "15:00",
    "tick_size": 0.05,
    "exchange": "NSE",
    "product": "MIS",
    "sl_limit_buffer_pct": 0.1,
    "broker": "kite",
    "paper": True,
    "blocked": [],
    "fo_stocks": [],
    "allow_options": False,
    "allow_margin": False,
    "mis_leverage": 1,
    "allow_short": False,
}
EPS = 1e-9
FRESH_MINUTES = 15
SETUP_WORDS = {"orb": "opening-range breakout", "flag": "flag", "bounce": "oversold bounce"}
KITE_ENTRY_NOTE = ("Proposals only. Nothing is placed until trader-risk passes each order and you say yes "
                   "to it in the chat. The stop and the target are separate orders, placed after the entry "
                   "fills (ticket.py protect); Kite does not link them: cancel the other when one fills.")


class SizeError(Exception):
    pass


# ---------------------------------------------------------------- time (India, no tz files)

def parse_now(text):
    try:
        return nm.parse_now(text)
    except nm.MarketError as e:
        raise SizeError(str(e))


def parse_utc(text):
    """"2026-10-29T04:52:13Z" -> naive UTC, or None."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def utc_iso(now_utc=None):
    now_utc = now_utc or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return now_utc.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- inputs

def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SizeError("no %s at %s" % (what, path))
    except json.JSONDecodeError as e:
        raise SizeError("%s is not valid JSON: %s" % (path, e))


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        cfg.update(read_json(path, "config"))
    return cfg


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _sym(raw):
    try:
        return nm.normalize_symbol(raw)
    except nm.MarketError as e:
        raise SizeError(str(e))


def parse_weights(data):
    """Accept a weights file, a {symbol: weight} dict or a list of rows.

    Returns [{"ticker", "name", "weight"}] with weight as a float."""
    if isinstance(data, dict) and "weights" in data:
        data = data["weights"]
    rows = []
    if isinstance(data, dict):
        for t, w in data.items():
            rows.append({"ticker": t, "name": "", "weight": w})
    elif isinstance(data, list):
        for i, r in enumerate(data):
            if not isinstance(r, dict):
                raise SizeError("weights[%d] is not an object" % i)
            t = r.get("ticker") or r.get("symbol")
            w = r.get("weight", r.get("weight_pct", r.get("pct")))
            if not t or w is None:
                raise SizeError("weights[%d] needs a ticker and a weight" % i)
            rows.append({"ticker": t, "name": r.get("name", ""), "weight": w})
    else:
        raise SizeError("weights must be a list or an object")
    out = []
    for r in rows:
        try:
            w = float(r["weight"])
        except (TypeError, ValueError):
            raise SizeError("weight for %s is not a number: %r" % (r["ticker"], r["weight"]))
        out.append({"ticker": _sym(r["ticker"]), "name": r["name"] or "", "weight": w})
    return out


def weights_from_arg(text):
    """RELIANCE=22.04,TCS=17.14 -> {symbol: weight}"""
    out = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SizeError("--weights wants SYMBOL=WEIGHT pairs, got %r" % part)
        t, w = part.split("=", 1)
        out[t.strip().upper()] = w.strip()
    return out


def load_prices(path):
    if not path:
        return {}
    data = read_json(path, "prices")
    prices = data.get("prices", data) if isinstance(data, dict) else {}
    out = {}
    for t, p in prices.items():
        try:
            out[nm.normalize_symbol(t)] = float(p)
        except (TypeError, ValueError, nm.MarketError):
            continue
    return out


def price_time(doc, sym):
    """When this symbol's price is from: the quote's own time (detail[sym].time), else when it
    was fetched (detail[sym].fetched), else the file's as_of."""
    det = ((doc or {}).get("detail") or {}).get(sym) or {}
    for key in ("time", "fetched"):
        t = parse_utc(det.get(key))
        if t:
            return t
    return parse_utc((doc or {}).get("as_of"))


def fresh_price(doc, sym, now_utc, minutes=FRESH_MINUTES):
    """(price, fetched_utc, fresh) for sym from a prices.json doc, or (None, None, False)."""
    if not isinstance(doc, dict):
        return None, None, False
    p = _f((doc.get("prices") or {}).get(sym))
    if p is None or p <= 0:
        return None, None, False
    t = price_time(doc, sym)
    if t is None:
        return p, None, False
    age = (now_utc - t).total_seconds()
    return p, t, -60 <= age <= minutes * 60


# ---------------------------------------------------------------- money helpers

def tick_of(c):
    t = _f(c.get("tick_size")) or 0.05
    if t <= 0:
        raise SizeError("tick_size must be positive")
    return t


def snap(x, tick, mode="nearest"):
    return nm.round_to_tick(x, tick, mode)


def shares_for(inr, per_share):
    """Whole shares that fit in inr at per_share, in exact integer paise."""
    u, p = int(round(float(inr) * 100)), int(round(float(per_share) * 100))
    if p <= 0 or u <= 0:
        return 0
    return u // p


def _price(x):
    """1250.5 -> "1250.50"."""
    return "%.2f" % float(x)


def _num(x):
    """1.5 -> "1.5", 75 -> "75"."""
    x = round(float(x), 4)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0")


def money(x):
    x = float(x)
    if abs(x - round(x)) < 0.005:
        return nm.inr(round(x), 0)
    return nm.inr(x, 2)


def costs_of(c):
    return c.get("costs") or None


def trade_loss(ref, stop, qty, c):
    """(gross loss, round-trip charges, total) in rupees if you buy at ref and the stop sells at stop."""
    if qty <= 0:
        return 0.0, 0.0, 0.0
    rt = nm.round_trip(ref, stop, qty, costs_of(c))
    gross = round(qty * (ref - stop), 2)
    return gross, rt["charges"], round(gross + rt["charges"], 2)


def trade_reward(ref, target, qty, c):
    """(gross gain, round-trip charges, net gain) if the target fills at target."""
    if qty <= 0:
        return 0.0, 0.0, 0.0
    rt = nm.round_trip(ref, target, qty, costs_of(c))
    gross = round(qty * (target - ref), 2)
    return gross, rt["charges"], round(gross - rt["charges"], 2)


def max_loss_shares(ref, stop, max_loss, c, hard_cap=None):
    """The most whole shares whose loss at the stop plus round-trip charges fits max_loss."""
    risk = ref - stop
    if risk <= 0 or not max_loss:
        return 0
    hi = int(max_loss / risk + 1e-9)
    if hard_cap is not None:
        hi = min(hi, hard_cap)
    lo = 0
    while lo < hi:                       # the loss grows with the quantity: binary search
        mid = (lo + hi + 1) // 2
        if trade_loss(ref, stop, mid, c)[2] <= max_loss + 0.005:
            lo = mid
        else:
            hi = mid - 1
    return lo


# ---------------------------------------------------------------- plan mode (the default)

def size_trade(plan, cfg, budget=None, limit=None, price=None, cash=None, today=None, market=False):
    """Pure function: one plan + your rules -> the sizing (a dict). qty may be 0.

    limit   the limit price (None = the plan's entry; ignored when market=True)
    market  a MARKET order: sized at the higher of a FRESH `price` and the plan's entry
    price   a fresh latest price (market orders only)
    cash    the cash in trader/portfolio.json, if there is one
    today   India date (YYYY-MM-DD); a plan for another day is refused"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    if not isinstance(plan, dict):
        raise SizeError("the plan is not a JSON object")
    tick = _f(plan.get("tick_size")) or tick_of(c)   # the plan's tick (per instrument) wins over the config
    sym = _sym(plan.get("symbol") or "")
    side = str(plan.get("side") or "buy").strip().lower()
    if side != "buy":
        raise SizeError("this pack sizes long (buy) day trades only; the plan says %r" % side)
    if today and plan.get("date") and str(plan["date"]) != today:
        raise SizeError("the plan is for %s and today is %s in India: make a new plan with trader-exit"
                        % (plan["date"], today))
    entry_plan, stop, target = _f(plan.get("entry")), _f(plan.get("stop")), _f(plan.get("target"))
    for name, v in (("entry", entry_plan), ("stop", stop), ("target", target)):
        if v is None or v <= 0:
            raise SizeError("the plan needs a positive %s (run trader-exit); a trade needs entry, "
                            "stop and target" % name)
    blocked = {str(b).upper() for b in (c.get("blocked") or [])}
    if sym in blocked:
        raise SizeError("%s is on your blocked list" % sym)

    budget = float(budget if budget is not None else (c.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json")
    buf_pct = float(c.get("cash_buffer_pct") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")

    if market:
        if price is not None and snap(price, tick) > snap(entry_plan, tick):
            ref, ref_from = snap(price, tick), "price"
        else:
            ref, ref_from = snap(entry_plan, tick), "plan"
        order_type = "MARKET"
    elif limit is not None:
        ref, ref_from, order_type = snap(limit, tick), "limit", "LIMIT"
    else:
        ref, ref_from, order_type = snap(entry_plan, tick), "plan", "LIMIT"
    stop, target = snap(stop, tick, "down"), snap(target, tick, "down")
    if stop >= ref - EPS:
        raise SizeError("the stop %s is not below the entry %s; a long trade needs its stop underneath"
                        % (_price(stop), _price(ref)))
    if target <= ref + EPS:
        raise SizeError("the target %s is not above the entry %s: the move has already happened. "
                        "Skip it or make a new plan with trader-exit" % (_price(target), _price(ref)))
    risk = round(ref - stop, 2)
    reward = round(target - ref, 2)

    lev = _f(c.get("mis_leverage")) or 1.0
    margin_on = bool(c.get("allow_margin")) and lev > 1
    cash_buffer = round(budget * buf_pct / 100.0, 2)
    base = budget * lev if margin_on else budget
    available = base - cash_buffer
    cash_note = "budget %s minus the %s cash buffer" % (money(budget), money(cash_buffer))
    if margin_on:
        cash_note = "budget %s x %s MIS leverage minus the %s cash buffer" % (money(budget), _num(lev),
                                                                              money(cash_buffer))
    if cash is not None:
        cash_cap = (cash * lev if margin_on else cash) - cash_buffer
        if cash_cap < available - 0.005:
            available = cash_cap
            cash_note = "cash %s in trader/portfolio.json minus the %s buffer" % (money(cash), money(cash_buffer))
    available = max(round(available, 2), 0.0)

    limits = []
    max_loss = _f(c.get("max_loss_per_trade_inr"))
    caps = []
    cap_pct = _f(c.get("max_position_pct"))
    if cap_pct:
        cap = round(budget * cap_pct / 100.0, 2)
        caps.append(shares_for(cap, ref))
    max_order = _f(c.get("max_order_inr"))
    if max_order:
        caps.append(shares_for(max_order, ref))
    caps.append(shares_for(available, ref))
    if max_loss:
        q_loss = max_loss_shares(ref, stop, max_loss, c, hard_cap=max(caps) if caps else None)
        one = trade_loss(ref, stop, max(q_loss, 1), c)
        limits.append({"name": "max loss", "inr": max_loss, "per": risk, "shares": q_loss,
                       "how": "%s max loss, charges included (%d x %s risk + about %s charges = %s)"
                              % (money(max_loss), max(q_loss, 1), _price(risk), money(one[1]), money(one[2]))})
    if cap_pct:
        limits.append({"name": "position cap", "inr": cap, "per": ref, "shares": shares_for(cap, ref),
                       "how": "%s%% of %s = %s / %s" % (_num(cap_pct), money(budget), money(cap), _price(ref))})
    if max_order:
        limits.append({"name": "max order", "inr": max_order, "per": ref, "shares": shares_for(max_order, ref),
                       "how": "%s / %s" % (money(max_order), _price(ref))})
    limits.append({"name": "cash", "inr": available, "per": ref, "shares": shares_for(available, ref),
                   "how": "%s = %s / %s" % (cash_note, money(available), _price(ref))})

    qty = max(0, min(l["shares"] for l in limits))
    bound = [l["name"] for l in limits if l["shares"] == qty]
    for l in limits:
        l["binds"] = l["name"] == bound[0]
    cost = round(qty * ref, 2)
    gross_loss, ch_stop, loss = trade_loss(ref, stop, qty, c)
    gross_gain, ch_target, net_gain = trade_reward(ref, target, qty, c)
    # the ratio is per rupee at risk: use one share for the plain figure when there is no size
    q_for_ratio = qty or 1
    _, _, loss_1 = trade_loss(ref, stop, q_for_ratio, c)
    _, _, gain_1 = trade_reward(ref, target, q_for_ratio, c)
    rr_net = round(gain_1 / loss_1, 2) if loss_1 > 0 else 0.0
    rr_gross = round(reward / risk, 2)
    be = nm.round_trip(ref, target, q_for_ratio, costs_of(c))["breakeven_per_share"]

    refusal = None
    min_rr = _f(c.get("min_reward_risk"))
    if qty == 0:
        if bound[0] == "max loss":
            one = trade_loss(ref, stop, 1, c)
            refusal = ("one share risks %s at the stop (entry %s minus stop %s, plus about %s charges), more "
                       "than your %s max loss per trade. No order. Skip this trade, or plan a closer stop "
                       "with trader-exit." % (money(one[2]), _price(ref), _price(stop), money(one[1]), money(max_loss)))
        else:
            refusal = ("one share of %s costs %s, more than your %s limit (%s). No order."
                       % (sym, money(ref), bound[0], limits[[l["name"] for l in limits].index(bound[0])]["how"]))
    elif min_rr and rr_net < min_rr - 1e-9:
        refusal = ("reward:risk after charges is %.2f (gross %.2f; target %s, entry %s, stop %s), under your "
                   "minimum %s, so trader-risk would block it. No order. Skip it, or make a new plan with "
                   "trader-exit." % (rr_net, rr_gross, _price(target), _price(ref), _price(stop), _num(min_rr)))
    elif cost < float(c.get("min_order_inr") or 0) - 0.005:
        refusal = ("%d share%s of %s is %s, under your %s minimum order. No order."
                   % (qty, "" if qty == 1 else "s", sym, money(cost), money(c["min_order_inr"])))
    return {
        "symbol": sym, "side": "buy", "setup": plan.get("setup", ""), "date": plan.get("date"),
        "entry_time": plan.get("entry_time", ""), "entry_plan": snap(entry_plan, tick),
        "entry": ref, "entry_from": ref_from, "stop": stop, "target": target,
        "risk_per_share": risk, "reward_per_share": reward, "reward_risk": rr_net, "reward_risk_gross": rr_gross,
        "stop_label": plan.get("stop_label", ""), "target_label": plan.get("target_label", ""),
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer, "cash_available": available,
        "max_loss_per_trade_inr": max_loss, "limits": limits, "qty": qty, "bound_by": bound[0],
        "cost": cost, "gross_loss": gross_loss, "charges_at_stop": ch_stop, "max_loss": loss,
        "gross_gain": gross_gain, "charges_at_target": ch_target, "net_gain": net_gain,
        "breakeven_per_share": be,
        "flat_by": plan.get("flat_by") or c.get("flat_by"),
        "order_type": order_type, "tick_size": tick, "product": str(c.get("product") or "MIS").upper(),
        "exchange": str(c.get("exchange") or "NSE").upper(),
        "sl_limit_buffer_pct": _f(c.get("sl_limit_buffer_pct")) or 0.0,
        "lower_circuit": _f(plan.get("lower_circuit")), "upper_circuit": _f(plan.get("upper_circuit")),
        "mis_leverage": lev if margin_on else 1, "refusal": refusal,
    }


def _tag(role, sym, date):
    """Kite's tag: alphanumeric, at most 20 characters. E = entry, S = stop, T = target, F = flatten."""
    stamp = re.sub(r"\D", "", str(date or ""))[4:8]
    return (re.sub(r"[^A-Za-z0-9]", "", "td%s%s%s" % (stamp, role, sym)))[:20]


def stop_limit_price(stop, tick, buffer_pct):
    """A SELL stop-loss (SL) order needs a limit price a little BELOW its trigger, so it can still
    fill when the price gaps through the trigger: at least two ticks, or buffer_pct of the trigger."""
    gap = max(stop * (buffer_pct or 0) / 100.0, 2 * tick)
    p = snap(stop - gap, tick, "down")
    return p if p > 0 else snap(stop, tick, "down")


def entry_order(s):
    """The one proposed entry order, in the place_order tool's argument names, plus
    "exit_orders" (stop and target, placed after the fill), "why" and "plan" (ours, never sent)."""
    if s["refusal"] or s["qty"] <= 0:
        return None
    qty = int(s["qty"])
    o = {"variety": "regular", "exchange": s["exchange"], "tradingsymbol": s["symbol"],
         "transaction_type": "BUY", "quantity": qty, "product": s["product"], "order_type": s["order_type"]}
    if s["order_type"] == "LIMIT":
        o["price"] = float(s["entry"])
    o["validity"] = "DAY"
    o["tag"] = _tag("E", s["symbol"], s["date"])
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "day trade")
    label = {"max loss": "%s max loss per trade, charges included" % money(s["max_loss_per_trade_inr"] or 0),
             "position cap": "position cap", "max order": "max order", "cash": "cash after the buffer"}
    o["why"] = ("%s on %s: %d shares, bound by your %s (%d x %s = %s + about %s charges = %s at the stop)"
                % (setup, s["symbol"], qty, label[s["bound_by"]], qty, _price(s["risk_per_share"]),
                   money(s["gross_loss"]), money(s["charges_at_stop"]), money(s["max_loss"])))
    slp = stop_limit_price(s["stop"], s["tick_size"], s["sl_limit_buffer_pct"])
    base = {"variety": "regular", "exchange": s["exchange"], "tradingsymbol": s["symbol"],
            "transaction_type": "SELL", "quantity": qty, "product": s["product"]}
    stop_o = dict(base, order_type="SL", price=float(slp), trigger_price=float(s["stop"]), validity="DAY",
                  tag=_tag("S", s["symbol"], s["date"]), role="stop")
    target_o = dict(base, order_type="LIMIT", price=float(s["target"]), validity="DAY",
                    tag=_tag("T", s["symbol"], s["date"]), role="target")
    o["exit_orders"] = [stop_o, target_o]
    o["plan"] = {"entry": s["entry"], "stop": s["stop"], "target": s["target"],
                 "risk_per_share": s["risk_per_share"], "gross_loss": s["gross_loss"],
                 "charges_at_stop": s["charges_at_stop"], "max_loss": s["max_loss"],
                 "net_gain": s["net_gain"], "reward_risk": s["reward_risk"], "setup": s["setup"],
                 "entry_time": s["entry_time"], "flat_by": s["flat_by"], "tick_size": s["tick_size"]}
    if s.get("lower_circuit") is not None:
        o["plan"]["lower_circuit"] = s["lower_circuit"]
    if s.get("upper_circuit") is not None:
        o["plan"]["upper_circuit"] = s["upper_circuit"]
    return o


def plan_report(s, order, notes=()):
    lines = []
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "setup")
    lines.append("%s buy (%s), %s, entry %s%s" % (s["symbol"], s["product"], setup, _price(s["entry_plan"]),
                                                  (" at " + s["entry_time"]) if s["entry_time"] else ""))
    lines.append("Stop %s%s. Target %s%s." % (_price(s["stop"]), (": " + s["stop_label"]) if s["stop_label"] else "",
                                             _price(s["target"]),
                                             (": " + s["target_label"]) if s["target_label"] else ""))
    for n in notes:
        lines.append(n)
    src = {"plan": "the plan's entry", "price": "the latest price, above the plan's entry",
           "limit": "your limit price"}[s["entry_from"]]
    lines.append("Sized at %s (%s). Risk %s a share (entry %s minus stop %s). Reward:risk %.2f after charges "
                 "(%.2f before)." % (_price(s["entry"]), src, _price(s["risk_per_share"]), _price(s["entry"]),
                                     _price(s["stop"]), s["reward_risk"], s["reward_risk_gross"]))
    lines.append("Charges: the price must gain about %s a share just to break even (Zerodha intraday "
                 "schedule, an estimate)." % money(s["breakeven_per_share"]))
    lines.append("")
    lines.append("Limits (whole shares; the smallest one wins):")
    for l in s["limits"]:
        lines.append("  %-13s %-72s %5d shares%s" % (l["name"], l["how"], l["shares"],
                                                     "  <- binds" if l["binds"] else ""))
    lines.append("")
    if order is None:
        lines.append("No order: " + s["refusal"])
        lines.append("Education and tooling, not financial advice.")
        return "\n".join(lines)
    lines.append("Size: %d shares of %s = %s at %s. Loss at the stop: %d x %s = %s, plus about %s charges = %s."
                 % (s["qty"], s["symbol"], money(s["cost"]), _price(s["entry"]), s["qty"],
                    _price(s["risk_per_share"]), money(s["gross_loss"]), money(s["charges_at_stop"]),
                    money(s["max_loss"])))
    lines.append("At the target: %s gross, about %s charges, %s net."
                 % (money(s["gross_gain"]), money(s["charges_at_target"]), money(s["net_gain"])))
    slp = order["exit_orders"][0]["price"]
    lines.append("A stop is an order, not a promise: the stop-loss triggers at %s and sells with a limit of %s; "
                 "in a fast market or a gap it can fill lower, or not at all." % (_price(s["stop"]), _price(slp)))
    lines.append("")
    lines.append("Proposed entry (Kite place_order arguments, nothing has been placed):")
    how = "LIMIT %s" % _price(order["price"]) if order["order_type"] == "LIMIT" else "MARKET"
    lines.append("  BUY %d %s on %s, %s, %s, valid for the day"
                 % (order["quantity"], order["tradingsymbol"], order["exchange"], order["product"], how))
    lines.append("After it fills (ticket.py protect), two separate SELL orders, not linked on Kite:")
    lines.append("  stop-loss: SL, trigger %s, limit %s" % (_price(s["stop"]), _price(slp)))
    lines.append("  target:    LIMIT %s" % _price(s["target"]))
    lines.append("When one fills, cancel the other. GTT does not cover MIS, so Kite cannot link them.")
    if s["flat_by"]:
        lines.append("Flat by %s IST: anything still open then gets closed (trader-alerts rings, trader-broker "
                     "flatten). Zerodha auto-squares MIS at 15:25 (15:12 for F&O stocks) for Rs 50 + GST an order."
                     % s["flat_by"])
    if s["mis_leverage"] > 1:
        lines.append("MIS leverage %sx is on: your loss limit still binds the size." % _num(s["mis_leverage"]))
    lines.append("")
    lines.append("Next: trader-risk checks it, then trader-broker asks you yes or no.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- weights mode (v1)

def normalise(rows, blocked=(), top=None):
    """Drop blocked and non-positive weights, keep the top N, renormalise to 100."""
    blocked = {str(b).upper() for b in (blocked or [])}
    dropped, keep, seen = [], [], set()
    for r in rows:
        if r["ticker"] in seen:
            raise SizeError("%s is listed twice in the weights" % r["ticker"])
        seen.add(r["ticker"])
        if r["ticker"] in blocked:
            dropped.append({"ticker": r["ticker"], "reason": "on your blocked list"})
        elif r["weight"] <= 0:
            dropped.append({"ticker": r["ticker"], "reason": "weight is zero or negative"})
        else:
            keep.append(dict(r))
    keep.sort(key=lambda r: -r["weight"])
    if top:
        for r in keep[top:]:
            dropped.append({"ticker": r["ticker"], "reason": "outside the top %d" % top})
        keep = keep[:top]
    total = sum(r["weight"] for r in keep)
    for r in keep:
        r["source_weight"] = r["weight"]
        r["weight"] = 100.0 * r["weight"] / total if total else 0.0
    return keep, dropped


def allocate(rows, investable, cap):
    """Exact (unrounded) rupees per symbol with the cap enforced.

    Proportional to weight; any stock over the cap is pinned at the cap and
    the rest of the money is shared over the others, repeated until no stock
    is over. Returns ({symbol: rupees}, set of capped symbols)."""
    alloc, capped = {}, set()
    active = [r for r in rows]
    money_left = investable
    while active:
        wsum = sum(r["weight"] for r in active)
        trial = {r["ticker"]: money_left * r["weight"] / wsum for r in active}
        over = [r for r in active if trial[r["ticker"]] > cap + EPS]
        if not over:
            alloc.update(trial)
            break
        for r in over:
            alloc[r["ticker"]] = cap
            capped.add(r["ticker"])
            money_left -= cap
        active = [r for r in active if r["ticker"] not in capped]
        if money_left <= EPS:
            for r in active:
                alloc[r["ticker"]] = 0.0
            break
    return alloc, capped


def round_to_unit(exact, capped, order, cap, unit):
    """Round to whole paise (unit 0.01) and keep the total.

    The total placed is the exact total rounded DOWN to the unit, so it never
    spends money you do not have. Each slice is rounded to the nearest unit,
    then the difference goes to the biggest stock that is not capped, then the
    next, never pushing anyone over the cap."""
    def units(x):
        return int(math.floor(x / unit + EPS))

    cap_u = units(cap)
    total_u = units(sum(exact.values()))
    out = {}
    for t, v in exact.items():
        out[t] = cap_u if t in capped else int(math.floor(v / unit + 0.5 + EPS))
    diff = total_u - sum(out.values())
    takers = [t for t in order if t not in capped]
    guard = 0
    while diff != 0 and takers and guard < 100000:
        moved = False
        for t in takers:
            if diff == 0:
                break
            if diff > 0 and out[t] + 1 <= cap_u:
                out[t] += 1
                diff -= 1
                moved = True
            elif diff < 0 and out[t] > 0:
                out[t] -= 1
                diff += 1
                moved = True
        if not moved:
            break
        guard += 1
    return {t: round(u * unit, 2) for t, u in out.items()}


def whole_shares(rupees, prices, order, cap, cash):
    """Whole shares: floor each slice, then buy one more share at a time for the
    stock furthest below its slice while cash lasts and the cap holds."""
    shares = {}
    for t in order:
        shares[t] = int(math.floor(rupees[t] / prices[t] + EPS)) if rupees[t] > 0 else 0
    spent = sum(shares[t] * prices[t] for t in order)
    left = cash - spent
    while True:
        best, gap = None, None
        for t in order:
            p = prices[t]
            if p > left + EPS or (shares[t] + 1) * p > cap + EPS:
                continue
            g = rupees[t] - shares[t] * p
            if best is None or g > gap + EPS:
                best, gap = t, g
        if best is None:
            break
        shares[best] += 1
        left -= prices[best]
    return shares


def size(rows, cfg, budget=None, prices=None, top=None):
    """Weights mode. Pure function: weights + rules -> the full sizing result (a dict). Whole shares only."""
    budget = float(budget if budget is not None else (cfg.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json")
    buf_pct = float(cfg.get("cash_buffer_pct", 10))
    cap_pct = float(cfg.get("max_position_pct", 25))
    min_order = float(cfg.get("min_order_inr", 500) or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    if not 0 < cap_pct <= 100:
        raise SizeError("max_position_pct must be above 0 and at most 100")
    prices = prices or {}

    keep, dropped = normalise(rows, cfg.get("blocked"), top)
    if not keep:
        raise SizeError("nothing to size: no weights left after the blocked list")

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    investable = round(budget - cash_buffer, 2)
    cap = round(budget * cap_pct / 100.0, 2)
    unit = 0.01

    missing = [r["ticker"] for r in keep if not prices.get(r["ticker"])]
    if missing:
        raise SizeError("whole shares need a price for: %s (add them to trader/prices.json, "
                        "e.g. with trader-alerts: alerts.py quote %s --write)"
                        % (", ".join(missing), " ".join(missing)))

    while True:
        live = [dict(r) for r in keep]
        tot = sum(r["source_weight"] for r in live)
        for r in live:
            r["weight"] = 100.0 * r["source_weight"] / tot
        exact, capped = allocate(live, investable, cap)
        order = [r["ticker"] for r in sorted(live, key=lambda r: -exact[r["ticker"]])]
        rupees = round_to_unit(exact, capped, order, cap, unit)
        shares = whole_shares(rupees, prices, order, cap, sum(rupees.values()))
        rupees = {t: round(shares[t] * prices[t], 2) for t in order}
        small = [r for r in live if rupees[r["ticker"]] < min_order - EPS]
        if not small:
            break
        worst = min(small, key=lambda r: (r["source_weight"], r["ticker"]))
        if shares[worst["ticker"]] == 0 and prices.get(worst["ticker"], 0) > 0:
            reason = ("one share costs %s, more than its slice; it cannot be bought in whole shares"
                      % money(prices[worst["ticker"]]))
        else:
            reason = "its slice (%s) is under your %s minimum order" % (money(rupees[worst["ticker"]]),
                                                                       money(min_order))
        dropped.append({"ticker": worst["ticker"], "reason": reason})
        keep = [r for r in keep if r["ticker"] != worst["ticker"]]
        if not keep:
            raise SizeError("every slice is under your minimum order; raise the budget or hold fewer stocks")

    positions = []
    for r in sorted(live, key=lambda r: -r["weight"]):
        t = r["ticker"]
        p = {"ticker": t, "name": r["name"], "weight": round(r["weight"], 2),
             "source_weight": r["source_weight"], "inr": rupees[t], "capped": t in capped}
        if prices.get(t):
            p["price"] = prices[t]
            p["shares"] = shares[t]
        positions.append(p)
    invested = round(sum(p["inr"] for p in positions), 2)
    return {
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer,
        "investable": investable, "max_position_pct": cap_pct, "cap": cap,
        "min_order_inr": min_order, "whole_shares": True,
        "positions": positions, "invested": invested,
        "idle": round(investable - invested, 2), "dropped": dropped,
    }


def orders_from(result, cfg=None, source="", date=None):
    """Weights mode: proposed CNC (delivery) market buys, one per position, in place_order shape."""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    out = []
    for p in result["positions"]:
        if p["inr"] <= 0 or not p.get("shares"):
            continue
        why = "%.2f%% of the split" % p["weight"]
        if source:
            why = "%s, weights from %s" % (why, source)
        if p["capped"]:
            why += "; capped at %s%% of the %s budget" % (_num(result["max_position_pct"]), money(result["budget"]))
        out.append({"variety": "regular", "exchange": str(c.get("exchange") or "NSE").upper(),
                    "tradingsymbol": p["ticker"], "transaction_type": "BUY", "quantity": int(p["shares"]),
                    "product": "CNC", "order_type": "MARKET", "validity": "DAY",
                    "tag": _tag("W", p["ticker"], date), "why": why})
    return out


def report(result, orders, source=""):
    """Weights mode table."""
    r = result
    lines = []
    if source:
        lines.append("Weights from: %s" % source)
    lines.append("Budget %s, cash buffer %s%% = %s, investing %s, cap %s%% = %s per stock"
                 % (money(r["budget"]), _num(r["cash_buffer_pct"]), money(r["cash_buffer"]),
                    money(r["investable"]), _num(r["max_position_pct"]), money(r["cap"])))
    lines.append("")
    head = "%-12s %-22s %8s %14s %12s %8s" % ("symbol", "name", "weight", "rupees", "price", "shares")
    lines.append(head + "  note")
    for p in r["positions"]:
        row = "%-12s %-22s %7.2f%% %14s %12s %8s" % (p["ticker"], (p["name"] or "")[:22], p["weight"],
                                                    money(p["inr"]), money(p["price"]) if "price" in p else "",
                                                    p.get("shares", ""))
        if p["capped"]:
            row += "  capped at %s%%" % _num(r["max_position_pct"])
        lines.append(row)
    lines.append("%-12s %-22s %8s %14s" % ("", "invested", "", money(r["invested"])))
    lines.append("%-12s %-22s %8s %14s" % ("", "cash buffer", "", money(r["cash_buffer"])))
    lines.append("%-12s %-22s %8s %14s" % ("", "left idle", "", money(r["idle"])))
    if r["idle"] >= 1:
        lines.append("")
        lines.append("Left idle %s: whole shares do not divide evenly." % money(r["idle"]))
    for d in r["dropped"]:
        lines.append("Dropped %s: %s" % (d["ticker"], d["reason"]))
    lines.append("")
    lines.append("Proposed orders (Kite place_order arguments, CNC delivery, nothing has been placed):")
    for o in orders:
        lines.append("  %s %s x %d, %s, %s, %s" % (o["transaction_type"], o["tradingsymbol"], o["quantity"],
                                                  o["product"], o["order_type"], o["validity"]))
    lines.append("")
    lines.append("These buys have no stop. trader-risk blocks them while \"require_stop\" is true and applies "
                 "your day-trade limits to every buy.")
    lines.append("Next: trader-risk checks every order, then trader-broker asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- orders file

def write_orders(path, orders, cfg, source="", now_utc=None):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    doc = {
        "created_by": "trader-budget",
        "created_at": utc_iso(now_utc),
        "status": "proposed",
        "paper": bool(cfg.get("paper", True)),
        "broker": "kite",
        "note": KITE_ENTRY_NOTE,
        "source": source,
        "orders": orders,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- main

def run_plan(a, cfg):
    now = parse_now(a.now)
    today = nm.to_ist(now).date().isoformat()
    plan = read_json(a.plan, "plan (run trader-exit first, or use --weights for a split)")
    limit = None
    if a.limit is not None and not a.market:
        limit = _f(a.limit)
        if limit is None or limit <= 0:
            raise SizeError("--limit wants a price, like --limit 1250.50")
    sym = ""
    try:
        sym = nm.normalize_symbol((plan or {}).get("symbol") or "")
    except nm.MarketError:
        pass
    notes, price = [], None
    ppath = a.prices or ("trader/prices.json" if os.path.exists("trader/prices.json") else None)
    if ppath and a.market and sym:
        pdoc = read_json(ppath, "prices")
        p, t, fresh = fresh_price(pdoc, sym, now)
        if p is not None and fresh:
            price = p
            if snap(p, tick_of(cfg)) > snap(_f(plan.get("entry")) or 0, tick_of(cfg)):
                notes.append("%s last %s (as of %s IST) is above the plan's entry %s, so the risk "
                             "is bigger: sized at %s." % (sym, _price(p), nm.to_ist(t).strftime("%H:%M"),
                                                          _price(plan.get("entry")), _price(p)))
        elif p is not None:
            notes.append("%s in %s is %s: not used (older than %d minutes). Sized at the plan's entry."
                         % (sym, ppath, ("from " + nm.to_ist(t).strftime("%Y-%m-%d %H:%M") + " IST")
                            if t else "undated", FRESH_MINUTES))
    cash = None
    if a.portfolio and os.path.exists(a.portfolio):
        pf = read_json(a.portfolio, "portfolio")
        cash = _f(pf.get("cash")) if isinstance(pf, dict) else None
    if a.lower_circuit is not None:
        plan["lower_circuit"] = a.lower_circuit
    if a.upper_circuit is not None:
        plan["upper_circuit"] = a.upper_circuit
    s = size_trade(plan, cfg, a.budget, limit, price, cash, today, market=a.market)
    order = entry_order(s)
    if order and a.write:
        src = "trader/plan.json (%s %s %s)" % (s["symbol"], s["setup"] or "plan", s["date"] or today)
        write_orders(a.write, [order], cfg, src, now)
    if a.json:
        print(json.dumps({"sizing": s, "orders": [order] if order else [], "notes": notes}, indent=2))
    else:
        print(plan_report(s, order, notes))
        if order and a.write:
            print("Wrote 1 proposed entry order (with its stop and target) to %s" % a.write)
    return 0 if order else 1


def run_weights(a, cfg):
    source = ""
    if a.weights:
        rows = parse_weights(weights_from_arg(a.weights))
    else:
        data = read_json(a.targets, "weights file")
        rows = parse_weights(data)
        if isinstance(data, dict):
            source = data.get("source", "")
    prices = load_prices(a.prices)
    if not prices and os.path.exists("trader/prices.json") and not a.prices:
        prices = load_prices("trader/prices.json")
    result = size(rows, cfg, a.budget, prices, a.top)
    now = parse_now(a.now)
    orders = orders_from(result, cfg, source, nm.to_ist(now).date().isoformat())
    if a.write:
        write_orders(a.write, orders, cfg, source, now)
    if a.json:
        print(json.dumps({"result": result, "orders": orders}, indent=2))
    else:
        print(report(result, orders, source))
        if a.write:
            print("Wrote %d proposed orders to %s" % (len(orders), a.write))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Size a day trade to your budget and max loss (trader-budget).")
    ap.add_argument("--plan", default="trader/plan.json", help="the trade plan (default trader/plan.json)")
    ap.add_argument("--config", default="trader/config.json", help="rules (default trader/config.json)")
    ap.add_argument("--budget", type=float, help="override the budget in the config")
    ap.add_argument("--limit", metavar="PRICE", help="a limit order at PRICE (default: the plan's entry)")
    ap.add_argument("--market", action="store_true", help="a MARKET order instead of a limit")
    ap.add_argument("--lower-circuit", type=float, help="today's lower circuit (from Kite get_quotes)")
    ap.add_argument("--upper-circuit", type=float, help="today's upper circuit (from Kite get_quotes)")
    ap.add_argument("--prices", help="prices JSON (default trader/prices.json if it exists)")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; its cash caps the size if it exists")
    ap.add_argument("--now", help="pretend it is this India time, e.g. \"2026-10-29 10:15\"")
    ap.add_argument("--write", nargs="?", const="trader/orders.json", metavar="PATH",
                    help="write the proposed order (default trader/orders.json)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the report")
    g = ap.add_argument_group("weights mode (split a budget across a list)")
    g.add_argument("--weights", help="inline weights: RELIANCE=22.04,TCS=17.14")
    g.add_argument("--targets", help="a weights file: {\"weights\": [{\"ticker\", \"weight\"}]}")
    g.add_argument("--top", type=int, help="keep only the N biggest weights")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config)
        if a.weights or a.targets:
            return run_weights(a, cfg)
        if a.top:
            raise SizeError("--top works with --weights or --targets")
        return run_plan(a, cfg)
    except SizeError as e:
        print("size.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
