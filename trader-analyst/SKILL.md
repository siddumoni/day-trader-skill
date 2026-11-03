---
name: trader-analyst
description: >-
  Earnings Analyst for NSE companies. Reads a company's reported quarterly
  results from NSE's public corporate-financial-results data and sums up what
  changed in rupee crore: revenue (income from operations), operating profit,
  net profit and EPS, quarter by quarter, each against the same quarter a year
  earlier, with the trailing twelve months, preferring consolidated numbers
  (standalone on request). Also checks NSE's board-meeting calendar and flags
  "results due within N trading days", which means an intraday trade carries
  gap risk. Reads an earnings call transcript when you paste one. Use when the
  user says "how did Reliance's results look", "analyze the earnings", "what
  changed this quarter", "is revenue growing", "check the results", "compare
  margins", "sum up the call", "does it report this week", or before trading a
  stock around its results.
argument-hint: "SYMBOL [SYMBOL ...] [--quarters N] [--standalone]"
---

# trader-analyst (NSE)

Reads reported numbers and does the arithmetic. It reports what changed; it
does not say whether to buy.

On Windows, `python3` is usually missing: use `python` (or `py -3`) in place of `python3` in the commands below.

```bash
python3 "${CLAUDE_SKILL_DIR}/facts.py" RELIANCE
python3 "${CLAUDE_SKILL_DIR}/facts.py" RELIANCE TCS "M&M" --quarters 8
python3 "${CLAUDE_SKILL_DIR}/facts.py" INFY --standalone --announcements
python3 "${CLAUDE_SKILL_DIR}/facts.py" RELIANCE --metrics trader/metrics.json
python3 "${CLAUDE_SKILL_DIR}/facts.py" RELIANCE --offline DIR --now "2026-10-30 09:00"
```

Other flags: `--json`, `--unit` (the unit NSE's amounts use: lakh, crore,
rupees; default as NSE publishes), `--no-calendar`, `--within 5`.

## What it prints

- Per quarter, in **₹ crore**: revenue, operating profit and margin, net profit
  and margin, EPS (₹).
- **YoY** for each quarter against the same quarter a year earlier, in percent
  and rupees. Loss-making comparisons print "n/m" rather than a misleading %.
- **TTM**: the last four quarters summed, and the TTM a year earlier.
- **Results soon**: if the board-meeting calendar shows results within
  `--within` trading days (NSE calendar), it says so with the date, and why that
  matters for a same-day trade.
- Consolidated by default; `--standalone` switches. A mismatch between the two
  is called out, not hidden.
- Field names are matched by aliases (NSE's keys vary by filing), and any
  figure it cannot find is listed under `missing`, never guessed.

## Pasted call transcripts

If the user pastes an earnings call, read it yourself and summarise guidance,
margin comments, order book, capex, risks and tone, quoting briefly. Say what
is management's claim and what is reported data. Do not predict the stock move.

## Workflow

1. Run `facts.py SYMBOL`. Lead with revenue and net profit YoY and the margin
   change, then anything unusual (loss, one-off, big EPS divergence).
2. If results are within a few days, say so first.
3. Offer `/trader-news` for announcements and `/trader-insider` for disclosures.

## Safety

Not financial advice. Quarterly numbers are history; the price already knows
most of them. Results days gap both ways, and a stop is not guaranteed to fill
in a gap. Most retail intraday traders lose money.

## Verified vs not

Tested offline on synthetic filings: YoY, TTM, margins by hand calculation,
alias matching, loss handling, the results-soon flag. NSE's live JSON field
names (for example `re_net_sale`, `re_profit_after_tax`, `re_diluted_eps`) are
matched by a list of likely aliases written from memory, and could not be checked
against the live site from the build sandbox; the first live run may list fields
under `missing`. If so, show the user the raw field names (`--json`) so the
alias list can be fixed.
