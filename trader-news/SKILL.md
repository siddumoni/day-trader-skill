---
name: trader-news
description: >-
  News Monitor for NSE stocks. Tracks headlines (Yahoo Finance, SYMBOL.NS) and
  NSE corporate announcements (results, board meetings, dividends, order wins,
  pledges, SEBI disclosures, resignations, mergers) for every stock you watch
  and flags what moves it, with how far each stock is from yesterday's close.
  Shows only what is new since the last check, newest first, in India time,
  tags each item (results, board meeting, dividend, order win, pledge, rating,
  regulatory, halt, macro...) and never repeats one. Use when the user says
  "any news on RELIANCE", "why is this moving", "what's the news on my
  watchlist", "morning briefing", "pre-market news", "did anything break",
  "check the announcements", or after /trader-scanner finds setups.
argument-hint: "[SYMBOL ...] [--hours N]"
---

# trader-news (NSE)

Reads public headlines and NSE announcements for your symbols and prints only
what is new. Headlines are leads, not facts: open the story before acting.

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py"                          # watchlist + plan + portfolio symbols
python3 "${CLAUDE_SKILL_DIR}/news.py" --tickers RELIANCE TCS "M&M" --hours 12
python3 "${CLAUDE_SKILL_DIR}/news.py" --tickers RELIANCE --all --hours 72   # seen items too
python3 "${CLAUDE_SKILL_DIR}/news.py" --no-nse --no-moves      # headlines only
```

`--json`, `--dry-run` (do not update the seen-list), `--move-pct 3`,
`--now "2026-10-30 15:00"` (IST), `--offline DIR` (reads `yahoo-<SYM>.xml`,
`chart-<SYM>.json`, `nse-<SYM>.json`).

Without `--tickers` it reads `trader/watchlist.json` (trader-scanner),
`trader/plan.json` (trader-exit) and `trader/portfolio.json`.

## Sources, one request each per symbol

| Source | Notes |
|---|---|
| Yahoo Finance RSS `...?s=RELIANCE.NS&region=IN&lang=en-IN` | free, can lag |
| NSE `corporate-announcements?index=equities&symbol=RELIANCE` | needs browser-like headers and cookies; NSE often blocks scripts (401/403): the tool says so and carries on with headlines |
| Yahoo chart `RELIANCE.NS` | the price move against yesterday's close, flagged `MOVING` at 3% |

Symbols with `&` (like `M&M`) are URL-quoted for you.

## How to read it

- Times are IST. `!` marks an announcement whose tags are important
  (results, merger, regulatory, pledge, risk, legal, halt, people).
- Tags: results, board meeting, dividend, buyback, deal (bulk/block), insider,
  pledge, order win, merger, capital raise, rating, analyst, people,
  regulatory (SEBI, RBI, CCI), legal, risk, halt (circuit, ASM/GSM), macro.
- **Announcements beat headlines.** If an exchange filing and a headline
  disagree, trust the filing and say so.
- **Results days.** An announcement about a board meeting for financial results
  means results are near: an intraday trade is exposed to a gap. Say so, and
  suggest `/trader-analyst`.
- State is `trader/state/news.json` (30 days), so an item shows once.

## Workflow

1. Run `news.py` before the open and again before entering a trade.
2. Report what is new, lead with `!` items and MOVING stocks, one line each.
3. If NSE was blocked, say so and do not imply there is nothing to see.
4. If nothing is new, say "Nothing new" and when it was checked.

## Safety

Not financial advice. Acting on unpublished price-sensitive information is
illegal under SEBI's insider-trading rules; everything here is public.
Headlines can be wrong, late or already priced in.

## Verified vs not

Tested offline on synthetic data (RSS, an NSE announcements JSON, a chart):
parsing, India time, tagging, dedupe, once-only display, blocked-NSE handling.
The NSE JSON field names (`symbol`, `desc`, `attchmntText`, `an_dt`,
`attchmntFile`, `seq_id`) are from memory and could not be checked against the
live site from the build sandbox; parsing is defensive and an unreadable item
shows "time unknown" instead of being invented.
