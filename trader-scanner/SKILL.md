---
name: trader-scanner
description: >-
  Chart Scanner for the Indian market (NSE). Scans thousands of NSE charts for
  setups: opening-range breakouts, 20-day closing-high breakouts, bull flags,
  reclaimed 20-day average, oversold and oversold bounces. Builds the universe
  from NSE's EQUITY_L.csv (EQ series only, ETFs and funds dropped), filters out
  illiquid stocks by price and average daily traded value in rupee crore, can
  mark F&O stocks (which now have a closing auction), and writes
  trader/watchlist.json for trader-news and trader-entry. Reads Yahoo
  (SYMBOL.NS) or candles from the Kite connector. Use when the user says "scan
  the market", "find me setups", "what is breaking out", "opening range
  breakouts", "pre-market scan", "build my watchlist", "oversold stocks",
  "what should I look at today", or at the start of a trading day before
  /trader-news.
argument-hint: "[universe | daily | orb] [--at HH:MM]"
---

# trader-scanner (NSE)

The first step of an intraday trade: read many charts, list the ones that match
a rule. A scan is a list of charts that match a rule, not a list of stocks to
buy, and a setup is not a prediction. Most retail intraday traders lose money
(SEBI's own studies say so); this does not change that, it only saves you
flipping through 2,000 charts.

`${CLAUDE_SKILL_DIR}` is this skill's folder. All times are India time (IST).

On Windows, `python3` is usually missing: use `python` (or `py -3`) in place of `python3` in the commands below.

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe --offline EQUITY_L.csv     # -> trader/universe.txt
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily                              # before 09:15: daily setups
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --universe nifty200 --universe-file ind_nifty200list.csv
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb                                # after 09:30: opening-range breakouts
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --fno-list fno.txt --at 10:15   # mark F&O stocks, cut the clock
```

Other flags: `--symbols RELIANCE,TCS`, `--watchlist FILE`, `--limit N`,
`--min-price 50`, `--min-value-cr 10`, `--min-volume`, `--or-minutes 15`,
`--now "2026-10-30 09:50"` (IST) to replay or pin the clock, `--offline PATH`,
`--json`, `--no-write`.

## What it does

- **universe**: reads NSE's `EQUITY_L.csv`, keeps series `EQ`, drops ETFs and
  funds (by name), BE-series (trade-for-trade) and the rest. NSE often blocks
  scripts: if the download fails, ask the user to save the file from
  nseindia.com in a browser and pass it with `--offline`.
- **daily** (pre-market): from daily closes, 20-day closing-high breakout, bull
  flag, reclaimed the 20-day average, oversold, oversold bounce. Each hit
  carries the 20-day average traded value in crore.
- **orb** (intraday): the opening range is the first 15 minutes, 09:15 to 09:30.
  A hit is the first 5-minute bar that closes above the range high; nothing
  before 09:30 (the range is still forming) and nothing on a holiday.
- **Liquidity matters more in India.** Defaults: price at least Rs 50 and
  average daily traded value at least Rs 10 crore. Thin stocks gap, hit
  circuits and fill badly. `--universe nifty100|nifty200|fno` takes a list you
  supply (`--universe-file`): the pack does not hard-code index members it
  cannot verify.
- **F&O stocks**: since 3 Aug 2026 their continuous trading ends at 15:15 and a
  closing auction follows. Give `--fno-list` and hits are marked `fno` / `cas`.

## Data when Yahoo is blocked

Claude can pull candles from the Kite connector: `search_instruments` for the
`instrument_token`, then `get_historical_data` (`5minute` or `day`). Save the
rows (date, open, high, low, close, volume) to a file, then:

```bash
python3 "${CLAUDE_SKILL_DIR}/nse_market.py" kite-to-chart candles.json --symbol RELIANCE --interval 5m > RELIANCE.json
```

and pass the folder or file to `--offline`. Offline accepts a folder of
`<SYMBOL>.json` v8 charts, or one bundle `{"RELIANCE": <chart>, ...}`.

## Output

`trader/watchlist.json`: `hits` with symbol, setups, price, time and range, plus
the filters used and counts (requested, fetched, illiquid, hits). trader-news,
trader-entry and trader-insider read it.

## Workflow

1. Before 09:15: `scan.py daily`. Show the hits and say what each rule means.
2. After 09:30 (10:00 is common): `scan.py orb`. State the time it was run.
3. Offer `/trader-news SYMBOL`, then `/trader-entry SYMBOL`.
4. Never call a hit a buy. Say what the rule found and what it cannot know.

## Safety

- Not financial advice: education and tooling.
- Nothing here places an order. Only `/trader-broker` can lead to one, after
  `/trader-risk` passes that exact order and the user types "yes" for it.
- Set the Kite order tools to "Needs approval", never "Always allow".

## Verified vs not

Tested offline on synthetic NSE data (EQUITY_L.csv parsing, ORB at 09:30,
daily setups, liquidity floor, F&O marking, holiday refusal). The live NSE and
Yahoo fetches could not be run from the build sandbox, which cannot reach
either site. Treat the first live run as a test.
