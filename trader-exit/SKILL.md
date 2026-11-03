---
name: trader-exit
description: >-
  Exit Planner for NSE intraday trades. Sets your stop and target before you
  enter, so you know when to get out. Reads the entry from trader-entry (or a
  price you give), puts the stop at the opening-range midpoint by default (or
  the range low, your price, a percent, or a multiple of the ATR) on the Rs
  0.05 tick, and sets the target at 2R (or 1R, 1.5R, 3R, or your price). Shows
  the round-trip charges, the break-even move per share and the NET R after
  Zerodha-style charges, and warns when a target barely clears costs. Flat-by
  defaults to 15:00 IST, before the broker's auto square-off. Refuses a stop
  at or above the entry or a target at or below it. Writes trader/plan.json
  for the Time Machine, Budget Manager and Alerts. Long side only. Use when
  the user says "where's my stop", "set my stop and target", "plan the exit",
  "when do I get out", "stop loss", "what's my R", "risk reward", or right
  after /trader-entry triggers.
argument-hint: "[--stop or-mid|or-low|PRICE|N%|NxATR] [--target 2R|PRICE] [--qty N]"
---

# trader-exit (NSE)

The plan is set before the entry. Prices are rupees on the Rs 0.05 tick.

On Windows, `python3` is usually missing: use `python` (or `py -3`) in place of `python3` in the commands below.

```bash
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan                              # from trader/entry.json
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --target 3R --qty 59 --write # also save trader/plan.json
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol RELIANCE --entry 1255.90 --or-high 1250.70 --or-low 1248.70
python3 "${CLAUDE_SKILL_DIR}/exit.py" show
```

Other flags: `--stop`, `--target`, `--qty`, `--tick`, `--cas` (F&O stock),
`--flat-by 15:00`, `--config`, `--now "2026-10-30 09:50"` (IST), `--json`.

## The rules

- **Tick is per stock.** Most NSE stocks trade on a Rs 0.05 tick, but Kite's
  `search_instruments` lists each stock's own `tick_size`: RELIANCE showed
  0.1 on 2026-11-02. Look it up and pass `--tick 0.1`; the plan carries it, and
  trader-budget and trader-risk then use it. A stop or target off the tick is
  rejected by Kite.

- **Stop**: opening-range midpoint by default, rounded to the tick on the safe
  side (a stop never ends up closer than asked). It must be below the entry and
  not so tight the spread would trigger it.
- **Target**: `2R` default, where R is the rupees at risk per share. Rounded to
  the tick.
- **Charges.** With `--qty`, the plan prints round-trip charges (brokerage,
  STT on the sell side, exchange and SEBI fees, stamp duty on the buy side,
  GST), the break-even move per share, the loss at the stop including charges,
  and the **net R**. A 1.5R target before charges can be about 1.1R after them.
  Say this plainly: it is the cost of intraday trading in India.
- **Flat by** 15:00 IST by default. Zerodha auto-squares MIS positions at 15:25
  (15:12 for F&O stocks) and charges Rs 50 + GST per order for it. No overnight
  hold.
- A stop is a plan, not a guarantee: a gap, a circuit limit or a fast market can
  fill past it.

## Example (synthetic data, not a market day)

```
Exit plan: RELIANCE long, Fri 2026-10-30, opening-range breakout on 5-minute bars
  Entry         ₹1,255.90 at 9:35 AM (bar 3)
  Stop          ₹1,249.70  opening-range midpoint
  Target        ₹1,265.20  1.5R (1.5 times the risk)
  Risk a share  ₹6.20 (0.49% of the entry)
  Charges       40 shares (--qty): break-even ₹1.33 a share before you make a rupee
  At the stop   lose ₹301.23 (₹53.23 of it charges)
  Net R         1.06 after charges (1.5 before)
  Flat by       15:00 IST, win or lose: no overnight hold
```

## Workflow

1. After `/trader-entry` triggers, run `exit.py plan --qty N --write`.
2. Show the plan; point out the net R if it differs much from the gross R.
3. Hand to `/trader-timemachine`, then `/trader-budget`.

## Safety

- Not financial advice: education and tooling. Most retail intraday traders
  lose money.
- Nothing here places an order. Only `/trader-broker` can, after `/trader-risk`
  passes it and the user types "yes". Kite order tools stay on "Needs approval".

## Verified vs not

Tested offline on synthetic data: stop and target maths on the tick, charges
and net R, refusals, flat-by, F&O cutoff, Kite-converted candles. Charges are
from Zerodha's published schedule as read on 2026-10-29: check the contract
note.
