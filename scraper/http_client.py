"""netkeiba への礼儀正しい HTTP クライアント。

- リクエスト間 sleep + ジッター(セッション=ワーカー毎に独立ペーシング)
- 429/5xx・接続エラーは指数バックオフでリトライ
- BlockGuard: 全ワーカー共有の連続失敗カウンタ。閾値超えで全ワーカー停止
  (BlockSuspectedError = ブロック疑いで即中断)
- PROXY_URL 環境変数で Cloudflare Worker (?url= 形式) を透過利用可能
"""

import json
import os
import random
import re
import threading
import time
import urllib.parse

import requests

from . import config


class BlockSuspectedError(RuntimeError):
    """連続失敗が閾値を超えた。netkeiba 側のブロック・障害の疑い。"""


class FetchError(RuntimeError):
    """リトライしても取得できなかった単発の失敗。"""


class BlockGuard:
    """複数セッション(ワーカー)で共有する失敗カウンタ+停止フラグ。"""

    def __init__(self, limit=None):
        self.limit = limit or config.CONSECUTIVE_FAILURE_LIMIT
        self._lock = threading.Lock()
        self._failures = 0
        self._stop = threading.Event()
        self.request_count = 0

    def check(self):
        if self._stop.is_set():
            raise BlockSuspectedError("他ワーカーがブロック疑いを検知したため停止")

    def record_request(self):
        with self._lock:
            self.request_count += 1

    def record_success(self):
        with self._lock:
            self._failures = 0

    def record_failure(self, last_err):
        with self._lock:
            self._failures += 1
            failures = self._failures
        if failures >= self.limit:
            self._stop.set()
            raise BlockSuspectedError(
                f"{failures}回連続で取得に失敗。ブロックの疑いがあるため中断: {last_err}"
            )


_JSONP_RE = re.compile(r"^[^(]+\(([\s\S]+)\)\s*;?\s*$")


class PoliteSession:
    def __init__(self, sleep_sec=None, proxy_url=None, guard=None):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = config.USER_AGENT
        self.sleep_sec = config.SLEEP_SEC if sleep_sec is None else sleep_sec
        self.proxy_url = proxy_url if proxy_url is not None else os.environ.get("PROXY_URL", "")
        self.guard = guard or BlockGuard()
        self._last_request_at = 0.0

    @property
    def request_count(self):
        return self.guard.request_count

    def _wait(self):
        elapsed = time.monotonic() - self._last_request_at
        wait = self.sleep_sec + random.uniform(0, config.SLEEP_JITTER) - elapsed
        if wait > 0:
            time.sleep(wait)

    def _build_url(self, url):
        if self.proxy_url:
            return f"{self.proxy_url}?url={urllib.parse.quote(url, safe='')}"
        return url

    def get_text(self, url):
        """URL を取得して本文テキストを返す。リトライ・失敗カウント込み。"""
        last_err = None
        for attempt in range(config.RETRY_MAX + 1):
            self.guard.check()  # 他ワーカーが停止を宣言していたら即中断
            self._wait()
            self._last_request_at = time.monotonic()
            self.guard.record_request()
            try:
                resp = self.session.get(self._build_url(url), timeout=config.TIMEOUT_SEC)
                if resp.status_code == 200:
                    self.guard.record_success()
                    resp.encoding = resp.apparent_encoding or "utf-8"
                    return resp.text
                # 404 等はリトライしても無駄なので即失敗扱い
                if resp.status_code not in (429, 500, 502, 503, 504):
                    last_err = FetchError(f"HTTP {resp.status_code}: {url}")
                    break
                last_err = FetchError(f"HTTP {resp.status_code}: {url}")
            except requests.RequestException as e:
                last_err = FetchError(f"{type(e).__name__}: {e}")
            if attempt < config.RETRY_MAX:
                time.sleep(config.RETRY_BACKOFF_BASE ** (attempt + 1))
        self.guard.record_failure(last_err)  # 閾値超えなら BlockSuspectedError
        raise last_err

    def get_jsonp(self, url):
        """JSONP レスポンスを dict にして返す。"""
        text = self.get_text(url)
        m = _JSONP_RE.match(text.strip())
        if not m:
            raise FetchError(f"JSONP形式ではないレスポンス: {text[:80]!r}")
        return json.loads(m.group(1))
