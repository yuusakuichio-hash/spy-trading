"""common/hmm_regime.py — HMM市場レジーム分類器

Sentinel Algo研究（2019-2024 OOS）に基づく3状態ガウシアンHMM。
STABLE状態のみエントリーでMax DD削減62%、P&L+35%、p<0.002。

状態定義:
  STABLE       — 低ボラ・トレンド安定。エントリー適格
  FRAGILE_TREND— 方向性はあるが脆い。サイズ0.5倍
  FRAGILE_DIV  — 発散・混乱状態。skip推奨

7次元特徴量:
  1. VIX日次変化率
  2. SPY日次リターン
  3. realized_vol (20日)
  4. term_structure (VIX9D/VIX比)
  5. GEX proxy (VIX反転×market_cap proxy)
  6. put/call ratio proxy (VIX水準ベース)
  7. IV skew proxy (VIX - VIX9D 乖離)

使い方:
    from common.hmm_regime import HMMRegimeClassifier, RegimeState, get_global_classifier

    clf = get_global_classifier()
    state = clf.predict_current()  # RegimeState.STABLE など

    # strategy_selector 統合用
    multiplier = clf.get_size_multiplier()  # STABLE→1.0, FRAGILE_TREND→0.5, FRAGILE_DIV→0.0
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import pickle
from enum import Enum
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

# ── 定数 ──────────────────────────────────────────────────────────────────────
N_STATES = 3
LOOKBACK_DAYS = 756          # 学習期間: 約3年の取引日数
MIN_TRAIN_ROWS = 60          # 最低学習行数
MODEL_CACHE_PATH = Path(__file__).parent.parent / "data" / "hmm_model.pkl"
FEATURE_CACHE_PATH = Path(__file__).parent.parent / "data" / "hmm_features_cache.json"
RETRAIN_INTERVAL_DAYS = 7    # 週次再学習

# サイズ乗数
SIZE_MULTIPLIER = {
    "STABLE": 1.0,
    "FRAGILE_TREND": 0.5,
    "FRAGILE_DIV": 0.0,
}


class RegimeState(str, Enum):
    STABLE = "STABLE"
    FRAGILE_TREND = "FRAGILE_TREND"
    FRAGILE_DIV = "FRAGILE_DIV"
    UNKNOWN = "UNKNOWN"          # データ不足 / モデル未学習時


# ── 特徴量取得 ─────────────────────────────────────────────────────────────────

def _fetch_raw_data(lookback_days: int = LOOKBACK_DAYS) -> Optional[np.ndarray]:
    """yfinanceでSPY/VIX/VIX9D履歴を取得し7次元特徴量行列を返す。

    Returns:
        shape (T, 7) の float64 配列。取得失敗時はNone。
    """
    try:
        import yfinance as yf  # type: ignore
    except ImportError:
        log.error("[HMM] yfinance未インストール。pip install yfinance")
        return None

    end = datetime.date.today()
    # lookback + バッファ
    start = end - datetime.timedelta(days=int(lookback_days * 1.5))

    try:
        spy = yf.download("SPY", start=start.isoformat(), end=end.isoformat(),
                          auto_adjust=True, progress=False)
        vix = yf.download("^VIX", start=start.isoformat(), end=end.isoformat(),
                          auto_adjust=True, progress=False)
        vix9d = yf.download("^VIX9D", start=start.isoformat(), end=end.isoformat(),
                             auto_adjust=True, progress=False)
    except Exception as e:
        log.warning(f"[HMM] yfinanceダウンロード失敗: {e}")
        return None

    if spy.empty or vix.empty:
        log.warning("[HMM] SPY or VIX データ空")
        return None

    # 共通インデックスで揃える
    spy_close = spy["Close"].squeeze()
    vix_close = vix["Close"].squeeze()

    common_idx = spy_close.index.intersection(vix_close.index)
    if len(common_idx) < MIN_TRAIN_ROWS:
        log.warning(f"[HMM] 共通日数不足: {len(common_idx)}")
        return None

    spy_c = spy_close.reindex(common_idx).ffill()
    vix_c = vix_close.reindex(common_idx).ffill()

    # VIX9D: 存在しない場合はVIX×0.95 で代替
    if not vix9d.empty:
        vix9d_c = vix9d["Close"].squeeze().reindex(common_idx).ffill().bfill()
    else:
        vix9d_c = vix_c * 0.95

    # 特徴量計算
    vix_arr = vix_c.values.astype(float)
    vix9d_arr = vix9d_c.values.astype(float)
    spy_arr = spy_c.values.astype(float)

    # 1. VIX日次変化率
    vix_chg = np.diff(vix_arr, prepend=vix_arr[0]) / (np.abs(vix_arr) + 1e-6)

    # 2. SPY日次リターン
    spy_ret = np.diff(spy_arr, prepend=spy_arr[0]) / (spy_arr + 1e-6)

    # 3. realized_vol (20日ローリング標準偏差)
    rv = np.zeros(len(spy_arr))
    for i in range(len(spy_arr)):
        window = spy_ret[max(0, i - 19): i + 1]
        rv[i] = np.std(window) * np.sqrt(252) if len(window) > 1 else 0.0

    # 4. term_structure: VIX9D/VIX (コンタンゴ=1超, バックワーデーション=1未満)
    term = np.where(vix_arr > 0.5, vix9d_arr / vix_arr, 1.0)

    # 5. GEX proxy: VIX反転 (高VIX=負のGEX proxy)
    gex_proxy = -vix_arr / 40.0  # 正規化

    # 6. put/call ratio proxy: VIXが高いほどput優位
    pc_proxy = np.clip(vix_arr / 30.0, 0.5, 2.0)

    # 7. IV skew proxy: VIX - VIX9D 乖離
    iv_skew = (vix_arr - vix9d_arr) / (vix_arr + 1e-6)

    features = np.column_stack([
        vix_chg,
        spy_ret,
        rv,
        term,
        gex_proxy,
        pc_proxy,
        iv_skew,
    ])

    # NaN除去
    mask = np.isfinite(features).all(axis=1)
    features = features[mask]

    # 最新lookback_days行に絞る
    if len(features) > lookback_days:
        features = features[-lookback_days:]

    log.info(f"[HMM] 特徴量取得完了: shape={features.shape}")
    return features.astype(np.float64)


# ── HMM学習・予測 ──────────────────────────────────────────────────────────────

def _label_states(model, n_states: int) -> dict[int, str]:
    """学習済みモデルの状態を平均特徴量から自動ラベリング。

    ロジック:
      - realized_vol（index=2）が最小 → STABLE
      - |VIX変化率|（index=0）が最大 → FRAGILE_DIV
      - 残り → FRAGILE_TREND
    """
    means = model.means_  # shape (n_states, n_features)
    rv_means = means[:, 2]          # realized_vol
    vix_chg_abs = np.abs(means[:, 0])  # |VIX変化率|

    stable_idx = int(np.argmin(rv_means))
    div_idx = int(np.argmax(vix_chg_abs))
    if div_idx == stable_idx:
        # 同じなら2番目を選ぶ
        sorted_idx = np.argsort(vix_chg_abs)[::-1]
        div_idx = int(sorted_idx[1])

    labels: dict[int, str] = {}
    for i in range(n_states):
        if i == stable_idx:
            labels[i] = "STABLE"
        elif i == div_idx:
            labels[i] = "FRAGILE_DIV"
        else:
            labels[i] = "FRAGILE_TREND"
    return labels


class HMMRegimeClassifier:
    """3状態ガウシアンHMM市場レジーム分類器。

    Args:
        n_states:       HMM状態数（デフォルト3）
        lookback_days:  学習に使う取引日数（デフォルト756≒3年）
        model_cache_path: 学習済みモデルの保存先
        retrain_interval_days: 再学習間隔（日）
    """

    def __init__(
        self,
        n_states: int = N_STATES,
        lookback_days: int = LOOKBACK_DAYS,
        model_cache_path: Path = MODEL_CACHE_PATH,
        retrain_interval_days: int = RETRAIN_INTERVAL_DAYS,
    ) -> None:
        self.n_states = n_states
        self.lookback_days = lookback_days
        self.model_cache_path = model_cache_path
        self.retrain_interval_days = retrain_interval_days

        self._model = None           # hmmlearn GaussianHMM
        self._state_labels: dict[int, str] = {}
        self._last_train_date: Optional[datetime.date] = None
        self._last_features: Optional[np.ndarray] = None
        self._current_state: RegimeState = RegimeState.UNKNOWN

        # 起動時にキャッシュ読み込み
        self._load_model_cache()

    # ── モデル保存・読み込み ──────────────────────────────────────────────────

    def _load_model_cache(self) -> bool:
        """ディスクからモデルを読み込む。成功したらTrue。"""
        if not self.model_cache_path.exists():
            return False
        try:
            with open(self.model_cache_path, "rb") as f:
                cached = pickle.load(f)
            self._model = cached["model"]
            self._state_labels = cached["state_labels"]
            self._last_train_date = cached.get("train_date")
            log.info(
                f"[HMM] モデルキャッシュ読み込み完了 "
                f"(学習日: {self._last_train_date}, labels: {self._state_labels})"
            )
            return True
        except Exception as e:
            log.warning(f"[HMM] キャッシュ読み込み失敗: {e}")
            return False

    def _save_model_cache(self) -> None:
        """モデルをディスクに保存。"""
        try:
            self.model_cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.model_cache_path, "wb") as f:
                pickle.dump({
                    "model": self._model,
                    "state_labels": self._state_labels,
                    "train_date": self._last_train_date,
                }, f)
            log.info(f"[HMM] モデル保存: {self.model_cache_path}")
        except Exception as e:
            log.warning(f"[HMM] モデル保存失敗: {e}")

    # ── 学習 ──────────────────────────────────────────────────────────────────

    def _needs_retrain(self) -> bool:
        """再学習が必要かどうか。"""
        if self._model is None:
            return True
        if self._last_train_date is None:
            return True
        today = datetime.date.today()
        delta = (today - self._last_train_date).days
        return delta >= self.retrain_interval_days

    def train(self, features: Optional[np.ndarray] = None) -> bool:
        """HMMを学習する。

        Args:
            features: shape (T, 7) の特徴量行列。Noneの場合はyfinanceから自動取得。

        Returns:
            学習成功したらTrue。
        """
        try:
            from hmmlearn.hmm import GaussianHMM  # type: ignore
        except ImportError:
            log.error("[HMM] hmmlearn未インストール。pip install hmmlearn")
            return False

        if features is None:
            features = _fetch_raw_data(self.lookback_days)
        if features is None or len(features) < MIN_TRAIN_ROWS:
            log.warning(f"[HMM] 学習データ不足: {len(features) if features is not None else 0}行")
            return False

        self._last_features = features

        model = GaussianHMM(
            n_components=self.n_states,
            covariance_type="full",
            n_iter=200,
            tol=1e-4,
            random_state=42,
            verbose=False,
        )

        try:
            model.fit(features)
        except Exception as e:
            log.error(f"[HMM] 学習失敗: {e}")
            return False

        self._model = model
        self._state_labels = _label_states(model, self.n_states)
        self._last_train_date = datetime.date.today()
        log.info(
            f"[HMM] 学習完了: {len(features)}行, "
            f"labels={self._state_labels}, "
            f"log_likelihood={model.score(features):.2f}"
        )
        self._save_model_cache()
        return True

    def ensure_trained(self) -> bool:
        """未学習 or 再学習タイミングなら学習を実行。"""
        if self._needs_retrain():
            log.info("[HMM] (再)学習を実行")
            return self.train()
        return self._model is not None

    # ── 予測 ──────────────────────────────────────────────────────────────────

    def predict(self, features: np.ndarray) -> RegimeState:
        """特徴量行列の最終行の状態を返す。

        Args:
            features: shape (T, 7)

        Returns:
            RegimeState
        """
        if self._model is None:
            log.warning("[HMM] モデル未学習 → UNKNOWN")
            return RegimeState.UNKNOWN
        if features is None or len(features) == 0:
            return RegimeState.UNKNOWN

        try:
            state_seq = self._model.predict(features)
            last_state_idx = int(state_seq[-1])
            label = self._state_labels.get(last_state_idx, "UNKNOWN")
            return RegimeState(label)
        except Exception as e:
            log.warning(f"[HMM] 予測失敗: {e}")
            return RegimeState.UNKNOWN

    def predict_current(self, lookback_recent: int = 60) -> RegimeState:
        """最新市場データを取得して現在の状態を返す。

        Args:
            lookback_recent: 予測に使う直近取引日数

        Returns:
            RegimeState
        """
        self.ensure_trained()

        # 直近データを取得（学習データの再利用 or 再取得）
        features = _fetch_raw_data(lookback_recent + 30)
        if features is None or len(features) == 0:
            log.warning("[HMM] 最新データ取得失敗 → キャッシュ状態を維持")
            return self._current_state

        state = self.predict(features)
        self._current_state = state
        log.info(f"[HMM] 現在のレジーム: {state.value}")
        return state

    # ── サイズ乗数 ────────────────────────────────────────────────────────────

    def get_size_multiplier(self, state: Optional[RegimeState] = None) -> float:
        """ポジションサイズ乗数を返す。

        STABLE → 1.0 (フルサイズ)
        FRAGILE_TREND → 0.5 (半分)
        FRAGILE_DIV → 0.0 (スキップ)
        UNKNOWN → 0.5 (保守的)

        Args:
            state: 指定しない場合は _current_state を使用。

        Returns:
            0.0 〜 1.0
        """
        if state is None:
            state = self._current_state
        return SIZE_MULTIPLIER.get(state.value, 0.5)

    def should_skip_entry(self, state: Optional[RegimeState] = None) -> bool:
        """FRAGILE_DIV状態ならエントリーをスキップすべきかを返す。"""
        if state is None:
            state = self._current_state
        return state == RegimeState.FRAGILE_DIV

    # ── OOS検証 ──────────────────────────────────────────────────────────────

    def oos_validate(
        self,
        oos_start: datetime.date,
        oos_end: datetime.date,
        train_end: Optional[datetime.date] = None,
    ) -> dict:
        """Out-of-sample検証。

        Args:
            oos_start: OOS期間開始日
            oos_end:   OOS期間終了日
            train_end: 学習終了日（Noneなら oos_start-1日）

        Returns:
            dict with keys: stable_days, fragile_days, stable_ratio,
                            oos_days, state_sequence
        """
        if train_end is None:
            train_end = oos_start - datetime.timedelta(days=1)

        log.info(f"[HMM OOS] 学習期間: ~{train_end}, OOS: {oos_start}~{oos_end}")

        # 全データ取得
        total_lookback = (oos_end - (oos_start - datetime.timedelta(days=LOOKBACK_DAYS))).days
        features_all = _fetch_raw_data(min(total_lookback, LOOKBACK_DAYS * 2))
        if features_all is None:
            return {"error": "データ取得失敗"}

        # OOSのフィーチャー数を推定（末尾N行）
        oos_days = (oos_end - oos_start).days
        trading_days_oos = int(oos_days * 252 / 365)

        if len(features_all) <= trading_days_oos:
            return {"error": f"OOSデータ不足: total={len(features_all)}, oos_est={trading_days_oos}"}

        train_features = features_all[:-trading_days_oos]
        oos_features = features_all[-trading_days_oos:]

        # 学習
        try:
            from hmmlearn.hmm import GaussianHMM  # type: ignore
        except ImportError:
            return {"error": "hmmlearn未インストール"}

        model = GaussianHMM(
            n_components=self.n_states,
            covariance_type="full",
            n_iter=200,
            tol=1e-4,
            random_state=42,
            verbose=False,
        )
        try:
            model.fit(train_features)
        except Exception as e:
            return {"error": f"学習失敗: {e}"}

        labels = _label_states(model, self.n_states)
        state_seq = model.predict(oos_features)
        state_names = [labels.get(s, "UNKNOWN") for s in state_seq]

        stable_days = state_names.count("STABLE")
        fragile_trend_days = state_names.count("FRAGILE_TREND")
        fragile_div_days = state_names.count("FRAGILE_DIV")
        total = len(state_names)

        result = {
            "oos_start": oos_start.isoformat(),
            "oos_end": oos_end.isoformat(),
            "oos_days": total,
            "stable_days": stable_days,
            "fragile_trend_days": fragile_trend_days,
            "fragile_div_days": fragile_div_days,
            "stable_ratio": round(stable_days / total, 3) if total > 0 else 0.0,
            "state_sequence_tail20": state_names[-20:],
            "state_labels": labels,
            "train_rows": len(train_features),
            "log_likelihood_oos": round(float(model.score(oos_features)), 2),
        }
        log.info(f"[HMM OOS] 結果: {result}")
        return result


# ── グローバルシングルトン ────────────────────────────────────────────────────

_global_classifier: Optional[HMMRegimeClassifier] = None


def get_global_classifier(
    force_retrain: bool = False,
) -> HMMRegimeClassifier:
    """プロセスごとのシングルトンを返す。

    Args:
        force_retrain: Trueの場合は即座に再学習を実行。
    """
    global _global_classifier
    if _global_classifier is None:
        _global_classifier = HMMRegimeClassifier()
    if force_retrain:
        _global_classifier.train()
    return _global_classifier


def get_regime_state() -> RegimeState:
    """ショートカット: 現在のレジーム状態を返す。"""
    return get_global_classifier().predict_current()


def get_regime_size_multiplier() -> float:
    """ショートカット: 現在のレジームに基づくサイズ乗数を返す。"""
    clf = get_global_classifier()
    state = clf.predict_current()
    return clf.get_size_multiplier(state)
