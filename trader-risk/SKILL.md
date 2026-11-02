---
name: trader-risk
description: >-
  Risk Manager for NSE intraday orders. Checks every order against your limits
  and stops anything over them, before it can reach the broker. For a day
  trade: a stop is required, the loss at the stop (plus charges) must fit your
  max loss per trade in rupees, reward:risk must meet your minimum, the day's
  total risk must fit your daily loss limit, no more than your trades per day,
  and no new entries outside your entry window, on a weekend, or on an NSE
  holiday. It also checks the Rs 0.05 tick, circuit bands, order size, position
  cap, cash buffer, margin, short sales, F&O and options, blocked symbols,
  duplicates, and a daily order limit. Prints PASS or BLOCK with the reason and
  records verdicts so trader-broker only shows an order that passed. Getting
  out is never blocked by a day limit. Use when the user says "check my
  order", "is this safe", "risk check", "does this break my rules", "can I take
  this trade", "how much have I risked today", or before /trader-broker,
  always.
argument-hint: "[--orders FILE] [--now HH:MM]"
---

# trader-risk (NSE)

The gate between sizing and the broker. It reads `trader/orders.json`, checks
each order, and records PASS or BLOCK in `trader/state/risk.json`. Every
check is plain arithmetic on your rules.

```bash
python3 "${CLAUDE_SKILL_DIR}/check.py"                       # checks trader/orders.json
python3 "${CLAUDE_SKILL_DIR}/check.py" --now "2026-10-30 10:15"
python3 "${CLAUDE_SKILL_DIR}/check.py" --json
python3 "${CLAUDE_SKILL_DIR}/check.py" --orders other.json --no-write
```

Other flags: `--config`, `--portfolio`, `--prices` (a price file, for the
circuit and price checks), `--state` (what trader-broker placed today),
`--today YYYY-MM-DD`, `--out`.

## What it checks

- **Window**: entries only between `no_entries_before` (09:30) and
  `no_entries_after` (14:30) India time, on a trading day. Weekends and NSE
  holidays (2026 list built in; 2027 unknown, it warns) block. Muhurat is not
  a normal day.
- **Stop required**: a BUY with no stop leg is blocked.
- **Loss at the stop**, including round-trip charges, must be within
  `max_loss_per_trade_inr`. Measured from the higher of the plan entry and a
  fresh price, so a stale entry does not hide risk.
- **Reward:risk** at least `min_reward_risk`.
- **Daily limit**: today's recorded risk plus this order within
  `max_daily_loss_inr`; at most `max_trades_per_day` entries; at most
  `max_orders_per_day` orders.
- **Order shape**: quantity a whole number, price and trigger on the 0.05 tick,
  entry and stop inside the circuit band when known, an order type of MARKET /
  LIMIT / SL / SL-M, a stop on the right side of the entry, a tag of at most 20
  letters and digits.
- **Money**: order value within `max_order_inr`, `max_position_pct` of budget,
  cash after the `cash_buffer_pct`.
- **Blocks**: short sales (`allow_short`), NRML or MTF products and buys that
  need margin (`allow_margin`), F&O and options (`allow_options`), commodities,
  blocked symbols, duplicates already placed today, and a MIS entry once
  Zerodha's square-off has started (15:12 for F&O stocks, 15:25 for the rest).
- **Exits are never blocked** by the daily limits: a SELL that closes a
  position always passes the window and limit checks (the tick and shape checks
  still apply), because trapping a loss is worse.

## Output

```
RELIANCE BUY 59 @ ₹1,255.90 MIS   PASS
  loss at stop ₹498 incl. ₹73 charges (limit ₹500), reward:risk 2.7, entry window ok
```
or `BLOCK` with each failed check named and what would fix it. The BLOCK is the
answer: do not look for a way around it. A fingerprint of the order is stored
so trader-broker can see the order is exactly what passed.

## Workflow

1. After `/trader-budget --write`, run `check.py`.
2. PASS: say so and hand to `/trader-broker`. BLOCK: list the reasons; offer to
   re-plan (smaller size, different stop), never to loosen the config to make an
   order pass unless the user asks and understands.
3. Any edit to the order after PASS needs a new check.

## Safety

- Not financial advice. Passing means the order is inside your own rules, not
  that the trade is good. Most retail intraday traders lose money.
- Only `/trader-broker` can lead to an order, and only after the user types
  "yes". Kite order tools stay on "Needs approval".

## Verified vs not

Tested offline: every check above passing and failing, exits never blocked,
holiday and weekend blocks, fingerprints, state recording. Not tested against
Kite's own order validation or live circuit data.
