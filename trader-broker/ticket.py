#!/usr/bin/env python3
"""Order tickets and the decision log for trader-broker (India, Zerodha Kite). Standard library only.

This tool never talks to a broker and never places anything. It does its
work around the one moment that matters, your yes:

  show      renders each proposed order as a plain-English ticket plus the
            exact JSON Claude would send to the Kite connector's place_order
            tool. An entry shows its stop-loss and target (separate orders,
            placed after the fill) and the max loss at the stop, charges
            included. An order is READY only if trader-risk passed THIS exact
            order today (matched by fingerprint) and nobody has decided on it.
  approve   records your explicit yes for ONE order, with your reply word for
            word. It refuses "yes to all", "sure", "ok", a yes that names a
            different stock or amount, and any order that is not READY.
  decline   records a no.
  result    records what happened after the Allow once / Deny card:
            placed (with Kite's order id), denied, or failed.
  protect   after the entry is placed and filled: the stop-loss and target
            tickets, exactly as you approved them, stop first.
  fill      records a fill in trader/portfolio.json (cash and shares).
  flatten   builds a SELL ticket that closes a symbol's open MIS position by
            flat_by. It still goes through trader-risk and your yes.
  log       prints today's decisions.

    python3 ticket.py show                    # every order, READY or not
    python3 ticket.py show --index 1 --json   # one ticket as JSON
    python3 ticket.py approve --index 1 --reply "yes"
    python3 ticket.py result --index 1 --status placed --broker-id 2610300000123
    python3 ticket.py fill --symbol RELIANCE --side buy --qty 40 --price 1255.90
    python3 ticket.py flatten --symbol RELIANCE --qty 40
    python3 ticket.py log

Everything is written to trader/state/broker.json (append only). Dates are
India dates; --now "2026-10-30 10:15" pins the clock (India time).

"paper": true in trader/config.json (the default) means ADVISORY: Kite has no
paper account, so in this mode the tickets are for you to place in the Kite
app yourself, and Claude does not call place_order. Set "paper": false only
when you want Claude to call place_order after your yes; Claude's own Allow
once / Deny card still asks first.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_market  # noqa: E402  (a byte-identical copy sits next to this script)

# Must match trader-risk/check.py exactly: these are place_order's argument names.
KITE_FIELDS = ("variety", "exchange", "tradingsymbol", "transaction_type", "quantity", "product",
               "order_type", "price", "trigger_price", "validity", "validity_ttl", "disclosed_quantity",
               "iceberg_legs", "iceberg_quantity", "tag")
INT_FIELDS = ("quantity", "disclosed_quantity", "iceberg_legs", "iceberg_quantity", "validity_ttl")
PRICE_FIELDS = ("price", "trigger_price")
VALIDITY_WORDS = {"DAY": "good for today", "IOC": "immediate or cancel", "TTL": "good for a few minutes"}
YES_WORDS = ("yes", "y")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later",
               "hold", "unless", "if"}
SYMBOL_TOKEN = re.compile(r"[A-Z][A-Z0-9&\-]{1,19}")


class TicketError(Exception):
    pass


# ---------------------------------------------------------------- time (India, no tz files)

def parse_now(text):
    try:
        return nse_market.parse_now(text)
    except nse_market.MarketError as e:
        raise TicketError(str(e))


def ist_iso(now_utc):
    """naive UTC -> "2026-10-30T10:15:00+05:30" (India wall clock with its offset)."""
    return nse_market.to_ist(now_utc).replace(microsecond=0).isoformat() + "+05:30"


def today_of(now_utc):
    return nse_market.to_ist(now_utc).date().isoformat()


# ---------------------------------------------------------------- files

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise TicketError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise TicketError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def rec_day(rec):
    """The trading day a decision belongs to: its "date" (the day it was made under), else the day of "at"."""
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


# ---------------------------------------------------------------- the order

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _norm(k, v):
    """One field as Kite takes it. Identical to trader-risk/check.py so the fingerprints agree."""
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
    """Same function as trader-risk/check.py: identifies the exact order."""
    core = {k: _norm(k, order[k]) for k in KITE_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def tool_input(order):
    """The exact arguments for the Kite connector's place_order tool. "why", "plan",
    "role" and "exit_orders" are ours and are never sent."""
    return {k: _norm(k, order[k]) for k in KITE_FIELDS if k in order and order[k] is not None}


def exit_legs(order):
    legs = order.get("exit_orders") or []
    return [l for l in legs if isinstance(l, dict)]


def _inr(v, places=2):
    f = float(v)
    return nse_market.inr(f, 0 if abs(f - round(f)) < 0.005 else places)


def _p(v):
    return "%.2f" % float(v)


def max_loss(order):
    """(rupees, stop): the plan's max loss (charges included) when trader-budget wrote one,
    else the gross loss if the stop fills exactly at the stop, else (None, stop)."""
    plan = order.get("plan") or {}
    stop = _f(plan.get("stop"))
    for leg in exit_legs(order):
        if str(leg.get("order_type", "")).upper() in ("SL", "SL-M") and _f(leg.get("trigger_price")):
            stop = _f(leg["trigger_price"])
    if _f(plan.get("max_loss")) is not None:
        return round(_f(plan["max_loss"]), 2), stop
    q = _f(order.get("quantity"))
    entry = _f(order.get("price")) or _f(plan.get("entry"))
    if not (stop and q and entry):
        return None, stop
    return round(q * abs(entry - stop), 2), stop


def plain_parts(order):
    """The ticket in plain words, one part per line."""
    side = str(order.get("transaction_type", "")).upper()
    sym = str(order.get("tradingsymbol", "?")).upper()
    q = int(_f(order.get("quantity")) or 0)
    typ = str(order.get("order_type", "MARKET")).upper()
    prod = str(order.get("product", "")).upper()
    val = VALIDITY_WORDS.get(str(order.get("validity", "DAY")).upper(), order.get("validity"))
    if typ == "LIMIT":
        how = "limit at %s" % _inr(order.get("price"))
    elif typ == "SL":
        how = "stop-loss limit: trigger %s, price %s" % (_inr(order.get("trigger_price")), _inr(order.get("price")))
    elif typ == "SL-M":
        how = "stop-loss market: trigger %s" % _inr(order.get("trigger_price"))
    else:
        how = "market order"
    parts = ["%s %d %s (%s, %s), %s, %s" % (side, q, sym, order.get("exchange", "NSE"), prod or "?", how, val)]
    for leg in exit_legs(order):
        lt = str(leg.get("order_type", "")).upper()
        role = leg.get("role") or ("stop-loss" if lt in ("SL", "SL-M") else "target")
        if lt in ("SL", "SL-M"):
            what = "trigger %s" % _inr(leg.get("trigger_price"))
            if leg.get("price") is not None:
                what += ", limit %s" % _inr(leg["price"])
        else:
            what = "limit %s" % _inr(leg.get("price"))
        parts.append("then %s: %s %d %s, %s (placed after the entry fills)" % (
            role, str(leg.get("transaction_type", "SELL")).upper(), int(_f(leg.get("quantity")) or q), sym, what))
    loss, stop = max_loss(order)
    plan = order.get("plan") or {}
    if loss is not None and exit_legs(order):
        chg = plan.get("charges_at_stop")
        parts.append("max loss about %s%s if the stop fills at %s (it can fill worse in a fast market or at a circuit)"
                     % (_inr(loss), (", of which about %s is charges" % _inr(chg)) if chg else "", _p(stop)))
    return parts


def plain(order):
    """BUY 40 RELIANCE (NSE, MIS), limit at ₹1,255.90, good for today / then stop-loss: ... / max loss ..."""
    return " / ".join(plain_parts(order))


def amount_numbers(order):
    nums = []
    for src in [order] + exit_legs(order):
        for k in ("quantity", "price", "trigger_price"):
            v = _f(src.get(k))
            if v is not None:
                nums.append(v)
    loss, _ = max_loss(order)
    if loss is not None:
        nums.append(loss)
    return nums


def parse_yes(reply, order, batch_symbols=()):
    """Is this reply an explicit yes for THIS one order? Returns (ok, why)."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-z']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" counts (not ok, sure, go, do it)"
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    sym = str(order.get("tradingsymbol", "")).upper()
    tokens = set(SYMBOL_TOKEN.findall(raw.upper()))
    others = {s.upper() for s in batch_symbols} - {sym}
    named = tokens & others
    if named:
        return False, "the reply names %s, but this order is for %s" % (", ".join(sorted(named)), sym)
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", raw)]
    if nums:
        want = amount_numbers(order)
        if not any(abs(n - w) < 0.005 for n in nums for w in want):
            return False, "the reply says %s, but this order is %s" % (
                ", ".join(("%g" % n) for n in nums), plain(order))
    return True, "explicit yes for %s" % plain(order)


# ---------------------------------------------------------------- state

def load_state(path):
    return read_json(path, "broker state", required=False) or {"orders": []}


def latest(state, fp, today):
    """The latest decision recorded today for this fingerprint, or None."""
    found = None
    for rec in state.get("orders", []):
        if rec.get("fingerprint") == fp and rec_day(rec) == today:
            found = rec
    return found


def risk_verdict(risk, fp, today):
    if not risk:
        return None, "trader-risk has not checked these orders (run check.py)"
    if risk.get("date") != today:
        return None, "the risk check is from %s, not today; run check.py again" % risk.get("date")
    for r in risk.get("results", []):
        if r.get("fingerprint") == fp:
            return r, None
    return None, "this exact order was not in the last risk check (it changed or is new); run check.py again"


def advisory(cfg):
    """True while "paper" is on: tickets only, Claude does not call place_order."""
    return bool(cfg.get("paper", True))


def status_of(order, risk, state, today):
    """(ready, label, risk_result)"""
    fp = fingerprint(order)
    rv, why = risk_verdict(risk, fp, today)
    if rv is None:
        return False, "NOT READY: " + why, None
    if rv.get("verdict") != "PASS":
        return False, "BLOCKED by trader-risk: " + "; ".join(rv.get("reasons", [])), rv
    rec = latest(state, fp, today)
    if rec:
        st = rec.get("status")
        if st == "approved":
            return False, "APPROVED by you at %s, waiting for the place_order call" % rec.get("at"), rv
        return False, "DONE: %s at %s" % (st, rec.get("at")), rv
    return True, "READY: trader-risk PASS, waiting for your yes", rv


def kind_of(order, rv):
    """entry or exit, as trader-risk saw it (a BUY is an entry)."""
    if rv and rv.get("kind"):
        return rv["kind"]
    return "entry" if str(order.get("transaction_type", "")).upper() == "BUY" else "exit"


# ---------------------------------------------------------------- rendering

def render(order, idx, total, cfg, risk, state, today):
    ready, label, rv = status_of(order, risk, state, today)
    mode = ("ADVISORY (paper): you place it in Kite yourself" if advisory(cfg)
            else "LIVE ACCOUNT, REAL MONEY via the Kite connector")
    lines = ["ORDER %d of %d, %s" % (idx, total, mode)]
    for part in plain_parts(order):
        lines.append("  " + part)
    if order.get("why"):
        lines.append("  why: " + order["why"])
    lines.append("  " + label)
    for w in (rv or {}).get("warnings", []):
        lines.append("  note: " + w)
    lines.append("  Claude would call place_order with exactly:")
    for l in json.dumps(tool_input(order), indent=2).splitlines():
        lines.append("    " + l)
    for leg in exit_legs(order):
        lines.append("  then, after the fill, %s:" % (leg.get("role") or "exit"))
        for l in json.dumps(tool_input(leg), indent=2).splitlines():
            lines.append("    " + l)
    if exit_legs(order):
        lines.append("  Kite does not link the stop and the target: when one fills, cancel the other")
        lines.append("  (cancel_order). Place the stop right after the entry fills.")
    if ready:
        if advisory(cfg):
            lines.append("  Reply \"yes\" to record this one order as taken, then place it in the Kite app.")
        else:
            lines.append("  Reply \"yes\" to place this one entry and, once it fills, its stop and target, or \"no\" to skip.")
            lines.append("  Claude's own card then asks again: Allow once places it, Deny stops it.")
    return "\n".join(lines), ready


def ticket_json(order, idx, cfg, risk, state, today):
    ready, label, rv = status_of(order, risk, state, today)
    loss, stop = max_loss(order)
    return {"index": idx, "ready": ready, "status": label, "plain": plain(order),
            "fingerprint": fingerprint(order), "advisory": advisory(cfg),
            "broker": cfg.get("broker", "kite"), "tool": "place_order",
            "tool_input": tool_input(order), "exit_tool_inputs": [tool_input(l) for l in exit_legs(order)],
            "why": order.get("why", ""), "max_loss": loss, "stop": stop}


# ---------------------------------------------------------------- fills -> portfolio

def apply_fill(portfolio_path, cfg, symbol, side, qty, price, date, charges=0.0):
    """Record a filled order in trader/portfolio.json: cash and shares, with a cost-basis lot
    per buy (oldest lots are relieved first on a sell). `charges` (rupees) comes off cash."""
    pf = read_json(portfolio_path, "portfolio", required=False)
    if pf is None:
        budget = cfg.get("budget")
        if not budget:
            raise TicketError("no %s yet and no budget in the config to start it from" % portfolio_path)
        pf = {"cash": float(budget), "positions": []}
    symbol = symbol.upper()
    qty, price, charges = float(qty), float(price), float(charges or 0)
    if qty <= 0 or price <= 0:
        raise TicketError("a fill needs a positive qty and price")
    cost = round(qty * price, 2)
    pos = None
    for p in pf.setdefault("positions", []):
        if str(p.get("ticker", "")).upper() == symbol:
            pos = p
    if side == "buy":
        if pos is None:
            pos = {"ticker": symbol, "shares": 0.0, "lots": []}
            pf["positions"].append(pos)
        lots = pos.setdefault("lots", [])
        lots.append({"id": "%s-%d" % (symbol, len(lots) + 1), "date": date, "shares": qty, "cost": cost})
        pos["shares"] = round(float(pos.get("shares", 0)) + qty, 6)
        pf["cash"] = round(float(pf.get("cash", 0)) - cost - charges, 2)
    else:
        if pos is None or float(pos.get("shares", 0)) + 1e-9 < qty:
            raise TicketError("cannot record selling %g %s: the portfolio holds %s"
                              % (qty, symbol, pos.get("shares", 0) if pos else 0))
        left = qty
        for lot in sorted(pos.get("lots", []), key=lambda l: l.get("date", "")):
            if left <= 1e-9:
                break
            take = min(left, float(lot["shares"]))
            per = float(lot["cost"]) / float(lot["shares"]) if float(lot["shares"]) else 0
            lot["cost"] = round(float(lot["cost"]) - per * take, 2)
            lot["shares"] = round(float(lot["shares"]) - take, 6)
            left -= take
        pos["lots"] = [l for l in pos.get("lots", []) if float(l["shares"]) > 1e-9]
        pos["shares"] = round(float(pos["shares"]) - qty, 6)
        if pos["shares"] <= 1e-9:
            pf["positions"].remove(pos)
        pf["cash"] = round(float(pf.get("cash", 0)) + cost - charges, 2)
    write_json(portfolio_path, pf)
    return pf


def log_fill(state_path, now, today, symbol, side, qty, price, charges=0.0):
    """A fill in the log, so the day's trades and charges can be counted."""
    state = load_state(state_path)
    rec = {"at": ist_iso(now), "date": today, "status": "filled", "symbol": symbol.upper(), "side": side,
           "qty": "%g" % float(qty), "price": float(price),
           "plain": "%s %g %s at %s" % (side.upper(), float(qty), symbol.upper(), _p(price))}
    if charges:
        rec["charges"] = round(float(charges), 2)
    state.setdefault("orders", []).append(rec)
    write_json(state_path, state)


# ---------------------------------------------------------------- commands

def _clock(a):
    now = parse_now(getattr(a, "now", None))
    today = getattr(a, "today", None) or today_of(now)
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", today):
        raise TicketError("--today wants a date like 2026-10-30")
    return now, today


def _load(a):
    doc = read_json(a.orders, "orders file (run trader-budget, or ticket.py flatten, first)")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise TicketError("no orders in %s" % a.orders)
    cfg = read_json(a.config, "config", required=False) or {}
    risk = read_json(a.risk, "risk check", required=False)
    state = load_state(a.state)
    now, today = _clock(a)
    return orders, cfg, risk, state, today, now


def _pick(orders, index):
    if index is None:
        raise TicketError("say which order: --index N")
    if not 1 <= index <= len(orders):
        raise TicketError("there is no order %d (there are %d)" % (index, len(orders)))
    return orders[index - 1]


def cmd_show(a):
    orders, cfg, risk, state, today, now = _load(a)
    picks = [(a.index, _pick(orders, a.index))] if a.index else list(enumerate(orders, 1))
    if a.json:
        out = [ticket_json(o, i, cfg, risk, state, today) for i, o in picks]
        print(json.dumps(out[0] if a.index else out, indent=2))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    for i, o in picks:
        text, ready = render(o, i, len(orders), cfg, risk, state, today)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    print("\nOne order at a time: a yes covers one ticket, never the batch. Education and tooling, "
          "not financial advice. Most retail intraday traders lose money.")
    return 0 if any_ready else 1


def cmd_approve(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    ready, label, rv = status_of(order, risk, state, today)
    if not ready:
        raise TicketError("order %d is not ready: %s" % (a.index, label))
    ok, why = parse_yes(a.reply, order, [o.get("tradingsymbol", "") for o in orders])
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for order %d." % (why, a.index))
        return 1
    loss, stop = max_loss(order)
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": str(order.get("tradingsymbol", "")).upper(),
           "side": str(order.get("transaction_type", "")).lower(),
           "qty": order.get("quantity"), "kind": kind_of(order, rv), "max_loss": loss, "stop": stop,
           "plain": plain(order), "status": "approved", "reply": a.reply,
           "advisory": advisory(cfg), "broker": cfg.get("broker", "kite"),
           "exit_fingerprints": [fingerprint(l) for l in exit_legs(order)]}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded your yes for order %d: %s." % (a.index, plain(order)))
    if advisory(cfg):
        print("Advisory mode: Claude does NOT place this. Place it in the Kite app yourself, then record it:")
        print("  ticket.py result --index %d --status placed --broker-id <Kite order id>" % a.index)
        print("(Set \"paper\": false in trader/config.json only if you want Claude to call place_order.)")
    else:
        print("Now call place_order with exactly:")
        print(json.dumps(tool_input(order), indent=2))
        print("Claude's card shows Allow once / Deny. Then: ticket.py result --index %d --status "
              "placed|denied|failed" % a.index)
        if exit_legs(order):
            print("Once the entry fills, place its stop-loss, then its target, exactly as the ticket shows.")
    return 0


def cmd_decline(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": str(order.get("tradingsymbol", "")).upper(),
           "side": str(order.get("transaction_type", "")).lower(),
           "plain": plain(order), "status": "declined", "reply": a.reply or "no"}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded: you said no to order %d (%s). Nothing was sent." % (a.index, plain(order)))
    return 0


def cmd_result(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    fp = fingerprint(order)
    rec = latest(state, fp, today)
    if not rec or rec.get("status") != "approved":
        raise TicketError("order %d has no yes recorded today; a result can only follow an approval" % a.index)
    new = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fp, "symbol": rec["symbol"],
           "side": rec["side"], "plain": rec["plain"], "status": a.status}
    for k in ("qty", "kind", "max_loss", "stop", "exit_fingerprints", "advisory"):
        if rec.get(k) is not None:
            new[k] = rec[k]
    if a.broker_id:
        new["broker_order_id"] = a.broker_id
    if a.note:
        new["note"] = a.note
    state["orders"].append(new)
    write_json(a.state, state)
    print("Recorded order %d as %s." % (a.index, a.status))
    if a.status == "placed" and a.filled_qty and a.filled_price:
        chg = _f(a.charges) or 0.0
        apply_fill(a.portfolio, cfg, rec["symbol"], rec["side"], a.filled_qty, a.filled_price, today, chg)
        log_fill(a.state, now, today, rec["symbol"], rec["side"], a.filled_qty, a.filled_price, chg)
        print("Added the fill to %s." % a.portfolio)
    elif a.status == "placed":
        print("When it fills, record it: ticket.py fill --symbol %s --side %s --qty Q --price P"
              % (rec["symbol"], rec["side"]))
        if rec.get("kind") == "entry" and rec.get("exit_fingerprints"):
            print("When the stop-loss or the target fills later, record that sell the same way, "
                  "and cancel the other leg.")
    return 0


def cmd_protect(a):
    """After an approved entry is placed and filled: the stop-loss and target tickets, exactly as approved."""
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    rec = latest(state, fingerprint(order), today)
    if not rec or rec.get("status") != "placed":
        raise TicketError("order %d is not recorded as placed today; protect only follows a placed entry "
                          "(ticket.py result --status placed)" % a.index)
    legs = exit_legs(order)
    approved = set(rec.get("exit_fingerprints") or [])
    if not legs:
        raise TicketError("order %d has no stop or target legs" % a.index)
    shown = []
    for leg in legs:
        role = str(leg.get("role") or "exit")
        if a.which != "both" and role != a.which:
            continue
        if approved and fingerprint(leg) not in approved:
            raise TicketError("the %s leg changed since you said yes; run check.py and ask again" % role)
        shown.append((role, leg))
    if not shown:
        raise TicketError("no %s leg on order %d" % (a.which, a.index))
    # the stop first: it is the one that limits the loss
    shown.sort(key=lambda rl: 0 if rl[0] == "stop" else 1)
    for role, leg in shown:
        print("%s: %s" % (role.upper(), plain({k: v for k, v in leg.items() if k != "exit_orders"}).split(" / ")[0]))
        print(json.dumps(tool_input(leg), indent=2))
    print("Confirm the entry filled first (Kite get_orders / get_positions). Kite does not link the two: "
          "when one fills, cancel the other. Be out by %s." % ((order.get("plan") or {}).get("flat_by")
                                                                or cfg.get("flat_by") or "15:00"))
    if advisory(cfg):
        print("Advisory mode: place these in the Kite app yourself.")
    return 0


def cmd_fill(a):
    cfg = read_json(a.config, "config", required=False) or {}
    now = parse_now(a.now)
    date = a.date or today_of(now)
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise TicketError("--date wants a date like 2026-10-30")
    qty, price = _f(a.qty), _f(a.price)
    if not qty or not price:
        raise TicketError("--qty and --price must be numbers")
    chg = _f(a.charges)
    if chg is None:       # an estimate from Zerodha's published schedule; pass --charges for the contract note's figure
        chg = nse_market.charges(price, int(qty), a.side.upper())["total"]
    pf = apply_fill(a.portfolio, cfg, a.symbol, a.side, qty, price, date, chg)
    log_fill(a.state, now, date, a.symbol, a.side, qty, price, chg)
    print("Recorded %s %g %s at %s on %s in %s (charges %s). Cash now %s." % (
        a.side, qty, a.symbol.upper(), _inr(price), date, a.portfolio, _inr(chg), _inr(pf["cash"])))
    return 0


def cmd_flatten(a):
    cfg = read_json(a.config, "config", required=False) or {}
    try:
        sym = nse_market.normalize_symbol(a.symbol)
    except nse_market.MarketError as e:
        raise TicketError(str(e))
    now, today = _clock(a)
    source = "the broker (Kite get_positions)"
    if a.qty is not None:
        qty = _f(a.qty)
        if qty is None or qty <= 0 or qty != int(qty):
            raise TicketError("--qty wants the whole number of shares you hold, like --qty 40")
    else:
        pf = read_json(a.portfolio, "portfolio", required=False) or {}
        qty = sum(_f(p.get("shares", p.get("qty"))) or 0 for p in pf.get("positions", [])
                  if str(p.get("ticker") or p.get("symbol") or "").upper() == sym)
        source = a.portfolio
        if qty <= 1e-9:
            raise TicketError("nothing to flatten: no %s shares in %s. Check Kite (get_positions) and "
                              "pass --qty if you do hold some" % (sym, a.portfolio))
    plan = read_json(a.plan, "plan", required=False) or {}
    flat_by = (plan.get("flat_by") if str(plan.get("symbol", "")).upper() == sym else None) \
        or cfg.get("flat_by") or "15:00"
    exchange = str(cfg.get("exchange") or "NSE").upper()
    order = {"variety": "regular", "exchange": exchange, "tradingsymbol": sym, "transaction_type": "SELL",
             "quantity": int(qty), "product": str(cfg.get("product") or "MIS").upper(),
             "order_type": "MARKET", "validity": "DAY",
             "tag": ("F%s%s" % (sym, today.replace("-", "")))[:20],
             "why": "flat by %s India time: closes the %s intraday position before Zerodha's auto square-off "
                    "(qty from %s)" % (flat_by, sym, source)}
    old = read_json(a.orders, "orders", required=False)
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    doc = {"created_by": "trader-broker", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "proposed",
           "paper": bool(cfg.get("paper", True)),
           "note": "Flatten ticket. Nothing is placed until trader-risk passes it and you say yes in the chat.",
           "source": "ticket.py flatten %s on %s" % (sym, today), "orders": [order]}
    write_json(a.orders, doc)
    print("Flatten %s: %s (flat by %s India time)." % (sym, plain(order), flat_by))
    print("Wrote it to %s%s." % (a.orders, (" (it replaced %d earlier proposal%s)"
                                             % (replaced, "" if replaced == 1 else "s")) if replaced else ""))
    if a.qty is None:
        print("The qty comes from %s. Check it against Kite get_positions before you say yes." % source)
    print("Next:")
    print("  1. trader-risk: check.py. A sell that only closes what you hold is never blocked by the daily")
    print("     limits or the entry window.")
    print("  2. ticket.py show, then the user's yes for this one order.")
    print("  3. If the stop-loss and target are still open, cancel them first (get_orders, then cancel_order for")
    print("     each open leg, Needs approval); Kite may reject a SELL while they hold the shares. Then place the")
    print("     flatten ticket. Zerodha's own auto square-off costs Rs 50 + GST an order, so be out before it.")
    return 0


def cmd_log(a):
    state = load_state(a.state)
    now, today = _clock(a)
    rows = [r for r in state.get("orders", []) if a.all or rec_day(r) == today]
    if not rows:
        print("No decisions recorded%s." % ("" if a.all else " today"))
        return 1
    for r in rows:
        extra = (" id " + str(r["broker_order_id"])) if r.get("broker_order_id") else ""
        reply = (' reply "%s"' % r["reply"]) if r.get("reply") and r["status"] in ("approved", "declined") else ""
        loss = (" max loss %s" % _inr(r["max_loss"])) if r.get("max_loss") is not None and r["status"] == "placed" else ""
        idx = ("#%s" % r["index"]) if r.get("index") is not None else "  "
        print("%s  %s %-9s %s%s%s%s" % (r.get("at"), idx, r.get("status"), r.get("plain"), reply, loss, extra))
    placed = sum(1 for r in rows if r.get("status") == "placed")
    print("%d placed%s." % (placed, "" if a.all else " today"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Order tickets and your yes (trader-broker, Kite). Never places orders.")
    sub = ap.add_subparsers(dest="cmd")

    def clock(p):
        p.add_argument("--now", help="pretend it is this India time, e.g. \"2026-10-30 10:15\"")
        p.add_argument("--today", help="YYYY-MM-DD (default: today in India)")

    def common(p):
        p.add_argument("--orders", default="trader/orders.json")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--risk", default="trader/state/risk.json")
        p.add_argument("--state", default="trader/state/broker.json")
        clock(p)

    p = sub.add_parser("show", help="render the tickets")
    common(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("approve", help="record the user's explicit yes for one order")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply", required=True, help="the user's reply, word for word")
    p = sub.add_parser("decline", help="record a no")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply")
    p = sub.add_parser("result", help="record what happened (Kite placed it, or Deny, or an error)")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--status", required=True, choices=["placed", "denied", "failed"])
    p.add_argument("--broker-id")
    p.add_argument("--note")
    p.add_argument("--filled-qty")
    p.add_argument("--filled-price")
    p.add_argument("--charges", help="rupees of charges for this fill (from the contract note)")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p = sub.add_parser("protect", help="the stop-loss and target tickets for a placed entry")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--which", choices=["both", "stop", "target"], default="both")
    p = sub.add_parser("fill", help="record a fill in trader/portfolio.json")
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["buy", "sell"])
    p.add_argument("--qty", required=True)
    p.add_argument("--price", required=True)
    p.add_argument("--charges", help="rupees of charges (default: estimated from Zerodha's schedule)")
    p.add_argument("--date", help="YYYY-MM-DD (default: today in India)")
    p.add_argument("--now")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--state", default="trader/state/broker.json")
    p = sub.add_parser("flatten", help="a SELL ticket that closes a symbol's position by flat_by")
    p.add_argument("--symbol", required=True)
    p.add_argument("--qty", help="shares you hold, from Kite get_positions (default: trader/portfolio.json)")
    p.add_argument("--orders", default="trader/orders.json", help="where the ticket is written")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json")
    clock(p)
    p = sub.add_parser("log", help="today's decisions")
    p.add_argument("--state", default="trader/state/broker.json")
    clock(p)
    p.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "approve": cmd_approve, "decline": cmd_decline, "result": cmd_result,
                "protect": cmd_protect, "fill": cmd_fill, "flatten": cmd_flatten, "log": cmd_log}[a.cmd](a)
    except TicketError as e:
        print("ticket.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
