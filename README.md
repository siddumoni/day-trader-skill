# The Day Trader skill, India edition (NSE)

11 Claude skills for intraday trading on the NSE: scan charts, track NSE announcements, time every entry and exit, replay every outcome before you risk a rupee, and size each trade in rupees after Zerodha-style charges.

This is a port of Jake Schincariol's MIT-licensed
[day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill) from
the US market to India. The US data sources (SEC EDGAR, Nasdaq Trader, the
House Clerk), the NYSE calendar, dollars and the Alpaca connector are gone.
In their place: NSE, IST, rupees, the Rs 0.05 tick, Indian charges and
Zerodha Kite. The original licence and credit are kept (see LICENSE).

Free, MIT, no signup. The tools are plain Python (3.8+, standard library only)
that read public data: Yahoo Finance charts (`SYMBOL.NS`), NSE's public
files and disclosures, and, when you connect it, your Kite account through
Claude's Kite connector.

**Not financial advice.** This is education and tooling. Most retail
intraday traders lose money (SEBI's own studies say so), and nothing here
changes that. Nothing in this pack places an order on its own: it is an
**advisory** assistant by default, and every order needs your typed "yes"
and then Claude's own Allow once / Deny card.

## The eleven skills

| Skill | What it does |
|---|---|
| `/trader-scanner` | Scans NSE EQ stocks for opening-range breakouts, 20-day highs, flags and oversold bounces, with a liquidity floor in rupee crore |
| `/trader-news` | Yahoo headlines plus NSE corporate announcements (results, board meetings, pledges, SEBI), new items only |
| `/trader-entry` | Watches the 09:15 to 09:30 opening range and calls the entry on a closed bar |
| `/trader-exit` | Stop and target on the 0.05 tick, with charges and **net R** |
| `/trader-timemachine` | 10,000 replays plus history on the stock's own last 59 sessions, net of charges |
| `/trader-budget` | Sizes whole shares so loss at the stop, charges included, fits your max loss in rupees |
| `/trader-risk` | PASS or BLOCK against your limits, IST window, NSE holidays, circuits |
| `/trader-broker` | Zerodha Kite tickets, your word-for-word yes, stop and target legs, flatten |
| `/trader-alerts` | Stop, target, flat-by and price alerts on NSE hours |
| `/trader-analyst` | Quarterly results from NSE in crore, YoY and TTM, and "results due soon" |
| `/trader-insider` | SEBI PIT insider and promoter filings, bulk and block deals, FII/DII flows |

The flow: scanner, news, entry, exit, time machine, budget, risk, broker. Alerts
run during the trade; analyst and insider run on a schedule you choose.

## Install

**Windows:** install Python 3.8+ from python.org (tick "Add python.exe to PATH"); the command is `python` or `py -3`, not `python3`, which on Windows is a Microsoft Store shortcut that fails. Use `Copy-Item -Recurse` where this page says `cp -r`, and `%USERPROFILE%\.claude\skills` for `~/.claude/skills`.

Copy the eleven `trader-*` folders and `nse_market.py` (already inside each
folder) into `~/.claude/skills/`:

```
cp -r trader-* ~/.claude/skills/
```

Or as a plugin from a copy of this repository you host yourself:

```
/plugin marketplace add <your-github-user>/<your-repo>
/plugin install day-trader-india@day-trader-india
```

Then copy `trader-budget/config.example.json` to `trader/config.json` in your
working folder and set your own limits. Money values end in `_inr`.

## India specifics

- **Time**: everything is IST (UTC+05:30, no daylight saving). The session is
  09:15 to 15:30. The opening range is the first 15 minutes (09:15 to 09:30).
  Defaults: no entries before 09:30 or after 14:30, flat by 15:00.
- **Closing auction (CAS)**: since 3 Aug 2026, F&O stocks stop continuous
  trading at 15:15. Pass `--cas` where a tool asks. A report that this changes
  again from 7 Dec 2026 is unconfirmed and is **not** applied: check NSE's
  circular and edit the cut-off if it is real.
- **Zerodha auto square-off** of MIS positions: 15:12 for F&O stocks, 15:25 for
  others, at Rs 50 + GST per order. The flat-by default is before both.
- **Holidays**: the 2026 NSE list is built in (16 dates). One date, 15 Jan 2026,
  is listed by one source and omitted by another. Muhurat trading (Sun 8 Nov
  2026) is not treated as a normal day. The 2027 list is not published in the
  pack: the clock warns when it runs past the known year.
- **Charges** (intraday equity, Zerodha schedule as read on 2026-10-29):
  brokerage the lower of 0.03% or Rs 20, STT 0.025% on the sell side, NSE
  transaction charge 0.00307%, SEBI Rs 10 per crore, stamp duty 0.003% on the
  buy side, GST 18% on brokerage plus exchange plus SEBI fees. These are
  estimates: your contract note is the truth. Charges are why a 1.5R target
  can be about 1.1R net, and the sizer and risk check count them.
- **Tick** Rs 0.05 for most stocks, but it is per instrument (RELIANCE showed Rs 0.10 on Kite's 2026-11-02 listing): read `tick_size` from Kite `search_instruments` and pass `exit.py --tick`. **Circuit bands** read from Kite quotes, whole shares,
  long side only, MIS product, no margin, no F&O unless you turn it on.
- **Kite stop and target are separate orders**, not a bracket (GTT does not
  cover MIS). `/trader-broker protect` prints the stop first, then the
  target, and you cancel the other when one fills.

## Data

Yahoo (`SYMBOL.NS`) is the default for candles. NSE's own endpoints need
browser headers and cookies and often refuse scripts; every tool says so and
carries on. The fallback is the Kite connector: `get_historical_data` and
`get_quotes`, then

```
python3 nse_market.py kite-to-chart candles.json --symbol RELIANCE > RELIANCE.json
```

and pass the file to the tool's `--offline`. Kite sessions expire daily; log in
each morning.

## Safety

1. Set Kite's order tools (`place_order`, `modify_order`, `cancel_order`,
   `place_gtt_order`) to **Needs approval**, never "Always allow".
2. `paper: true` in the config means advisory: Claude prints the ticket and you
   place it yourself. Set it to false only when you want Claude to place the
   order through Kite, and even then it asks twice.
3. A yes counts only if it is a plain "yes" for the order shown. "ok", "yes to
   all", "yes but" and a wrong quantity are refused.
4. A stop is a plan, not a guarantee: a gap or a circuit freeze can fill past
   it.

## Verified vs not

Verified offline, on synthetic data labelled as such (all eleven test
modules): the IST clock and holiday logic, tick rounding, charges, the opening
range and entry rule, exit maths and net R, the time machine, the sizer, every
risk check, the broker tickets and yes rules, alerts, quarterly arithmetic and
disclosure delays.

Verified live, once (Mon 2026-11-02, RELIANCE, through the Kite connector,
read-only): `search_instruments`, `get_quotes` and `get_historical_data` return
the shapes the converter expects; the whole chain (entry, exit, budget, risk)
ran on real 5-minute candles with Kite's real circuit band. That run found the
per-instrument tick bug, now fixed and tested. No order was placed.

Not verified: NSE and Yahoo calls (the build sandbox gets 403 from both) and
order placement. These were never run for real: the NSE endpoints and their JSON field
names (written from memory, parsed defensively, unreadable items reported as
missing), the Kite connector's exact tool arguments, and live prices. Treat your
first live run as a test: paper or one share.

Also open: the 15 Jan 2026 holiday, the possible 7 Dec 2026 CAS change, the 2027
holiday list, and charge rates, which Zerodha can change.

## Tests

```
cd tests && python3 -W ignore -m unittest discover -v
```

## Fine print

Not financial advice and no warranty. Markets are risky, you can lose all you
put in, and intraday trading in India is taxed as business income (speak to a
chartered accountant). Public disclosures and headlines can be wrong, late or
already priced in. Never trade on unpublished price-sensitive information:
it is illegal under SEBI's insider-trading rules. MIT licence, original work by
Jake Schincariol, India port by the repository owner.
