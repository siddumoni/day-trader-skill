---
name: trader-broker
description: >-
  Order Executor for Zerodha Kite, advisory by default, never without your
  yes. Shows each order that passed trader-risk as a plain ticket (entry, stop
  and target legs, the max loss at the stop including charges) plus the exact
  arguments for Kite's place_order, records your yes word for word, then
  Claude's own Allow once / Deny card asks again. Because Kite's stop and
  target are separate orders (not OCO), it builds the stop ticket and the
  target ticket after the entry fills, and the flatten ticket (SELL MARKET, MIS)
  that closes the trade by your flat-by time. Records fills and charges and
  logs each trade's max loss so trader-risk can hold the daily limit. Covers
  connecting the Kite MCP connector (daily login, "Needs approval", never
  "Always allow"). Use when the user says "place the order", "buy it",
  "execute", "send it to Zerodha", "close my position", "flatten", "get me
  out", "connect Kite", "set up Zerodha", or after /trader-risk passes.
argument-hint: "show | approve | protect | fill | flatten | log"
---

# trader-broker (Zerodha Kite)

The only skill that can lead to an order, and it still cannot place one by
itself: you type "yes" for one specific order, and Claude's tool card asks
again. `paper` in `trader/config.json` defaults to **true**, which means
**advisory**: Claude prints the ticket and you place it yourself in Kite.
Set `paper` to false only when you want Claude to place it through the Kite
connector, and even then it asks twice.

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                       # tickets for orders that PASSED trader-risk
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "yes"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status placed --broker-id 2610300001234
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol RELIANCE --side buy --qty 59 --price 1255.90
python3 "${CLAUDE_SKILL_DIR}/ticket.py" protect --index 1          # stop ticket first, then target
python3 "${CLAUDE_SKILL_DIR}/ticket.py" flatten --symbol RELIANCE --qty 59
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log
```

Other flags: `--now`, `--today`, `--orders`, `--risk`, `--state`,
`--charges` (from the contract note; default is an estimate), `--filled-qty`,
`--filled-price`, `--note`, `--status placed|denied|failed`, `--which stop|target`.

## The flow

1. `show` lists only orders that PASSED and whose fingerprint still matches
   what trader-risk checked. Anything edited since is refused.
2. Read the ticket to the user in plain words: symbol, BUY, quantity, LIMIT
   price, MIS, stop trigger and limit, target, **max loss including charges**,
   flat by. Then ask for a "yes" for that order.
3. `approve --reply "<their words>"` accepts only a clear yes. It refuses:
   "ok", "sure", "go ahead", "yes to all" or any batch wording, a yes with doubt
   ("yes but", "maybe"), a question, and a reply naming the wrong symbol or
   quantity. A refusal means ask again; never reword their answer into a yes.
4. Advisory (`paper` true): show the exact Kite fields and stop. The user places
   it. Live (`paper` false): call `mcp__Kite__place_order` with the ticket's
   `tool_input` and nothing else. Claude's Allow once / Deny card appears; Deny
   means `result --status denied`.
5. Record with `result`. After the entry fills, `fill` records price and
   charges, and `protect` prints the **stop ticket first, then the target**.
   They are separate Kite orders: when one fills, cancel the other
   (`mcp__Kite__cancel_order`). GTT is not available for MIS.
6. By your flat-by time (15:00, before Zerodha's auto square-off at 15:25, or
   15:12 for F&O stocks with ₹50 + GST per auto-squared order), `flatten` builds a
   SELL MARKET MIS ticket. It needs its own "yes" like any other order.

## Connecting Kite

Add the Kite MCP connector in Claude (Settings, Connectors). Log in once per
day: Kite sessions expire daily (`mcp__Kite__login`). Open the connector's tool
permissions and set `place_order`, `modify_order`, `cancel_order` and
`place_gtt_order` to **Needs approval**, never "Always allow". Keep the API
credentials out of chat.
`claude_desktop_config.example.json` is the hosted-endpoint entry from
Zerodha's kite-mcp-server README (`https://mcp.kite.trade/mcp`, via
`mcp-remote`). That README says the hosted version excludes potentially
destructive trading operations, so `place_order` may simply not exist there
(self-hosting with your own Kite Connect keys is the other route). If the
order tools are absent, keep `paper` true: advisory mode needs only the
read tools (quotes, historical data, positions, orders).

## Safety

- Not financial advice. Most retail intraday traders lose money, and a stop
  order can fill past its price in a gap or a circuit freeze.
- Only a plain "yes" for the order shown counts. Silence, thumbs up and "go on"
  do not.
- Never place an order that trader-risk blocked, and never edit one after PASS.

## Verified vs not

Tested offline: tickets, fingerprint match with trader-risk, every accepted and
refused reply, protect ordering, fills with charges, flatten ticket, advisory
vs live paths. The Kite connector itself (login, `place_order` argument names,
approvals) could not be exercised from the build sandbox: check the tool's
schema before the first live order and start with one share.
