#!/usr/bin/env python3
"""
Atlas月利v6実測検証スクリプト (2026-04-21) — v2 修正版

修正点 (v1 → v2):
  - v1: 戦術別月利を加重平均 → 誤り (BlindedBTは各戦術$10K独立・ポートフォリオは同じ資本で排他選択)
  - v2: 資本回転率ベースで再計算
        ・各戦術の「1トレードあたり収益率」と「月間トレード数」から
        ・資本を1つの戦術に集中させた場合の理論月利 = avg_return_per_trade × trades_per_month
        ・Atlas は環境適応で最適戦術を選択 → 「選択された戦術の実測月利」が実効月利
        ・戦術選択精度を考慮して期待月利を算出

  v2では 3つの手法で月利を推定し、ペーパー実績に最も近い値を採用:
  (A) Blinded BT total_pnl / 28ヶ月 / $10K → 各戦術を単独で1本回した月利
  (B) Grid_results.csv monthly_return_pct → BS理論月利 (SPY CS最良70%/月)
  (C) ペーパー実績 margin比 35%/月 (BS含む実運用)

  BS割引率 (0.30-0.45) を公開BT値に適用 → 実効レンジ確定
"""
import json
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. 入力データ (既存BT結果・ペーパー実績の生値)
# ---------------------------------------------------------------------------

# Blinded BT 8戦術結果 (2026-04-18, ThetaData 28ヶ月, $10K元本, Atlas各戦術単独実行想定)
BLINDED_BT = {
    "butterfly":             {"n": 38,  "wr": 0.579, "sharpe": 7.61,  "dd": 0.012, "pnl": 1016.50,  "pass": True},
    "ic_sell":               {"n": 340, "wr": 0.815, "sharpe": 3.01,  "dd": 0.109, "pnl": 7075.03,  "pass": True},
    "strangle_sell":         {"n": 208, "wr": 0.779, "sharpe": 8.23,  "dd": 0.036, "pnl": 7240.25,  "pass": True},
    "symbol_selector_plus":  {"n": 503, "wr": 0.841, "sharpe": 11.84, "dd": 0.015, "pnl": 17456.51, "pass": True},
    "earnings_iv_crush":     {"n": 73,  "wr": 0.904, "sharpe": 7.26,  "dd": 0.134, "pnl": 15095.76, "pass": True},
    "portfolio_aggregator":  {"n": 510, "wr": 0.863, "sharpe": 4.46,  "dd": 0.059, "pnl": 10741.01, "pass": True},
    "ivr_credit_spread":     {"n": 281, "wr": 0.964, "sharpe": 1.25,  "dd": 0.044, "pnl": 1092.54,  "pass": True},
}

# ORB 1DTE (再設計・SPY PASS, 135日=6.4ヶ月)
ORB_1DTE = {
    "spy":  {"n": 70, "wr": 0.600, "sharpe": 2.49,  "dd": 0.052, "pnl": 1735.00, "months": 6.4, "pass": True},
}

# Grid最良CS売り (ThetaData SPY 0DTE 10:30 delta0.15 width1 none/0.75)
# このパラメータ単独の月利 61.15% = BS理論・10枚/$10K・28ヶ月平均
CS_GRID_BEST_MONTHLY_THEO = 61.15  # %

# ペーパー実績 (2026-04-17〜04-20, 4日, 統合Atlas稼働・BS含む実運用)
PAPER_LIVE = [
    {"date": "2026-04-17", "trades": 132, "pnl_usd": 8309.24, "margin": 380000},
    {"date": "2026-04-18", "trades":  69, "pnl_usd": 5410.00, "margin": 380000},
    {"date": "2026-04-19", "trades":  52, "pnl_usd": 4030.00, "margin": 380000},
    {"date": "2026-04-20", "trades": 116, "pnl_usd": 8990.00, "margin": 380000},
]

# ---------------------------------------------------------------------------
# 2. 手法A: Blinded BT 戦術別の単独月利 (BS理論)
# ---------------------------------------------------------------------------
# 各戦術を $10K 単独で28ヶ月走らせた場合の平均月利
# Atlas は環境適応で最良戦術を選択 → 選択精度で加重
print("=" * 80)
print("手法A: Blinded BT 戦術別単独月利 (BS理論・$10K単独運用・28ヶ月)")
print("=" * 80)
n_months_theta = 28
strategy_monthly_theo = {}
for name, d in BLINDED_BT.items():
    if d["pass"]:
        strategy_monthly_theo[name] = d["pnl"] / n_months_theta / 10000 * 100
strategy_monthly_theo["orb_1dte_spy"] = ORB_1DTE["spy"]["pnl"] / ORB_1DTE["spy"]["months"] / 10000 * 100

for k, v in sorted(strategy_monthly_theo.items(), key=lambda x: -x[1]):
    print(f"  {k:30s}: {v:7.2f}%/月")

# Atlas strategy_selector の理想的選択: 毎日最良戦術を選ぶ = 上位戦術の平均
# 現実の選択精度を考慮 (選択ミス・トレードなし日を含む)
best_3_avg = sum(sorted(strategy_monthly_theo.values(), reverse=True)[:3]) / 3
median_monthly_theo = sorted(strategy_monthly_theo.values())[len(strategy_monthly_theo)//2]
print(f"\n  TOP3平均月利 (理想選択):      {best_3_avg:.2f}%/月")
print(f"  中央値月利 (平均的選択):      {median_monthly_theo:.2f}%/月")

# ---------------------------------------------------------------------------
# 3. 手法B: Grid最良CS単独 (BS理論)
# ---------------------------------------------------------------------------
print("\n" + "=" * 80)
print("手法B: Grid最良CS (BS理論)")
print("=" * 80)
print(f"  SPY CS 0DTE 10:30 delta0.15 width1 TP0.75 → {CS_GRID_BEST_MONTHLY_THEO:.2f}%/月 (BS)")
print("  注: これはBS理論値・実運用では割引率0.30-0.45掛けで20-30%相当")

# ---------------------------------------------------------------------------
# 4. 手法C: ペーパー実績 (BS含む実運用)
# ---------------------------------------------------------------------------
print("\n" + "=" * 80)
print("手法C: ペーパー実績 (2026-04-17〜04-20, 4日)")
print("=" * 80)
total_pnl = sum(d["pnl_usd"] for d in PAPER_LIVE)
total_trades = sum(d["trades"] for d in PAPER_LIVE)
days = len(PAPER_LIVE)
avg_daily = total_pnl / days
est_monthly_usd_20d = avg_daily * 20
margin = PAPER_LIVE[0]["margin"]
paper_monthly_pct_margin = est_monthly_usd_20d / margin * 100

# ペーパー margin $380K は名目margin。実質的な運用可能元本と合致しない可能性
# 実際の現金 + オプション証拠金使用量で解釈
# ここでは保守的にmargin比を「BS抜き実月利」として採用
print(f"  合計: {days}日 {total_trades}trades $ {total_pnl:,.0f}")
print(f"  日次平均: $ {avg_daily:,.1f}")
print(f"  月換算 (20営業日): $ {est_monthly_usd_20d:,.0f}")
print(f"  margin比: {paper_monthly_pct_margin:.1f}%/月")

# 注意: 4日サンプルは小さい (外れ値1日で大きくブレる)
# 4/17 $8309 | 4/18 $5410 | 4/19 $4030 | 4/20 $8990
# 分散: std(pnl) = 2400 → 月利の95%CI は [15%, 55%]
import statistics
pnl_list = [d["pnl_usd"] for d in PAPER_LIVE]
pnl_mean = statistics.mean(pnl_list)
pnl_std = statistics.stdev(pnl_list)
std_error = pnl_std / (len(pnl_list) ** 0.5)
ci95_low = (pnl_mean - 1.96*std_error) * 20 / margin * 100
ci95_high = (pnl_mean + 1.96*std_error) * 20 / margin * 100
print(f"  95%信頼区間 (4サンプルのみ): [{ci95_low:.1f}%, {ci95_high:.1f}%] /月")
print(f"  注: サンプル数不十分(n=4)・統計的有意性低い")

# ---------------------------------------------------------------------------
# 5. 統合: 楽観/中央/保守
# ---------------------------------------------------------------------------
print("\n" + "=" * 80)
print("v6月利レンジ 最終確定")
print("=" * 80)

# 採用根拠:
# - 楽観: TOP3平均 (symbol_selector+earnings+portfolio_agg) × 割引0.45
# - 中央: 中央値×割引0.35 または 各戦術加重(環境均等)
# - 保守: 最下位戦術群の平均×割引0.25

# ここで割引率は BS vs tastytrade実測 0.30-0.45 (data/backtest_discount_factor.md)
# より厳密には「Atlas運用」の割引率 = ペーパー実測 / BS理論

# ペーパー 35.2%/月 (margin比・BS含む) vs Blinded TOP3平均 5.15%/月 (BS理論)
# この差は margin vs 資本比の取り方の違い (ペーパー$380K margin で最大バトラ・単位トレード5000$相当)
# → Blinded BT の $10K 元本 と ペーパー $380K margin を直接比較は不正確
# → 代わりに「全戦術合計がAtlasで同時稼働」 + 「資本回転率を実運用レベル」で再評価

# 保守的整合: ペーパー 35% を上限キャップとし、
#   - 4日は外れ値バイアス含む (後続でさらに下方修正される可能性)
#   - BS割引適用後のBT値と整合する範囲で採用
#
# 最終採用:
#   楽観: TOP3 5.15% × 割引0.50 = 2.6%相当... だが BlindedBT は全戦術合計なので
#   実際はポートフォリオ単位で 複数戦術合成 (同時並行=単純和)

# 全7戦術 + ORB 1DTE 合計月利 (各$10K独立運用) = 同時並行運用想定
total_sum_monthly = sum(strategy_monthly_theo.values())
print(f"\n全戦術合計 (各$10K独立・BS理論): {total_sum_monthly:.2f}%/月")
print("= 各戦術が独立口座で同時並行運用された場合の月利合計")

# Atlas は環境適応で排他選択するので、全8戦術常時稼働 != 運用実態
# 実運用では 1日に 1-2戦術選択 → 月間で約2-3戦術が主稼働
# よって総和の 30-50% が実効月利となる

realistic_combined = {
    "optimistic": total_sum_monthly * 0.50,  # 同時並行率50%
    "central":    total_sum_monthly * 0.35,
    "conservative": total_sum_monthly * 0.25,
}

# BS割引率 (公開BTはBS理論 → 実運用は0.30-0.45で圧縮)
DISCOUNTS = {
    "optimistic": 0.45,
    "central":    0.35,
    "conservative": 0.25,
}

# 税率 (総合課税雑所得・project_atlas_tax_correction_20260420)
TAX_FACTORS = {
    "optimistic": 0.78,  # 実効税率22% (Bot単独・低年収)
    "central":    0.70,  # 実効税率30%
    "conservative": 0.60,  # 実効税率40% (給与+Bot合算・高額)
}

results = {}
for sc in ["optimistic", "central", "conservative"]:
    bs_theo = realistic_combined[sc]
    disc = DISCOUNTS[sc]
    tax = TAX_FACTORS[sc]
    pre_tax = bs_theo * disc
    post_tax = pre_tax * tax
    results[sc] = {
        "bs_theo_pct": round(bs_theo, 2),
        "discount": disc,
        "pre_tax_pct": round(pre_tax, 2),
        "tax_factor": tax,
        "post_tax_pct": round(post_tax, 2),
    }

print("\nScenario     | BS理論  × 割引 = 税引前 × 税率 = 税引後")
for sc, d in results.items():
    print(f"  {sc:11s}: {d['bs_theo_pct']:5.1f}% × {d['discount']:.2f} = {d['pre_tax_pct']:4.1f}% × {d['tax_factor']:.2f} = {d['post_tax_pct']:4.1f}%")

# ---------------------------------------------------------------------------
# 6. ペーパー実績とのsanity check
# ---------------------------------------------------------------------------
print(f"\n[Sanity check] ペーパー実測 {paper_monthly_pct_margin:.1f}%/月 (margin比)")
print(f"  vs v6 central 税引前 {results['central']['pre_tax_pct']:.1f}% → 差 {paper_monthly_pct_margin - results['central']['pre_tax_pct']:+.1f}%")
print(f"  ペーパーは margin比(名目$380K)・BTは資本比($10K) — 直接比較不可・参考値のみ")

# ---------------------------------------------------------------------------
# 7. 資金レベル別月額
# ---------------------------------------------------------------------------
FX_RATE = 150
CAPITAL_LEVELS = {
    "120万円 (phase1 初期)":    120_0000 / FX_RATE,   # $8K相当
    "300万円 (FX資金移行後)":   300_0000 / FX_RATE,   # $20K
    "600万円 (phase2)":          600_0000 / FX_RATE,   # $40K
}

print("\n=== 資金レベル別 月収 (税引後・円) ===")
print(f"{'資金':20s} | {'楽観':>12s} | {'中央':>12s} | {'保守':>12s}")
for lvl_name, lvl_usd in CAPITAL_LEVELS.items():
    line = f"{lvl_name:20s} |"
    for sc in ["optimistic", "central", "conservative"]:
        m_pct = results[sc]["post_tax_pct"]
        jpy = lvl_usd * m_pct / 100 * FX_RATE
        line += f" {jpy:>10,.0f}円 |"
    print(line)

# ---------------------------------------------------------------------------
# 8. v3/v4/v5 との比較
# ---------------------------------------------------------------------------
print("\n=== バージョン比較 (税引前月利%) ===")
versions = {
    "v3 (2026-04-16)": {"opt": 20.0, "cen": 12.0, "con":  7.0, "note": "推測・ペーパー前"},
    "v4 (2026-04-20)": {"opt": 15.0, "cen":  8.0, "con":  4.0, "note": "7戦術実測3.84%ベース"},
    "v5 (2026-04-20)": {"opt": 19.0, "cen": 12.0, "con":  6.5, "note": "ORB 1DTE PASS推測反映"},
    "v6 (2026-04-21)": {
        "opt": results["optimistic"]["pre_tax_pct"],
        "cen": results["central"]["pre_tax_pct"],
        "con": results["conservative"]["pre_tax_pct"],
        "note": "統合8戦術+同時並行係数+割引+ペーパー照合",
    },
}
print(f"{'Version':20s} | {'Opt':>6s} | {'Cen':>6s} | {'Con':>6s} | Note")
for v, d in versions.items():
    print(f"{v:20s} | {d['opt']:5.1f}% | {d['cen']:5.1f}% | {d['con']:5.1f}% | {d['note']}")

# ---------------------------------------------------------------------------
# 9. 月67万円退職ライン到達
# ---------------------------------------------------------------------------
print("\n=== 月67万円 (退職ライン・税引後) 到達時期 ===")
print("前提: 初期120万円・月20万追加入金・税引後月利で複利")
target_retire = 670_000
initial = 1_200_000
monthly_contribution = 200_000

def months_to_target(monthly_rate_pct, target):
    cap = initial
    for m in range(1, 361):
        cap = cap * (1 + monthly_rate_pct / 100) + monthly_contribution
        monthly_income = cap * monthly_rate_pct / 100
        if monthly_income >= target:
            return m, cap, monthly_income
    return None, None, None

for sc, d in results.items():
    m_rate = d["post_tax_pct"]
    months, cap_at, income_at = months_to_target(m_rate, target_retire)
    if months:
        years = months // 12
        mths = months % 12
        print(f"  {sc:11s}: 税引後月利{m_rate:5.2f}% → {years}年{mths:2d}ヶ月  (元本{cap_at/10000:.0f}万・月収{income_at/10000:.0f}万)")
    else:
        print(f"  {sc:11s}: 税引後月利{m_rate:5.2f}% → 30年以内に到達せず")

# ---------------------------------------------------------------------------
# 10. 月300万円到達
# ---------------------------------------------------------------------------
print("\n=== 月300万円 (目標・税引後) 到達時期 ===")
target_300 = 3_000_000
for sc, d in results.items():
    m_rate = d["post_tax_pct"]
    months, cap_at, income_at = months_to_target(m_rate, target_300)
    if months:
        years = months // 12
        mths = months % 12
        print(f"  {sc:11s}: 税引後月利{m_rate:5.2f}% → {years}年{mths:2d}ヶ月  (元本{cap_at/10000:.0f}万・月収{income_at/10000:.0f}万)")
    else:
        print(f"  {sc:11s}: 税引後月利{m_rate:5.2f}% → 30年以内に到達せず")

# ---------------------------------------------------------------------------
# 11. JSON出力
# ---------------------------------------------------------------------------
output = {
    "generated_at": "2026-04-21",
    "version": "v6",
    "data_sources": [
        "data/backtest_blinded_20260418_fixed.md (8戦術 ThetaData 28ヶ月)",
        "data/backtest_orb_1dte_20260418.md (ORB 1DTE SPY 70trades PASS)",
        "data/thetadata/grid_results.csv (SPY CS 1680パラメータグリッド)",
        "data/thetadata/1dte_vs_0dte_summary.csv (全戦術×DTE)",
        "data/eval/daily/ (ペーパー4日実績 369trades $26,739)",
    ],
    "method": "統合ポートフォリオ = Σ(戦術単独月利) × 同時並行率 × BS割引 × 税率",
    "strategy_monthly_theo_pct_10k": strategy_monthly_theo,
    "total_strategies_sum_monthly_pct": round(total_sum_monthly, 2),
    "realistic_combined_pct": realistic_combined,
    "discounts": DISCOUNTS,
    "tax_factors": TAX_FACTORS,
    "scenarios": results,
    "paper_live_4days": {
        "total_pnl_usd": total_pnl,
        "total_trades": total_trades,
        "days": days,
        "avg_daily_usd": round(avg_daily, 2),
        "est_monthly_usd_20d": round(est_monthly_usd_20d, 2),
        "est_monthly_pct_margin": round(paper_monthly_pct_margin, 2),
        "ci95_margin_pct_low": round(ci95_low, 2),
        "ci95_margin_pct_high": round(ci95_high, 2),
        "caveat": "n=4の信頼区間広・margin比は資本比と異なる",
    },
    "version_comparison": versions,
    "capital_levels_jpy_post_tax": {
        lvl_name: {
            sc: round(lvl_usd * results[sc]["post_tax_pct"] / 100 * FX_RATE, 0)
            for sc in ["optimistic", "central", "conservative"]
        }
        for lvl_name, lvl_usd in CAPITAL_LEVELS.items()
    },
}

output_path = Path("/Users/yuusakuichio/trading/data/atlas_monthly_verification_v6_20260421.json")
output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str))
print(f"\n結果JSON: {output_path}")
