#!/usr/bin/env python3
"""Earnings reader for trader-analyst (India / NSE). Standard library only.

Reads a listed company's quarterly results from NSE and lays out what changed,
quarter by quarter, in Rs crore: revenue from operations, total expenses,
operating profit and margin, profit before tax, net profit and margin, EPS,
each against the same quarter a year earlier, plus the trailing twelve months.
It also reads the board-meeting calendar so you can tell whether results land
inside the trade ("does it report this week?").

    python3 facts.py RELIANCE
    python3 facts.py TCS INFY --quarters 8
    python3 facts.py M\\&M --json
    python3 facts.py HDFCBANK --standalone
    python3 facts.py RELIANCE --offline DIR --now "2026-10-30 09:00"
    python3 facts.py TCS INFY --metrics trader/metrics.json

Sources (NSE; they want a browser-like User-Agent and the cookies that the
home page sets, so the tool visits https://www.nseindia.com first):
  https://www.nseindia.com/api/corporates-financial-results?index=equities&symbol=SYM&period=Quarterly
  https://www.nseindia.com/api/corporate-board-meetings?index=equities&symbol=SYM
  https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=SYM   (--announcements)

NOT VERIFIED: these endpoints and their JSON field names are written from
memory and could not be tested from the build sandbox (it cannot reach NSE).
Fields are matched by a list of plausible names, case and punctuation ignored;
a field that is not found stays empty, is listed under "absent", and nothing
is ever guessed. NSE also blocks many scripts and cloud IPs; when it does, the
tool says so. Save the JSON from your browser and use --offline instead.

--offline DIR reads results-SYM.json, meetings-SYM.json and
announcements-SYM.json from DIR. A file is a JSON list of rows, or an object
with the list under "data" or "results". An optional "unit" (rupee, thousand,
lakh, million, crore) says what the amounts are in; --unit sets it for all
(NSE results are normally in Rs lakh, the default here: check a known company).

How the quarters are built:
  - A quarter is a period of 80 to 100 days. Half-year and annual rows are skipped.
  - Consolidated figures are used when the company has them, else standalone
    (--standalone prefers standalone). The basis is printed.
  - A period filed twice (a restatement or revised result) keeps the latest filing.
  - Indian companies report Q4 themselves, so nothing is derived.
  - "Operating profit" here is revenue from operations minus total expenses. NSE's
    total expenses include interest and depreciation, and other income is left out, so
    this is NOT EBITDA. It is comparable quarter to quarter for the same company.
  - Fiscal quarters follow the Indian year: the quarter ended June is Q1, March is Q4.
  - TTM needs four consecutive quarters with the field present in all four.

Numbers only: this tool never says whether a stock is a buy.
"""

import argparse
import datetime
import gzip
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_market as nm  # noqa: E402

HOME_URL = "https://www.nseindia.com"
RESULTS_URL = HOME_URL + "/api/corporates-financial-results?index=equities&symbol=%s&period=Quarterly"
MEETINGS_URL = HOME_URL + "/api/corporate-board-meetings?index=equities&symbol=%s"
ANNOUNCE_URL = HOME_URL + "/api/corporate-announcements?index=equities&symbol=%s"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
PAUSE = 1.0            # seconds between NSE requests
UNIT_TO_CRORE = {"rupee": 1e-7, "thousand": 1e-4, "lakh": 0.01, "million": 0.1, "crore": 1.0}
DEFAULT_UNIT = "lakh"
QUARTER_DAYS = (80, 100)

# plausible field names, lower case with punctuation removed (UNVERIFIED, see above)
ALIASES = {
    "revenue": ["renetsale", "reincomefromop", "reincomefromoperations", "incomefromoperations",
                "revenuefromoperations", "totalrevenuefromoperations", "netsales", "revenue"],
    "revenue_fallback": ["retotalinc", "totalincome", "income"],
    "total_expenses": ["retotalexpenses", "totalexpenses", "reexpenses", "expenses", "retotexp"],
    "other_income": ["reotherincome", "otherincome", "reothinc"],
    "pbt": ["reprofitbeforetax", "profitbeforetax", "pbt", "reproloss", "reprobt", "reprobtbtax"],
    "net_profit": ["reprofitaftertax", "reprofitlossforperiod", "reprofitloss", "reprolosfperiod",
                   "netprofit", "profitaftertax", "profitlossfortheperiod", "profitfortheperiod",
                   "reprolossaftertax", "reprolosaftertax"],
    "eps": ["redilutedeps", "redilutedepsfinal", "dilutedeps", "eps", "rebasiceps", "basiceps",
            "reepsdiluted", "reepsbasic"],
}
START_KEYS = ["fromdate", "periodstart", "periodfrom", "startdate"]
END_KEYS = ["todate", "periodend", "periodto", "quarterend", "enddate"]
FILED_KEYS = ["filingdate", "broadcastdt", "broadcastdate", "exchdistime", "submissiondate"]
CONSOL_KEYS = ["consolidated", "consolidatedstandalone", "resulttype", "relatingto"]
AUDIT_KEYS = ["audited", "auditstatus", "auditedunaudited"]
UNIT_KEYS = ["unit", "resultunit", "amountunit", "amountin"]
MEETING_DATE_KEYS = ["bmdate", "meetingdate", "boardmeetingdate", "bmdt", "date"]
MEETING_TEXT_KEYS = ["bmpurpose", "purpose", "subject", "bmdesc", "description", "bmdescription"]


class NseError(Exception):
    pass


# ---------------------------------------------------------------- parsing

def nkey(k):
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


def pick(row, aliases):
    norm = {nkey(k): v for k, v in row.items()}
    for a in aliases:
        v = norm.get(a)
        if v is not None and str(v).strip() not in ("", "-", "--"):
            return v
    return None


def num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("₹", "")
    if s.upper() in ("", "-", "--", "NA", "N/A", "NULL", "NIL"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    try:
        x = float(s.strip("()"))
    except ValueError:
        return None
    return -x if neg else x


DATE_FORMATS = ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%B-%Y", "%d %b %Y", "%d-%b-%y")


def parse_date(v):
    if v is None:
        return None
    s = str(v).strip()
    for text in (s, re.split(r"[ T]", s)[0]):
        for f in DATE_FORMATS:
            try:
                return datetime.datetime.strptime(text, f).date()
            except ValueError:
                pass
    return None


def rows_of(data):
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for k in ("data", "results", "rows"):
            if isinstance(data.get(k), list):
                return [r for r in data[k] if isinstance(r, dict)]
    return []


def unit_of(data, default):
    if isinstance(data, dict) and data.get("unit"):
        return str(data["unit"]).lower()
    return default


def row_unit(row, default):
    t = str(pick(row, UNIT_KEYS) or "").lower()
    for name, words in (("crore", ("crore", " cr")), ("lakh", ("lakh", "lac")), ("million", ("million",)),
                        ("thousand", ("thousand",)), ("rupee", ("rupee", "inr"))):
        if any(w in t for w in words):
            return name
    return default


def fiscal_label(end):
    q = {6: "Q1", 9: "Q2", 12: "Q3", 3: "Q4"}.get(end.month)
    fy = end.year + (1 if end.month >= 4 else 0)
    return "%s FY%02d" % (q or "Q?", fy % 100)


def normalise(row, unit):
    """One NSE results row -> a clean dict in Rs crore, or None if it has no usable dates."""
    end = parse_date(pick(row, END_KEYS))
    start = parse_date(pick(row, START_KEYS))
    if not end or not start:
        return None
    factor = UNIT_TO_CRORE[row_unit(row, unit)]
    out = {"start": start.isoformat(), "end": end.isoformat(), "days": (end - start).days + 1,
           "fiscal": fiscal_label(end), "missing": [], "notes": []}
    cons = str(pick(row, CONSOL_KEYS) or "").lower()
    out["consolidated"] = None if not cons else ("consolidated" in cons and "non" not in cons
                                                  and "standalone" not in cons)
    aud = str(pick(row, AUDIT_KEYS) or "").lower()
    out["audited"] = None if not aud else ("un" not in aud)
    filed = parse_date(pick(row, FILED_KEYS))
    out["filed"] = filed.isoformat() if filed else None
    for field in ("revenue", "total_expenses", "other_income", "pbt", "net_profit"):
        v = num(pick(row, ALIASES[field]))
        if v is None and field == "revenue":
            v = num(pick(row, ALIASES["revenue_fallback"]))
            if v is not None:
                out["notes"].append("revenue is total income (revenue from operations not found)")
        out[field] = None if v is None else round(v * factor, 4)
        if v is None:
            out["missing"].append(field)
    eps = num(pick(row, ALIASES["eps"]))
    out["eps"] = eps
    if eps is None:
        out["missing"].append("eps")
    if out["revenue"] is not None and out["total_expenses"] is not None:
        out["operating_profit"] = round(out["revenue"] - out["total_expenses"], 4)
    else:
        out["operating_profit"] = None
    return out


# ---------------------------------------------------------------- analysis

def growth(a, b):
    if a is None or b is None or b <= 0:
        return None
    return round((a / b - 1) * 100, 1)


def margin(x, rev):
    if x is None or rev is None or rev <= 0:
        return None
    return round(x / rev * 100, 1)


def pp(a, b):
    return None if a is None or b is None else round(a - b, 1)


def analyse(data, quarters=8, prefer_standalone=False, unit=DEFAULT_UNIT):
    unit = unit_of(data, unit)
    if unit not in UNIT_TO_CRORE:
        raise NseError("unit must be one of %s" % ", ".join(UNIT_TO_CRORE))
    parsed = [r for r in (normalise(x, unit) for x in rows_of(data)) if r]
    warnings = []
    if not parsed:
        raise NseError("no usable result rows (need a from-date and to-date on each; field names are unverified)")
    quarterly = [r for r in parsed if QUARTER_DAYS[0] <= r["days"] <= QUARTER_DAYS[1]]
    if not quarterly:
        raise NseError("found %d result rows but none covers a three-month quarter" % len(parsed))
    want = False if prefer_standalone else True
    pool = [r for r in quarterly if r["consolidated"] is want or r["consolidated"] is None]
    basis = "standalone" if prefer_standalone else "consolidated"
    if not [r for r in pool if r["consolidated"] is want]:
        pool = [r for r in quarterly if r["consolidated"] is (not want) or r["consolidated"] is None]
        basis = "consolidated" if prefer_standalone else "standalone"
        warnings.append("no %s rows for this company: showing %s" % (
            "standalone" if prefer_standalone else "consolidated", basis))
    if any(r["consolidated"] is None for r in pool):
        basis += " (basis not stated in some rows)"
    latest = {}
    for r in pool:                                   # a later filing of the same period wins
        old = latest.get(r["end"])
        if old is None or (r["filed"] or "") >= (old["filed"] or ""):
            latest[r["end"]] = r
    rows = sorted(latest.values(), key=lambda r: r["end"], reverse=True)
    by_end = {datetime.date.fromisoformat(r["end"]): r for r in rows}
    for r in rows:
        end = datetime.date.fromisoformat(r["end"])
        prev = next((p for e, p in by_end.items() if abs((end - e).days - 365) <= 10), None)
        r["prior_end"] = prev["end"] if prev else None
        r["op_margin"] = margin(r["operating_profit"], r["revenue"])
        r["net_margin"] = margin(r["net_profit"], r["revenue"])
        for f in ("revenue", "operating_profit", "net_profit", "eps"):
            r[f + "_yoy"] = growth(r[f], prev[f]) if prev else None
        r["op_margin_pp"] = pp(r["op_margin"], margin(prev["operating_profit"], prev["revenue"])) if prev else None
        r["net_margin_pp"] = pp(r["net_margin"], margin(prev["net_profit"], prev["revenue"])) if prev else None
    shown = rows[:quarters]
    absent = sorted({m for r in shown for m in r["missing"]})
    if absent:
        warnings.append("fields not found in NSE's response for some quarters: %s "
                        "(field names are from memory, unverified)" % ", ".join(absent))
    return {"basis": basis, "unit_read_as": unit, "rows": shown, "ttm": ttm(rows),
            "absent": absent, "warnings": warnings}


def ttm(rows):
    """Trailing twelve months from the newest four consecutive quarters."""
    if len(rows) < 4:
        return {"available": False, "reason": "fewer than four quarters"}
    four = rows[:4]
    ends = [datetime.date.fromisoformat(r["end"]) for r in four]
    for a, b in zip(ends, ends[1:]):
        if not 80 <= (a - b).days <= 100:
            return {"available": False, "reason": "the newest four quarters are not consecutive (%s to %s)"
                                                  % (four[-1]["end"], four[0]["end"])}
    out = {"available": True, "from": four[-1]["end"], "to": four[0]["end"], "unavailable": {}}
    for f in ("revenue", "total_expenses", "operating_profit", "pbt", "net_profit", "eps"):
        vals = [r[f] for r in four]
        if any(v is None for v in vals):
            bad = next(r for r in four if r[f] is None)
            out[f] = None
            out["unavailable"][f] = "quarter ended %s has no %s" % (bad["end"], f)
        else:
            out[f] = round(sum(vals), 2)
    out["op_margin"] = margin(out["operating_profit"], out["revenue"])
    out["net_margin"] = margin(out["net_profit"], out["revenue"])
    return out


# ---------------------------------------------------------------- results calendar

def parse_meetings(data):
    out = []
    for r in rows_of(data):
        d = parse_date(pick(r, MEETING_DATE_KEYS))
        if not d:
            continue
        text = " ".join(str(pick(r, [k]) or "") for k in MEETING_TEXT_KEYS).strip()
        out.append({"date": d.isoformat(), "purpose": text, "is_results": "result" in text.lower()})
    return sorted(out, key=lambda m: m["date"])


def trading_days_between(today, day):
    """Trading days after `today` up to and including `day`; 0 when day is today."""
    if day <= today:
        return 0
    n, d = 0, today
    while d < day:
        d += datetime.timedelta(days=1)
        if nm.trading_day(d)[0]:
            n += 1
    return n


def calendar_status(meetings, today, within=5):
    upcoming = [m for m in meetings if m["is_results"] and m["date"] >= today.isoformat()]
    past = [m for m in meetings if m["is_results"] and m["date"] < today.isoformat()]
    out = {"next_results": None, "trading_days_until": None, "flag": None, "advice": None,
           "last_results_meeting": past[-1]["date"] if past else None,
           "meetings_seen": len(meetings), "calendar_known": nm.calendar_known(today)}
    if not upcoming:
        out["advice"] = ("no upcoming results board meeting in NSE's feed. That is not proof there is none: "
                         "check the company's announcements before holding anything")
        return out
    m = upcoming[0]
    n = trading_days_between(today, datetime.date.fromisoformat(m["date"]))
    out.update(next_results=m["date"], trading_days_until=n, purpose=m["purpose"])
    if n == 0:
        out.update(flag="today", advice="results board meeting is TODAY: results can land during the session "
                                        "and gap the price. Skip an intraday trade in this stock.")
    elif n == 1:
        out.update(flag="imminent", advice="results meeting is the next trading day: expect pre-results positioning; "
                                           "do not carry anything overnight (MIS is squared off anyway).")
    elif n <= within:
        out.update(flag="soon", advice="results meeting within %d trading days: intraday only, expect "
                                       "unusual volume and moves." % within)
    else:
        out.update(flag="later", advice="no results meeting inside the next %d trading days." % within)
    return out


def results_announcements(data, limit=5):
    """Announcements that look like results or board-meeting outcomes, newest first."""
    out = []
    for r in rows_of(data):
        text = " ".join(str(pick(r, [k]) or "") for k in ("desc", "subject", "attchmnttext", "headline", "an_desc"))
        d = parse_date(pick(r, ["andt", "date", "sortdate", "bdtdate", "exchdisstime"]))
        if re.search(r"financial result|outcome of board|results", text, re.I):
            out.append({"date": d.isoformat() if d else None, "text": " ".join(text.split())[:200]})
    out.sort(key=lambda x: x["date"] or "", reverse=True)
    return out[:limit]


# ---------------------------------------------------------------- NSE access

class Session:
    """Cookie-holding session for NSE: visit the home page, then call the API."""

    def __init__(self, opener=None, sleep=time.sleep):
        self.jar = http.cookiejar.CookieJar()
        self.opener = opener or urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.sleep = sleep
        self.primed = False

    def _read(self, url, accept):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Accept": accept, "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate", "Referer": HOME_URL + "/"})
        with self.opener.open(req, timeout=25) as r:
            raw = r.read()
            enc = (r.headers.get("Content-Encoding") or "").lower()
        if enc == "gzip":
            raw = gzip.decompress(raw)
        elif enc == "deflate":
            raw = zlib.decompress(raw)
        elif enc == "br":
            raise NseError("NSE answered with brotli, which the standard library cannot read")
        return raw.decode("utf-8", "replace")

    def prime(self):
        try:
            self._read(HOME_URL, "text/html,application/xhtml+xml")
        except (urllib.error.URLError, OSError):
            pass                                    # the API call below reports the real problem
        self.primed = True

    def get_json(self, url):
        if not self.primed:
            self.prime()
        for attempt in (1, 2):
            self.sleep(PAUSE)
            try:
                text = self._read(url, "application/json, text/plain, */*")
                try:
                    return json.loads(text)
                except ValueError:
                    raise NseError("NSE did not answer with JSON (usually a block or a changed endpoint): %s"
                                   % text[:80].replace("\n", " "))
            except urllib.error.HTTPError as e:
                if e.code in (401, 403) and attempt == 1:
                    self.prime()
                    continue
                if e.code in (401, 403):
                    raise NseError("NSE refused the request (HTTP %d). NSE blocks many scripts and cloud/VPN "
                                   "addresses. Save the JSON from your browser and use --offline DIR." % e.code)
                raise NseError("NSE answered HTTP %d for %s" % (e.code, url))
            except (urllib.error.URLError, OSError) as e:
                raise NseError("could not reach NSE (%s). A sandbox network allowlist can cause this: "
                               "use --offline DIR." % getattr(e, "reason", e))


def load(sym, kind, a, session):
    if a.offline:
        path = os.path.join(a.offline, "%s-%s.json" % (kind, sym))
        if not os.path.exists(path):
            raise NseError("offline file %s not found" % path)
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    url = {"results": RESULTS_URL, "meetings": MEETINGS_URL, "announcements": ANNOUNCE_URL}[kind] % urllib.parse.quote(sym)
    return session.get_json(url)


# ---------------------------------------------------------------- output

def cr(x):
    return "-" if x is None else nm.inr(x)


def pct(x):
    return "-" if x is None else "%+.1f%%" % x


def render(res):
    out = []
    an = res["analysis"]
    out.append("%s  %s results, Rs crore (amounts read as Rs %s; NSE data, unverified field names)" % (
        res["symbol"], an["basis"], an["unit_read_as"]))
    out.append("%-9s %-10s %12s %7s %12s %6s %12s %7s %6s %8s %7s" % (
        "Quarter", "Ended", "Revenue", "YoY", "OpProfit", "Mgn%", "NetProfit", "YoY", "Mgn%", "EPS", "YoY"))
    for r in an["rows"]:
        out.append("%-9s %-10s %12s %7s %12s %6s %12s %7s %6s %8s %7s" % (
            r["fiscal"], r["end"], cr(r["revenue"]), pct(r["revenue_yoy"]), cr(r["operating_profit"]),
            "-" if r["op_margin"] is None else "%.1f" % r["op_margin"], cr(r["net_profit"]),
            pct(r["net_profit_yoy"]), "-" if r["net_margin"] is None else "%.1f" % r["net_margin"],
            "-" if r["eps"] is None else "%.2f" % r["eps"], pct(r["eps_yoy"])))
    t = an["ttm"]
    if t["available"]:
        out.append("TTM to %s: revenue %s Cr, net profit %s Cr, EPS %s, op margin %s, net margin %s" % (
            t["to"], cr(t["revenue"]), cr(t["net_profit"]), "-" if t["eps"] is None else "%.2f" % t["eps"],
            "-" if t["op_margin"] is None else "%.1f%%" % t["op_margin"],
            "-" if t["net_margin"] is None else "%.1f%%" % t["net_margin"]))
        for f, why in t["unavailable"].items():
            out.append("  TTM %s unavailable: %s" % (f, why))
    else:
        out.append("TTM unavailable: %s" % t["reason"])
    out.append("Operating profit = revenue minus total expenses (after interest and depreciation, before "
               "other income): not EBITDA.")
    for w in an["warnings"]:
        out.append("Note: " + w)
    cal = res.get("calendar")
    if cal:
        if cal["next_results"]:
            out.append("RESULTS: board meeting on %s (%s trading day(s) away; %s). %s" % (
                cal["next_results"], cal["trading_days_until"], cal.get("purpose") or "results", cal["advice"]))
        else:
            out.append("RESULTS: " + cal["advice"])
        if not cal["calendar_known"]:
            out.append("Note: the NSE holiday list covers 2026 only, so the day count ignores holidays after that.")
    for x in res.get("announcements") or []:
        out.append("Announcement %s: %s" % (x["date"] or "?", x["text"]))
    return "\n".join(out)


def metrics_row(res):
    an = res["analysis"]
    top = an["rows"][0]
    t = an["ttm"]
    return {"latest_quarter": top["fiscal"], "ended": top["end"], "basis": an["basis"],
            "revenue_cr": top["revenue"], "revenue_yoy": top["revenue_yoy"],
            "net_profit_cr": top["net_profit"], "net_profit_yoy": top["net_profit_yoy"],
            "eps": top["eps"], "eps_yoy": top["eps_yoy"], "op_margin": top["op_margin"],
            "net_margin": top["net_margin"],
            "ttm_revenue_cr": t.get("revenue") if t["available"] else None,
            "ttm_net_profit_cr": t.get("net_profit") if t["available"] else None,
            "ttm_eps": t.get("eps") if t["available"] else None,
            "next_results": (res.get("calendar") or {}).get("next_results")}


def merge_metrics(path, rows):
    cur = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                cur = json.load(fh)
        except ValueError:
            cur = {}
    cur.update(rows)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cur, fh, indent=2)


def build_parser():
    p = argparse.ArgumentParser(description="NSE quarterly results reader (trader-analyst).")
    p.add_argument("symbols", nargs="+", help="NSE symbols, e.g. RELIANCE TCS M&M")
    p.add_argument("--quarters", type=int, default=8)
    p.add_argument("--json", action="store_true")
    p.add_argument("--standalone", action="store_true", help="prefer standalone over consolidated figures")
    p.add_argument("--unit", default=DEFAULT_UNIT, choices=sorted(UNIT_TO_CRORE), help="unit of NSE's amounts")
    p.add_argument("--offline", metavar="DIR")
    p.add_argument("--no-calendar", action="store_true", help="skip the board-meeting calendar")
    p.add_argument("--announcements", action="store_true", help="also list recent results announcements")
    p.add_argument("--within", type=int, default=5, help="trading days that count as 'soon' (5)")
    p.add_argument("--now", help='pin the India time, e.g. "2026-10-30 09:00"')
    p.add_argument("--metrics", metavar="FILE", help="merge one summary row per symbol into this JSON file")
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        today = nm.to_ist(nm.parse_now(a.now)).date()
    except nm.MarketError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    session = Session()
    results, errors, metrics = [], [], {}
    for raw in a.symbols:
        try:
            sym = nm.normalize_symbol(raw)
        except nm.MarketError as e:
            errors.append({"symbol": raw, "error": str(e)})
            continue
        try:
            res = {"symbol": sym, "analysis": analyse(load(sym, "results", a, session), a.quarters,
                                                      a.standalone, a.unit)}
            if not a.no_calendar:
                try:
                    res["calendar"] = calendar_status(parse_meetings(load(sym, "meetings", a, session)),
                                                      today, a.within)
                except NseError as e:
                    res["calendar_error"] = str(e)
            if a.announcements:
                try:
                    res["announcements"] = results_announcements(load(sym, "announcements", a, session))
                except NseError as e:
                    res["announcements_error"] = str(e)
            results.append(res)
            metrics[sym] = metrics_row(res)
        except NseError as e:
            errors.append({"symbol": sym, "error": str(e)})
    if a.json:
        print(json.dumps({"results": results, "errors": errors}, indent=2))
    else:
        for i, r in enumerate(results):
            if i:
                print()
            print(render(r))
            for k in ("calendar_error", "announcements_error"):
                if r.get(k):
                    print("Note: %s: %s" % (k.replace("_", " "), r[k]))
        for e in errors:
            print("%s: %s" % (e["symbol"], e["error"]), file=sys.stderr)
    if a.metrics and metrics:
        merge_metrics(a.metrics, metrics)
    return 0 if results else 2


if __name__ == "__main__":
    sys.exit(main())
