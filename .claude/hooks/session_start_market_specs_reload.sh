#!/usr/bin/env bash
# .claude/hooks/session_start_market_specs_reload.sh — セッション開始時 市場仕様強制表示
#
# SessionStart フック: セッション開始時に Atlas/Chronos の市場セッションを
# 強制表示することで、混同を物理的に防ぐ。

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  MARKET SPECS REMINDER (common/market_specs.yaml)"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Atlas  = SPX/SPY オプション"
echo "           JST(EDT): 22:20〜05:10  ※日跨ぎ・平日のみ"
echo "           市場: CBOE / NYSE 営業日"
echo ""
echo "  Chronos = CME 先物 (ES/MES/NQ/MNQ)"
echo "            JST(EDT): 月 07:00 〜 土 06:00  ※ほぼ24時間"
echo "            デイリー休止: 毎日 06:00-07:00 JST (EDT)"
echo ""
echo "  !! Atlas の時間帯を Chronos にコピーしてはいけない !!"
echo "  !! 時間帯変更前は common/market_specs.yaml を確認すること !!"
echo ""
echo "  DST: 夏時間(EDT) 3/8〜11/1 / 冬時間(EST) 11/1〜3/8"
echo "       冬時間は全セッションが +1h ずれる"
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo ""
