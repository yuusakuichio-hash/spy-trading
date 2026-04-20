#!/usr/bin/env python3
"""
tradovate_client.py — Tradovate REST API クライアント

公式APIエンドポイント仕様:
  https://demo.tradovateapi.com/v1  (demo)
  https://live.tradovateapi.com/v1  (live)

認証フロー (公式 example-api-trading-strategy より確認):
  POST /auth/accessTokenRequest
    body: {name, password, appId, appVersion, deviceId, cid, sec}
    response: {accessToken, mdAccessToken, expirationTime, ...}
  POST /auth/renewAccessToken
    header: Authorization: Bearer <accessToken>

発注エンドポイント (公式 endpoints/placeOrder.js より確認):
  POST /order/placeOrder
    body: {accountSpec, accountId, action, symbol, orderQty, orderType,
           price, timeInForce, isAutomated}

レート制限:
  公式ドキュメント上の明示的な数値は未記載。
  実践者報告(GitHub dearvn/tradovate)では一般的なREST上限として
  1分あたり数十リクエストが目安。実運用で検証要。

環境変数:
  TRADOVATE_USERNAME  — Tradovateユーザー名
  TRADOVATE_PASSWORD  — パスワード
  TRADOVATE_APP_ID    — Developer登録したアプリID（例: "ApexBot"）
  TRADOVATE_APP_VERSION — アプリバージョン（例: "1.0"）
  TRADOVATE_CID       — Client ID（Developer portalで取得）
  TRADOVATE_SEC       — Client Secret（Developer portalで取得）
  TRADOVATE_ENV       — "DEMO" or "LIVE" (デフォルト: "DEMO")
"""

from __future__ import annotations

import os
import time
import uuid
import hashlib
import logging
import datetime
import requests
import platform
import zoneinfo
from typing import Optional

log = logging.getLogger(__name__)

# ── Rate-limit ハンドラー定数 ─────────────────────────────────────────────────
# Tradovate 公式ドキュメントには明示的なrate limit値の記載なし。
# 実践者報告(GitHub tradovate/example-api-faq, dearvn/tradovate)では
# 一般的なREST上限として1分あたり数十リクエストが目安。
# 429受信時は exponential backoff で対処する。
# Source: https://github.com/tradovate/example-api-faq (confirmed 2026-04-19)
RATE_LIMIT_BACKOFF_BASE_SEC   = 1     # 初回待機秒数
RATE_LIMIT_BACKOFF_MAX_SEC    = 60    # 最大待機秒数
RATE_LIMIT_CONSECUTIVE_HALT   = 3     # 連続429回数でその日の取引停止
RATE_LIMIT_STATUS_CODE        = 429   # HTTP Too Many Requests

ET  = zoneinfo.ZoneInfo("America/New_York")
JST = zoneinfo.ZoneInfo("Asia/Tokyo")

# ── エンドポイント定義 ─────────────────────────────────────────────────────────
DEMO_BASE  = "https://demo.tradovateapi.com/v1"
LIVE_BASE  = "https://live.tradovateapi.com/v1"
MD_BASE    = "https://md.tradovateapi.com/v1"

# ── アクセストークンの有効期間（公式: 24時間。10分前更新） ─────────────────────
TOKEN_REFRESH_MARGIN_SECS = 600  # 10分前にrenewする

# ── 先物シンボル定義 ──────────────────────────────────────────────────────────
# Tradovateシンボル命名規則: 製品コード + 月コード + 年下2桁
# 例: MESU5 = Micro E-mini S&P 500, Sep 2025
# 月コード: F=Jan G=Feb H=Mar J=Apr K=May M=Jun N=Jul Q=Aug U=Sep V=Oct X=Nov Z=Dec
MONTH_CODES = {
    1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
    7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z"
}

PRODUCT_CODES = {
    "MES": "Micro E-mini S&P 500",
    "ES":  "E-mini S&P 500",
    "MNQ": "Micro E-mini Nasdaq-100",
    "NQ":  "E-mini Nasdaq-100",
}

# ── 先物コントラクト仕様 ──────────────────────────────────────────────────────
CONTRACT_SPECS = {
    "MES": {
        "tick_size":    0.25,
        "tick_value":   1.25,   # ドル
        "point_value":  5.0,    # 1ポイントの価値（ドル）
        "initial_margin": 40,   # Apex $50K口座でのMES証拠金（概算）
    },
    "ES": {
        "tick_size":    0.25,
        "tick_value":   12.50,
        "point_value":  50.0,
        "initial_margin": 400,
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# ユーティリティ
# ─────────────────────────────────────────────────────────────────────────────

def _generate_device_id() -> str:
    """
    デバイスIDを生成する。
    公式実装 (requestAccessToken.js) と同一ロジック:
      SHA256( platform + arch + username )
    """
    username = os.environ.get("TRADOVATE_USERNAME", "")
    raw = platform.system() + platform.machine() + username
    return hashlib.sha256(raw.encode()).hexdigest()


def _get_front_month_symbol(product: str) -> str:
    """
    現在の最近限月シンボルを返す。
    CME先物の限月ロールオーバールール:
      S&P500先物(MES/ES)は四半期限月(H/M/U/Z)
      限月の第3金曜日の8日前（前の木曜）にロールオーバー
    簡易実装: 現在日付から次の四半期限月を算出
    """
    now = datetime.datetime.now(ET)
    year = now.year
    month = now.month

    # 四半期限月: 3, 6, 9, 12月
    quarterly = [3, 6, 9, 12]

    for q_month in quarterly:
        if month <= q_month:
            # 限月の第3金曜日 = ロールオーバー基準
            # 実際のロールオーバーは第3金曜日の8日前（木曜）
            # 簡易版: 月の第3金曜日を計算
            first_day = datetime.date(year, q_month, 1)
            # 第1金曜日
            days_to_friday = (4 - first_day.weekday()) % 7
            first_friday = first_day + datetime.timedelta(days=days_to_friday)
            # 第3金曜日
            third_friday = first_friday + datetime.timedelta(weeks=2)
            # ロールオーバー: 第3金曜日の8日前（木曜日）
            rollover_date = third_friday - datetime.timedelta(days=8)

            if now.date() < rollover_date:
                break
    else:
        # 12月を超えたら翌年3月
        year += 1
        q_month = 3

    month_code = MONTH_CODES[q_month]
    year_code  = str(year)[-1]  # 下1桁: 2025 → "5"
    return f"{product}{month_code}{year_code}"


# ─────────────────────────────────────────────────────────────────────────────
# メインクライアント
# ─────────────────────────────────────────────────────────────────────────────

class TradovateClient:
    """
    Tradovate REST APIクライアント。

    使い方:
        client = TradovateClient()
        client.authenticate()  # アクセストークン取得
        client.get_account()   # アカウント情報
        client.place_order(...)
    """

    def __init__(
        self,
        env: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        app_id: Optional[str] = None,
        app_version: Optional[str] = None,
        cid: Optional[str] = None,
        sec: Optional[str] = None,
    ):
        self.env = (env or os.environ.get("TRADOVATE_ENV", "DEMO")).upper()
        self.username    = username    or os.environ.get("TRADOVATE_USERNAME", "")
        self.password    = password    or os.environ.get("TRADOVATE_PASSWORD", "")
        self.app_id      = app_id      or os.environ.get("TRADOVATE_APP_ID", "ApexBot")
        self.app_version = app_version or os.environ.get("TRADOVATE_APP_VERSION", "1.0")
        self.cid         = cid         or os.environ.get("TRADOVATE_CID", "")
        self.sec         = sec         or os.environ.get("TRADOVATE_SEC", "")

        self.base_url = DEMO_BASE if self.env == "DEMO" else LIVE_BASE

        # トークン状態
        self._access_token:    Optional[str]   = None
        self._md_access_token: Optional[str]   = None
        self._token_expiry:    Optional[float] = None  # Unix timestamp

        # アカウント情報（authenticate後にセット）
        self.account_id:   Optional[int] = None
        self.account_spec: Optional[str] = None

        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "Accept":       "application/json",
        })

        # Rate-limit 状態管理
        self._rate_limit_consecutive: int = 0    # 連続429カウンタ
        self._rate_limit_halted:      bool = False  # 当日取引停止フラグ

        log.info(f"[TradovateClient] initialized env={self.env} base={self.base_url}")

    # ── 認証 ──────────────────────────────────────────────────────────────────

    def authenticate(self) -> bool:
        """
        アクセストークンを取得する。
        成功時にself.account_id / account_specもセットする。
        Returns: 成功=True, 失敗=False
        """
        url = f"{self.base_url}/auth/accessTokenRequest"
        device_id = _generate_device_id()

        payload = {
            "name":        self.username,
            "password":    self.password,
            "appId":       self.app_id,
            "appVersion":  self.app_version,
            "deviceId":    device_id,
            "cid":         int(self.cid) if self.cid else 0,
            "sec":         self.sec,
        }

        try:
            resp = self._session.post(url, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            log.error(f"[TradovateClient] authenticate HTTP error: {e}")
            return False

        if "errorText" in data:
            log.error(f"[TradovateClient] authenticate failed: {data['errorText']}")
            return False

        if "p-ticket" in data:
            log.error(f"[TradovateClient] CAPTCHA required: ticket={data['p-ticket']}")
            return False

        self._access_token    = data.get("accessToken")
        self._md_access_token = data.get("mdAccessToken")

        # expirationTimeのパース (例: "2025-04-18T09:00:00Z")
        expiry_str = data.get("expirationTime", "")
        try:
            expiry_dt = datetime.datetime.fromisoformat(
                expiry_str.replace("Z", "+00:00")
            )
            self._token_expiry = expiry_dt.timestamp()
        except Exception:
            # パース失敗時は24時間後をデフォルトにする
            self._token_expiry = time.time() + 86400

        # 認証ヘッダーをセッションにセット
        self._session.headers["Authorization"] = f"Bearer {self._access_token}"

        log.info(f"[TradovateClient] authenticated OK, token expires: {expiry_str}")

        # アカウント情報取得
        return self._load_account_info()

    def renew_token(self) -> bool:
        """アクセストークンを更新する。"""
        if not self._access_token:
            log.warning("[TradovateClient] renew_token: no existing token, calling authenticate()")
            return self.authenticate()

        url = f"{self.base_url}/auth/renewAccessToken"
        try:
            resp = self._session.post(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            log.error(f"[TradovateClient] renew_token HTTP error: {e}")
            return False

        if "errorText" in data:
            log.error(f"[TradovateClient] renew_token failed: {data['errorText']}")
            return False

        self._access_token    = data.get("accessToken", self._access_token)
        self._md_access_token = data.get("mdAccessToken", self._md_access_token)

        expiry_str = data.get("expirationTime", "")
        try:
            expiry_dt = datetime.datetime.fromisoformat(
                expiry_str.replace("Z", "+00:00")
            )
            self._token_expiry = expiry_dt.timestamp()
        except Exception:
            self._token_expiry = time.time() + 86400

        self._session.headers["Authorization"] = f"Bearer {self._access_token}"
        log.info(f"[TradovateClient] token renewed OK")
        return True

    def ensure_authenticated(self) -> bool:
        """
        トークンの有効期限を確認し、必要に応じてrenewする。
        Bot稼働中に定期的に呼び出す。
        """
        if not self._access_token:
            return self.authenticate()

        now = time.time()
        if self._token_expiry and (self._token_expiry - now) < TOKEN_REFRESH_MARGIN_SECS:
            log.info("[TradovateClient] token near expiry, renewing...")
            return self.renew_token()

        return True

    @property
    def is_authenticated(self) -> bool:
        return bool(self._access_token)

    # ── Rate-limit ハンドラー ────────────────────────────────────────────────
    @property
    def is_rate_limit_halted(self) -> bool:
        """当日 rate-limit 停止フラグ。True の場合は発注不可。"""
        return self._rate_limit_halted

    def reset_rate_limit_daily(self) -> None:
        """日次リセット: ET 09:00 に呼ぶ。連続カウンタ・停止フラグをクリア。"""
        self._rate_limit_consecutive = 0
        self._rate_limit_halted = False
        log.info("[TradovateClient] rate-limit counter reset (daily)")

    def _request_with_backoff(
        self,
        method: str,       # "GET" | "POST"
        url: str,
        timeout: int = 15,
        **kwargs,
    ) -> Optional[requests.Response]:
        """
        HTTP リクエストを送信する。429 受信時は exponential backoff を適用する。

        Rate-limit 設計 (chronos_rules.yaml の rate_limit_handler セクション参照):
          - 429 受信: 1s → 2s → 4s → 8s → 16s → 60s(上限) で backoff
          - 連続 3 回 429: _rate_limit_halted = True (当日取引停止)
          - 成功時: _rate_limit_consecutive = 0 にリセット

        Source: Tradovate rate limit 公式明示なし。実践者報告による (2026-04-19)

        Args:
            method:  "GET" または "POST"
            url:     エンドポイント URL
            timeout: タイムアウト秒数
            **kwargs: requests.Session.get/post に渡す追加引数

        Returns:
            requests.Response (成功時) または None (停止・失敗時)
        """
        if self._rate_limit_halted:
            log.error("[TradovateClient] rate-limit HALT: skip request")
            return None

        backoff = RATE_LIMIT_BACKOFF_BASE_SEC
        attempt = 0

        while True:
            try:
                if method.upper() == "GET":
                    resp = self._session.get(url, timeout=timeout, **kwargs)
                else:
                    resp = self._session.post(url, timeout=timeout, **kwargs)
            except requests.RequestException as e:
                log.error(f"[TradovateClient] _request_with_backoff network error: {e}")
                return None

            if resp.status_code != RATE_LIMIT_STATUS_CODE:
                # 成功 (or 4xx/5xx 以外の429): 連続カウンタをリセット
                self._rate_limit_consecutive = 0
                return resp

            # 429 受信
            self._rate_limit_consecutive += 1
            attempt += 1
            log.warning(
                f"[TradovateClient] 429 Too Many Requests "
                f"(consecutive={self._rate_limit_consecutive}, attempt={attempt})"
            )

            if self._rate_limit_consecutive >= RATE_LIMIT_CONSECUTIVE_HALT:
                self._rate_limit_halted = True
                log.error(
                    f"[TradovateClient] rate-limit HALT: "
                    f"{RATE_LIMIT_CONSECUTIVE_HALT} consecutive 429s. "
                    "当日取引停止。reset_rate_limit_daily() で翌日リセット。"
                )
                return None

            log.info(f"[TradovateClient] backoff {backoff}s before retry...")
            time.sleep(backoff)
            backoff = min(backoff * 2, RATE_LIMIT_BACKOFF_MAX_SEC)

    # ── アカウント情報 ─────────────────────────────────────────────────────────

    def _load_account_info(self) -> bool:
        """
        アカウントリストを取得してaccount_id / account_specをセットする。
        """
        url = f"{self.base_url}/account/list"
        try:
            resp = self._session.get(url, timeout=15)
            resp.raise_for_status()
            accounts = resp.json()
        except requests.RequestException as e:
            log.error(f"[TradovateClient] account/list HTTP error: {e}")
            return False

        if not accounts:
            log.error("[TradovateClient] account/list: empty response")
            return False

        # 最初のアカウントを使用（通常は1アカウント）
        acct = accounts[0]
        self.account_id   = acct.get("id")
        self.account_spec = acct.get("name")

        log.info(f"[TradovateClient] account loaded: id={self.account_id} spec={self.account_spec}")
        return True

    def get_account_balance(self) -> Optional[dict]:
        """
        口座残高・証拠金情報を取得する。
        Returns:
            {
              "balance":         float,  — 現金残高
              "unrealized_pnl":  float,  — 含み損益
              "total_equity":    float,  — 総資産
              "initial_margin":  float,  — 必要証拠金
              "available":       float,  — 出金可能残高
            }
            またはNone（取得失敗時）
        """
        if not self.account_id:
            log.warning("[TradovateClient] get_account_balance: account_id not set")
            return None

        url = f"{self.base_url}/cashBalance/getCashBalanceSnapshot"
        try:
            resp = self._session.get(
                url,
                params={"accountId": self.account_id},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            log.error(f"[TradovateClient] get_account_balance error: {e}")
            return None

        return {
            "balance":        data.get("totalCashValue", 0.0),
            "unrealized_pnl": data.get("openPnl", 0.0),
            "total_equity":   data.get("netLiquidationValue", 0.0),
            "initial_margin": data.get("initialMargin", 0.0),
            "available":      data.get("availableFunds", 0.0),
        }

    # ── ポジション ─────────────────────────────────────────────────────────────

    def get_positions(self) -> list[dict]:
        """
        現在のオープンポジションを取得する。
        Returns:
            [
              {
                "id":           int,
                "symbol":       str,   — 例: "MESU5"
                "net_pos":      int,   — 正=ロング, 負=ショート
                "net_price":    float, — 平均コスト
                "unrealized_pnl": float,
              },
              ...
            ]
        """
        url = f"{self.base_url}/position/list"
        try:
            resp = self._session.get(url, timeout=15)
            resp.raise_for_status()
            raw_positions = resp.json()
        except requests.RequestException as e:
            log.error(f"[TradovateClient] get_positions error: {e}")
            return []

        # contractId(int) → contract name(str) の解決
        # Tradovate /position/list は contractId しか返さないため、
        # /contract/items で一括逆引きして "symbol" を文字列にする。
        open_positions = [p for p in raw_positions if p.get("netPos", 0) != 0]

        contract_id_to_name: dict[int, str] = {}
        if open_positions:
            contract_ids = list({p["contractId"] for p in open_positions if p.get("contractId")})
            try:
                items_url = f"{self.base_url}/contract/items"
                items_resp = self._session.get(
                    items_url,
                    params={"ids": ",".join(str(cid) for cid in contract_ids)},
                    timeout=15,
                )
                items_resp.raise_for_status()
                items_data = items_resp.json()
                for item in items_data:
                    cid  = item.get("id")
                    name = item.get("name")
                    if cid is not None and name:
                        contract_id_to_name[cid] = name
            except Exception as e:
                log.warning(
                    f"[TradovateClient] contract/items lookup failed: {e}; "
                    "falling back to contractId as symbol"
                )

        result = []
        for p in open_positions:
            contract_id = p.get("contractId")
            # name解決できた場合は文字列シンボル、失敗時は str(contractId) で代替
            symbol = contract_id_to_name.get(contract_id, str(contract_id) if contract_id else "")
            result.append({
                "id":             p.get("id"),
                "contract_id":    contract_id,
                "symbol":         symbol,
                "net_pos":        p.get("netPos", 0),
                "net_price":      p.get("netPrice", 0.0),
                "unrealized_pnl": p.get("openPnl", 0.0),
            })

        return result

    def get_positions_for_rules(self) -> list[dict]:
        """prop_firm_rules が期待するスキーマに変換した positionsを返す。

        CR-5修正: get_positions() は {"net_pos": int, ...} を返すが、
        prop_firm_rules.check_hedge_prohibition() / check_dca_pattern() は
        {"side": "BUY"|"SELL", ...} を期待する。
        net_pos > 0 → side="BUY", net_pos < 0 → side="SELL" に変換する。

        Returns:
            [
              {
                "symbol":         str,
                "side":           "BUY" | "SELL",
                "unrealized_pnl": float,
                "net_pos":        int,   # 元フィールドも残す
              },
              ...
            ]
        """
        raw = self.get_positions()
        result = []
        for p in raw:
            net_pos = p.get("net_pos", 0)
            if net_pos == 0:
                continue
            side = "BUY" if net_pos > 0 else "SELL"
            result.append({
                "symbol":         p.get("symbol", ""),
                "side":           side,
                "unrealized_pnl": p.get("unrealized_pnl", 0.0),
                "net_pos":        net_pos,
                "id":             p.get("id"),
                "contract_id":    p.get("contract_id"),
            })
        return result

    # ── 価格データ ─────────────────────────────────────────────────────────────

    def get_quote(self, symbol: str) -> Optional[dict]:
        """
        現在の気配値を取得する。
        symbol: Tradovateシンボル (例: "MESU5")
        Returns:
            {"symbol": str, "bid": float, "ask": float, "last": float, "timestamp": str}
            またはNone
        """
        url = f"{self.base_url}/md/getQuote"
        try:
            resp = self._session.get(
                url,
                params={"symbol": symbol},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            log.error(f"[TradovateClient] get_quote({symbol}) error: {e}")
            return None

        if not data:
            return None

        return {
            "symbol":    symbol,
            "bid":       data.get("bid", 0.0),
            "ask":       data.get("ask", 0.0),
            "last":      data.get("tradePrice", 0.0),
            "timestamp": data.get("timestamp", ""),
        }

    def get_bars(
        self,
        symbol: str,
        bar_type: str = "MinuteBar",
        unit: int = 5,
        count: int = 50,
    ) -> list[dict]:
        """
        ローソク足データを取得する。
        bar_type: "MinuteBar" | "HourlyBar" | "DailyBar"
        unit: 1分足=1, 5分足=5, など
        count: 取得本数
        Returns:
            [{"timestamp": str, "open": float, "high": float, "low": float, "close": float, "volume": int}, ...]
        """
        url = f"{self.base_url}/md/getBars"
        params = {
            "symbol":  symbol,
            "chartDescription": f"{bar_type},{unit}",
            "elementNumber": count,
        }
        try:
            resp = self._session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            log.error(f"[TradovateClient] get_bars({symbol}) error: {e}")
            return []

        bars = data.get("bars", [])
        result = []
        for b in bars:
            result.append({
                "timestamp": b.get("timestamp", ""),
                "open":      b.get("open", 0.0),
                "high":      b.get("high", 0.0),
                "low":       b.get("low", 0.0),
                "close":     b.get("close", 0.0),
                "volume":    b.get("upVolume", 0) + b.get("downVolume", 0),
            })

        return result

    # ── 発注 ──────────────────────────────────────────────────────────────────

    def place_order(
        self,
        symbol:     str,
        action:     str,   # "Buy" | "Sell"
        qty:        int,
        order_type: str,   # "Market" | "Limit" | "Stop" | "StopLimit"
        price:      Optional[float] = None,
        stop_price: Optional[float] = None,
        time_in_force: str = "Day",
        bypass_rate_limit: bool = False,  # CRIT-5: close/exit order専用バイパス
        client_order_id: Optional[str] = None,  # MF-7: 二重発注防止用べき等キー
    ) -> Optional[dict]:
        """
        注文を送信する。
        公式エンドポイント: POST /order/placeOrder (endpoints/placeOrder.js より確認)

        Args:
            symbol:          Tradovateシンボル (例: "MESU5")
            action:          "Buy" | "Sell"
            qty:             数量（契約数）
            order_type:      "Market" | "Limit" | "Stop" | "StopLimit"
            price:           指値価格（Limit/StopLimit時に必要）
            stop_price:      ストップ価格（Stop/StopLimit時に必要）
            time_in_force:   "Day" | "GTC" | "GTD" | "IOC" | "FOK"
            client_order_id: MF-7 fix — べき等キー。
                Tradovate API は clOrdId フィールドをサポートする
                (公式 endpoints/placeOrder.js: "clOrdId" string optional)。
                ネットワーク切断後の再送時に同じ client_order_id を渡すことで
                Knight Capital 型の二重発注を防ぐ。
                None の場合は uuid4 を自動生成して設定する（未設定防止）。
        Returns:
            {"order_id": int, "status": str, "symbol": str, ...}
            またはNone（送信失敗時）
        """
        if not self.account_id or not self.account_spec:
            log.error("[TradovateClient] place_order: not authenticated")
            return None

        url = f"{self.base_url}/order/placeOrder"

        order_payload: dict = {
            "accountSpec":   self.account_spec,
            "accountId":     self.account_id,
            "action":        action,
            "symbol":        symbol,
            "orderQty":      qty,
            "orderType":     order_type,
            "timeInForce":   time_in_force,
            "isAutomated":   True,
        }

        if price is not None:
            order_payload["price"] = price
        if stop_price is not None:
            order_payload["stopPrice"] = stop_price

        # MF-7 fix: clOrdId（べき等キー）を付与して二重発注を防ぐ。
        # Tradovate API は clOrdId (string) をサポートする。
        # 呼出側が client_order_id を指定しない場合は uuid4 を自動生成して設定する。
        # ネットワーク切断後の再送時に同じ clOrdId を渡せば二重発注にならない。
        _clord_id = client_order_id or f"sora-{uuid.uuid4().hex[:16]}"
        order_payload["clOrdId"] = _clord_id

        log.info(f"[TradovateClient] placing order: {action} {qty}x{symbol} "
                 f"type={order_type} price={price} stop={stop_price} "
                 f"clOrdId={_clord_id}")

        # CRIT-5: rate-limit 停止中の扱い
        # bypass_rate_limit=True（close/exitオーダー）の場合は通す
        # 通常の新規注文は halted を維持
        if self._rate_limit_halted:
            if bypass_rate_limit:
                log.warning(
                    "[TradovateClient] place_order: rate-limit HALT but forcing exit "
                    f"(bypass_rate_limit=True): {action} {qty}x{symbol}"
                )
            else:
                log.error("[TradovateClient] place_order: rate-limit HALT, order rejected")
                return None

        resp = self._request_with_backoff("POST", url, json=order_payload, timeout=15)
        if resp is None:
            log.error(f"[TradovateClient] place_order: request failed (rate-limit or network)")
            return None
        try:
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.error(f"[TradovateClient] place_order HTTP error: {e}")
            return None

        if "errorText" in data:
            log.error(f"[TradovateClient] place_order API error: {data['errorText']}")
            return None

        order_id = data.get("orderId")
        log.info(f"[TradovateClient] order placed: order_id={order_id} status={data.get('orderStatus')}")

        return {
            "order_id":    order_id,
            "status":      data.get("orderStatus"),
            "symbol":      symbol,
            "action":      action,
            "qty":         qty,
            "order_type":  order_type,
            "price":       price,
            "raw":         data,
        }

    def cancel_order(self, order_id: int) -> bool:
        """注文をキャンセルする。"""
        url = f"{self.base_url}/order/cancelOrder"
        try:
            resp = self._session.post(
                url,
                json={"orderId": order_id},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            log.error(f"[TradovateClient] cancel_order({order_id}) error: {e}")
            return False

        log.info(f"[TradovateClient] cancel_order({order_id}): {data.get('orderStatus')}")
        return True

    def close_position(self, symbol: str) -> Optional[dict]:
        """
        指定シンボルのポジションをマーケット注文でクローズする。
        ポジションがない場合はNoneを返す。
        """
        positions = self.get_positions()
        target = next((p for p in positions if p.get("symbol") == symbol), None)

        if not target:
            log.info(f"[TradovateClient] close_position({symbol}): no open position")
            return None

        net_pos = target["net_pos"]
        action  = "Sell" if net_pos > 0 else "Buy"
        qty     = abs(net_pos)

        log.info(f"[TradovateClient] closing position: {action} {qty}x{symbol}")
        # CRIT-5: close_position は rate_limit_halted でも通す
        return self.place_order(symbol=symbol, action=action, qty=qty, order_type="Market",
                                bypass_rate_limit=True)

    def close_all_positions(self) -> list[dict]:
        """全ポジションをマーケット注文でクローズする。

        CRIT-5: rate_limit_halted でも close/exit order は通す（bypass_rate_limit=True）。
        """
        positions = self.get_positions()
        results = []
        for p in positions:
            symbol  = p.get("symbol")
            net_pos = p.get("net_pos", 0)
            if net_pos == 0 or not symbol:
                continue
            action = "Sell" if net_pos > 0 else "Buy"
            qty    = abs(net_pos)
            # CRIT-5: クローズは rate_limit halted 中でも強制実行
            result = self.place_order(symbol=symbol, action=action, qty=qty, order_type="Market",
                                      bypass_rate_limit=True)
            if result:
                results.append(result)

        if self._rate_limit_halted and results:
            # CRIT-5: halted 中に強制クローズした場合はPushoverで警告通知
            # (pushoverはchronos_bot.pyに依存しないため、ここではlogのみ)
            log.warning(
                f"[TradovateClient] RATE LIMITED but forcing exit: "
                f"{len(results)} positions closed via bypass"
            )

        return results

    # ── コントラクト検索 ──────────────────────────────────────────────────────

    def find_contract(self, symbol: str) -> Optional[dict]:
        """
        シンボル名からコントラクト情報を検索する。
        Returns: {"id": int, "name": str, "contractMaturityId": int, ...}
        """
        url = f"{self.base_url}/contract/find"
        try:
            resp = self._session.get(
                url,
                params={"name": symbol},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            log.error(f"[TradovateClient] find_contract({symbol}) error: {e}")
            return None

        return data

    def get_front_month_symbol(self, product: str = "MES") -> str:
        """
        現在の最近限月シンボルを返す。
        product: "MES" | "ES" | "MNQ" | "NQ"
        """
        return _get_front_month_symbol(product)

    # ── 接続テスト ────────────────────────────────────────────────────────────

    def test_connection(self) -> dict:
        """
        接続テストを実行する。認証情報が設定されていれば実際に認証テストも行う。
        Returns: {"success": bool, "env": str, "authenticated": bool, "account_id": int|None, "error": str|None}
        """
        result = {
            "success":       False,
            "env":           self.env,
            "base_url":      self.base_url,
            "authenticated": False,
            "account_id":    None,
            "error":         None,
        }

        # 認証情報なしの場合はスキップ
        if not self.username or not self.password:
            result["error"] = "credentials not set (TRADOVATE_USERNAME/PASSWORD)"
            log.warning("[TradovateClient] test_connection: no credentials configured")
            return result

        if self.authenticate():
            result["success"]       = True
            result["authenticated"] = True
            result["account_id"]    = self.account_id
            balance = self.get_account_balance()
            result["balance"] = balance
        else:
            result["error"] = "authentication failed"

        return result


# ─────────────────────────────────────────────────────────────────────────────
# スタンドアロン接続テスト
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))

    # .envロード
    from pathlib import Path
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    client = TradovateClient()
    result = client.test_connection()

    print("\n=== Tradovate Connection Test ===")
    for k, v in result.items():
        print(f"  {k}: {v}")

    if result.get("authenticated"):
        # フロント限月シンボルの確認
        symbol = client.get_front_month_symbol("MES")
        print(f"\n  Front month MES symbol: {symbol}")

        # 気配値テスト
        quote = client.get_quote(symbol)
        print(f"  Quote({symbol}): {quote}")
