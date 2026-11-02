"""NSE market helpers for the India port of the day-trader skills.

Standard library only. ONE source file lives here at the repo root; an identical
copy sits in every skill folder that needs it (a test fails if the copies drift),
so each skill still installs on its own.

    python3 nse_market.py clock [--now "2026-10-29 10:15"] [--cas]
    python3 nse_market.py charges --price 1250.5 --qty 40 [--exit 1262.0]
    python3 nse_market.py symbol "M&M"
    python3 nse_market.py kite-to-chart candles.json --symbol RELIANCE --interval 5m > chart.json

What is encoded here, and how sure each part is (checked 2026-10-29):

* Time: India Standard Time, UTC+05:30, no daylight saving. Certain.
* Hours: the normal session is 09:15-15:30 IST. Since 3 Aug 2026 the Closing
  Auction Session (CAS) applies to stocks with F&O contracts: their continuous
  trading ends at 15:15 and the auction runs 15:15-15:35. Non-F&O stocks keep
  09:15-15:30. Reported by several broker explainers; confirm against NSE circulars.
  One secondary source reports a SEBI circular dated 15 Oct 2026 moving the CAS
  schedule (continuous trading to 15:30 from 7 Dec 2026). That is NOT applied here
  because it is unconfirmed; set "cts_end_fo" in trader/config.json if NSE confirms.
* Holidays: the 2026 NSE equity-segment list (two broker sites agree on 15 dates, one
  adds 15 Jan 2026). The 2027 list is not published yet, so the clock says "unknown"
  for 2027 instead of guessing. Muhurat trading is Sunday 8 Nov 2026, hours not yet
  notified: treated as not a day-trading day.
* Charges: Zerodha's published equity intraday schedule (see CHARGES). Estimates,
  within about a rupee of the contract note; rates can change.
* Tick size: Rs 0.05 for ordinary equity (ETFs and some series differ: set "tick_size").
"""

import argparse
import datetime
import json
import re
import sys
import urllib.parse
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

IST = datetime.timedelta(hours=5, minutes=30)
EPOCH = datetime.datetime(1970, 1, 1)

OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)          # normal session end, stocks without F&O
CTS_END_FO = datetime.time(15, 15)     # continuous session end for CAS (F&O) stocks
CAS_START = datetime.date(2026, 8, 3)  # CAS went live for F&O stocks
BROKER_SQUAREOFF_CAS = datetime.time(15, 12)   # Zerodha MIS auto square-off, CAS stocks
BROKER_SQUAREOFF_OTHER = datetime.time(15, 25)  # Zerodha MIS auto square-off, others
DEFAULT_TICK = Decimal("0.05")

# NSE equity-segment trading holidays, 2026
NSE_HOLIDAYS = {
    "2026-01-15": "Municipal Corporation Election, Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
# Weekend or evening sessions: not treated as normal trading days.
SPECIAL_SESSIONS = {
    "2026-11-08": "Muhurat trading (Diwali Laxmi Pujan), Sunday, hours not yet notified by NSE",
}
CALENDAR_YEARS = (2026,)

# Zerodha equity intraday (MIS) schedule, from zerodha.com/charges
CHARGES = {
    "brokerage_pct": Decimal("0.03"),       # of turnover, per executed order ...
    "brokerage_cap": Decimal("20"),         # ... or Rs 20, whichever is lower
    "stt_sell_pct": Decimal("0.025"),       # sell side only
    "exchange_pct": Decimal("0.00307"),     # NSE transaction charge, both sides
    "sebi_per_crore": Decimal("10"),        # Rs 10 per crore of turnover
    "stamp_buy_pct": Decimal("0.003"),      # buy side only
    "gst_pct": Decimal("18"),               # on brokerage + exchange + SEBI
    "auto_squareoff_fee": Decimal("50"),    # per auto-squared-off order, before GST
}


class MarketError(Exception):
    pass


# ---------------------------------------------------------------- time

def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def utc_of(ts):
    """Unix seconds -> naive UTC."""
    return EPOCH + datetime.timedelta(seconds=int(ts))


def ts_of(utc):
    return int((utc.replace(tzinfo=None) - EPOCH).total_seconds())


def to_ist(utc):
    return utc.replace(tzinfo=None) + IST


def ist_to_utc(local):
    return local.replace(tzinfo=None) - IST


def iso(utc):
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_now(text):
    """--now -> naive UTC. "2026-10-29 10:15" (or ...T10:15, or just 10:15 for today)
    is India time; an explicit offset or Z makes it absolute."""
    if not text:
        return utc_now()
    t = text.strip()
    if t.upper().endswith(" IST"):
        t = t[:-4].strip()
    if re.match(r"^\d{1,2}:\d{2}$", t):
        t = "%s %s" % (to_ist(utc_now()).date().isoformat(), t.zfill(5))
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise MarketError('--now wants an India time like "2026-10-29 10:15" (or ...Z for UTC)')
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, what="time"):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise MarketError("%s wants an India time like 15:00, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def hm(t):
    return "%02d:%02d" % (t.hour, t.minute)


def short(t):
    return "%d:%02d" % (t.hour, t.minute)


def ampm(t):
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


# ---------------------------------------------------------------- calendar

def calendar_known(d):
    return d.year in CALENDAR_YEARS


def trading_day(d):
    """(is_trading_day, reason). Weekends, the holiday list, special sessions."""
    if d.weekday() >= 5 and d.isoformat() not in SPECIAL_SESSIONS:
        return False, "weekend"
    special = SPECIAL_SESSIONS.get(d.isoformat())
    if special:
        return False, "special session: " + special
    name = NSE_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "NSE holiday: " + name
    return True, ""


def session(d, cas=False, cts_end_fo=None):
    """(open, close) in India time, or None when closed all day. cas=True is for
    stocks with F&O contracts, whose continuous trading ends early since 3 Aug 2026."""
    ok, _ = trading_day(d)
    if not ok:
        return None
    if cas and d >= CAS_START:
        return OPEN, (cts_end_fo or CTS_END_FO)
    return OPEN, CLOSE


def next_open(d, cas=False):
    nd = d + datetime.timedelta(days=1)
    for _ in range(15):
        if session(nd, cas):
            return "%s %s" % (nd.isoformat(), hm(OPEN))
        nd += datetime.timedelta(days=1)
    return None


def clock(now_utc, cas=False, cts_end_fo=None):
    """Market status at one instant: a plain dict."""
    local = to_ist(now_utc)
    d = local.date()
    ok, why = trading_day(d)
    sess = session(d, cas, cts_end_fo)
    st = {"now_ist": local.strftime("%Y-%m-%d %H:%M:%S"), "weekday": d.strftime("%A"), "open": False,
          "reason": why, "calendar_known": calendar_known(d), "cas": bool(cas), "open_time": None,
          "close_time": None, "next_open": None}
    if sess:
        o, c = sess
        st.update(open_time=hm(o), close_time=hm(c))
        t = local.time()
        if t < o:
            st["reason"] = "before the open"
        elif t >= c:
            st["reason"] = "after the close"
        else:
            st.update(open=True, reason="")
    if not st["open"]:
        st["next_open"] = next_open(d if not sess or local.time() >= sess[1] else d - datetime.timedelta(days=1), cas)
        if sess and local.time() < sess[0]:
            st["next_open"] = "%s %s" % (d.isoformat(), hm(sess[0]))
    if not st["calendar_known"]:
        st["warning"] = ("the NSE holiday list for %d is not published here: weekends are handled, "
                         "holidays are not" % d.year)
    return st


def squareoff_time(cas=False):
    """Zerodha's MIS auto square-off: the latest you can be out without the Rs 50 + GST fee."""
    return BROKER_SQUAREOFF_CAS if cas else BROKER_SQUAREOFF_OTHER


# ---------------------------------------------------------------- price grid

def D(x):
    return x if isinstance(x, Decimal) else Decimal(str(x))


def round_to_tick(price, tick=DEFAULT_TICK, mode="nearest"):
    """Snap a price to the exchange tick: 'down', 'up' or 'nearest'. Returns a float."""
    p, t = D(price), D(tick)
    if t <= 0:
        raise MarketError("tick size must be positive")
    q = p / t
    q = {"down": q.to_integral_value(ROUND_FLOOR), "up": q.to_integral_value(ROUND_CEILING)}.get(
        mode, q.to_integral_value(ROUND_HALF_UP))
    return float((q * t).quantize(Decimal("0.01")))


def on_tick(price, tick=DEFAULT_TICK):
    return abs(round_to_tick(price, tick) - float(price)) < 1e-9


def within_circuit(price, lower=None, upper=None):
    """(ok, reason): is the price inside the day's circuit band (from Kite quotes)?"""
    if lower is not None and price < lower:
        return False, "below the lower circuit %.2f" % lower
    if upper is not None and price > upper:
        return False, "above the upper circuit %.2f" % upper
    return True, ""


# ---------------------------------------------------------------- charges

def _r2(x):
    return float(D(x).quantize(Decimal("0.01"), ROUND_HALF_UP))


def charges(price, qty, side, costs=None):
    """One executed order's charges in rupees. side is 'BUY' or 'SELL'."""
    c = dict(CHARGES)
    c.update({k: D(v) for k, v in (costs or {}).items() if k in CHARGES})
    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise MarketError("side is BUY or SELL")
    turnover = D(price) * D(qty)
    brokerage = min(turnover * c["brokerage_pct"] / 100, c["brokerage_cap"])
    exchange = turnover * c["exchange_pct"] / 100
    sebi = turnover * c["sebi_per_crore"] / Decimal("10000000")
    stt = turnover * c["stt_sell_pct"] / 100 if side == "SELL" else Decimal(0)
    stamp = turnover * c["stamp_buy_pct"] / 100 if side == "BUY" else Decimal(0)
    gst = (brokerage + exchange + sebi) * c["gst_pct"] / 100
    total = brokerage + exchange + sebi + stt + stamp + gst
    return {"side": side, "turnover": _r2(turnover), "brokerage": _r2(brokerage), "stt": _r2(stt),
            "exchange": _r2(exchange), "sebi": _r2(sebi), "stamp": _r2(stamp), "gst": _r2(gst),
            "total": _r2(total)}


def round_trip(entry, exit_price, qty, costs=None):
    """Buy at entry, sell at exit (a long day trade): gross, charges, net, and the
    break-even move per share (what the price must gain just to pay the charges)."""
    buy = charges(entry, qty, "BUY", costs)
    sell = charges(exit_price, qty, "SELL", costs)
    gross = float(D(exit_price) * D(qty) - D(entry) * D(qty))
    total = buy["total"] + sell["total"]
    return {"qty": int(qty), "buy": buy, "sell": sell, "charges": _r2(total), "gross": _r2(gross),
            "net": _r2(D(gross) - D(total)), "breakeven_per_share": _r2(D(total) / D(qty)) if qty else 0.0}


# ---------------------------------------------------------------- symbols and money

INDEX_MAP = {"NIFTY": "^NSEI", "NIFTY50": "^NSEI", "BANKNIFTY": "^NSEBANK", "SENSEX": "^BSESN"}
SYMBOL_RE = re.compile(r"^[A-Z0-9&-]{1,20}$")


def normalize_symbol(sym):
    """RELIANCE, reliance.ns, NSE:RELIANCE -> RELIANCE. Raises MarketError when it cannot be an NSE symbol."""
    s = str(sym).strip().upper()
    for pre in ("NSE:", "NSE_EQ:"):
        if s.startswith(pre):
            s = s[len(pre):]
    for suf in (".NS", ".BO"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    if not SYMBOL_RE.match(s):
        raise MarketError("%r is not an NSE trading symbol (letters, digits, & and - only)" % sym)
    return s


def yahoo_symbol(sym):
    """The Yahoo Finance ticker for an NSE symbol, URL-safe: RELIANCE -> RELIANCE.NS,
    M&M -> M%26M.NS, NIFTY -> ^NSEI (with %5E kept literal)."""
    raw = str(sym).strip().upper()
    if raw in INDEX_MAP:
        return urllib.parse.quote(INDEX_MAP[raw], safe="^")
    if raw.startswith("^"):
        return urllib.parse.quote(raw, safe="^")
    if raw.endswith(".BO"):
        return urllib.parse.quote(raw, safe=".-")
    return urllib.parse.quote(normalize_symbol(raw) + ".NS", safe=".-")


def kite_symbol(sym):
    return "NSE:" + normalize_symbol(sym)


def inr(x, places=2):
    """Rs 1,23,456.78 with lakh/crore grouping."""
    neg = x < 0
    s = "%.*f" % (places, abs(float(x)))
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return ("-" if neg else "") + "₹" + whole + ("." + frac if places else "")


def crore(x):
    return "₹%.2f Cr" % (float(x) / 1e7)


# ---------------------------------------------------------------- Kite bars -> Yahoo chart JSON

def _candle_ts(v):
    if isinstance(v, (int, float)):
        return int(v)
    dt = datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return ts_of(ist_to_utc(dt))
    return ts_of((dt - dt.utcoffset()).replace(tzinfo=None))


def kite_candles_to_chart(symbol, candles, interval="5m", prev_close=None):
    """Kite get_historical_data candles -> the Yahoo chart JSON the skills read with --offline.
    Candles are dicts {date, open, high, low, close, volume} or lists [date, o, h, l, c, v]."""
    rows = []
    for c in candles:
        if isinstance(c, dict):
            rows.append((c["date"], c["open"], c["high"], c["low"], c["close"], c.get("volume", 0)))
        else:
            rows.append(tuple(c[:6]) + (0,) * (6 - len(c[:6])))
    rows = sorted(((_candle_ts(r[0]),) + tuple(r[1:]) for r in rows), key=lambda r: r[0])
    if not rows:
        raise MarketError("no candles")
    meta = {"currency": "INR", "symbol": yahoo_symbol(symbol).replace("%26", "&"), "exchangeName": "NSI",
            "instrumentType": "EQUITY", "regularMarketTime": rows[-1][0], "gmtoffset": 19800,
            "timezone": "IST", "exchangeTimezoneName": "Asia/Kolkata", "regularMarketPrice": rows[-1][4],
            "priceHint": 2, "dataGranularity": interval, "range": "kite",
            "note": "converted from Kite get_historical_data"}
    if prev_close is not None:
        meta["chartPreviousClose"] = meta["previousClose"] = prev_close
    cols = list(zip(*rows))
    return {"chart": {"result": [{"meta": meta, "timestamp": list(cols[0]),
                                  "indicators": {"quote": [{"open": list(cols[1]), "high": list(cols[2]),
                                                            "low": list(cols[3]), "close": list(cols[4]),
                                                            "volume": list(cols[5])}]}}], "error": None}}


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="NSE clock, charges and symbol helpers.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("clock")
    p.add_argument("--now")
    p.add_argument("--cas", action="store_true", help="a stock with F&O contracts (CAS stock)")
    p = sub.add_parser("charges")
    p.add_argument("--price", type=float, required=True)
    p.add_argument("--qty", type=int, required=True)
    p.add_argument("--exit", type=float, help="exit price, for the round trip")
    p = sub.add_parser("symbol")
    p.add_argument("symbol")
    p = sub.add_parser("kite-to-chart")
    p.add_argument("file")
    p.add_argument("--symbol", required=True)
    p.add_argument("--interval", default="5m")
    p.add_argument("--prev-close", type=float)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "clock":
            print(json.dumps(clock(parse_now(a.now), a.cas), indent=2))
        elif a.cmd == "charges":
            out = round_trip(a.price, a.exit, a.qty) if a.exit else charges(a.price, a.qty, "BUY")
            print(json.dumps(out, indent=2))
        elif a.cmd == "symbol":
            print(json.dumps({"nse": normalize_symbol(a.symbol), "yahoo": yahoo_symbol(a.symbol),
                              "kite": kite_symbol(a.symbol)}))
        else:
            with open(a.file, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                data = data.get("candles") or data.get("data", {}).get("candles") or data
            print(json.dumps(kite_candles_to_chart(a.symbol, data, a.interval, a.prev_close)))
    except MarketError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
