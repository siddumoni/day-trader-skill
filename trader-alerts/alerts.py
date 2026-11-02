#!/usr/bin/env python3
"""Price alerts and the NSE clock for trader-alerts (India). Standard library only.
All times are India Standard Time (IST, UTC+05:30); prices are rupees.

    python3 alerts.py clock [--cas]                  # is the NSE open? next open / close
    python3 alerts.py quote RELIANCE --write         # quotes -> trader/prices.json
    python3 alerts.py quote --holdings --write       # every symbol in your portfolio and plan
    python3 alerts.py add --from-plan                # stop, target and flat-by from trader/plan.json
    python3 alerts.py add --symbol RELIANCE --stop 1250.25
    python3 alerts.py add --symbol RELIANCE --target 1265.10
    python3 alerts.py add --at 15:00 --label "flat by" --symbol RELIANCE
    python3 alerts.py add --symbol ITC --pct 2
    python3 alerts.py add --opening-bell
    python3 alerts.py check                          # rings what hit, once; exit 1 if nothing rang
    python3 alerts.py check --now "2026-10-30 15:00" --offline tests/fixtures/trader-alerts
    python3 alerts.py list
    python3 alerts.py reset reliance-plan-stop

Rule types:
  above / target  the price is at or above "price"
  below / stop    the price is at or below "price"
  pct_move        the price moved "pct" percent or more from the previous close
                  (or from "ref" if you give one); "direction": "up" or "down"
  opening_bell    the NSE opened today, 09:15 (rings once per trading day)
  time            the India clock passed "at" (HH:MM) on a trading day (rings
                  once per trading day). A flat-by alert defaults to 15:00:
                  Zerodha auto-squares-off MIS positions at 15:25 (15:12 for
                  F&O stocks) and charges Rs 50 + GST per order for it.
  "cas": true on a rule (or --cas) marks an F&O stock: since 3 Aug 2026 its
  continuous trading ends at 15:15 instead of 15:30 (Closing Auction Session).

A rule with "date" is only watched on that India date (rules from a plan are
for the plan's day only). An above / below alert rings ONCE and then stays
quiet until you reset it, unless the rule has "rearm": true (it re-arms when
the price crosses back). A pct_move alert rings at most once per day.

Quotes (free, delayed, unofficial; for alerts only, never for timing an order):
  yahoo   https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS (falls
          back to query1 on a 429 or a 5xx; one retry with a pause on a 429;
          one request per ticker, half a second apart; the default)
  kite    quotes saved from the Kite connector (get_quotes or get_ltp) and read
          with --source kite --offline DIR from DIR/kite-<SYMBOL>.json or one
          combined DIR/kite-quotes.json. Kite quotes also carry the circuit
          limits, which alerts.py uses to warn when a stock is locked.
  --offline DIR reads DIR/<source>-<SYMBOL>.json (or .json.gz) instead.

NSE calendar: the 2026 equity-segment holidays (a Muhurat session on Sunday
8 Nov 2026 is not treated as a normal day). Outside 2026 the clock says the
holiday list is unknown: check https://www.nseindia.com/resources/exchange-communication-holidays
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_market  # noqa: E402  (a byte-identical copy sits next to this script)

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
PAUSE = 0.5    # seconds between requests: be polite to free endpoints
BACKOFF = 2.0  # seconds to wait before the one retry after a 429
YAHOO_HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

CALENDAR_YEARS = nse_market.CALENDAR_YEARS


class AlertError(Exception):
    pass


# ---------------------------------------------------------------- time (India, no tz files)

to_ist = nse_market.to_ist
trading_day = nse_market.trading_day
session = nse_market.session


def parse_now(text):
    """--now -> naive UTC. A time without an offset is India time (IST)."""
    try:
        return nse_market.parse_now(text)
    except nse_market.MarketError as e:
        raise AlertError(str(e))


def norm_symbol(sym):
    """RELIANCE, reliance.ns or NSE:RELIANCE -> RELIANCE."""
    try:
        return nse_market.normalize_symbol(sym)
    except nse_market.MarketError as e:
        raise AlertError(str(e))


def market_status(now_utc, cas=False):
    """The NSE at one instant. cas=True: an F&O stock, whose continuous session ends at 15:15."""
    local = to_ist(now_utc)
    d = local.date()
    ok, reason = trading_day(d)
    st = {"now_ist": local.strftime("%Y-%m-%d %H:%M"), "date": d.isoformat(),
          "weekday": local.strftime("%A"), "calendar_known": nse_market.calendar_known(d),
          "cas": bool(cas), "open": False, "reason": reason}
    s = session(d, cas)
    if s:
        o, c = s
        st["opens"], st["closes"] = o.strftime("%H:%M"), c.strftime("%H:%M")
        if o <= local.time() < c:
            st["open"] = True
            st["reason"] = "regular session"
        elif local.time() < o:
            st["reason"] = "before the open"
        else:
            st["reason"] = "after the close"
    nd = d if (s and local.time() < s[0]) else d + datetime.timedelta(days=1)
    for _ in range(15):
        if session(nd, cas):
            st["next_open"] = "%s %s" % (nd.isoformat(), nse_market.hm(nse_market.OPEN))
            break
        nd += datetime.timedelta(days=1)
    return st


# ---------------------------------------------------------------- quotes

def yahoo_url(sym, host=YAHOO_HOSTS[0]):
    try:
        ysym = nse_market.yahoo_symbol(sym)              # RELIANCE.NS, M%26M.NS, ^NSEI
    except nse_market.MarketError as e:
        raise AlertError(str(e))
    return "https://%s/v8/finance/chart/%s?range=1d&interval=1d" % (host, ysym)


def parse_kite(text, sym):
    """A Kite get_quotes / get_ltp result -> quote. Accepts {"NSE:SYM": {...}}, {"data": {...}} or one bare quote."""
    try:
        doc = json.loads(text)
    except ValueError:
        raise AlertError("the Kite quote file for %s is not valid JSON" % sym)
    if isinstance(doc, dict) and isinstance(doc.get("data"), dict):
        doc = doc["data"]
    q = None
    if isinstance(doc, dict):
        for key in ("NSE:%s" % sym, "BSE:%s" % sym, sym):
            if isinstance(doc.get(key), dict):
                q = doc[key]
                break
        if q is None and "last_price" in doc:
            q = doc
    if not q or q.get("last_price") is None:
        raise AlertError("the Kite quotes have no price for %s" % sym)
    ohlc = q.get("ohlc") or {}
    return {"symbol": sym, "price": float(q["last_price"]), "open": ohlc.get("open"), "high": ohlc.get("high"),
            "low": ohlc.get("low"), "prev_close": ohlc.get("close"),    # Kite's ohlc.close is the previous close
            "time": str(q.get("last_trade_time") or q.get("timestamp") or ""), "currency": "INR",
            "upper_circuit": q.get("upper_circuit_limit"), "lower_circuit": q.get("lower_circuit_limit"),
            "source": "kite"}


def parse_yahoo(text, sym):
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise AlertError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise AlertError("Yahoo has no data for %s: %s" % (sym, err.get("description", "unknown ticker")))
    m = res[0].get("meta", {})
    price = m.get("regularMarketPrice")
    if price is None:
        raise AlertError("Yahoo has no price for %s" % sym)
    t = m.get("regularMarketTime")
    when = (datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if t else "")
    q = (res[0].get("indicators", {}).get("quote") or [{}])[0]
    opens = [x for x in (q.get("open") or []) if x is not None]
    return {"symbol": sym, "price": float(price), "open": opens[0] if opens else None,
            "high": m.get("regularMarketDayHigh"), "low": m.get("regularMarketDayLow"),
            "prev_close": m.get("previousClose") or m.get("chartPreviousClose"), "time": when,
            "currency": m.get("currency"), "source": "yahoo"}


_urlopen = urllib.request.urlopen   # tests swap these two
_sleep = time.sleep


def _get(url, timeout=20):
    """(HTTP status, body). A network failure is an AlertError."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with _urlopen(req, timeout=timeout) as r:
            return 200, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        finally:
            try:
                e.close()
            except Exception:
                pass
        return e.code, body
    except urllib.error.URLError as e:
        raise AlertError("could not reach %s: %s" % (url.split("/")[2], e.reason))


def fetch(url, timeout=20):
    code, text = _get(url, timeout)
    if code != 200:
        raise AlertError("%s answered HTTP %d" % (url.split("?")[0], code))
    return text


def fetch_yahoo(sym, timeout=20):
    """query2 first. A 429 waits BACKOFF seconds and tries once more; a 429 or a 5xx
    after that moves on to query1. A 404 comes back as Yahoo's own error JSON."""
    last = None
    for host in YAHOO_HOSTS:
        url = yahoo_url(sym, host)
        code, text = _get(url, timeout)
        if code == 429:
            _sleep(BACKOFF)
            code, text = _get(url, timeout)
        if code == 200:
            return text
        if code == 404 and text.strip().startswith("{"):
            return text
        last = (url, code)
        if code != 429 and code < 500:
            break
    raise AlertError("%s answered HTTP %d" % (last[0].split("?")[0], last[1]))


def _offline_text(folder, source, sym):
    names = ["%s-%s.json" % (source, sym)]
    if source == "kite":
        names.append("kite-quotes.json")           # one get_quotes result for several symbols
    for name in names:
        base = os.path.join(folder, name)
        if os.path.exists(base):
            with open(base, encoding="utf-8") as fh:
                return fh.read()
        if os.path.exists(base + ".gz"):
            with gzip.open(base + ".gz", "rt", encoding="utf-8") as fh:
                return fh.read()
    raise AlertError("no offline quote file %s" % os.path.join(folder, names[0]))


def get_quotes(symbols, source="yahoo", offline=None, pause=PAUSE):
    """{SYMBOL: quote} and [errors]. One request per ticker, with a pause between."""
    quotes, errors = {}, []
    for i, sym in enumerate(symbols):
        try:
            sym = norm_symbol(sym)
            if source == "kite" and not offline:
                raise AlertError("--source kite reads quotes Claude saved from the Kite connector: add --offline DIR")
            if offline:
                text = _offline_text(offline, source, sym)
            else:
                if i and pause:
                    _sleep(pause)
                text = fetch_yahoo(sym)
            quotes[sym] = parse_kite(text, sym) if source == "kite" else parse_yahoo(text, sym)
        except AlertError as e:
            errors.append(str(e))
    return quotes, errors


def write_prices(path, quotes, now_utc, source):
    old = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                old = json.load(fh)
        except (ValueError, OSError):
            old = {}
    prices = dict(old.get("prices", {}))
    detail = dict(old.get("detail", {}))
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    for s, q in quotes.items():
        prices[s] = q["price"]
        detail[s] = {"prev_close": q.get("prev_close"), "time": q.get("time"), "source": q.get("source"),
                     "currency": q.get("currency") or "INR", "fetched": stamp}
    doc = {"as_of": stamp, "source": source,
           "currency": "INR", "exchange": "NSE",
           "note": ("Delayed quotes for alerts and sizing checks. Your broker's price is the one that fills. "
                    "detail.fetched is when each price was read."),
           "prices": prices, "detail": detail}
    _write(path, doc)
    return doc


def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _read(path, default):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise AlertError("%s is not valid JSON: %s" % (path, e))


# ---------------------------------------------------------------- rules

PRICE_TYPES = {"above", "below", "stop", "target", "pct_move", "opening_bell", "time"}
NO_QUOTE = {"opening_bell", "time"}


def _hhmm(text):
    m = HHMM.match(str(text or "").strip())
    if not m:
        return None
    return datetime.time(int(m.group(1)), int(m.group(2)))


def validate(rule):
    t = rule.get("type")
    if t not in PRICE_TYPES:
        raise AlertError("rule %s: unknown type %r" % (rule.get("id"), t))
    if t not in NO_QUOTE and not rule.get("symbol"):
        raise AlertError("rule %s needs a symbol" % rule.get("id"))
    if t in ("above", "below", "stop", "target") and not isinstance(rule.get("price"), (int, float)):
        raise AlertError("rule %s needs a numeric price" % rule.get("id"))
    if t == "pct_move" and not isinstance(rule.get("pct"), (int, float)):
        raise AlertError("rule %s needs a numeric pct" % rule.get("id"))
    if t == "time" and _hhmm(rule.get("at")) is None:
        raise AlertError("rule %s needs \"at\" as an India time like \"15:00\"" % rule.get("id"))
    if rule.get("date") and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(rule["date"])):
        raise AlertError("rule %s: date must be YYYY-MM-DD" % rule.get("id"))


def active(rule, today):
    """A rule with a "date" is watched on that India date only."""
    return not rule.get("date") or str(rule["date"]) == today


def _rang_today(stamp, today):
    """Did a rule that rang at this UTC stamp ring on this India date?"""
    if not stamp:
        return False
    try:
        utc = datetime.datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return stamp[:10] == today
    return to_ist(utc).date().isoformat() == today


def ring_time(rule, status):
    """The time a time rule rings today (the NSE has no early-close days)."""
    return _hhmm(rule.get("at"))


def circuit_note(q):
    """A warning when the quote sits at the day's circuit limit (Kite quotes carry the limits)."""
    p = q["price"]
    lo, up = q.get("lower_circuit"), q.get("upper_circuit")
    if lo and p <= float(lo) + 1e-9:
        return " The stock is at its LOWER circuit limit (%s): there may be no buyers, so a sell or a stop may not fill." % nse_market.inr(lo)
    if up and p >= float(up) - 1e-9:
        return " The stock is at its UPPER circuit limit (%s): there may be no sellers, so a buy may not fill." % nse_market.inr(up)
    return ""


def evaluate(rules, quotes, status, state, now_utc):
    """Pure function. Returns (rings, new_state). Each ring: {id, symbol, message}."""
    state = json.loads(json.dumps(state or {}))
    rung = state.setdefault("rung", {})
    today = status["date"]
    local = to_ist(now_utc)
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    rings = []
    for r in rules:
        validate(r)
        rid = r.get("id") or "%s-%s" % (r.get("symbol", "market"), r["type"])
        if not active(r, today):
            continue
        typ = {"stop": "below", "target": "above"}.get(r["type"], r["type"])
        if typ == "opening_bell":
            if status["open"] and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": "", "message": "Opening bell: the NSE is open (%s IST)."
                              % status["now_ist"]})
                rung[rid] = stamp
            continue
        if typ == "time":
            at = ring_time(r, status)
            if status.get("opens") and local.time() >= at and not _rang_today(rung.get(rid), today):
                label = r.get("label") or "time alert"
                msg = "%s%s: it is %s IST (set for %s)" % (label[:1].upper(), label[1:],
                                                            local.strftime("%H:%M"), at.strftime("%H:%M"))
                msg += (", the market closes at %s." % status["closes"]) if status["open"] else \
                    ", the market has closed."
                sym = str(r.get("symbol") or "").upper()
                if sym:
                    so = nse_market.squareoff_time(status.get("cas"))
                    msg += (" If %s is still open, close it yourself: /trader-broker builds the sell ticket, then "
                            "trader-risk and your yes. Zerodha squares off MIS at %s and charges Rs 50 + GST."
                            % (sym, so.strftime("%H:%M")))
                rings.append({"id": rid, "symbol": sym, "message": msg})
                rung[rid] = stamp
            continue
        sym = r["symbol"].upper()
        q = quotes.get(sym)
        if not q:
            continue
        p = q["price"]
        if typ in ("above", "below"):
            hit = p >= r["price"] if typ == "above" else p <= r["price"]
            if hit and rid not in rung:
                word = "at or above" if typ == "above" else "at or below"
                label = {"stop": "stop", "target": "target"}.get(r["type"], "alert level")
                rings.append({"id": rid, "symbol": sym, "message": "%s %s %s: %s your %s %s%s.%s"
                              % (sym, "is" if status["open"] else "last", nse_market.inr(p), word, label,
                                 nse_market.inr(r["price"]), (" (" + r["note"] + ")") if r.get("note") else "",
                                 circuit_note(q))})
                rung[rid] = stamp
            elif not hit and rid in rung and r.get("rearm"):
                del rung[rid]
        elif typ == "pct_move":
            ref = r.get("ref") or q.get("prev_close") or q.get("open")
            if not ref:
                continue
            move = 100.0 * (p / float(ref) - 1)
            want = r.get("direction")
            hit = abs(move) >= r["pct"] and (not want or (want == "up") == (move > 0))
            if hit and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": sym, "message": "%s moved %+.2f%% to %s (from %s).%s"
                              % (sym, move, nse_market.inr(p), nse_market.inr(float(ref)), circuit_note(q))})
                rung[rid] = stamp
    state["last_check"] = stamp
    return rings, state


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def rules_from_plan(plan, cfg_flat_by=None):
    """trader/plan.json -> a stop alert, a target alert and a flat-by-close time alert, for the plan's day."""
    if not isinstance(plan, dict) or not plan.get("symbol"):
        raise AlertError("the plan has no symbol (run trader-exit first)")
    sym = norm_symbol(plan["symbol"])
    cas = bool(plan.get("cas"))
    out = []
    for key, typ, word in (("stop", "stop", "plan stop"), ("target", "target", "plan target")):
        v = plan.get(key)
        if not isinstance(v, (int, float)) or v <= 0:
            raise AlertError("the plan has no %s (run trader-exit)" % key)
        label = plan.get("%s_label" % key)
        rule = {"id": "%s-plan-%s" % (sym.lower(), key), "symbol": sym, "type": typ, "price": float(v),
                "note": word + (": " + label if label else "")}
        if cas:
            rule["cas"] = True
        out.append(rule)
    flat = plan.get("flat_by") or cfg_flat_by or "15:00"
    if _hhmm(flat) is None:
        raise AlertError("the plan's flat_by %r is not a time like 15:00" % flat)
    flat_rule = {"id": "%s-flat-by" % sym.lower(), "symbol": sym, "type": "time", "at": flat, "label": "flat-by exit"}
    if cas:
        flat_rule["cas"] = True
    out.append(flat_rule)
    if plan.get("date"):
        for r in out:
            r["date"] = str(plan["date"])
    return out


# ---------------------------------------------------------------- commands

def holdings(a):
    syms = []
    pf = _read(a.portfolio, {})
    for p in pf.get("positions", []) if isinstance(pf, dict) else []:
        syms.append(str(p.get("ticker") or p.get("symbol") or "").upper())
    plan = _read(a.plan, {})
    if isinstance(plan, dict) and plan.get("symbol"):
        syms.append(str(plan["symbol"]).upper())
    return [s for s in syms if s]


def _dedupe(items):
    out = []
    for s in items:
        if s and s not in out:
            out.append(s)
    return out


def cmd_clock(a):
    st = market_status(parse_now(a.now), a.cas)
    if a.json:
        print(json.dumps(st, indent=2))
    else:
        print("India %s IST (%s): NSE %s, %s." % (st["now_ist"], st["weekday"],
                                                 "OPEN" if st["open"] else "closed", st["reason"]))
        if st.get("opens"):
            print("Today's session: %s to %s%s." % (st["opens"], st["closes"],
                                                    " (F&O stock: continuous trading ends 15:15, then the closing auction)" if st["cas"] else ""))
            print("Zerodha MIS auto square-off: %s (Rs 50 + GST per order): be out before it." %
                  nse_market.squareoff_time(st["cas"]).strftime("%H:%M"))
        if not st["open"] and st.get("next_open"):
            print("Next open: %s IST." % st["next_open"])
        if not st["calendar_known"]:
            print("Holiday list covers %s only: check nseindia.com for this date." % " and ".join(map(str, CALENDAR_YEARS)))
    return 0 if st["open"] else 1


def cmd_quote(a):
    syms = _dedupe([norm_symbol(s) for s in a.symbols] + (holdings(a) if a.holdings else []))
    if not syms:
        raise AlertError("which symbols? alerts.py quote RELIANCE TCS, or --holdings")
    now = parse_now(a.now)
    quotes, errors = get_quotes(syms, a.source, a.offline)
    if a.write and quotes:
        write_prices(a.write, quotes, now, a.source)
    if a.json:
        print(json.dumps({"quotes": quotes, "errors": errors}, indent=2))
    else:
        for s in syms:
            q = quotes.get(s)
            if q:
                chg = ""
                if q.get("prev_close"):
                    chg = " (%+.2f%% vs prev close %s)" % (100.0 * (q["price"] / q["prev_close"] - 1), nse_market.inr(q["prev_close"]))
                print("%-12s %s%s  %s  [%s]" % (s, nse_market.inr(q["price"]), chg, q.get("time", ""), q["source"]))
        for e in errors:
            print("error: " + e)
        if a.write and quotes:
            print("Wrote %d price(s) to %s." % (len(quotes), a.write))
        print("Quotes are free, delayed and unofficial: for alerts only. Your broker's price is the one that fills.")
    return 0 if quotes else 1


def cmd_check(a):
    doc = _read(a.rules, None)
    if not doc or not doc.get("rules"):
        raise AlertError("no rules in %s: add some with alerts.py add --from-plan" % a.rules)
    rules = doc["rules"]
    now = parse_now(a.now)
    today = to_ist(now).date().isoformat()
    watched = [r for r in rules if active(r, today)]
    status = market_status(now, a.cas or any(r.get("cas") for r in watched))
    syms = _dedupe([norm_symbol(r["symbol"]) for r in watched if r.get("type") not in NO_QUOTE and r.get("symbol")])
    quotes, errors = (get_quotes(syms, a.source, a.offline) if syms else ({}, []))
    state = _read(a.state, {})
    rings, state = evaluate(rules, quotes, status, state, now)
    if not a.no_write:
        _write(a.state, state)
        if quotes:
            write_prices(a.prices_out, quotes, now, a.source)
    if a.json:
        print(json.dumps({"market": status, "rings": rings, "errors": errors,
                          "quotes": {s: q["price"] for s, q in quotes.items()}}, indent=2))
    else:
        print("NSE %s (%s IST, %s)." % ("OPEN" if status["open"] else "closed", status["now_ist"], status["reason"]))
        for r in rings:
            print("ALERT " + r["message"])
        if not rings:
            print("Nothing hit. %d rule(s) watched today%s." % (
                len(watched), (", " + ", ".join("%s %s" % (s, nse_market.inr(quotes[s]["price"])) for s in syms if s in quotes))
                if quotes else ""))
        for e in errors:
            print("error: " + e)
        if rings:
            print("An alert is information, not an order. A stop here protects you only if a stop-loss order is "
                  "actually placed with your broker: check get_positions in the Kite connector and record any "
                  "fill with /trader-broker. To close a position yourself: /trader-broker builds the sell ticket, "
                  "then trader-risk and your yes.")
    return 0 if rings else 1


def cmd_add(a):
    doc = _read(a.rules, {"rules": []})
    doc.setdefault("rules", [])
    if a.from_plan:
        plan = _read(a.plan, None)
        if plan is None:
            raise AlertError("no plan at %s (run trader-exit first)" % a.plan)
        cfg = _read(a.config, {}) if a.config else {}
        new = rules_from_plan(plan, (cfg or {}).get("flat_by"))
        ids = {r["id"] for r in new}
        doc["rules"] = [r for r in doc["rules"] if r.get("id") not in ids] + new
        state = _read(a.state, {})
        if state.get("rung"):
            for i in ids:
                state["rung"].pop(i, None)
            _write(a.state, state)
        _write(a.rules, doc)
        for r in new:
            print("Added %s: %s" % (r["id"], json.dumps(r)))
        print("Run alerts.py check every few minutes while the trade is open (a scheduled task or /loop). "
              "Free quotes are delayed; only a stop-loss order at your broker actually stops you out.")
        return 0
    if a.opening_bell:
        rule = {"id": a.id or "opening-bell", "type": "opening_bell"}
    elif a.at:
        if _hhmm(a.at) is None:
            raise AlertError("--at wants an India time like 15:00")
        rule = {"id": a.id or ("time-%s" % a.at.replace(":", "")), "type": "time", "at": a.at,
                "label": a.label or "time alert"}
        if a.symbol:
            rule["symbol"] = norm_symbol(a.symbol)
        if a.cas:
            rule["cas"] = True
    else:
        if not a.symbol:
            raise AlertError("--symbol is required (or --from-plan, --at, --opening-bell)")
        sym = norm_symbol(a.symbol)
        if a.above is not None:
            rule = {"type": "above", "price": a.above}
        elif a.below is not None:
            rule = {"type": "below", "price": a.below}
        elif a.stop is not None:
            rule = {"type": "stop", "price": a.stop}
        elif a.target is not None:
            rule = {"type": "target", "price": a.target}
        elif a.pct is not None:
            rule = {"type": "pct_move", "pct": a.pct}
            if a.direction:
                rule["direction"] = a.direction
        else:
            raise AlertError("give one of --above, --below, --stop, --target or --pct")
        val = rule.get("price", rule.get("pct"))
        rule = dict({"id": a.id or ("%s-%s-%s" % (sym.lower(), rule["type"], _n(val)))}, symbol=sym, **rule)
        if a.cas:
            rule["cas"] = True
        if a.rearm:
            rule["rearm"] = True
        if a.note:
            rule["note"] = a.note
    if a.date:
        rule["date"] = a.date
    validate(rule)
    if any(r.get("id") == rule["id"] for r in doc["rules"]):
        raise AlertError("a rule with id %s already exists" % rule["id"])
    doc["rules"].append(rule)
    _write(a.rules, doc)
    print("Added %s: %s" % (rule["id"], json.dumps(rule)))
    return 0


def cmd_list(a):
    doc = _read(a.rules, {"rules": []})
    state = _read(a.state, {})
    if not doc.get("rules"):
        print("No rules yet.")
        return 1
    for r in doc["rules"]:
        rid = r.get("id")
        rung = state.get("rung", {}).get(rid)
        print("%-24s %s%s" % (rid, json.dumps({k: v for k, v in r.items() if k != "id"}),
                              ("  (rang %s)" % rung) if rung else ""))
    return 0


def cmd_reset(a):
    state = _read(a.state, {})
    if a.id not in state.get("rung", {}):
        print("%s has not rung (nothing to reset)." % a.id)
        return 1
    del state["rung"][a.id]
    _write(a.state, state)
    print("Re-armed %s." % a.id)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Price alerts and the NSE clock (trader-alerts).")
    sub = ap.add_subparsers(dest="cmd")

    def net(p):
        p.add_argument("--source", default="yahoo", choices=["yahoo", "kite"])
        p.add_argument("--offline", metavar="DIR", help="read <source>-<SYMBOL>.json files, no network")
        p.add_argument("--now", help="pretend it is this time (no offset = India time)")
        p.add_argument("--cas", action="store_true", help="F&O stocks: continuous trading ends 15:15")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("clock", help="is the NSE open?")
    p.add_argument("--now")
    p.add_argument("--cas", action="store_true", help="an F&O stock: continuous trading ends 15:15")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("quote", help="latest delayed quotes")
    net(p)
    p.add_argument("symbols", nargs="*")
    p.add_argument("--holdings", action="store_true", help="add tickers from your portfolio and plan")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--write", nargs="?", const="trader/prices.json", metavar="PATH",
                   help="save to prices.json (default trader/prices.json)")
    p = sub.add_parser("check", help="check every rule, ring what hit")
    net(p)
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--prices-out", default="trader/prices.json")
    p.add_argument("--no-write", action="store_true")
    p = sub.add_parser("add", help="add a rule")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--symbol")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--above", type=float)
    g.add_argument("--below", type=float)
    g.add_argument("--stop", type=float)
    g.add_argument("--target", type=float)
    g.add_argument("--pct", type=float)
    g.add_argument("--opening-bell", action="store_true")
    g.add_argument("--at", metavar="HH:MM", help="a time alert, India time")
    g.add_argument("--from-plan", action="store_true", help="stop, target and flat-by-close from the plan")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json", help="flat_by when the plan has none")
    p.add_argument("--label", help="what a time alert says, e.g. \"flat by\"")
    p.add_argument("--cas", action="store_true", help="an F&O stock: continuous trading ends 15:15")
    p.add_argument("--date", help="watch this rule on this India date only (YYYY-MM-DD)")
    p.add_argument("--direction", choices=["up", "down"])
    p.add_argument("--rearm", action="store_true")
    p.add_argument("--note")
    p.add_argument("--id")
    p = sub.add_parser("list", help="list the rules")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p = sub.add_parser("reset", help="re-arm a rule that rang")
    p.add_argument("id")
    p.add_argument("--state", default="trader/state/alerts.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # the rupee sign, on any terminal
    except (AttributeError, ValueError):
        pass
    try:
        return {"clock": cmd_clock, "quote": cmd_quote, "check": cmd_check, "add": cmd_add,
                "list": cmd_list, "reset": cmd_reset}[a.cmd](a)
    except AlertError as e:
        print("alerts.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
