---
name: trader-alerts
description: >-
  Price Alerts for NSE. Rings when a price, a stop or the opening bell hits.
  Turns the trade plan into three alerts in one go (the stop, the target and
  "flat by" at your flat-by time, 15:00 IST by default), plus above / below
  levels, percent moves from the previous close, any time of day, and the
  09:15 opening bell, each ringing once so you are not spammed. Knows NSE hours
  (09:15 to 15:30, F&O stocks end continuous trading at 15:15 with a closing
  auction) and the 2026 holiday list, works out India time itself, reads free
  Yahoo quotes (SYMBOL.NS) or Kite get_quotes, warns when a price is at its
  circuit limit, and writes trader/prices.json for sizing and the risk check.
  Use when the user says "alert me when", "tell me if RELIANCE hits", "watch my
  stop", "watch my trade", "remind me to close by 3", "is the market open",
  "when does NSE open", "get me a price", or right after /trader-exit writes
  a plan.
argument-hint: "[add --from-plan | check | quote SYMBOL | clock]"
---

# trader-alerts (NSE)

Alerts only tell you. They never place or cancel an order. Run `check`
repeatedly (every minute or so, by hand or through a loop) and each rule
rings once.

```bash
python3 "${CLAUDE_SKILL_DIR}/alerts.py" clock                       # is the NSE open? next open
python3 "${CLAUDE_SKILL_DIR}/alerts.py" clock --cas                 # for an F&O stock
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote RELIANCE TCS --write  # prices -> trader/prices.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan             # stop + target + flat-by from trader/plan.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --above 1290
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol TCS --pct -2 --note "gap down"
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --at 14:45 --label "flat by"
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --opening-bell
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check
python3 "${CLAUDE_SKILL_DIR}/alerts.py" list
python3 "${CLAUDE_SKILL_DIR}/alerts.py" reset <id>
```

Global flags (before the command): `--source yahoo|kite`, `--offline DIR`
(reads `<source>-<SYMBOL>.json`), `--now`, `--cas`, `--json`.

## Rules

`--above`, `--below`, `--stop`, `--target`, `--pct` (move from the previous
close), `--at HH:MM` (India time), `--opening-bell`, `--from-plan`. Add
`--date` to limit a rule to one day, `--rearm` to ring again, `--direction`
for `--pct`. Rules live in `trader/alerts.json`, what already rang in
`trader/state/alerts.json`.

## What to know

- **Hours**: 09:15 to 15:30 IST. Since 3 Aug 2026 F&O stocks stop continuous
  trading at 15:15 and a closing auction follows; pass `--cas`. A "flat by"
  alert after that cut-off is flagged as late. Weekends, NSE holidays and
  Muhurat days are called out. The 2027 holiday list is not known: the clock
  says so.
- **Prices are delayed** (Yahoo about 1 to 15 minutes, can be stale) and a
  stale quote is labelled. For live quotes Claude can use `mcp__Kite__get_quotes`
  and the output saved with `--source kite --offline`. Kite quotes also carry the
  day's circuit limits: a price sitting AT the lower or upper circuit prints a
  warning, because a sell or a stop may not fill there.
- **Stop and target** alerts are reminders. Kite's actual stop order is a
  separate order you or `/trader-broker` place.
- Daily Kite login: sessions expire each day; log in again if quotes fail.

## Example

```
RING  RELIANCE stop ₹1,249.70: last ₹1,249.40 (9:52 AM IST)
  Your stop. Nothing was sold by this alert.
RING  flat by 15:00: close the position now.
```

## Workflow

1. After `/trader-exit`, `add --from-plan`; confirm the three rules.
2. During the trade, `check` on a cadence the user chooses; report only what rang.
3. At the bell, hand any "flat by" ring to `/trader-broker flatten`.

## Safety

Not financial advice. An alert is information, not an instruction: a price
can jump past a level between checks. Most retail intraday traders lose money.
Nothing here places an order: only `/trader-broker` can, after `/trader-risk`
passes it and the user types "yes". Kite order tools stay on "Needs approval".

## Verified vs not

Tested offline: clock and holiday logic, rule add/check/ring-once/re-arm, the
plan-to-three-rules path, circuit warning, Kite-format quotes. Live Yahoo and
Kite fetches could not be run from the build sandbox.
