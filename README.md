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

## 資金計画（ロードマップ）

| 時期 | Bot元本 | 月収益目標(8%) | 状態 |
|------|---------|--------------|------|
| 2026年4月 | 120万円 | 約10万円 | ⏳ Bot稼働待ち |
| 2026年7月 | 210万円 | 約17万円 | ⏳ |
| 2026年10月 | 320万円 | 約26万円 | ⏳ |
| 2027年1月 | 450万円 | 約36万円 | ⏳ |
| 2027年4月 | 600万円以上 | **40万円以上** | 🎯 目標 |

※月30万円追加入金込み・複利計算

## 📈 Roadmap

| Period | Capital | Monthly Target (8%) | Status |
|--------|---------|---------------------|--------|
| Apr 2026 | ¥1.2M | ~¥100K | ⏳ Bot launch |
| Jul 2026 | ¥2.1M | ~¥170K | ⏳ |
| Oct 2026 | ¥3.2M | ~¥260K | ⏳ |
| Jan 2027 | ¥4.5M | ~¥360K | ⏳ |
| Apr 2027 | ¥6M+ | **¥400K+** | 🎯 Goal |

*Includes ¥300K/month additional deposits + compound growth*
