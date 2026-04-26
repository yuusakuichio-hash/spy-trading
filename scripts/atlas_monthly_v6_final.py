#!/usr/bin/env python3
"""
Atlas月利v6 最終版 (2026-04-21) — v3 計算モデル修正

v2の問題: 有効並行率(0.3-0.6)とBS割引(0.2-0.5)を両方掛けて過度に縮小
v3の修正:
  - Blinded BTのtotal_pnlはBS理論の単独実測値
  - Atlas統合運用では各戦術が同一capitalを共有 → 理論上は合計pnl達成可能だが
    margin制約と戦術間の排他で実効並行率0.4-0.6に留まる
  - BS割引は単一トレードでBS価格vs実約定のギャップ (0.35-0.55が標準範囲)
  - これら両方を掛けるのは正しい (各独立な縮小要因)
  - ただし、v5設計時のペーパー35%/月margin比を無視してはいけない

v3 approach:
  - 下限: BT+並行率+割引の三段圧縮
  - 上限: ペーパー実測margin比 × 資本比換算係数
  - 中央: 両者の幾何平均
"""
import json
import statistics
from pathlib import Path

# ===========================================================================
# 入力データ
# ===========================================================================
BLINDED_BT = {
    "butterfly":             {"n": 38,  "wr": 0.579, "sharpe": 7.61,  "dd": 0.012, "pnl": 1016.50,  "pass": True},
    "ic_sell":               {"n": 340, "wr": 0.815, "sharpe": 3.01,  "dd": 0.109, "pnl": 7075.03,  "pass": True},
    "strangle_sell":         {"n": 208, "wr": 0.779, "sharpe": 8.23,  "dd": 0.036, "pnl": 7240.25,  "pass": True},
    "symbol_selector_plus":  {"n": 503, "wr": 0.841, "sharpe": 11.84, "dd": 0.015, "pnl": 17456.51, "pass": True},
    "earnings_iv_crush":     {"n": 73,  "wr": 0.904, "sharpe": 7.26,  "dd": 0.134, "pnl": 15095.76, "pass": True},
    "portfolio_aggregator":  {"n": 510, "wr": 0.863, "sharpe": 4.46,  "dd": 0.059, "pnl": 10741.01, "pass": True},
    "ivr_credit_spread":     {"n": 281, "wr": 0.964, "sharpe": 1.25,  "dd": 0.044, "pnl": 1092.54,  "pass": True},
}
ORB_1DTE_SPY = {"n": 70, "wr": 0.600, "sharpe": 2.49, "dd": 0.052, "pnl": 1735.00, "months": 6.4}

PAPER_LIVE = [
    {"date": "2026-04-17", "trades": 132, "pnl_usd": 8309.24, "rom_pct_day": 2.19, "margin": 380000},
    {"date": "2026-04-18", "trades":  69, "pnl_usd": 5410.00, "rom_pct_day": 1.42, "margin": 380000},
    {"date": "2026-04-19", "trades":  52, "pnl_usd": 4030.00, "rom_pct_day": 1.06, "margin": 380000},
    {"date": "2026-04-20", "trades": 116, "pnl_usd": 8990.00, "rom_pct_day": 2.37, "margin": 380000},
]

# ===========================================================================
# Step 1: 戦術別単独月利 (BS理論・$10K独立運用・28ヶ月)
# ===========================================================================
N_MONTHS_THETA = 28
strategy_monthly_theo = {
    name: d["pnl"] / N_MONTHS_THETA / 10000 * 100
    for name, d in BLINDED_BT.items() if d["pass"]
}
strategy_monthly_theo["orb_1dte_spy"] = ORB_1DTE_SPY["pnl"] / ORB_1DTE_SPY["months"] / 10000 * 100
total_sum = sum(strategy_monthly_theo.values())

# ===========================================================================
# Step 2: 下限推定 (BT合計 × 有効並行率 × BS割引)
# ===========================================================================
# Atlas は戦術選択で排他 + margin限界で同時並行数制約
# 実効並行率 = 各戦術の「選択確率 × margin割当率」の和
# 楽観 0.55 (環境多様): 3戦術同時 + 回転率高
# 中央 0.40 (標準): 2-3戦術並行
# 保守 0.25 (集中): 1-2戦術のみ
EFFECTIVE_PARALLELISM = {"optimistic": 0.55, "central": 0.40, "conservative": 0.25}

# BS割引: data/backtest_discount_factor.md + tastytrade実運用比較
# SPY 0DTEの典型値 0.35-0.55 (流動性・スプレッド・滑り)
BS_DISCOUNTS = {"optimistic": 0.55, "central": 0.40, "conservative": 0.25}

lower_bound = {}
for sc in ["optimistic", "central", "conservative"]:
    lb = total_sum * EFFECTIVE_PARALLELISM[sc] * BS_DISCOUNTS[sc]
    lower_bound[sc] = round(lb, 2)

# ===========================================================================
# Step 3: 上限推定 (ペーパー実測 margin比 × 資本換算係数)
# ===========================================================================
# ペーパー4日平均 ROM = 1.76%/日 = 35.2%/月 (margin比 $380K名目)
# ↓
# margin比 → capital比の変換:
#   実際に稼働したmargin平均は名目の30-50%程度と推測
#   ($380K発注可能枠のうち月平均使用量 $100-200K)
#   capital比月利 = margin比 × 0.30-0.50 = 10-18%/月
pnl_list = [d["pnl_usd"] for d in PAPER_LIVE]
rom_list = [d["rom_pct_day"] for d in PAPER_LIVE]
avg_rom_day = statistics.mean(rom_list)
paper_monthly_margin_pct = avg_rom_day * 20  # 35.2%/月

# margin utilization ratio
MARGIN_UTIL = {"optimistic": 0.55, "central": 0.40, "conservative": 0.25}
upper_bound = {sc: round(paper_monthly_margin_pct * MARGIN_UTIL[sc], 2) for sc in MARGIN_UTIL}

# ===========================================================================
# Step 4: 中央値 = 下限×上限の幾何平均
# ===========================================================================
# 下限 (BT理論ベース) と上限 (ペーパー実測ベース) の幾何平均を採用
final_monthly_pre_tax = {}
for sc in ["optimistic", "central", "conservative"]:
    geom = (lower_bound[sc] * upper_bound[sc]) ** 0.5
    final_monthly_pre_tax[sc] = round(geom, 2)

# ===========================================================================
# Step 5: 税引後
# ===========================================================================
TAX_FACTORS = {"optimistic": 0.75, "central": 0.65, "conservative": 0.55}
final_monthly_post_tax = {
    sc: round(final_monthly_pre_tax[sc] * TAX_FACTORS[sc], 2)
    for sc in TAX_FACTORS
}

# ===========================================================================
# 出力
# ===========================================================================
print("=" * 90)
print("Atlas月利v6 最終版 (2026-04-21) — v3計算モデル")
print("=" * 90)

print("\n--- Step 1: 戦術別単独月利 (BS理論・$10K独立運用・28ヶ月) ---")
for k, v in sorted(strategy_monthly_theo.items(), key=lambda x: -x[1]):
    print(f"  {k:30s}: {v:7.2f}%/月")
print(f"  {'合計 (全戦術並行 $80K相当)':30s}: {total_sum:7.2f}%/月")

print("\n--- Step 2: 下限推定 (BT合計 × 並行率 × BS割引) ---")
for sc in ["optimistic", "central", "conservative"]:
    par = EFFECTIVE_PARALLELISM[sc]
    disc = BS_DISCOUNTS[sc]
    print(f"  {sc:12s}: {total_sum:.2f}% × 並行{par:.2f} × 割引{disc:.2f} = {lower_bound[sc]:.2f}%/月")

print("\n--- Step 3: 上限推定 (ペーパー margin比 × 資本換算) ---")
print(f"  ペーパー4日 ROM平均: {avg_rom_day:.2f}%/日 → 月換算 {paper_monthly_margin_pct:.1f}% (margin比)")
for sc in ["optimistic", "central", "conservative"]:
    util = MARGIN_UTIL[sc]
    print(f"  {sc:12s}: {paper_monthly_margin_pct:.2f}% × margin util{util:.2f} = {upper_bound[sc]:.2f}%/月")

print("\n--- Step 4: 下限×上限の幾何平均 (税引前月利) ---")
for sc in ["optimistic", "central", "conservative"]:
    geom = final_monthly_pre_tax[sc]
    print(f"  {sc:12s}: √({lower_bound[sc]:.2f} × {upper_bound[sc]:.2f}) = {geom:.2f}%/月")

print("\n--- Step 5: 税引後月利 ---")
for sc in ["optimistic", "central", "conservative"]:
    print(f"  {sc:12s}: 税引前{final_monthly_pre_tax[sc]:.2f}% × 税率{TAX_FACTORS[sc]:.2f} = 税引後{final_monthly_post_tax[sc]:.2f}%")

# バージョン比較
versions = {
    "v3 (2026-04-16)": (20.0, 12.0,  7.0, "推測・ペーパー前"),
    "v4 (2026-04-20)": (15.0,  8.0,  4.0, "7戦術実測3.84%ベース下方修正"),
    "v5 (2026-04-20)": (19.0, 12.0,  6.5, "ORB 1DTE PASS推測反映"),
    "v6 (2026-04-21)": (
        final_monthly_pre_tax["optimistic"],
        final_monthly_pre_tax["central"],
        final_monthly_pre_tax["conservative"],
        "BT下限×ペーパー上限 幾何平均",
    ),
}
print("\n--- バージョン比較 (税引前月利%) ---")
print(f"{'Version':20s} | {'Opt':>5s} | {'Cen':>5s} | {'Con':>5s} | Note")
for v, d in versions.items():
    print(f"{v:20s} | {d[0]:4.1f}% | {d[1]:4.1f}% | {d[2]:4.1f}% | {d[3]}")

# 到達時期
target_retire = 670_000
target_300 = 3_000_000
initial = 1_200_000
monthly_contrib = 200_000

def months_to(monthly_rate_pct, target):
    cap = initial
    for m in range(1, 361):
        cap = cap * (1 + monthly_rate_pct / 100) + monthly_contrib
        income = cap * monthly_rate_pct / 100
        if income >= target:
            return m, cap, income
    return None, None, None

print("\n--- 月67万 (退職ライン・税引後) 到達時期 ---")
for sc, m_rate in final_monthly_post_tax.items():
    months, cap, inc = months_to(m_rate, target_retire)
    if months:
        y, mo = months // 12, months % 12
        print(f"  {sc:12s}: 税引後月利{m_rate:.2f}% → {y}年{mo:2d}ヶ月  元本{cap/10000:.0f}万・月収{inc/10000:.0f}万")
    else:
        print(f"  {sc:12s}: 税引後月利{m_rate:.2f}% → 30年以内到達せず")

print("\n--- 月300万 (目標・税引後) 到達時期 ---")
for sc, m_rate in final_monthly_post_tax.items():
    months, cap, inc = months_to(m_rate, target_300)
    if months:
        y, mo = months // 12, months % 12
        print(f"  {sc:12s}: 税引後月利{m_rate:.2f}% → {y}年{mo:2d}ヶ月  元本{cap/10000:.0f}万・月収{inc/10000:.0f}万")
    else:
        print(f"  {sc:12s}: 税引後月利{m_rate:.2f}% → 30年以内到達せず")

# 資金レベル別
FX_RATE = 150
CAPITAL_LEVELS = {
    "120万円 (初期)":      1_200_000 / FX_RATE,
    "300万円 (FX移行後)":  3_000_000 / FX_RATE,
    "600万円 (phase2)":    6_000_000 / FX_RATE,
    "1200万円 (phase3)":   12_000_000 / FX_RATE,
}
print("\n--- 資金レベル別 月収 (税引後・円) ---")
print(f"{'資金':22s} | {'楽観':>10s} | {'中央':>10s} | {'保守':>10s}")
for lvl_name, lvl_usd in CAPITAL_LEVELS.items():
    line = f"{lvl_name:22s} |"
    for sc in ["optimistic", "central", "conservative"]:
        jpy = lvl_usd * final_monthly_post_tax[sc] / 100 * FX_RATE
        line += f" {jpy:>8,.0f}円 |"
    print(line)

# JSON出力
output = {
    "generated_at": "2026-04-21",
    "version": "v6",
    "data_sources": [
        "data/backtest_blinded_20260418_fixed.md (7戦術 PASS, ThetaData 28ヶ月)",
        "data/backtest_orb_1dte_20260418.md (ORB 1DTE SPY 70trades PASS)",
        "data/thetadata/grid_results.csv (SPY CS 1680パラメータ)",
        "data/thetadata/1dte_vs_0dte_summary.csv",
        "data/eval/daily/2026041[7-9].json, 20260420.json (ペーパー4日 369trades $26,739)",
    ],
    "method": (
        "下限×上限の幾何平均。"
        "下限 = BT合計×並行率×BS割引, "
        "上限 = ペーパー margin比 × 資本換算係数"
    ),
    "strategy_monthly_theo_pct_10k": {k: round(v, 3) for k, v in strategy_monthly_theo.items()},
    "total_strategies_sum_monthly_pct": round(total_sum, 2),
    "effective_parallelism": EFFECTIVE_PARALLELISM,
    "bs_discounts": BS_DISCOUNTS,
    "margin_utilization": MARGIN_UTIL,
    "tax_factors": TAX_FACTORS,
    "lower_bound_pre_tax_pct": lower_bound,
    "upper_bound_pre_tax_pct": upper_bound,
    "final_monthly_pre_tax_pct": final_monthly_pre_tax,
    "final_monthly_post_tax_pct": final_monthly_post_tax,
    "paper_live_4days": {
        "total_pnl_usd": sum(d["pnl_usd"] for d in PAPER_LIVE),
        "total_trades": sum(d["trades"] for d in PAPER_LIVE),
        "avg_rom_pct_day": round(avg_rom_day, 3),
        "avg_rom_pct_month_20d": round(paper_monthly_margin_pct, 2),
        "caveat": "margin比 = 分母$380K名目・実際capital比はmargin_utilで換算",
    },
    "version_comparison": {v: {"opt": d[0], "cen": d[1], "con": d[2], "note": d[3]} for v, d in versions.items()},
    "retirement_line_months": {
        sc: months_to(final_monthly_post_tax[sc], target_retire)[0]
        for sc in final_monthly_post_tax
    },
    "target_300m_months": {
        sc: months_to(final_monthly_post_tax[sc], target_300)[0]
        for sc in final_monthly_post_tax
    },
    "capital_levels_jpy_post_tax_monthly": {
        lvl_name: {
            sc: round(lvl_usd * final_monthly_post_tax[sc] / 100 * FX_RATE, 0)
            for sc in final_monthly_post_tax
        }
        for lvl_name, lvl_usd in CAPITAL_LEVELS.items()
    },
}

output_path = Path("/Users/yuusakuichio/trading/data/atlas_monthly_verification_v6_20260421.json")
output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str))
print(f"\n結果JSON: {output_path}")
