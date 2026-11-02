#!/usr/bin/env python3
"""News monitor for trader-news (India, NSE). Standard library only.

Watches headlines and NSE corporate announcements for the stocks you watch or
hold and prints only what is new since the last check, newest first, with how
far each stock has moved today and a flag on anything that touches your symbols.

    python3 news.py                                  # watchlist + plan + portfolio symbols
    python3 news.py --tickers RELIANCE TCS "M&M"
    python3 news.py --tickers RELIANCE --all --hours 72   # everything in 3 days, seen or not
    python3 news.py --no-nse                         # headlines only
    python3 news.py --no-moves                       # skip the price line per symbol

Your symbols, when --tickers is not given: every hit in trader/watchlist.json
(trader-scanner), the symbol in trader/plan.json (trader-exit) and the
positions in trader/portfolio.json.

Sources, one request each per symbol:
  - Yahoo Finance headline RSS for the NSE listing:
    https://feeds.finance.yahoo.com/rss/2.0/headline?s=<SYMBOL>.NS&region=IN&lang=en-IN
  - NSE corporate announcements (results, board meetings, dividends, orders,
    SEBI disclosures, resignations, pledges, mergers), as JSON:
    https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=<SYMBOL>
    NSE wants a browser-like User-Agent and the cookies its home page sets, so
    the tool visits https://www.nseindia.com once first. NSE often blocks
    scripts or answers 401/403: the tool then says so and the headlines still run.
    The JSON field names (symbol, desc, attchmntText, an_dt, attchmntFile,
    seq_id) are as the NSE site served them when this was written; they are
    parsed defensively and an item with no usable time shows "time unknown".
  - The move: Yahoo Finance's chart endpoint (unofficial, can lag), the last
    price against yesterday's close:
    https://query2.finance.yahoo.com/v8/finance/chart/<SYMBOL>.NS?range=1d&interval=5m

Times print in India time (IST). State: trader/state/news.json remembers what
you have seen (30 days), so an item shows once. Offline: --offline DIR reads
yahoo-<SYMBOL>.xml, chart-<SYMBOL>.json and nse-<SYMBOL>.json from DIR. The
tests use it, and so can you with data Claude saved from another tool.

Headlines are leads, not facts: open the story before acting, and treat
nothing here as advice. Acting on unpublished price-sensitive information is
illegal under SEBI's insider-trading rules; everything here is public.
"""

import argparse
import datetime as _dt
import email.utils
import gzip
import hashlib
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_market  # noqa: E402  (a byte-identical copy sits next to this script)

YAHOO = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=IN&lang=en-IN"
CHART = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m"
NSE_HOME = "https://www.nseindia.com"
NSE_ANN = NSE_HOME + "/api/corporate-announcements?index=equities&symbol=%s"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")
KEEP_DAYS = 30
MOVE_PCT = 3.0  # a stock this far from yesterday's close is flagged as moving

# label, pattern: matched against the headline plus the announcement text
KEYWORDS = [
    ("results", r"\bfinancial results?\b|\bquarterly results?\b|\bQ[1-4]\s?FY\s?\d{2}\b|\bearnings\b|\bnet profit\b|\bEPS\b|\bguidance\b"),
    ("board meeting", r"\bboard meeting\b|\boutcome of (?:the )?board\b|\bintimation of board\b"),
    ("dividend", r"\bdividend\b|\brecord date\b|\bbonus (?:issue|shares)\b|\bstock split\b|\bsub-?division\b"),
    ("buyback", r"\bbuy-?back\b|\brepurchase\b"),
    ("deal", r"\bbulk deal\b|\bblock deal\b"),
    ("insider", r"\binsider\b|\bPIT\b|\bregulation 7\b|\btrading window\b|\bsubstantial acquisition\b|\bSAST\b"),
    ("pledge", r"\bpledge[sd]?\b|\binvocation\b|\bencumbrance\b"),
    ("order win", r"\bbags?\b.{0,30}\border\b|\border (?:win|worth|inflow)\b|\bletter of (?:award|intent)\b|\bcontract (?:win|worth|awarded)\b"),
    ("merger", r"\bmerger\b|\bamalgamation\b|\bdemerger\b|\bscheme of arrangement\b|\bacqui(?:re|res|red|sition)\b|\btakeover\b|\bbuyout\b"),
    ("capital raise", r"\bQIP\b|\bpreferential (?:issue|allotment)\b|\brights issue\b|\bFPO\b|\bNCDs?\b|\bfund ?raising\b|\bwarrants?\b"),
    ("rating", r"\bCRISIL\b|\bICRA\b|\bCARE Ratings\b|\bIndia Ratings\b|\bcredit rating\b|\bupgrade[sd]?\b|\bdowngrade[sd]?\b"),
    ("analyst", r"price target|\boutperform\b|\bunderperform\b|\bbrokerage\b|\bbuy rating\b|\bsell rating\b"),
    ("people", r"\bCEO\b|\bCFO\b|\bMD\b|\bresign|\bsteps down\b|\bappoint|\bcessation\b|\bauditor\b"),
    ("regulatory", r"\bSEBI\b|\bRBI\b|\bCCI\b|\bNCLT\b|\bED\b|\bshow[- ]cause\b|\bpenalty\b|\bFDA\b|\bUSFDA\b|\bobservations?\b"),
    ("legal", r"\blawsuit\b|\bsued\b|\bprobe\b|\binvestigation\b|\bfraud\b|\braid\b|\bcourt\b|\btribunal\b"),
    ("risk", r"\brecall\b|\bbankrupt|\binsolvency\b|\bIBC\b|\bdefault\b|\bdelist|\bhack|\bbreach\b|\bfire\b|\bshutdown\b"),
    ("halt", r"\bhalt(?:s|ed)?\b|\btrading (?:pause|suspen)|\bcircuit\b|\bASM\b|\bGSM\b|\bsurveillance\b"),
    ("macro", r"\bRBI policy\b|\brepo rate\b|\bCPI\b|\binflation\b|\bGDP\b|\bFII\b|\bFPI\b|\bDII\b|\bBudget\b|\bNifty\b|\bSensex\b|\bGIFT Nifty\b"),
]
# an announcement carrying one of these tags is marked with "!"
IMPORTANT_TAGS = {"results", "merger", "regulatory", "pledge", "risk", "legal", "halt", "people"}


class NewsError(Exception):
    pass


# ---------------------------------------------------------------- time

def to_ist(utc):
    """Aware or naive UTC -> naive India wall-clock time."""
    if utc.tzinfo is not None:
        utc = utc.astimezone(_dt.timezone.utc)
    return nse_market.to_ist(utc.replace(tzinfo=None))


# ---------------------------------------------------------------- inputs

def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def _norm(sym):
    try:
        return nse_market.normalize_symbol(sym)
    except nse_market.MarketError as exc:
        raise NewsError(str(exc))


def watch_from_files(portfolio="trader/portfolio.json", watchlist="trader/watchlist.json",
                     plan="trader/plan.json"):
    """{SYMBOL: name or None}: the scanner's hits, the planned trade and your holdings."""
    out = {}

    def put(sym, name):
        try:
            sym = _norm(sym)
        except NewsError:
            return
        out[sym] = name or out.get(sym)

    wl = load_json(watchlist) or {}
    for h in wl.get("hits", []):
        sym = h.get("symbol") or h.get("ticker")
        if sym:
            put(sym, h.get("name"))
    pl = load_json(plan) or {}
    if pl.get("symbol"):
        sym = pl["symbol"]
        try:
            sym = _norm(sym)
            out.setdefault(sym, pl.get("name"))
        except NewsError:
            pass
    pf = load_json(portfolio) or {}
    for p in pf.get("positions", []):
        sym = p.get("ticker") or p.get("symbol")
        if sym:
            put(sym, p.get("name"))
    return out


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, offline=None, pause=0.3):
        self.offline = offline
        self.pause = pause
        self._last = 0.0
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))
        self._warm = False

    def _raw(self, url, headers):
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        req = urllib.request.Request(url, headers=headers)
        try:
            with self._opener.open(req, timeout=30) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            raise NewsError("HTTP %s for %s" % (exc.code, url.split("?")[0]))
        except urllib.error.URLError as exc:
            raise NewsError("could not reach %s: %s" % (url.split("/")[2], exc.reason))

    def get(self, url, name, nse=False):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise NewsError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        headers = {"User-Agent": BROWSER_UA, "Accept-Encoding": "gzip"}
        if nse:
            headers.update({"Accept": "application/json, text/plain, */*", "Accept-Language": "en-IN,en;q=0.9",
                            "Referer": NSE_HOME + "/companies-listing/corporate-filings-announcements"})
            if not self._warm:                      # NSE sets its session cookies on the home page
                self._raw(NSE_HOME, {"User-Agent": BROWSER_UA, "Accept": "text/html"})
                self._warm = True
        return self._raw(url, headers)


# ---------------------------------------------------------------- parsing

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def parse_time(value):
    """RFC 822 (RSS) or ISO 8601 -> aware UTC datetime, or None. A bare ISO time is UTC."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def parse_nse_time(value):
    """NSE's own stamps ("30-Oct-2026 14:05:33", "2026-10-30 14:05:33") are India time -> aware UTC."""
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            local = _dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
        return (local - nse_market.IST).replace(tzinfo=_dt.timezone.utc)
    return None


def parse_rss(data, ticker):
    """Yahoo headline RSS -> items."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("RSS for %s is not valid XML: %s" % (ticker, exc))
    out = []
    for it in root.iter("item"):
        title = _text(it, "title")
        link = _text(it, "link")
        guid = _text(it, "guid") or link
        out.append({"id": "rss:" + guid, "source": "Yahoo Finance", "kind": "headline",
                    "ticker": ticker, "title": title, "summary": _text(it, "description"),
                    "link": link, "time": parse_time(_text(it, "pubDate"))})
    return out


def parse_nse_announcements(data, ticker):
    """NSE corporate-announcements JSON -> items. Tolerates a bare list or {"data": [...]},
    and missing fields: an announcement is kept if it has any text at all."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
    except ValueError:
        raise NewsError("NSE answered with something that is not JSON for %s (often a block page)" % ticker)
    if isinstance(doc, dict):
        doc = doc.get("data") or doc.get("rows") or []
    if not isinstance(doc, list):
        raise NewsError("NSE announcements for %s have an unexpected shape" % ticker)
    out = []
    for row in doc:
        if not isinstance(row, dict):
            continue
        desc = str(row.get("desc") or row.get("subject") or "").strip()
        body = str(row.get("attchmntText") or row.get("body") or "").strip()
        if not (desc or body):
            continue
        when = parse_nse_time(row.get("an_dt") or "") or parse_nse_time(row.get("sort_date") or "")
        sym = str(row.get("symbol") or ticker).upper()
        key = row.get("seq_id") or hashlib.sha1(("%s|%s|%s" % (sym, row.get("an_dt"), desc)).encode("utf-8")).hexdigest()[:12]
        title = "%s: %s" % (sym, desc or body[:90])
        out.append({"id": "nse:%s:%s" % (sym, key), "source": "NSE filing", "kind": "filing",
                    "ticker": ticker, "title": title, "summary": body,
                    "link": str(row.get("attchmntFile") or ""), "time": when})
    return out


def parse_chart(data, ticker):
    """Yahoo chart JSON -> {"ticker", "price", "prev_close", "pct", "time"} or raise."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        meta = doc["chart"]["result"][0]["meta"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise NewsError("chart for %s is not usable" % ticker)
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if not price or not prev:
        raise NewsError("chart for %s has no price" % ticker)
    when = meta.get("regularMarketTime")
    return {"ticker": ticker, "price": round(float(price), 2), "prev_close": round(float(prev), 2),
            "pct": round((float(price) / float(prev) - 1) * 100, 2),
            "time": _dt.datetime.fromtimestamp(when, _dt.timezone.utc) if when else None}


def collect_moves(fetcher, watch, errors=None):
    errors = [] if errors is None else errors
    out = []
    for t in sorted(watch):
        name = "chart-%s.json" % t
        try:
            ysym = nse_market.yahoo_symbol(t)
            try:
                data = fetcher.get(CHART % ("query2", ysym), name)
            except NewsError as exc:
                if fetcher.offline or not re.search(r"HTTP (429|5\d\d)", str(exc)):
                    raise
                data = fetcher.get(CHART % ("query1", ysym), name)
            out.append(parse_chart(data, t))
        except (NewsError, nse_market.MarketError) as exc:
            if not (fetcher.offline and "no file" in str(exc)):
                errors.append("price for %s: %s" % (t, exc))
    return out


def mention_patterns(watch):
    pats = {}
    for t, name in watch.items():
        alts = [r"\(%s\)" % re.escape(t), r"\b(?:NSE|BSE)\s*:\s*%s\b" % re.escape(t),
                r"(?<![A-Za-z0-9])%s\.(?:NS|BO)\b" % re.escape(t)]
        if name and len(name) >= 4:
            alts.append(r"\b%s\b" % re.escape(name).replace(r"\ ", r"[\s-]"))
        pats[t] = re.compile("|".join(alts), re.I)
    return pats


def tag(items, watch):
    """Which of your symbols each item touches, and why it matters."""
    pats = mention_patterns(watch)
    for it in items:
        hay = "%s %s" % (it["title"], it.get("summary") or "")
        touches = {it["ticker"]} if it["ticker"] in watch else set()
        for t, rx in pats.items():
            if rx.search(hay):
                touches.add(t)
        it["touches"] = sorted(touches)
        it["tags"] = [label for label, rx in KEYWORDS if re.search(rx, hay, re.I)]
        it["important"] = it["kind"] == "filing" and bool(set(it["tags"]) & IMPORTANT_TAGS)
    return items


def dedupe(items):
    """One item per story: same id, or same link, or same title the same day."""
    seen, out = {}, []
    for it in items:
        keys = [it["id"], it["link"],
                "%s|%s" % (re.sub(r"\W+", " ", it["title"].lower()).strip(),
                           it["time"].date() if it["time"] else "")]
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit is not None:
            for t in it.get("touches", []):
                if t not in hit["touches"]:
                    hit["touches"].append(t)
            hit["touches"].sort()
            continue
        for k in keys:
            if k:
                seen[k] = it
        out.append(it)
    return out


def fingerprint(it):
    return hashlib.sha1(it["id"].encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the check

def collect(fetcher, watch, nse=True, errors=None):
    errors = [] if errors is None else errors
    items = []
    for t in sorted(watch):
        try:
            ysym = nse_market.yahoo_symbol(t)
            items += parse_rss(fetcher.get(YAHOO % ysym, "yahoo-%s.xml" % t), t)
        except (NewsError, nse_market.MarketError) as exc:
            errors.append("headlines for %s: %s" % (t, exc))
        if nse:
            try:
                items += parse_nse_announcements(
                    fetcher.get(NSE_ANN % urllib.parse.quote(t, safe=""), "nse-%s.json" % t, nse=True), t)
            except NewsError as exc:
                if not (fetcher.offline and "no file" in str(exc)):
                    errors.append("NSE announcements for %s: %s" % (t, exc))
    return items


def select(items, state, now, hours=48, show_all=False):
    """Items within the window; only unseen ones unless show_all."""
    seen = state.setdefault("seen", {})
    cutoff = now - _dt.timedelta(hours=hours)
    ahead = now + _dt.timedelta(minutes=10)  # a pinned --now: nothing from after it
    out = []
    for it in items:
        if it["time"] is not None and (it["time"] < cutoff or it["time"] > ahead):
            continue
        fp = fingerprint(it)
        it["new"] = fp not in seen
        if it["new"] or show_all:
            out.append(it)
    out.sort(key=lambda i: (i["time"] or now), reverse=True)
    return out


def remember(items, state, now):
    seen = state.setdefault("seen", {})
    stamp = now.isoformat()
    for it in items:
        seen.setdefault(fingerprint(it), stamp)
    cutoff = (now - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_check"] = stamp


def render_moves(moves, move_pct=MOVE_PCT):
    out = []
    for m in moves:
        flag = "MOVING" if abs(m["pct"]) >= move_pct else ""
        when = (" at %s IST" % to_ist(m["time"]).strftime("%H:%M")) if m.get("time") else ""
        out.append("%-10s %s %+.2f%% vs yesterday's close %s%s  %s" % (
            m["ticker"], nse_market.inr(m["price"]), m["pct"], nse_market.inr(m["prev_close"]),
            when, flag))
    return out


def render(items, errors, now, moves=None, move_pct=MOVE_PCT):
    out = render_moves(moves or [], move_pct)
    if out:
        out.append("")
    if not items:
        out.append("Nothing new.")
    for it in items:
        when = to_ist(it["time"]).strftime("%Y-%m-%d %H:%M IST") if it["time"] else "time unknown"
        mark = "!" if it.get("important") else " "
        who = ",".join(it["touches"]) or it["ticker"]
        out.append("%s %s  %-11s %-13s %s" % (mark, when, who, it["source"], it["title"]))
        extra = []
        if it["tags"]:
            extra.append("tags: " + ", ".join(it["tags"]))
        if it["link"]:
            extra.append(it["link"])
        if extra:
            out.append("      " + "  ".join(extra))
    for e in errors:
        out.append("note: %s" % e)
    out.append("Checked %s. Headlines are leads, not facts; open the story before acting. "
               "Not financial advice." % to_ist(now).strftime("%Y-%m-%d %H:%M IST"))
    return "\n".join(out) + "\n"


def _json_ready(items):
    out = []
    for it in items:
        d = dict(it)
        d["time"] = it["time"].isoformat() if it["time"] else None
        out.append(d)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tickers", nargs="*", help="NSE symbols; default: watchlist + plan + portfolio")
    ap.add_argument("--portfolio", default="trader/portfolio.json")
    ap.add_argument("--watchlist", default="trader/watchlist.json")
    ap.add_argument("--plan", default="trader/plan.json")
    ap.add_argument("--state", default="trader/state/news.json")
    ap.add_argument("--hours", type=float, default=48, help="ignore items older than this")
    ap.add_argument("--all", action="store_true", help="show seen items too")
    ap.add_argument("--no-nse", action="store_true", help="headlines only, skip NSE announcements")
    ap.add_argument("--no-moves", action="store_true", help="skip the price line per symbol")
    ap.add_argument("--move-pct", type=float, default=MOVE_PCT, help="flag a move this big (percent)")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help="ISO time (India time unless it has an offset or Z), for reproducible runs")
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        if args.tickers:
            watch = {_norm(t): None for t in args.tickers}
            known = watch_from_files(args.portfolio, args.watchlist, args.plan)
            watch.update({t: known[t] for t in watch if known.get(t)})
        else:
            watch = watch_from_files(args.portfolio, args.watchlist, args.plan)
    except NewsError as exc:
        print("news: %s" % exc, file=sys.stderr)
        return 2
    if not watch:
        print("news: no symbols. Pass --tickers, or run /trader-scanner first (it writes "
              "trader/watchlist.json).", file=sys.stderr)
        return 2
    try:
        now = (_dt.datetime.now(_dt.timezone.utc) if not args.now else
               nse_market.parse_now(args.now).replace(tzinfo=_dt.timezone.utc))
    except nse_market.MarketError as exc:
        print("news: %s" % exc, file=sys.stderr)
        return 2
    state = load_json(args.state) or {}
    errors = []
    fetcher = Fetcher(offline=args.offline)
    items = collect(fetcher, watch, nse=not args.no_nse, errors=errors)
    items = dedupe(tag(items, watch))
    shown = select(items, state, now, hours=args.hours, show_all=args.all)
    moves = [] if args.no_moves else collect_moves(fetcher, watch, errors)
    if args.json:
        mv = [dict(m, time=m["time"].isoformat() if m.get("time") else None,
                   moving=abs(m["pct"]) >= args.move_pct) for m in moves]
        print(json.dumps({"checked": now.isoformat(), "moves": mv, "items": _json_ready(shown),
                          "notes": errors}, indent=2))
    else:
        sys.stdout.write(render(shown, errors, now, moves, args.move_pct))
    if not args.dry_run:
        remember(items, state, now)
        folder = os.path.dirname(args.state)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.write("\n")
    return 0 if any(i["new"] for i in shown) else 1


if __name__ == "__main__":
    sys.exit(main())
