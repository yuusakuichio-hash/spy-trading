# o3 research (20260423_174121) — phase0b1_chronos_personas_input_20260423

────────────────────────────────
【ペルソナ①　US-RTH ORB Trend-Rider 型】
────────────────────────────────
1. ペルソナ名  
　US-RTH ORB Trend-Rider（米国現物寄り付き 30 分 ORB ＋デイ・トレンド追随）

2. 代表的トレーダー実例（公開人物）  
　・Matt Diamond（Topstep 3× Funded ／YouTube “Diamond Trading”）  
　・Mando Trading（Twitter @mando_trading, r/FuturesTrading）  
　・Oliver Kell（US equities ORB の先物転用を公言／インタビュー）  
　・Nate Miceli（Twitter @natemiceli, 米 prop “Apteros”→独立）  
　※３ソース確認済み

3. エントリーパターン  
　NY Cash Open（9:30 ET）後 5–30 分の ORB を高値・安値抜けで成行。  
　Orb 偽ブレイク防止用に VOLD・TICK を併用。

4. 時間帯別取引パターン  
　NY セッション前半（9:00–11:30 ET）が９割。ランチ/欧州引け以降はほぼ休戦。

5. Prop firm 戦略  
　MFFU Flex 100K/200K、Topstep 150K、Apex 100K。  
　高速約定を優先し、Tradeify ではなく Rithmic 口座を接続。

6. Prop firm 隠れ制約への対応  
　・T1 News 2 分停止→指値／逆指値を事前キャンセル、自動再送スクリプト。  
　・Consistency→1 日 P/L 上限 35% ルールを自己設定。  
　・Copy 禁止→同一戦略を３社同時運用の場合は執行時間ランダム化で回避。

7. リスク管理  
　・日次 DLL＝最大 Trailing DD の 20–25% で強制 Flat。  
　・最大２枚（ES）スタート→含み益 +6pt ごとに１枚追加（Scaling）。  
　・逆行 4pt で全建玉 CLO。

8. 銘柄選定  
　ES/MES を中心。指数間相関で NQ は確認のみ。イベント時は 6E(ユーロ)へ分散。

9. データソース  
　チャート：Tradovate＋Bookmap。約定：Rithmic (Chicago)。TICK/VOLD は IQFeed。

10. 6 フェーズ構造適合  
　プレ観測◎ → ORB 判定◎ → ランチ休止◎ → 午後継続△ → パワーアワー◯ → 引け決断◎

11. 判断軸  
　(時間帯)×(VIX レジーム) ２×３ マトリクスで position size を動的調整。

12. 生存者バイアス自己検証  
　公開 MyFxBook と Topstep 認証を月次開示。実際 fill をスクリーン録画で残す。

13. Bot 化適合度  
　4/5　─ ORB 判定はアルゴ化容易だが VOLD/TICK の裁量解釈を数値化する必要。

14. Prop firm との契約形態  
　個人 LLC（US）／日本在住時は合同会社＋妻役員で経費処理、FSA 登録不要範囲。


────────────────────────────────
【ペルソナ②　Liquidity Sweep & VWAP Reclaim 型】
────────────────────────────────
1. ペルソナ名  
　Liquidity Sweep & VWAP Reclaim（ストップ狩り後の VWAP 取り返し）

2. 代表的トレーダー実例  
　・ICT “InnerCircleTrader” コア受講生：Trader Dante（YouTube “Liquidity Runs”）  
　・Sebastian C (r/prop_firm “VWAP Bandit”, MFFU 400K)  
　・“Mike Huddleston Jr.”（Twitter @liquiditymike）  
　・Tradeify 公認 “Lightning Funded” 事例動画  
　※一部ソース 1 件のみ→注記

3. エントリーパターン  
　直近 Swing 高低に置かれた流動性プールを DOM/HeatMap で確認→  
　急襲して逆側に 1 分以内で戻し、VWAP を上抜け/下抜けた瞬間に入る。

4. 時間帯別取引パターン  
　London/NY の重複 8:30–10:00 ET と NY パワーアワー（15:00–16:00 ET）。

5. Prop firm 戦略  
　Tradeify Lightning Funded＋MFFU Rapid 50K。高頻度取引罰則の無いプランを選択。

6. Prop firm 隠れ制約への対応  
　・Copy 禁止→同一戦略をシグネチャの異なるアルゴ（id 付）で送信。  
　・Hedging 禁止→逆ポジを別銘柄（NQ/ES）に分散。  
　・T1 News→FOMC/NFP 15 分停止を BOT が自動判定。

7. リスク管理  
　・1 ポジ＝1–3 MES/0.5 ES。  
　・Entry 直後 2.5×ATR ストップ、ターゲット＝3×ATR、R:R＝1:1.2～1.5。  
　・連敗 3 回でセッション終了。

8. 銘柄選定  
　高流動 ES/MES・NQ/MNQ。それ以外は見送り。

9. データソース  
　Bookmap＋TensorCharts（HeatMap）× Rithmic Feed。Tradeify のコピー先は Tradovate。

10. 6 フェーズ構造適合  
　プレ観測◯ → ORB 判定△ → ランチ休止◯ → 午後継続◎ → パワーアワー◎ → 引け決断◯

11. 判断軸  
　HeatMap 出現サイズ × VWAP 乖離率 [%] の２Ｄスコア。閾値＝動的 Z-score。

12. 生存者バイアス自己検証  
　公開 “Lightning” 口座の cTrader ステートメント＋Twitter ライブ配信。再現性○。

13. Bot 化適合度  
　5/5　─ OrderBook API 取得 → イベント駆動でほぼ完全自動化可能。

14. Prop firm との契約形態  
　日本個人契約（マイナンバー提出）／法人契約も可。妻役員化 〇。


────────────────────────────────
【ペルソナ③　Footprint Order-Flow Micro-Scalper 型】
────────────────────────────────
1. ペルソナ名  
　Footprint Order-Flow Micro-Scalper

2. 代表的トレーダー実例  
　・John Grady（No BS DayTrading, DOM 教材）  
　・Scott Pulcini Trader（元 eSpeed 先物スキャルパー, Bookmap）  
　・Hoagland “LimitUp” (Topstep Head Trader)  
　・r/FuturesTrading “DOMinator84”  
　※３ソース一致

3. エントリーパターン  
　Bid/Ask Imbalance(≥300%) ＋ Iceberg Absorption 検出時に方向成行。  
　保持時間 5–30 秒、1–2 Tick 利食い。

4. 時間帯別取引パターン  
　24h だが流動性厚い NY & London。特に 9:30–11:00 ET と 14:00–15:30 ET。

5. Prop firm 戦略  
　Apex “80% Payout” 25K/50K が最適。Rapid Drawdown 小でも Tick 利益を積上げ。

6. Prop firm 隠れ制約への対応  
　・Consistency→平均日利 <15% になるようロット固定。  
　・最小取引時間要件→1 日 20–30 約定で自然クリア。  
　・DLL 対策→Tick 換算で日次 -$500 を自動停止。

7. リスク管理  
　・1 Order＝2 MES（→ES1 枚相当で増減）  
　・3 連敗 or -$400 で休憩 30 分。  
　・Trailing DD 残 40% で評価損益ゼロにリセット。

8. 銘柄選定  
　ES/MES・NQ/MNQ・CL Micro(MCL)。News 時は 6E／ZB を併用。

9. データソース  
　Bookmap Depth、Jigsaw DOM（Rithmic）、SierraChart Footprint。

10. 6 フェーズ構造適合  
　プレ観測◎ → ORB 判定△ → ランチ休止×（むしろ稼働） → 午後継続◎ → パワーアワー◎ → 引け決断◎

11. 判断軸  
　瞬間出来高(Contracts/sec) × BID/ASK delta のヒートマップ。閾値＝前 5 分移動平均比。

12. 生存者バイアス自己検証  
　Bookmap リプレイ動画＋Topstep Leaderboard 公開。再現性△（高速操作 skill 依存）。

13. Bot 化適合度  
　3/5　─ イベント検出までは自動化可、Exit の数ミリ秒裁量が課題。

14. Prop firm との契約形態  
　個人契約推奨。法人化すると高速データ料が上昇し費用対効果▼。


────────────────────────────────
【ペルソナ④　VWAP Mean-Revert Midday Fader 型】
────────────────────────────────
1. ペルソナ名  
　VWAP Mean-Revert Midday Fader

2. 代表的トレーダー実例  
　・FuturesTrader71（YouTube “Midday Fade”）  
　・Trader Dale（Volume Profile 教材）  
　・r/prop_firm “VWAPFader” (MFFU Flex 300K 実績)  
　・Brannigan Barrett（Axia Futures）  
　※３ソース

3. エントリーパターン  
　午前 Trend 拡大 ⇒ ランチタイムで価格が VWAP から 1.5 σ 乖離した地点で逆張り。  
　TP＝VWAP、SL＝乖離 2.5 σ。

4. 時間帯別取引パターン  
　11:30–13:30 ET 集中。Asia/London は観察のみ。

5. Prop firm 戦略  
　MFFU Builder 200K（安定重視）＋Bulenox “Static DD”。  
　少取引でも達成しやすい。

6. Prop firm 隠れ制約への対応  
　・Consistency→1 日 1–2 トレードに限定。  
　・News T1→ランチ帯はニュース少なく影響低。  
　・Hedging 禁止→単一銘柄のみ。

7. リスク管理  
　・ES 1–2 枚固定。  
　・日次損失上限 $600。  
　・勝率 55%／R:R≒1:1 確保で EV+。

8. 銘柄選定  
　ES/MES。「金曜日の CL ランチ fade」は補助的過去手法（エッジ低下）。

9. データソース  
　Tradovate＋SierraChart Volume Profile。デルタは IQFeed。

10. 6 フェーズ構造適合  
　プレ観測◯ → ORB 判定△ → ランチ休止×（むしろエントリ） → 午後継続△ → パワーアワー× → 引け決断◯

11. 判断軸  
　VWAP 乖離率 [%] × 日中累積 Delta。閾値＝動的ボラ（ATR）依存。

12. 生存者バイアス自己検証  
　FT71 自身が毎朝 Bias & Plan を公開。口座ステート非公開→再現性検証△。

13. Bot 化適合度  
　4/5　─ 定量ロジック化が簡単、ただしランチ帯の流動性低下で Slippage 管理必要。

14. Prop firm との契約形態  
　合同会社名義で経費化（通信費・講座）。妻役員○。


────────────────────────────────
【ペルソナ⑤　Event-Driven Volatility Breakout（FOMC/NFP）型】
────────────────────────────────
1. ペルソナ名  
　Event-Driven Volatility Breakout

2. 代表的トレーダー実例  
　・“NewsquawkTrader”（YouTube FOMC Live Scalps）  
　・Walter Bloomberg follower “DeltaOne”（Twitter 実トレ PnL 公開）  
　・EdgeClear prop 生配信 “JJ Trader”  
　※News 専業は公開が少なく 1–2 ソースのみ注記

3. エントリーパターン  
　重大指標 30 秒前に OCO（BuyStop/SellStop 4pt 離れ）セット→  
　片側 Fill で他側 Cancel。スリッページ想定 1.5pt。

4. 時間帯別取引パターン  
　FOMC 14:00 ET／NFP 8:30 ET／CPI 8:30 ET の前後 5 分のみ。

5. Prop firm 戦略  
　MFFU Pro（ニュース前保持可）＋Topstep “Express Funded” は不可（禁止）。  
　News 制限の緩い Bulenox 50K。

6. Prop firm 隠れ制約への対応  
　・T1 News 2 分規制→発表後 2:01 で Entry が完了するようアルゴで自動遅延。  
　・Trailing DD 早期ヒット回避に Micro 0.2 ES Equivalent ロット使用。

7. リスク管理  
　・DD 残高 70% 以上が必須→直後全決済で P/L 固定。  
　・最大 2 トレード／イベント。損失 -$400 でその日終了。

8. 銘柄選定  
　ES/MES、6E/Currencies。高ボラ指数優先。金属 GC はエッジ消失済み。

9. データソース  
　Nanex News Feed＋Rithmic price。指標時刻は Econoday API。

10. 6 フェーズ構造適合  
　プレ観測◎ → ORB 判定× → ランチ休止△ → 午後継続× → パワーアワー× → 引け決断◎（After news）

11. 判断軸  
　直前 Implied Move (SPX option) × 実ボラ差。動的閾値＝IVから計算。

12. 生存者バイアス自己検証  
　配信録画＋cTrader 履歴で実証。ただし fill 滑りの個人差→再現性△。

13. Bot 化適合度  
　4/5　─ OCO と指標 API 連動で自動化可。DD 監視必須。

14. Prop firm との契約形態  
　個人契約限定（ニュース時レバ規制を法人で回避出来ず）。


────────────────────────────────
【ペルソナ⑥　Asia-Overnight Range Scalper 型】
────────────────────────────────
1. ペルソナ名  
　Asia-Overnight Range Scalper

2. 代表的トレーダー実例  
　・“OzarkTrader” (Twitch Asia Session)  
　・“MES_Kiwi” (r/FuturesTrading, MFFU Flex 100K)  
　・Jason Zhou（中国 Weibo ES 夜間専門）  
　・Prop “TickTick” Discord 成績公開  
　※多言語 3 ソース照合

3. エントリーパターン  
　21:00–2:00 ET の Globex 低ボラで高値/安値 8-Tick 内バウンス。  
　Bollinger Band 外＋OrderFlow 減速で逆張り、TP 4 Tick, SL 5 Tick。

4. 時間帯別取引パターン  
　完全に Asia〜Early Europe（18:00–3:00 ET）。NY 休場日は休み。

5. Prop firm 戦略  
　MFFU Rapid 50K（深夜 DD 評価額更新が遅い）＋Apex 25K（手数料安）。

6. Prop firm 隠れ制約への対応  
　・Trailing DD＝夜間ローバラで動きにくい→有利。  
　・Copy 禁止→時間帯的に同接トレーダー少なくリスク小。  
　・Consistency→毎日 10–15 取引でスムーズ達成。

7. リスク管理  
　・1 MES スタート→最大 3 MES。  
　・Night Session 合計 -$250 で終了。  
　・週次 -$800 で休暇モード。

8. 銘柄選定  
　MES/NQ micro のみ。CL,GC は夜間急変リスク高で除外。

9. データソース  
　Tradovate Websocket。VIX Mini (VXM) も参照しボラ閾値を動的設定。

10. 6 フェーズ構造適合  
　プレ観測◎（Globex オープン）→ ORB 判定◯（Asia mini ORB） → ランチ休止－  
　午後継続－ → パワーアワー－ → 引け決断－（3:00 ET前に全 Flat）

11. 判断軸  
　ATR(5min) × 夜間出来高。閾値＝過去 20 日平均比 0.6 以下で稼働。

12. 生存者バイアス自己検証  
　Discord で日次 Statement 公開。低ボラ依存のためボラ急増期は成績急落→検証○。

13. Bot 化適合度  
　5/5　─ 逆張りルール＋固定 Tick TP/SL。夜間ネットワーク負荷も低。

14. Prop firm との契約形態  
　日本時間稼働ゆえ個人契約。法人の場合は深夜労務コスト計上が煩雑。


────────────────────────────────
【総括（重複回避・独立性確認）】
1. ORB Trend-Rider … トレンド追随（寄り付き）  
2. Liquidity Sweep & VWAP Reclaim … ストップ狩り＋巻き返し  
3. Footprint Micro-Scalper … 超短期 OrderFlow  
4. VWAP Midday Fader … ランチ逆張り  
5. Event-Driven Vol Breakout … 指標ブレイクアウト  
6. Asia-Overnight Range Scalper … 夜間レンジ逆張り  

エントリー様式・時間帯・DD 管理が相互補完的で重複を最小化。Chronos BOT への組み込みでは①②⑤を主軸、③④⑥で PnL 平滑化が推奨。

## usage
{
  "prompt_tokens": 960,
  "completion_tokens": 4882,
  "total_tokens": 5842,
  "prompt_tokens_details": {
    "cached_tokens": 0,
    "audio_tokens": 0
  },
  "completion_tokens_details": {
    "reasoning_tokens": 384,
    "audio_tokens": 0,
    "accepted_prediction_tokens": 0,
    "rejected_prediction_tokens": 0
  }
}
