# SPY Credit Spread Strategy Specification

## Instruments

- **Underlying**: SPY (SPDR S&P 500 ETF)
- **Expiry**: 0DTE (Mon/Wed/Fri), 1DTE (Tue/Thu)
- **Structure**: Credit spreads only

## Entry Rules

### Timing
- 10:30 ET (primary entry)
- 14:00 ET (secondary entry, direction-match only)

### Direction Filter
- SPY > 20-day SMA → **Bull Put Spread** (sell OTM put, buy further OTM put)
- SPY < 20-day SMA → **Bear Call Spread** (sell OTM call, buy further OTM call)
- 14:00 entry: skip if current direction differs from 10:30 direction

### VIX Gate
- VIX >= 25 → no trade

## Strike Selection

- Sell delta: ~0.20 OTM
- Buy strike: sell strike ± $5 (fixed $5 spread width)

## Position Sizing

| Condition | Portfolio % |
|-----------|------------|
| Default | 20% |
| VIX spike +20% vs prev close | 40% |
| OpEx week | 30% |
| Friday or Monday | 25% |

**Seasonal multipliers:**
- Sep/Oct: × 0.5
- Jul/Nov: × 1.5

Each contract: $5 wide × 100 = $500 margin required.

## Exit Rules

| Rule | Trigger |
|------|---------|
| Profit target | P&L >= +50% of credit received |
| Stop loss | P&L <= -200% of credit received |
| Force close | 15:50 ET (market on close) |
| Alert | 15:55 ET if positions still open |

## Tail Hedge

- Buy 1 delta-0.05 OTM put per trading day (once per day)
- Max cost: $10 per contract
- Skip if price exceeds limit

## No-Trade Days

- FOMC, CPI, NFP event days
- Quarterly OpEx (3rd Friday of Mar/Jun/Sep/Dec)
- Day before NYSE market holiday
- NYSE market holidays

## Alerts (Pushover — critical only)

1. **Naked position**: Leg2 failed AND Leg1 buyback failed
2. **15:55 residual**: Positions remain open after 15:55 ET
3. **3 consecutive startup failures**: OpenD connection fails 3x in a row
4. **Bot crash**: Unhandled exception in main loop

## Leg Failure Handling

1. Sell Leg1 → wait 1s
2. Buy Leg2 (3 attempts, 2s apart)
3. If Leg2 fails all 3: buy back Leg1 (3 attempts)
4. If Leg1 buyback fails: send CRITICAL Pushover alert (naked position)

## Infrastructure

- VPS: ConoHa (160.251.138.33), Ubuntu 22.04
- API: moomoo OpenD v10.2.6208 → futu-api Python SDK
- Logs: /var/log/spx_bot/bot.log
- Systemd: network → opend → spx_bot
