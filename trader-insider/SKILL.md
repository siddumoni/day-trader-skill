---
name: trader-insider
description: >-
  Insider and Big-Money Tracker for NSE. Replaces the US congressional-trades
  tracker. Reads the public disclosures Indian rules require: SEBI PIT insider
  and promoter trades (buys, sells, pledges, with the days taken to disclose),
  NSE bulk and block deals, and daily FII/DII cash-market flows. Shows who,
  which stock, buy or sell, quantity, value in rupee crore and disclosure delay
  counted in NSE trading days, flags stocks on your watchlist, plan or
  portfolio, and alerts once per new filing. Use when the user says "what are
  insiders buying", "promoter buying", "promoter pledge", "bulk deals today",
  "block deals", "FII DII data", "did the promoters sell", "any big deals in
  RELIANCE", "insider trades on my watchlist", or "alert me on new insider
  filings".
argument-hint: "pit | deals | fiidii | watch [--symbols SYM ...]"
---

# trader-insider (NSE)

Public disclosures, not tips. A filing shows what someone did days ago, for
reasons the data cannot show. It is context for a trade, never a reason by
itself.

```bash
python3 "${CLAUDE_SKILL_DIR}/insider.py" pit --symbols RELIANCE TCS            # SEBI PIT disclosures
python3 "${CLAUDE_SKILL_DIR}/insider.py" pit --side buy --min-crore 1          # only promoter/insider buys over Rs 1 crore
python3 "${CLAUDE_SKILL_DIR}/insider.py" pit --side pledge                     # pledges
python3 "${CLAUDE_SKILL_DIR}/insider.py" deals --kind block                    # bulk and block deals
python3 "${CLAUDE_SKILL_DIR}/insider.py" fiidii                                # FII/DII net flows
python3 "${CLAUDE_SKILL_DIR}/insider.py" watch                                 # new filings on your symbols, once each
python3 "${CLAUDE_SKILL_DIR}/insider.py" watch --dry-run
```

Other flags: `--offline DIR`, `--json`, `--watchlist`, `--plan`, `--portfolio`
(where "your symbols" come from), `--today YYYY-MM-DD`, `--state`.

## What each shows

- **pit**: SEBI's Prohibition of Insider Trading disclosures. Rows with the
  person and category (promoter, director, employee), stock, acquisition or
  disposal or pledge, quantity, **value in ₹ crore**, transaction date,
  disclosure date, and **days to disclose** counted over NSE trading days. A
  late filing is called out.
- **deals**: bulk deals (a client trading 0.5% or more of a company's shares in
  a day) and block deals, with client, buy or sell, quantity, price and value.
- **fiidii**: net FII and DII cash-market buying or selling for the day, in
  ₹ crore (negative shown as `-₹x Cr`). A market-wide mood read, nothing about
  a single stock.
- **watch**: new PIT and deal filings for your watchlist, plan and portfolio
  symbols, alerting once each (`trader/state/insider.json`). Run it on a
  schedule you choose; it does not trade or message anyone.

## How to report it

1. Lead with the biggest and the ones on the user's symbols.
2. Say the facts: who, what, how much, when, and how late.
3. Note what it cannot say: promoters sell for tax or family reasons, pledges
   can be routine, a block deal may be a stake transfer. Do not call a filing
   bullish or bearish.
4. If a field was missing in NSE's data, say so ("value not reported").

## Safety

Not financial advice. Everything here is public. Using unpublished
price-sensitive information is illegal under SEBI's rules, and this tool never
touches any. Disclosures lag, and most retail intraday traders lose money.

## Verified vs not

Tested offline on synthetic data: parsing, `-₹x Cr` formatting, delay counted
over NSE trading days and holidays, once-only alerts, missing fields. The live
endpoints (`corporates-pit`, bulk and block CSVs, `fiidiiTradeReact`) need
browser headers and cookies, are often blocked for scripts, and their field
names are from memory: the build sandbox could not reach NSE, so treat the
first live run as a test. If blocked, save the data from nseindia.com and use
`--offline`.
