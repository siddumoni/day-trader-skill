---
name: trader-entry
description: >-
  Entry Timer for NSE intraday trades. Watches a setup bar by bar and tells you
  the moment to get in. Reads 5-minute (or 1-minute) bars from Yahoo
  (SYMBOL.NS) or the Kite connector, builds the opening range (09:15 to 09:30
  by default) and calls the entry on the first completed bar that CLOSES above
  the range high, or above a level you give it, with an optional volume check.
  Says waiting and how far, triggered with the time and price, expired, or not
  open yet (weekends and NSE holidays included), and writes trader/entry.json
  for /trader-exit. Long side only. It never places an order. Use when the user
  says "tell me when to get in", "watch RELIANCE", "has it broken out", "did it
  break the opening range", "entry signal", "time my entry", "check my
  watchlist", or after /trader-scanner and /trader-news, before /trader-exit.
argument-hint: "SYMBOL [--setup orb|level] [--level PRICE]"
---

# trader-entry (NSE)

Calls the entry on a closed bar, never on a bar still forming. It does not
place anything.

```bash
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE                 # 15-minute range, 5-minute bars
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --write         # also save trader/entry.json
python3 "${CLAUDE_SKILL_DIR}/entry.py" check TCS --setup level --level 3945.50
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --interval 1m --min-volume-x 1.5
python3 "${CLAUDE_SKILL_DIR}/entry.py" watch RELIANCE --until 14:30   # one line per closed bar
python3 "${CLAUDE_SKILL_DIR}/entry.py" watchlist                      # every hit in trader/watchlist.json
```

Other flags: `--or-minutes 15`, `--cas` (F&O stock: continuous session ends 15:15),
`--offline FILE`, `--now "2026-10-30 09:50"` (IST) to replay a day, `--json`.
Config (`trader/config.json`): `no_entries_before` (09:30), `no_entries_after`
(14:30), both India time.

## The rule

- The **opening range** is the first `--or-minutes` of the session: the 09:15,
  09:20 and 09:25 five-minute bars (or fifteen 1-minute bars). Range high and
  low come from their highs and lows. The range is set at 09:30.
- **Triggered** is the first completed bar after the range whose close is above
  the range high (or above `--level`). Its time is the bar's close time (the
  09:30 bar closes at 09:35) and its price is that close.
- With `--min-volume-x 1.5` the trigger bar must also trade 1.5 times the range
  bars' average volume.
- Statuses: waiting, triggered, expired (past the entry cutoff), no-data, or
  not open yet. Before 09:30 the range is still forming. Weekends and NSE
  holidays: the market is closed. Muhurat sessions are not treated as normal.
- A late trigger is a different trade: the tool says how long ago it fired and
  where the price is now.

## Data when Yahoo is blocked

Fetch candles with the Kite connector (`search_instruments`, then
`get_historical_data`, interval `5minute`), save the rows, convert, and use
`--offline`:

```bash
python3 "${CLAUDE_SKILL_DIR}/nse_market.py" kite-to-chart candles.json --symbol RELIANCE > RELIANCE.json
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --offline RELIANCE.json
```

## Example (synthetic data, not a market day)

```
RELIANCE  Fri 2026-10-30 09:35 IST  opening-range breakout (15-minute range) on 5-minute bars
Opening range 9:15-9:30: high ₹1250.70, low ₹1248.70 (3 of 3 bars).
TRIGGERED 9:35 AM at ₹1255.90: the 9:30 bar (bar 3) closed above the range high ₹1250.70 (+0.42%).
This is the moment to get in, if you take the trade. Nothing was placed.
Next: set the stop and target BEFORE you buy: /trader-exit
```

## Workflow

1. After `/trader-scanner` and `/trader-news`, run `entry.py check SYMBOL --write`.
2. Report the status in one or two lines. If triggered, hand to `/trader-exit`.
3. Never say "buy". Say what the rule found.

## Safety

- Not financial advice: education and tooling. Most retail intraday traders
  lose money.
- Nothing here places an order. Only `/trader-broker` can lead to one, after
  `/trader-risk` passes it and the user types "yes". Keep the Kite order tools
  on "Needs approval".

## Verified vs not

Tested offline on synthetic NSE bars (forming range, trigger, expiry, volume
check, holiday and weekend, Kite-converted candles). Live Yahoo fetches were not
possible from the build sandbox.
