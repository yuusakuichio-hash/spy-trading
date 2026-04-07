# SPY 0DTE/1DTE Credit Spread Bot

Automated SPY options trading bot running on a VPS (ConoHa Ubuntu 22.04).

## Architecture

```
moomoo OpenD (API gateway)
    └── spx_bot.py (Python 3, systemd service)
            └── Pushover (critical alerts only)
```

## Deployment Status

| Step | Status |
|------|--------|
| VPS SSH access | ✅ Done |
| spx_bot.py implementation | ✅ Done |
| moomoo OpenD headless login | 🔄 Pending moomoo support |
| systemd opend + spx_bot services | ⏳ After OpenD login resolved |
| Live trading | ⏳ After OpenD login resolved |

## Strategy Overview

See [STRATEGY.md](STRATEGY.md) for full specification.

**Key parameters:**
- Instrument: SPY (0DTE Mon/Wed/Fri, 1DTE Tue/Thu)
- Entry: 10:30 ET and 14:00 ET
- Direction: Bull Put Spread (SPY > SMA20) / Bear Call Spread (SPY < SMA20)
- VIX gate: no trade if VIX >= 25
- Profit target: 50% of credit received
- Stop loss: 200% of credit received
- Force close: 15:50 ET

## Files

- `spx_bot.py` — Main bot (production-ready, dry-run mode if OpenD not connected)
- `STRATEGY.md` — Full strategy specification

## Running

```bash
# Install dependencies
pip install futu-api requests

# Run (dry-run mode if OpenD not connected)
python3 spx_bot.py

# Production (via systemd after OpenD login)
systemctl start spx_bot
```

## Alerts (Pushover)

Critical alerts only:
- Naked position risk (Leg2 failed + Leg1 buyback failed)
- 15:55 ET residual position
- 3 consecutive startup failures
- Bot crash
