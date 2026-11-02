#!/usr/bin/env python3
"""Pre-trade risk check for trader-risk (NSE, rupees, Kite order fields). Standard library only, no network.

Reads the proposed orders (trader/orders.json) and checks every one against
your rules (trader/config.json), your account (trader/portfolio.json),
prices (trader/prices.json) and what was placed today (trader/state/broker.json).
Each order gets PASS or BLOCK with the reasons. Orders are checked in file
order as if each PASS had filled, so buys that are fine one by one but break
a limit together are caught.

    python3 check.py                                 # the trader/ files, writes trader/state/risk.json
    python3 check.py --now "2026-10-29 10:15"        # pin the clock (India time)
    python3 check.py --orders trader/orders.json --json
    python3 check.py --no-write                      # check without recording the verdicts

Orders are in the argument names of the Kite connector's place_order tool: variety,
exchange, tradingsymbol, transaction_type, quantity, product, order_type, price,
trigger_price, validity, tag. Extra keys ("why", "plan", "role", "exit_orders") are ours
and are never sent to Kite.

Entries and exits. An ENTRY opens or adds to a position (a BUY, or a SELL that would go
short). An EXIT only reduces what you hold. Getting out must always be possible: an exit is
checked for format, asset and short sales only; every other rule turns into a warning for it.

What blocks an order (every order):

  format        missing symbol or side, quantity not a whole number, unknown order_type /
                product / validity, a variety other than "regular", a LIMIT or SL order
                without a price, an SL / SL-M order without a trigger_price, a SELL SL whose
                limit is above its trigger (or a BUY SL below it), a price off the tick
                (Rs 0.05), a tag that is not alphanumeric or is over 20 characters, a TTL
                order without validity_ttl
  asset         futures and options (NFO / BFO, or an F&O-looking symbol) unless
                "allow_options": true; commodities (MCX); anything that does not look like
                an NSE cash symbol
  short         selling more than you hold, unless "allow_short": true

What blocks an entry (on top):

  blocked       a symbol on your "blocked" list
  size          under min_order_inr, over max_order_inr
  product       NRML or MTF (margin funding) unless "allow_margin": true
  position      the stock would be over max_position_pct of the account
  cash          the buy would dip into your cash buffer, or need margin
  duplicate     the same symbol and side twice in this batch, or already placed today
  daily limit   max_orders_per_day orders placed today
  stop          no stop while "require_stop": true (an "exit_orders" stop-loss trigger or the
                plan stop); a stop on the wrong side of the entry
  circuit       the entry or the stop outside today's circuit band (plan.lower_circuit /
                upper_circuit, or trader/prices.json detail), when the band is known
  loss          the loss at the stop PLUS the round-trip charges (brokerage, STT, exchange,
                GST, stamp) over max_loss_per_trade_inr. The entry is the limit price for a
                LIMIT order, else the HIGHER of the plan's entry and a fresh price (under 15
                minutes old), so a stale plan cannot understate the risk
  reward:risk   (target gain minus charges) / (loss at the stop plus charges) under
                min_reward_risk. Charges are an estimate from Zerodha's published equity
                intraday schedule
  daily loss    the max losses at the stops of today's entries plus this one over
                max_daily_loss_inr (win or lose, each counts in full)
  trades        max_trades_per_day entries already placed today
  window        before no_entries_before or after no_entries_after (India time), or the
                market is closed (weekend, NSE holiday, outside 09:15 to the close; stocks in
                "fo_stocks" stop continuous trading at 15:15), or a MIS entry once Zerodha's
                intraday square-off has started (15:12 for F&O stocks, 15:25 for the rest)

Exit code 0 when every order passes, 1 when any order is blocked.
The verdicts go to trader/state/risk.json with a fingerprint of each order,
so trader-broker only shows an order as ready if THIS exact order passed today.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_market as nm  # noqa: E402  (one shared copy of the India helpers, copied into this folder)

DEFAULTS = {
    "budget": None, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_inr": 80000,
    "min_order_inr": 500, "max_loss_per_trade_inr": 500, "max_daily_loss_inr": 1500, "max_trades_per_day": 3,
    "max_orders_per_day": 10, "min_reward_risk": 1.5, "require_stop": True,
    "no_entries_before": "09:30", "no_entries_after": "14:30", "flat_by": "15:00", "tick_size": 0.05,
    "paper": True, "blocked": [], "fo_stocks": [], "allow_options": False, "allow_margin": False,
    "allow_short": False,
}
KITE_FIELDS = ("variety", "exchange", "tradingsymbol", "transaction_type", "quantity", "product",
               "order_type", "price", "trigger_price", "validity", "validity_ttl", "disclosed_quantity",
               "iceberg_legs", "iceberg_quantity", "tag")
INT_FIELDS = ("quantity", "disclosed_quantity", "iceberg_legs", "iceberg_quantity", "validity_ttl")
PRICE_FIELDS = ("price", "trigger_price")
ORDER_TYPES = {"MARKET", "LIMIT", "SL", "SL-M"}
PRODUCTS = {"MIS", "CNC", "NRML", "MTF"}
VALIDITIES = {"DAY", "IOC", "TTL"}
EXCHANGES = {"NSE", "BSE", "NFO", "BFO", "MCX"}
DERIV_EXCHANGES = {"NFO", "BFO"}
DERIV_SYMBOL = re.compile(r"(\d{2}[A-Z]{3}.*(FUT|CE|PE)$)|(\d+(CE|PE)$)|(FUT$)")
TAG = re.compile(r"^[A-Za-z0-9]{1,20}$")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
CENT = 0.005
FRESH_MINUTES = 15
REF_WORDS = {"plan": "the plan's entry", "price": "the latest price", "limit": "the limit price",
             "trigger": "the trigger price", "stale price": "an old price"}


class RiskError(Exception):
    pass


# ---------------------------------------------------------------- time (India, no tz files)

def parse_now(text):
    try:
        return nm.parse_now(text)
    except nm.MarketError as e:
        raise RiskError(str(e))


def parse_utc(text):
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def hhmm(value, key):
    """"09:30" -> time(9, 30); None or "" -> None (that bound is off)."""
    if value in (None, "", False):
        return None
    m = HHMM.match(str(value).strip())
    if not m:
        raise RiskError("%s must be an India time like \"09:30\", got %r" % (key, value))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def window_check(now_ist, c, cas=False, product="MIS"):
    """Reasons an entry cannot be placed now (India time). Empty when the window is off."""
    before = hhmm(c.get("no_entries_before"), "no_entries_before")
    after = hhmm(c.get("no_entries_after"), "no_entries_after")
    if before is None and after is None:
        return [], []
    warns = []
    d, t = now_ist.date(), now_ist.time().replace(second=0, microsecond=0)
    if not nm.calendar_known(d):
        warns.append("the NSE holiday list covers %s only: check nseindia.com for %s"
                     % (" and ".join(map(str, nm.CALENDAR_YEARS)), d.isoformat()))
    ok, why = nm.trading_day(d)
    stamp = now_ist.strftime("%a %Y-%m-%d %H:%M")
    if not ok:
        return ["window: the market is closed today (%s, %s IST)" % (why, stamp)], warns
    o, close = nm.session(d, cas)
    if t < o or now_ist.time() >= close:
        return ["window: the market is closed (%s IST; the session is %s to %s)"
                % (stamp, o.strftime("%H:%M"), close.strftime("%H:%M"))], warns
    if before is not None and t < before:
        return ["window: no new entries before %s IST (it is %s)" % (before.strftime("%H:%M"),
                                                                     t.strftime("%H:%M"))], warns
    if after is not None and t > after:
        return ["window: no new entries after %s IST (it is %s)" % (after.strftime("%H:%M"),
                                                                    t.strftime("%H:%M"))], warns
    if product == "MIS" and t >= nm.squareoff_time(cas):
        return ["window: Zerodha squares off MIS positions from %s IST (%s stock): a MIS entry now "
                "would be closed at once, for Rs 50 + GST an order"
                % (nm.squareoff_time(cas).strftime("%H:%M"), "F&O" if cas else "non-F&O")], warns
    return [], warns


# ---------------------------------------------------------------- files and helpers

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise RiskError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise RiskError("%s is not valid JSON: %s" % (path, e))


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _norm(k, v):
    """One field as Kite takes it: whole numbers as ints, prices to the paisa, symbols upper-case."""
    if k in INT_FIELDS:
        f = _f(v)
        return int(f) if f is not None and f == int(f) else v
    if k in PRICE_FIELDS:
        f = _f(v)
        return round(f, 2) if f is not None else v
    if k in ("variety",):
        return str(v).lower()
    if k in ("exchange", "tradingsymbol", "transaction_type", "product", "order_type", "validity"):
        return str(v).upper()
    return v


def fingerprint(order):
    """Same function in trader-broker/ticket.py: the exact order that was checked."""
    core = {k: _norm(k, order[k]) for k in KITE_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def money(x):
    x = float(x)
    if abs(x - round(x)) < 0.005:
        return nm.inr(round(x), 0)
    return nm.inr(x, 2)


def _p(x):
    return "%.2f" % float(x)


def _pct(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.2f" % x)


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0")


def pair_charges(entry, exit_price, qty, side, costs=None):
    """Round-trip charges in rupees for a trade opened at entry and closed at exit_price."""
    first, second = ("BUY", "SELL") if side == "buy" else ("SELL", "BUY")
    return nm.charges(entry, qty, first, costs)["total"] + nm.charges(exit_price, qty, second, costs)["total"]


def account(cfg, portfolio, prices):
    """Starting account: cash and position values. No portfolio = a fresh
    account funded with the budget."""
    warnings = []
    if portfolio is None:
        budget = _f(cfg.get("budget"))
        if not budget:
            raise RiskError("no trader/portfolio.json and no budget in the config: "
                            "nothing to check the orders against")
        return {"cash": budget, "shares": {}, "values": {}}, warnings
    cash = _f(portfolio.get("cash"))
    if cash is None:
        raise RiskError("portfolio.json needs a \"cash\" number")
    shares, values = {}, {}
    for p in portfolio.get("positions", []):
        t = str(p.get("ticker") or p.get("symbol") or "").upper()
        if not t:
            continue
        n = _f(p.get("shares", p.get("qty")))
        if n is None:
            n = sum(_f(l.get("shares")) or 0 for l in p.get("lots", []))
        shares[t] = shares.get(t, 0) + n
        if prices.get(t):
            values[t] = shares[t] * prices[t]
        else:
            basis = sum(_f(l.get("cost")) or 0 for l in p.get("lots", []))
            values[t] = basis
            warnings.append("no price for %s: valued at its cost basis %s" % (t, money(basis)))
    return {"cash": cash, "shares": shares, "values": values}, warnings


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def rec_is_entry(rec):
    """trader-broker logs the kind trader-risk gave; older records: a buy is an entry."""
    k = rec.get("kind")
    if k:
        return k == "entry"
    return str(rec.get("side", "")).lower() == "buy"


def placed_today(state, today):
    """Orders recorded as placed today by trader-broker."""
    out = []
    for rec in (state or {}).get("orders", []):
        if rec.get("status") == "placed" and rec_day(rec) == today:
            out.append(rec)
    return out


def exit_legs(o):
    """The entry's own stop and target orders: (stop_order or None, target_order or None, [problems])."""
    legs = o.get("exit_orders")
    if not legs:
        return None, None, []
    stop = target = None
    problems = []
    if not isinstance(legs, list):
        return None, None, ["exit_orders must be a list of orders"]
    for leg in legs:
        role = str((leg or {}).get("role") or "").lower()
        if role == "stop" and stop is None:
            stop = leg
        elif role == "target" and target is None:
            target = leg
        else:
            problems.append("exit_orders has a leg with role %r that is not a single stop or target" % role)
    return stop, target, problems


# ---------------------------------------------------------------- the check

def check(orders, cfg, portfolio=None, prices=None, state=None, today=None, now=None, price_times=None,
          circuits=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...]}.

    now          naive UTC (default: the real clock); its India date is "today"
    price_times  {SYM: naive UTC time of that price}; None = every price counts as fresh
    circuits     {SYM: (lower, upper)} from Kite quotes, when known"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    cfg_tick = _f(c.get("tick_size")) or 0.05
    tick = cfg_tick
    costs = c.get("costs") or None
    prices = {str(k).upper(): float(v) for k, v in (prices or {}).items() if _f(v)}
    circuits = {str(k).upper(): v for k, v in (circuits or {}).items()}
    now_utc = now or parse_now(None)
    now_ist = nm.to_ist(now_utc)
    today = today or now_ist.date().isoformat()
    acct, top_warn = account(c, portfolio, prices)
    blocked = {str(b).upper() for b in c.get("blocked") or []}
    fo = {str(s).upper() for s in c.get("fo_stocks") or []}
    done_today = placed_today(state, today)
    count_today = len(done_today)
    entries_today = [r for r in done_today if rec_is_entry(r)]
    loss_today = 0.0
    for r in entries_today:
        ml = _f(r.get("max_loss"))
        if ml is None:
            top_warn.append("an entry placed today (%s %s) has no max loss in the log; not counted in "
                            "the daily loss" % (r.get("side"), r.get("symbol")))
        else:
            loss_today += ml
    placed_keys = {(str(r.get("symbol", "")).upper(), str(r.get("side", "")).lower()) for r in done_today}
    seen = set()
    results = []
    batch_entries, batch_loss = 0, 0.0
    if not c.get("paper", True):
        top_warn.append("LIVE account (\"paper\": false): these orders use real money")
    if not nm.calendar_known(now_ist.date()):
        top_warn.append("the NSE holiday list covers %s only: check nseindia.com for %s"
                        % (" and ".join(map(str, nm.CALENDAR_YEARS)), now_ist.date().isoformat()))

    def fresh(sym):
        if sym not in prices:
            return None
        if price_times is None:
            return prices[sym]
        t = price_times.get(sym)
        if t is None:
            return None
        age = (now_utc - t).total_seconds()
        return prices[sym] if -60 <= age <= FRESH_MINUTES * 60 else None

    for i, o in enumerate(orders, 1):
        reasons, warns, info = [], [], []
        later = []                       # entry-only reasons, turned into warnings for an exit
        sym = str(o.get("tradingsymbol", "")).strip().upper()
        side = str(o.get("transaction_type", "")).strip().lower()
        typ = str(o.get("order_type", "")).strip().upper()
        product = str(o.get("product", "")).strip().upper()
        exch = str(o.get("exchange", "NSE")).strip().upper()
        validity = str(o.get("validity", "DAY")).strip().upper()
        variety = str(o.get("variety", "regular")).strip().lower()
        qty = o.get("quantity")
        plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
        tick = _f(plan.get("tick_size")) or cfg_tick   # per instrument: Kite lists it (some stocks trade on Rs 0.10)
        value = None

        # format
        if not sym:
            reasons.append("format: no tradingsymbol")
        if side not in ("buy", "sell"):
            reasons.append("format: transaction_type must be BUY or SELL, got %r" % o.get("transaction_type"))
        q = _f(qty)
        if q is None or q <= 0:
            reasons.append("format: quantity must be a positive whole number, got %r" % qty)
            q = None
        elif abs(q - round(q)) > 1e-9:
            reasons.append("format: quantity must be whole shares, got %r" % qty)
        if exch not in EXCHANGES:
            reasons.append("format: unknown exchange %r" % o.get("exchange"))
        if typ not in ORDER_TYPES:
            reasons.append("format: unknown order_type %r (MARKET, LIMIT, SL or SL-M)" % o.get("order_type"))
        if product not in PRODUCTS:
            reasons.append("format: unknown product %r (MIS, CNC, NRML or MTF)" % o.get("product"))
        if validity not in VALIDITIES:
            reasons.append("format: unknown validity %r (DAY, IOC or TTL)" % o.get("validity"))
        if validity == "TTL" and not _f(o.get("validity_ttl")):
            reasons.append("format: a TTL order needs validity_ttl (minutes)")
        if variety != "regular":
            reasons.append("format: variety %r: this pack places regular orders only" % o.get("variety"))
        for k in PRICE_FIELDS:
            if o.get(k) is not None:
                v = _f(o.get(k))
                if v is None or v <= 0:
                    reasons.append("format: %s must be a positive number, got %r" % (k, o.get(k)))
                elif not nm.on_tick(v, tick):
                    reasons.append("format: %s %s is off the Rs %s tick (try %s)"
                                   % (k, _p(v), _num(tick), _p(nm.round_to_tick(v, tick))))
        price, trigger = _f(o.get("price")), _f(o.get("trigger_price"))
        if typ in ("LIMIT", "SL") and price is None:
            reasons.append("format: a %s order needs a price" % typ)
        if typ in ("SL", "SL-M") and trigger is None:
            reasons.append("format: a %s order needs a trigger_price" % typ)
        if typ == "SL" and price is not None and trigger is not None:
            if side == "sell" and price > trigger + CENT:
                reasons.append("format: a SELL SL order's limit price %s is above its trigger %s: Kite wants "
                               "the limit at or below the trigger" % (_p(price), _p(trigger)))
            if side == "buy" and price < trigger - CENT:
                reasons.append("format: a BUY SL order's limit price %s is below its trigger %s: Kite wants "
                               "the limit at or above the trigger" % (_p(price), _p(trigger)))
        tag = o.get("tag")
        if tag is not None and not TAG.match(str(tag)):
            reasons.append("format: tag must be 1 to 20 letters and digits, got %r" % tag)

        # asset
        if exch == "MCX":
            reasons.append("asset: %s is on MCX (commodities); this pack trades NSE and BSE cash stocks only" % sym)
        elif exch in DERIV_EXCHANGES or (sym and DERIV_SYMBOL.search(sym)):
            if not c.get("allow_options"):
                reasons.append("asset: %s is a futures or options contract; F&O is off "
                               "(\"allow_options\": false)" % sym)
            else:
                warns.append("F&O order: derivatives can lose more than you pay; lots and margin apply")
        elif sym and not nm.SYMBOL_RE.match(sym):
            reasons.append("asset: %s does not look like an NSE or BSE cash symbol; this pack trades "
                           "stocks and ETFs only" % sym)

        # the reference price: what one share costs to get in
        plan_entry = _f(plan.get("entry"))
        live = fresh(sym)
        ref, ref_from = None, None
        if typ in ("LIMIT", "SL") and price is not None:
            ref, ref_from = price, "limit"
        elif typ == "SL-M" and trigger is not None:
            ref, ref_from = trigger, "trigger"
        if ref is None:
            cands = [(p, w) for p, w in ((plan_entry, "plan"), (live, "price")) if p]
            if cands:
                ref, ref_from = max(cands, key=lambda x: x[0])
                ref = nm.round_to_tick(ref, tick)
                if live and ref_from == "price" and plan_entry and live > plan_entry + CENT:
                    info.append("%s last %s is above the plan's entry %s: the risk is measured from %s"
                                % (sym, _p(ref), _p(plan_entry), _p(ref)))
            elif sym in prices:
                ref, ref_from = prices[sym], "stale price"
        if plan_entry and live is None and sym in prices and price_times is not None and typ == "MARKET":
            warns.append("the %s price in trader/prices.json is older than %d minutes: not used"
                         % (sym, FRESH_MINUTES))
        if q:
            if ref:
                value = q * ref

        # entry or exit
        held = acct["shares"].get(sym, 0.0)
        held_value = acct["values"].get(sym, 0.0)
        exiting = False
        if side == "sell" and held > 1e-9:
            if q is not None:
                exiting = q <= held + 1e-9
        elif side == "buy" and held < -1e-9 and q is not None:
            exiting = q <= -held + 1e-9
        kind = "exit" if exiting else "entry"
        if kind == "entry" and value is None and q:
            later.append("price: no price for %s to size a %s-share order; add it to "
                         "trader/prices.json (trader-alerts: alerts.py quote %s --write)" % (sym, qty, sym))

        # blocked list, size, product
        if sym in blocked:
            later.append("blocked: %s is on your blocked list" % sym)
        if product in ("NRML", "MTF") and not c.get("allow_margin"):
            later.append("product: %s is %s; margin is off (\"allow_margin\": false). Use MIS for an "
                         "intraday trade or CNC for delivery" % (product, "margin funding" if product == "MTF"
                                                                   else "a carry-forward product"))
        if value is not None:
            if value < float(c.get("min_order_inr") or 0) - CENT:
                later.append("size: %s is under your %s minimum order"
                             % (money(value), money(c["min_order_inr"])))
            if c.get("max_order_inr") and value > float(c["max_order_inr"]) + CENT:
                later.append("size: %s is over your %s max order"
                             % (money(value), money(c["max_order_inr"])))

        # account effects, simulated in order
        cash = acct["cash"]
        total = cash + sum(acct["values"].values())
        buffer_inr = total * float(c.get("cash_buffer_pct") or 0) / 100.0
        if value is not None and side == "buy" and kind == "entry":
            after_cash = cash - value
            if after_cash < -CENT and not c.get("allow_margin"):
                later.append("cash: needs %s but only %s cash; that would be margin (borrowed money)"
                             % (money(value), money(cash)))
            elif after_cash < buffer_inr - CENT and not (c.get("allow_margin") and after_cash < 0):
                later.append("cash: leaves %s cash, under your %s%% buffer of %s"
                             % (money(max(after_cash, 0)), _pct(c["cash_buffer_pct"]), money(buffer_inr)))
            pos_after = held_value + value
            limit = total * float(c.get("max_position_pct") or 100) / 100.0
            if total > 0 and pos_after > limit + CENT:
                later.append("position: %s would be %s, %.1f%% of the %s account, over your %s%% cap"
                             % (sym, money(pos_after), 100.0 * pos_after / total, money(total),
                                _pct(c["max_position_pct"])))
        if side == "sell" and sym and kind == "entry":
            if q is not None:
                over = q > held + 1e-9
            else:
                over = held <= 0
            if over and not c.get("allow_short"):
                reasons.append("short: selling more %s than you hold (%s shares); short selling is off. "
                               "If you bought it, record the fill first (ticket.py fill)" % (sym, _num(held)))

        # day-trade checks: stop, loss, reward:risk, daily loss, trades, window, circuit
        max_loss = risk_ps = rr = stop = target = None
        charges_stop = None
        if kind == "entry" and side in ("buy", "sell"):
            long = side == "buy"
            stop_leg, target_leg, leg_problems = exit_legs(o)
            for pr in leg_problems:
                reasons.append("format: " + pr)
            for leg, nm_ in ((stop_leg, "stop"), (target_leg, "target")):
                if not leg:
                    continue
                lq = _f(leg.get("quantity"))
                if lq is not None and q is not None and abs(lq - q) > 1e-9:
                    reasons.append("format: the %s leg is for %s shares but the entry is for %s"
                                   % (nm_, _num(lq), _num(q)))
                for k in PRICE_FIELDS:
                    v = _f(leg.get(k))
                    if v is not None and not nm.on_tick(v, tick):
                        reasons.append("format: the %s leg's %s %s is off the Rs %s tick (try %s)"
                                       % (nm_, k, _p(v), _num(tick), _p(nm.round_to_tick(v, tick))))
                want_side = "SELL" if long else "BUY"
                if str(leg.get("transaction_type", "")).upper() != want_side:
                    reasons.append("format: the %s leg must be a %s for a %s entry" % (nm_, want_side, side.upper()))
            leg_stop = _f((stop_leg or {}).get("trigger_price"))
            leg_sl_price = _f((stop_leg or {}).get("price"))
            if stop_leg and (str(stop_leg.get("order_type", "")).upper() not in ("SL", "SL-M") or leg_stop is None):
                reasons.append("format: the stop leg must be an SL or SL-M order with a trigger_price")
            plan_stop = _f(plan.get("stop"))
            stop = leg_stop if leg_stop else plan_stop
            target = _f((target_leg or {}).get("price")) or _f(plan.get("target"))
            if leg_stop and plan_stop and abs(leg_stop - plan_stop) > CENT:
                warns.append("the stop leg trigger %s differs from the plan's stop %s: the leg's is the one "
                             "that will be placed" % (_p(leg_stop), _p(plan_stop)))
            if stop:
                warns.append("the stop is a separate order, placed only after the entry fills (ticket.py "
                             "protect): until then nothing at Kite protects the position. Set a stop alert "
                             "(trader-alerts: alerts.py add --from-plan)")
            if leg_sl_price and leg_stop:
                gap = leg_stop - leg_sl_price if long else leg_sl_price - leg_stop
                if gap > 0:
                    info.append("stop-loss triggers at %s and sells with a limit of %s: a gap below the limit "
                                "leaves it unfilled" % (_p(leg_stop), _p(leg_sl_price)))
            if not stop:
                if c.get("require_stop", True):
                    later.append("stop: no stop. Every entry needs one (\"require_stop\": true): an "
                                 "\"exit_orders\" stop-loss, from trader-exit and trader-budget")
                else:
                    warns.append("no stop: the max loss is not capped, so it is not counted in the daily loss")
            elif ref:
                if (long and stop >= ref - CENT) or (not long and stop <= ref + CENT):
                    later.append("stop: the stop %s must be %s the entry %s for a %s"
                                 % (_p(stop), "below" if long else "above", _p(ref), side))
                else:
                    risk_ps = round(abs(ref - stop), 2)
                    shares = q if q is not None else 0
                    gross = round(shares * risk_ps, 2)
                    charges_stop = round(pair_charges(ref, stop, shares, side, costs), 2) if shares else 0.0
                    max_loss = round(gross + charges_stop, 2)
                    cap = _f(c.get("max_loss_per_trade_inr"))
                    if cap and max_loss > cap + CENT:
                        later.append("loss: %s shares x %s risk a share = %s at the stop, plus about %s charges = "
                                     "%s, over your %s max loss per trade"
                                     % (_num(shares), _p(risk_ps), money(gross), money(charges_stop),
                                        money(max_loss), money(cap)))
                    min_rr = _f(c.get("min_reward_risk"))
                    if target:
                        if (long and target <= ref + CENT) or (not long and target >= ref - CENT):
                            later.append("target: the target %s must be %s the entry %s"
                                         % (_p(target), "above" if long else "below", _p(ref)))
                        elif shares and max_loss:
                            gain = round(shares * abs(target - ref), 2)
                            net = gain - pair_charges(ref, target, shares, side, costs)
                            rr = round(net / max_loss, 2)
                            if min_rr and rr < min_rr - 1e-9:
                                later.append("reward:risk: %.2f after charges (target %s, entry %s, stop %s; "
                                             "%s net gain against %s loss) is under your minimum %s"
                                             % (rr, _p(target), _p(ref), _p(stop), money(net), money(max_loss),
                                                _num(min_rr)))
                    elif min_rr:
                        later.append("reward:risk: no target to measure it against (min_reward_risk %s)"
                                     % _num(min_rr))
            # circuit band
            lo = _f(plan.get("lower_circuit"))
            hi = _f(plan.get("upper_circuit"))
            if lo is None and hi is None and sym in circuits:
                lo, hi = circuits[sym]
            if lo is not None or hi is not None:
                for label, val in (("entry", ref), ("stop", stop)):
                    if val:
                        okc, whyc = nm.within_circuit(val, lo, hi)
                        if not okc:
                            later.append("circuit: the %s %s is %s: the price cannot trade there today"
                                         % (label, _p(val), whyc))
            daily = _f(c.get("max_daily_loss_inr"))
            if daily and max_loss is not None:
                used = loss_today + batch_loss
                if used + max_loss > daily + CENT:
                    later.append("daily loss: %s already at risk today plus %s for this one = %s, over your %s "
                                 "daily loss limit" % (money(used), money(max_loss), money(used + max_loss),
                                                       money(daily)))
            cap_t = int(c.get("max_trades_per_day") or 0)
            if cap_t and len(entries_today) + batch_entries >= cap_t:
                later.append("trades: %d entries placed today plus %d passed earlier in this batch, your limit "
                             "is %d a day" % (len(entries_today), batch_entries, cap_t))
            win_reasons, win_warns = window_check(now_ist, c, sym in fo, product if product in PRODUCTS else "MIS")
            later.extend(win_reasons)
            warns.extend(w for w in win_warns if w not in top_warn)
            if stop and ref and risk_ps:
                info.append("entry %s (%s), stop %s, target %s: risk %s a share"
                            % (_p(ref), REF_WORDS.get(ref_from, ref_from), _p(stop),
                               _p(target) if target else "none", _p(risk_ps)))
                info.append("max loss %s (incl. about %s charges)%s, reward:risk after charges %s%s"
                            % (money(max_loss), money(charges_stop or 0),
                               (" (limit %s)" % money(c["max_loss_per_trade_inr"]))
                               if _f(c.get("max_loss_per_trade_inr")) else "",
                               ("%.2f" % rr) if rr is not None else "n/a",
                               (" (min %s)" % _num(c["min_reward_risk"])) if _f(c.get("min_reward_risk")) else ""))
            if daily or cap_t:
                info.append("today before this: %d of %s trades, %s of %s daily loss"
                            % (len(entries_today) + batch_entries, cap_t or "unlimited",
                               money(loss_today + batch_loss), money(daily) if daily else "no limit on"))
        elif kind == "exit":
            # a protective exit: tell the user about a trigger outside the circuit band
            lo, hi = _f(plan.get("lower_circuit")), _f(plan.get("upper_circuit"))
            if lo is None and hi is None and sym in circuits:
                lo, hi = circuits[sym]
            for label, val in (("trigger", trigger), ("price", price)):
                if val and (lo is not None or hi is not None):
                    okc, whyc = nm.within_circuit(val, lo, hi)
                    if not okc:
                        later.append("circuit: the %s %s is %s" % (label, _p(val), whyc))

        # duplicates and the daily order limit
        key = (sym, side)
        if key in seen:
            later.append("duplicate: a second %s %s in this batch" % (side.upper(), sym))
        if key in placed_keys:
            later.append("duplicate: a %s %s was already placed today" % (side.upper(), sym))
        passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
        cap_n = int(c.get("max_orders_per_day") or 0)
        if cap_n and count_today + passed_so_far >= cap_n:
            later.append("daily limit: %d placed today plus %d passed earlier in this batch, "
                         "your limit is %d a day" % (count_today, passed_so_far, cap_n))
        seen.add(key)

        if kind == "entry":
            reasons.extend(later)
        else:
            warns.extend("exit, not blocked: " + x for x in later)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS":
            if kind == "entry" and side in ("buy", "sell"):
                batch_entries += 1
                batch_loss += max_loss or 0.0
            if side == "buy":
                if value is not None:
                    acct["cash"] -= value
                    acct["values"][sym] = acct["values"].get(sym, 0.0) + value
                if q is not None:
                    acct["shares"][sym] = held + q
            elif side == "sell":
                if value is not None:
                    acct["cash"] += value
                    acct["values"][sym] = max(acct["values"].get(sym, 0.0) - value, 0.0)
                if q is not None:
                    acct["shares"][sym] = held - q
        results.append({
            "index": i, "symbol": sym, "side": side, "kind": kind,
            "amount": "%s shares" % qty, "product": product, "order_type": typ,
            "value_inr": round(value, 2) if value is not None else None,
            "entry_ref": round(ref, 2) if ref else None, "entry_ref_from": ref_from,
            "stop": stop, "target": target, "risk_per_share": risk_ps, "max_loss": max_loss,
            "charges_at_stop": charges_stop, "reward_risk": rr, "fingerprint": fingerprint(o),
            "verdict": verdict, "reasons": reasons, "warnings": warns, "info": info,
        })

    return {
        "date": today,
        "now_ist": now_ist.strftime("%Y-%m-%d %H:%M"),
        "paper": bool(c.get("paper", True)),
        "results": results,
        "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
        "warnings": top_warn,
        "cash_after": round(acct["cash"], 2),
        "today": {"orders_placed": count_today, "entries_placed": len(entries_today),
                  "loss_at_risk": round(loss_today, 2)},
    }


def report(res):
    lines = []
    for w in res["warnings"]:
        lines.append("WARNING: " + w)
    if res["warnings"]:
        lines.append("")
    for r in res["results"]:
        lines.append("%-5s #%d %s %s %s, %s" % (r["verdict"], r["index"], r["side"].upper(), r["symbol"],
                                                r["amount"], r["kind"]))
        for x in r.get("info", []):
            lines.append("          " + x)
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    lines.append("")
    lines.append("%d passed, %d blocked at %s IST. Cash after the passed orders: %s (%s account)."
                 % (res["pass"], res["block"], res["now_ist"], money(res["cash_after"]),
                    "advisory/paper" if res["paper"] else "LIVE"))
    if res["block"]:
        lines.append("Blocked orders do not go to the broker. Fix the order or your rules, then check again.")
    else:
        lines.append("Every order passed. trader-broker still asks you yes or no for each one.")
    lines.append("Charges are an estimate from Zerodha's published equity intraday schedule. "
                 "Education and tooling, not financial advice.")
    return "\n".join(lines)


def load_prices(pdoc):
    """prices.json -> ({SYM: price}, {SYM: price time as naive UTC or None}, {SYM: (lower, upper)}).

    The price time is the quote's own time (detail.time), else when it was fetched
    (detail.fetched), else the file's as_of. Circuits come from detail.lower_circuit / upper_circuit."""
    if not isinstance(pdoc, dict):
        return {}, {}, {}
    prices = pdoc.get("prices", pdoc)
    if not isinstance(prices, dict):
        return {}, {}, {}
    out, times, circuits = {}, {}, {}
    as_of = parse_utc(pdoc.get("as_of"))
    detail = pdoc.get("detail") or {}
    for s, p in prices.items():
        if _f(p) is None:
            continue
        try:
            s = nm.normalize_symbol(s)
        except nm.MarketError:
            s = str(s).upper()
        out[s] = float(p)
        d = detail.get(s) or {}
        times[s] = parse_utc(d.get("time")) or parse_utc(d.get("fetched")) or as_of
        lo, hi = _f(d.get("lower_circuit")), _f(d.get("upper_circuit"))
        if lo is not None or hi is not None:
            circuits[s] = (lo, hi)
    return out, times, circuits


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check proposed orders against your limits (trader-risk).")
    ap.add_argument("--orders", default="trader/orders.json")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; if missing, a fresh account funded with the budget")
    ap.add_argument("--prices", default="trader/prices.json")
    ap.add_argument("--state", default="trader/state/broker.json", help="what trader-broker placed today")
    ap.add_argument("--now", help="pretend it is this India time, e.g. \"2026-10-29 10:15\"")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today in India)")
    ap.add_argument("--out", default="trader/state/risk.json", help="where the verdicts are recorded")
    ap.add_argument("--no-write", action="store_true", help="do not record the verdicts")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        now = parse_now(a.now)
        if a.today and not re.match(r"^\d{4}-\d{2}-\d{2}$", a.today):
            raise RiskError("--today wants a date like 2026-10-29")
        doc = read_json(a.orders, "orders file (run trader-budget first)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % a.orders)
        cfg = read_json(a.config, "config", required=False) or {}
        portfolio = read_json(a.portfolio, "portfolio", required=False)
        pdoc = read_json(a.prices, "prices", required=False) or {}
        prices, times, circuits = load_prices(pdoc)
        state = read_json(a.state, "broker state", required=False)
        res = check(orders, cfg, portfolio, prices, state, a.today, now, times, circuits)
        if pdoc.get("as_of"):
            res["prices_as_of"] = pdoc["as_of"]
        if not a.no_write:
            d = os.path.dirname(a.out)
            if d:
                os.makedirs(d, exist_ok=True)
            rec = dict(res)
            rec["checked_at"] = datetime.datetime.now(datetime.timezone.utc).replace(
                microsecond=0, tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")
            rec["orders_file"] = a.orders
            tmp = a.out + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, a.out)
    except RiskError as e:
        print("check.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    sys.exit(main())
