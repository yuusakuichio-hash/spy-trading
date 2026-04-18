# ThetaData 1DTE Extension Research (2026-04-18)

## Purpose
既存 ThetaData Standard契約 (監査済・支払済) を活用し、1DTE データ拡張DLを行うための調査。
**新規サービスの登録推奨ではない。** 既存サブスクリプションの使用方法拡張のみ。

## 既存契約状況
- ThetaData Standard プラン (memory: project_thetadata 参照)
- ThetaTerminal: localhost:25503 で稼働中
- 既存 DL 済: thetadata/ SPY 524日 / QQQ 546日 / IWM 544日 等 (全て 0DTE)

## 今回の拡張内容
- 0DTE のみ DL 済 → 1DTE (翌日満期) を追加 DL
- start_date = trade_date, expiration = next business day
- エンドポイント: `/v3/option/history/greeks/first_order` と `/eod`
- 追加課金なし (Standardプランで実証済)

## 実証ログ
```
curl http://localhost:25503/v3/option/history/greeks/first_order?\
symbol=SPY&expiration=20260416&start_date=20260415&end_date=20260415&interval=5m

→ 200 OK, 1DTE PUT delta=-0.98 等 intraday 5分足データ 79本返却
```

## データ量試算
- SPY/QQQ/IWM: ~575日 × 3シンボル × 約50 KB/file × 3 file = ~260 MB
- 個別株: ~140日 × 7シンボル = ~190 MB
- 合計約 450 MB (ディスク負担なし)

## 出力先
- `data/thetadata_1dte/{YYYYMMDD}/greeks_first_order_{SYMBOL}.parquet`
- `data/thetadata_1dte/{YYYYMMDD}/greeks_eod_{SYMBOL}.parquet`
- `data/thetadata_1dte/{YYYYMMDD}/greeks_expiration_eod_{SYMBOL}.parquet`

## 関連タスク
- orb_breakout 1DTE 化プロトタイプ (2026-04-18)
- Blinded Backtest で唯一 FAIL した戦術を 1DTE で再設計
