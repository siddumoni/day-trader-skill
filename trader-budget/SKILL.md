---
name: trader-budget
description: >-
  Budget Manager for NSE intraday trades. Sizes every position to your budget
  and your max loss per trade in rupees, counting the round-trip charges as
  part of the loss. Reads the trade plan from trader-exit (entry, stop,
  target), finds the whole shares that fit inside four limits (max loss per
  trade, position cap, max order value, cash after your buffer), says which
  limit decided it, and writes ONE Kite order (MIS, a LIMIT entry) plus the
  stop (SL, with a limit buffer) and target (LIMIT) legs to
  trader/orders.json. A stop too far for your max loss, or a target whose net
  reward:risk after charges is below your minimum, is a plain refusal, not a
  smaller order. Nothing is placed. Use when the user says "how many shares",
  "size this trade", "how much should I buy", "I have Rs 1 lakh", "I only want
  to risk Rs 500 a trade", "set my rules", "keep 10% in cash", or after
  /trader-exit writes a plan.
argument-hint: "[--budget INR] [--limit PRICE | --market] [--write]"
---

# trader-budget (NSE)

Turns a plan into a share count and one order. It never places anything.

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py"                          # reads trader/plan.json and trader/config.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --write                  # also write trader/orders.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --budget 150000 --limit 1255.90 --write
python3 "${CLAUDE_SKILL_DIR}/size.py" --lower-circuit 1130.20 --upper-circuit 1381.10   # from Kite get_quotes
python3 "${CLAUDE_SKILL_DIR}/size.py" --weights RELIANCE=40,TCS=30,INFY=30   # long-term weights -> shares
```

Other flags: `--market` (MARKET entry instead of LIMIT), `--prices FILE`,
`--portfolio FILE`, `--targets FILE`, `--top N`, `--now "2026-10-30 09:50"`
(IST), `--json`.

## Rules (`trader/config.json`, copy `config.example.json`)

All money is in rupees. Defaults: budget 1,00,000, `cash_buffer_pct` 10,
`max_position_pct` 75, `max_order_inr` 80,000, `max_loss_per_trade_inr` 500,
`max_daily_loss_inr` 1,500, `max_trades_per_day` 3, `min_reward_risk` 1.5,
`flat_by` 15:00, `product` MIS, `broker` kite, `paper` true, `mis_leverage` 1.

## What it decides

1. Shares from the max loss: the loss at the stop **plus round-trip charges**
   must stay under `max_loss_per_trade_inr`. Charges (brokerage, STT, exchange
   and SEBI fees, stamp, GST) come from `nse_market.py`: estimates of
   Zerodha's schedule, so check the contract note.
2. Capped by the position cap, the max order value and cash after the buffer.
   Whole shares only. It names the limit that bound.
3. **Refuses** (does not shrink) when the stop is so far that even one share
   breaks the max loss, when net reward:risk after charges is below
   `min_reward_risk` (a 1.5R target can be about 1.1R net), when the price is
   outside the circuit band you pass, or when the stop is not below the entry.
4. Writes the orders: entry (BUY, LIMIT or MARKET, MIS), `exit_orders` with a
   stop (SL: trigger at the stop, limit a little below, `sl_limit_buffer_pct`)
   and a target (SELL LIMIT). On Kite these are **separate orders, not OCO**:
   when one fills, the other must be cancelled. `/trader-broker` handles that.

## Example (synthetic data, not a market day)

```
Size: RELIANCE, entry ₹1,255.90, stop ₹1,248.70, target 3R
  Shares        59 (position cap 75% of ₹1,00,000 binds)
  Order         ₹74,098 of ₹1,00,000, MIS
  Loss at stop  ₹425 price move + ₹73 charges, inside the ₹500 limit
  Net R         2.7 after charges
  Nothing was placed. Next: /trader-risk
```

## Workflow

1. After `/trader-exit`, run `size.py --write`.
2. Say which limit decided, what the max loss is including charges, and the net R.
3. Hand to `/trader-risk`, always, before `/trader-broker`.

## Safety

- Not financial advice. Most retail intraday traders lose money (SEBI studies).
- Nothing here places an order; only `/trader-broker` can, after `/trader-risk`
  passes the exact order and the user types "yes". Keep Kite order tools on
  "Needs approval", never "Always allow".
- Leverage: the default is 1 (no margin). More leverage raises losses as well as
  gains, and the risk check blocks buys that need margin unless `allow_margin`
  is on.

## Verified vs not

Tested offline on synthetic prices: sizing under each limit, charges inside the
max loss, refusals, tick rounding, circuit check, order shape. Kite's real
margin and order validation could not be exercised from the build sandbox.
