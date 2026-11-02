#!/usr/bin/env python3
"""Insider, bulk-deal and FII/DII tracker for trader-insider (India). Standard library only.

India's public disclosures that the US "Congress trades" idea maps to:
  pit      SEBI PIT disclosures: promoters, directors and key staff report their
           trades in their own company's shares (within two trading days)
  deals    NSE bulk and block deals: large trades (bulk: over 0.5% of the shares,
           block: a single big negotiated trade) with the client's name
  fiidii   daily FII/FPI and DII cash-market buying and selling in rupee crore

    python3 insider.py pit --offline DIR [--symbols RELIANCE TCS] [--min-crore 1]
    python3 insider.py deals --offline DIR [--kind bulk|block|both]
    python3 insider.py fiidii --offline DIR
    python3 insider.py watch --offline DIR           # only filings not alerted before, on your watchlist
    python3 insider.py pit --symbols RELIANCE        # live (NSE often blocks scripts: see below)

Sources (public, unofficial endpoints, NSE changes them without notice):
  PIT       https://www.nseindia.com/api/corporates-pit?index=equities&symbol=<SYMBOL>
  bulk      https://nsearchives.nseindia.com/content/equities/bulk.csv
  block     https://nsearchives.nseindia.com/content/equities/block.csv
  FII/DII   https://www.nseindia.com/api/fiidiiTradeReact
NSE wants a browser-like User-Agent and the cookies its home page sets, so the
tool visits https://www.nseindia.com first. NSE often blocks scripts (401/403)
and the tool then says so plainly instead of guessing. Offline: --offline DIR
reads pit-<SYMBOL>.json, bulk.csv, block.csv and fiidii.json from DIR, so
Claude can save data fetched another way and run the same analysis.

The JSON field names (symbol, acqName, personCategory, secAcq, secVal,
tdpTransactionType, acqfromDt, acqtoDt, intimDt, acqMode, befAcqSharesNo,
afterAcqSharesNo) are as the NSE site served them when this was written. They
are parsed defensively: a missing field stays blank and is reported, never
invented. Amounts are in rupees; the tool prints crore (1 crore = 10,000,000).

Public information is not a tip. A promoter selling or a fund buying can have
many reasons, and SEBI treats trading on unpublished price-sensitive
information as a serious offence. Disclosures arrive late by design.
Not financial advice.
"""

import argparse
import csv
import datetime
import gzip
import hashlib
import http.cookiejar
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_market  # noqa: E402  (a byte-identical copy sits next to this script)

NSE_HOME = "https://www.nseindia.com"
PIT_URL = NSE_HOME + "/api/corporates-pit?index=equities&symbol=%s"
BULK_URL = "https://nsearchives.nseindia.com/content/equities/bulk.csv"
BLOCK_URL = "https://nsearchives.nseindia.com/content/equities/block.csv"
FIIDII_URL = NSE_HOME + "/api/fiidiiTradeReact"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")
CRORE = 10000000.0
KEEP_DAYS = 60


class InsiderError(Exception):
    pass


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, offline=None, pause=0.4):
        self.offline = offline
        self.pause = pause
        self._last = 0.0
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self._warm = False

    def _raw(self, url, headers):
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        try:
            with self._opener.open(urllib.request.Request(url, headers=headers), timeout=30) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as e:
            raise InsiderError("HTTP %s for %s (NSE often blocks scripts; save the data another way and use --offline)"
                               % (e.code, url.split("?")[0]))
        except urllib.error.URLError as e:
            raise InsiderError("could not reach %s: %s" % (url.split("/")[2], e.reason))

    def get(self, url, name):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise InsiderError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        headers = {"User-Agent": BROWSER_UA, "Accept-Encoding": "gzip", "Accept-Language": "en-IN,en;q=0.9",
                   "Accept": "application/json, text/csv, text/plain, */*",
                   "Referer": NSE_HOME + "/report-detail/display-bulk-and-block-deals"}
        if "www.nseindia.com/api" in url and not self._warm:
            self._raw(NSE_HOME, {"User-Agent": BROWSER_UA, "Accept": "text/html"})
            self._warm = True
        return self._raw(url, headers)


# ---------------------------------------------------------------- parsing helpers

def num(x):
    """'1,23,456.50', '12.5', 12 -> float, or None."""
    if x is None:
        return None
    s = re.sub(r"[,\s]", "", str(x))
    s = s.replace("₹", "").replace("Rs.", "").replace("Rs", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_date(value):
    """NSE dates: '30-Oct-2026', '30-Oct-2026 14:05', '2026-10-30', '30/10/2026' -> date or None."""
    value = (value or "").strip() if isinstance(value, str) else ""
    if not value or value in ("-", "NA"):
        return None
    head = value.split(" ")[0] if re.match(r"^\d{1,2}-[A-Za-z]{3}-\d{4}\b", value) else value[:10]
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.datetime.strptime(head.strip(), fmt).date()
        except ValueError:
            continue
    return None


def crore(rupees):
    return None if rupees is None else round(rupees / CRORE, 2)


def trading_days_between(a, b):
    """Whole trading days after a up to and including b (NSE calendar; negative if b is before a)."""
    if a is None or b is None:
        return None
    if b < a:
        return -trading_days_between(b, a)
    n, d = 0, a
    while d < b:
        d += datetime.timedelta(days=1)
        if nse_market.trading_day(d)[0]:
            n += 1
    return n


def _norm_symbol(sym):
    try:
        return nse_market.normalize_symbol(sym)
    except nse_market.MarketError as e:
        raise InsiderError(str(e))


# ---------------------------------------------------------------- PIT disclosures

SIDE_WORDS = [("pledge", "PLEDGE"), ("revoke", "REVOKE"), ("invoke", "INVOKE"), ("buy", "BUY"), ("acqui", "BUY"),
              ("sell", "SELL"), ("dispos", "SELL"), ("allot", "ALLOT")]


def side_of(text):
    t = (text or "").lower()
    for word, label in SIDE_WORDS:
        if word in t:
            return label
    return (text or "").strip().upper() or "?"


def parse_pit(data, symbol=None):
    """NSE PIT JSON -> rows. Tolerates a bare list or {"data": [...]}. Missing fields stay None."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
    except ValueError:
        raise InsiderError("the PIT data is not JSON (often an NSE block page)")
    if isinstance(doc, dict):
        doc = doc.get("data") or doc.get("rows") or []
    if not isinstance(doc, list):
        raise InsiderError("the PIT data has an unexpected shape")
    rows = []
    for r in doc:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol") or symbol or "").upper()
        trade_to = parse_date(r.get("acqtoDt") or r.get("acqfromDt") or "")
        intimated = parse_date(r.get("intimDt") or r.get("date") or "")
        qty, value = num(r.get("secAcq")), num(r.get("secVal"))
        row = {"kind": "pit", "symbol": sym, "company": r.get("company") or "",
               "person": r.get("acqName") or r.get("name") or "", "category": r.get("personCategory") or "",
               "side": side_of(r.get("tdpTransactionType") or r.get("transactionType")),
               "quantity": qty, "value_inr": value, "value_crore": crore(value),
               "trade_date": trade_to.isoformat() if trade_to else None,
               "disclosed": intimated.isoformat() if intimated else None,
               "days_to_disclose": trading_days_between(trade_to, intimated),
               "mode": r.get("acqMode") or "", "security": r.get("secType") or "",
               "before_shares": num(r.get("befAcqSharesNo")), "after_shares": num(r.get("afterAcqSharesNo"))}
        missing = [k for k in ("person", "quantity", "trade_date") if not row[k]]
        if missing:
            row["missing"] = missing
        row["id"] = "pit:" + hashlib.sha1(json.dumps([sym, row["person"], row["side"], qty, row["trade_date"],
                                                      row["disclosed"]]).encode("utf-8")).hexdigest()[:14]
        rows.append(row)
    return rows


# ---------------------------------------------------------------- bulk and block deals

def parse_deals(text, kind):
    """NSE bulk.csv / block.csv -> rows. Header names are matched loosely (case, spaces, slashes)."""
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig", "replace")
    reader = csv.reader(io.StringIO(text))
    header = None
    rows = []
    for line in reader:
        if not line or not any(c.strip() for c in line):
            continue
        key = [re.sub(r"[^a-z]", "", c.lower()) for c in line]
        if header is None:
            if "symbol" in key:
                header = key
                continue
            continue
        d = dict(zip(header, line))

        def pick(*names):
            for n in names:
                for h, v in d.items():
                    if h.startswith(n):
                        return v
            return None

        qty, price = num(pick("quantitytraded", "quantity")), num(pick("tradeprice", "price"))
        value = qty * price if qty is not None and price is not None else None
        when = parse_date(pick("date") or "")
        sym = (pick("symbol") or "").strip().upper()
        client = (pick("clientname", "client") or "").strip()
        side = side_of(pick("buysell", "buy"))
        row = {"kind": kind, "symbol": sym, "company": (pick("securityname") or "").strip(), "person": client,
               "category": "", "side": side, "quantity": qty, "price": price, "value_inr": value,
               "value_crore": crore(value), "trade_date": when.isoformat() if when else None,
               "disclosed": when.isoformat() if when else None, "days_to_disclose": 0,
               "remarks": (pick("remarks") or "").strip()}
        row["id"] = "%s:" % kind + hashlib.sha1(json.dumps([sym, client, side, qty, price, row["trade_date"]])
                                              .encode("utf-8")).hexdigest()[:14]
        rows.append(row)
    if header is None:
        raise InsiderError("the %s deals file has no header with a Symbol column" % kind)
    return rows


# ---------------------------------------------------------------- FII / DII

def parse_fiidii(data):
    """NSE fiidiiTradeReact JSON -> rows {date, category, buy_crore, sell_crore, net_crore}."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
    except ValueError:
        raise InsiderError("the FII/DII data is not JSON (often an NSE block page)")
    if isinstance(doc, dict):
        doc = doc.get("data") or []
    out = []
    for r in doc if isinstance(doc, list) else []:
        if not isinstance(r, dict):
            continue
        buy, sell, net = num(r.get("buyValue")), num(r.get("sellValue")), num(r.get("netValue"))
        if net is None and buy is not None and sell is not None:
            net = buy - sell
        cat = str(r.get("category") or "").strip()
        out.append({"kind": "fiidii", "date": (parse_date(str(r.get("date") or "")) or "") and
                    parse_date(str(r.get("date"))).isoformat(), "category": cat,
                    "group": "FII" if "FII" in cat.upper() or "FPI" in cat.upper() else
                             ("DII" if "DII" in cat.upper() else cat),
                    "buy_crore": buy, "sell_crore": sell, "net_crore": net})
    return out


# ---------------------------------------------------------------- watch state

def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def watch_symbols(watchlist, plan, portfolio):
    out = set()
    for h in (load_json(watchlist) or {}).get("hits", []):
        s = h.get("symbol") or h.get("ticker")
        if s:
            out.add(str(s).upper())
    pl = load_json(plan) or {}
    if pl.get("symbol"):
        out.add(str(pl["symbol"]).upper())
    for p in (load_json(portfolio) or {}).get("positions", []):
        s = p.get("ticker") or p.get("symbol")
        if s:
            out.add(str(s).upper())
    return out


def new_only(rows, state, today):
    """Rows not alerted before; the ids are remembered KEEP_DAYS days."""
    seen = state.setdefault("seen", {})
    fresh = []
    for r in rows:
        if r["id"] not in seen:
            fresh.append(r)
            seen[r["id"]] = today
    cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    return fresh


# ---------------------------------------------------------------- rendering

def _cr(x):
    if x is None:
        return "-"
    return "%s₹%s Cr" % ("-" if x < 0 else "", "{:,.2f}".format(abs(x)))


def render_rows(rows, watch=()):
    if not rows:
        return ["Nothing to show."]
    out = []
    for r in rows:
        flag = "*" if r["symbol"] in watch else " "
        late = ""
        if r.get("days_to_disclose") not in (None, 0) and r["kind"] == "pit":
            late = ", disclosed %d trading day%s later%s" % (
                r["days_to_disclose"], "" if r["days_to_disclose"] == 1 else "s",
                " (late: SEBI allows two)" if r["days_to_disclose"] > 2 else "")
        qty = "{:,.0f}".format(r["quantity"]) if r.get("quantity") is not None else "?"
        out.append("%s %-10s %-5s %-7s %s shares, %s  %s%s%s" % (
            flag, r["symbol"], r["kind"].upper(), r["side"], qty, _cr(r.get("value_crore")),
            r["person"] or "unnamed", (" (%s)" % r["category"]) if r.get("category") else "",
            ", %s%s" % (r["trade_date"] or "date unknown", late)))
        if r.get("missing"):
            out.append("      not in the data: %s" % ", ".join(r["missing"]))
    return out


def render_fiidii(rows):
    out = []
    for r in rows:
        out.append("%s  %-14s buy %s  sell %s  net %s" % (r["date"] or "date unknown", r["category"],
                                                          _cr(r["buy_crore"]), _cr(r["sell_crore"]),
                                                          _cr(r["net_crore"])))
    return out or ["Nothing to show."]


FOOT = ("Public disclosures, not tips: they arrive after the trade and say nothing about why. "
        "Trading on unpublished price-sensitive information is illegal. Not financial advice.")


# ---------------------------------------------------------------- commands

def _filter(rows, symbols, min_crore, side):
    out = []
    for r in rows:
        if symbols and r["symbol"] not in symbols:
            continue
        if min_crore and (r.get("value_crore") is None or r["value_crore"] < min_crore):
            continue
        if side and r["side"] != side.upper():
            continue
        out.append(r)
    return out


def get_pit(f, symbols):
    rows, errors = [], []
    for s in symbols or []:
        try:
            rows += parse_pit(f.get(PIT_URL % urllib.parse.quote(s, safe=""), "pit-%s.json" % s), s)
        except InsiderError as e:
            errors.append("PIT for %s: %s" % (s, e))
    return rows, errors


def get_deals(f, kind):
    rows, errors = [], []
    for k, url in (("bulk", BULK_URL), ("block", BLOCK_URL)):
        if kind not in ("both", k):
            continue
        try:
            rows += parse_deals(f.get(url, "%s.csv" % k), k)
        except InsiderError as e:
            errors.append("%s deals: %s" % (k, e))
    return rows, errors


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--offline", metavar="DIR")
        p.add_argument("--symbols", nargs="*")
        p.add_argument("--min-crore", type=float, default=0.0, help="hide rows under this many rupee crore")
        p.add_argument("--side", choices=["buy", "sell", "pledge"])
        p.add_argument("--watchlist", default="trader/watchlist.json")
        p.add_argument("--plan", default="trader/plan.json")
        p.add_argument("--portfolio", default="trader/portfolio.json")
        p.add_argument("--json", action="store_true")
    p = sub.add_parser("pit", help="SEBI PIT insider disclosures")
    common(p)
    p = sub.add_parser("deals", help="bulk and block deals")
    common(p)
    p.add_argument("--kind", choices=["bulk", "block", "both"], default="both")
    p = sub.add_parser("fiidii", help="FII/DII cash flows")
    p.add_argument("--offline", metavar="DIR")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("watch", help="new filings on your symbols, once each")
    common(p)
    p.add_argument("--state", default="trader/state/insider.json")
    p.add_argument("--today", help="YYYY-MM-DD (default: today in India)")
    p.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        f = Fetcher(offline=getattr(a, "offline", None))
        if a.cmd == "fiidii":
            rows = parse_fiidii(f.get(FIIDII_URL, "fiidii.json"))
            print(json.dumps(rows, indent=2) if a.json else "\n".join(render_fiidii(rows) + [FOOT]))
            return 0 if rows else 1
        syms = {_norm_symbol(s) for s in (a.symbols or [])}
        mine = watch_symbols(a.watchlist, a.plan, a.portfolio)
        errors = []
        if a.cmd == "pit":
            rows, errors = get_pit(f, sorted(syms or mine))
        elif a.cmd == "deals":
            rows, errors = get_deals(f, a.kind)
        else:  # watch
            rows, errors = get_pit(f, sorted(syms or mine))
            d_rows, d_err = get_deals(f, "both")
            rows += d_rows
            errors += [e for e in d_err if not (a.offline and "no file" in e)]
            syms = syms or mine
        rows = _filter(rows, syms, a.min_crore, a.side)
        rows.sort(key=lambda r: (r.get("disclosed") or r.get("trade_date") or ""), reverse=True)
        if a.cmd == "watch":
            today = a.today or nse_market.to_ist(nse_market.utc_now()).date().isoformat()
            state = load_json(a.state) or {}
            rows = new_only(rows, state, today)
            if not a.dry_run:
                os.makedirs(os.path.dirname(a.state) or ".", exist_ok=True)
                with open(a.state, "w", encoding="utf-8") as fh:
                    json.dump(state, fh, indent=2)
                    fh.write("\n")
        if a.json:
            print(json.dumps({"rows": rows, "notes": errors}, indent=2))
        else:
            print("\n".join(render_rows(rows, mine) + ["note: " + e for e in errors] + [FOOT]))
        return 0 if rows else 1
    except InsiderError as e:
        print("insider.py: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
