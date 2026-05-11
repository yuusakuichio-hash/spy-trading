# 独立検証(redteam)必須規律

## 結論

完了宣言前に **redteam(攻撃的レビュー)** を別 session で実施。自分のコードは自分の盲点で見えない。

## 手順

1. builder/main session が実装完了したと判断
2. 別の Claude Code session で redteam モード起動(`Agent` tool で `subagent_type=redteam`)
3. redteam に以下を依頼:
   - 「この実装が失敗するパターンを 5 つ列挙」
   - 「ここで仕様が誤読されてる箇所を探せ」
   - 「pytest が pass してるが実際は壊れてる可能性を検証」
4. redteam の指摘を全件解消してから完了宣言

## アンチパターン

- 「テスト通ったから OK」 → テストが意図した挙動を表現してない可能性
- 「自分で見直したから大丈夫」 → 同じ盲点で見直すので意味なし
- 「時間ないから redteam スキップ」 → 後で 10 倍の時間を払う

## 出典

- プロイセン Kriegsspiel 1812(軍事 red team 文化起源)
- NASA Flight Rules: 「Confidence without independent verification is speculation」
