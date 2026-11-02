---
name: trader-timemachine
description: >-
  Time Machine for NSE intraday trades. Replays every possible outcome of a
  trade before you risk a rupee: 10,000 simulated sessions plus every past time
  the setup fired. Takes the plan from trader-exit (entry, stop, target),
  rebuilds the rest of the day 10,000 times from the stock's own 5-minute bars
  over the last 59 NSE sessions (09:15 to 15:30 IST, or to 15:15 for F&O
  stocks with the closing auction), re-runs the same rule on every past
  session, and prices everything NET of Zerodha-style charges. Prints the odds
  of target first, stop first, or neither by the close, average R before and
  after charges, and one honest verdict, then hands off to /trader-budget. Use
  when the user says "run the time machine", "replay this trade", "what are
  the odds", "simulate this trade", "monte carlo", "backtest this setup", "how
  often does this hit the target", "is this a coin flip", or after
  /trader-exit writes a plan.
argument-hint: "[--qty N] [--cas] [--seed N] [--replays N]"
---

# trader-timemachine (NSE)

A simulation, not a prediction. It shows how a plan would have fared on the
stock's own recent behaviour, so you can see whether you are looking at an edge
or a coin flip that charges turn into a loss.

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                    # trader/plan.json, 10,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --qty 59 --write   # also save trader/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --cas              # an F&O (closing auction) stock
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 15:00      # end each replay at your flat-by time
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --offline RELIANCE-60d5m.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from trader/timemachine.json
```

Other flags: `--seed`, `--replays`, `--plan`, `--now "2026-10-30 09:45"` (IST),
`--json`, and `--symbol/--entry/--stop/--target/--entry-time` to run without a
plan file.

## What it does

1. **Monte Carlo.** The rest of the session (09:35 to 15:30, 71 bars after a
   09:35 entry) is rebuilt 10,000 times. Each 5-minute bar is drawn whole from
   the stock's own bars at about the same time of day over the last 59 full
   sessions. Prices land on the Rs 0.05 tick.
2. **History.** The same rule (15-minute opening range, stop rule, R multiple)
   is run on each of those real sessions: how often it fired, hit the target,
   hit the stop, or neither.
3. **Charges.** Everything is also priced net: average R gross and net, the
   round-trip charges in rupees, the break-even per share and what charges cost
   in R. Use `--qty` (or the plan's qty).
4. **Verdict.** One line, and it says when the odds lean toward the stop or the
   net average R is not positive.

NSE sessions have 75 five-minute bars. There are no half days. F&O stocks end
continuous trading at 15:15 from 3 Aug 2026 (`--cas`); the closing auction price
is not simulated.

## Data

Needs about 60 sessions of 5-minute bars plus today's bars up to the entry.
Yahoo (`SYMBOL.NS`) gives about 60 days. When it is blocked, pull the candles
with the Kite connector, convert with
`nse_market.py kite-to-chart`, and use `--offline`. If today's entry bar is not
in the data it refuses rather than guess.

## Workflow

1. After `/trader-exit` writes `trader/plan.json`: `timemachine.py run --qty N --write`.
2. Report target first, stop first, neither, and the **net** average R. Quote the
   verdict. If net R is near zero or negative, say that charges, not the setup,
   are the problem.
3. Mention the seed so the run can be repeated, then hand to `/trader-budget`.

## Safety

- Not financial advice. A simulation built from a stock's last 59 sessions
  cannot see results, news, halts or circuit limits.
- Nothing here places an order; only `/trader-broker` can, after `/trader-risk`
  passes it and the user types "yes". Kite order tools stay on "Needs approval".
  Most retail intraday traders lose money.

## Verified vs not

Tested on synthetic random-walk history (so the odds themselves mean nothing):
session length, F&O cutoff, tick, reproducibility by seed, probabilities adding
to 100%, net below gross, holiday-free history, refusals. Not tested on real
NSE data, which the build sandbox could not reach.
