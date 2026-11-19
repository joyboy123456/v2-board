"""看板登录：输一次密码，之后靠签名 cookie 记住这台设备。

密码取 V2_BOARD_PASSWORD，没配就用 V2_INTERVENTION_TOKEN；两个都没有（只读、演示实例）就不要求登录。
V2_BOARD_LOGIN=0 可以整个关掉登录，回到「谁都能看、写操作填令牌」。

cookie 不落盘，内容是「到期时间.随机数.签名」，签名密钥由密码派生：改密码会让所有已登录的设备失效，
退出登录只清掉当前浏览器的 cookie。脚本和其他 Agent 不用 cookie，带 Authorization: Bearer <写入令牌> 即可。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from functools import lru_cache

COOKIE = 'v2b_session'
TTL = 30 * 24 * 3600
WINDOW = 300  # 秒：输错密码按这个窗口计数


def password(environ=None) -> str:
    env = os.environ if environ is None else environ
    if env.get('V2_BOARD_LOGIN', '').strip() == '0':
        return ''
    return env.get('V2_BOARD_PASSWORD', '') or env.get('V2_INTERVENTION_TOKEN', '')


def required(environ=None) -> bool:
    return bool(password(environ))


@lru_cache(maxsize=4)
def _key(secret: str) -> bytes:
    return hashlib.pbkdf2_hmac('sha256', secret.encode('utf-8'), b'v2-board-session-v1', 100_000)


def _sign(secret: str, body: str) -> str:
    return hmac.new(_key(secret), body.encode('utf-8'), hashlib.sha256).hexdigest()


def issue(environ=None, now=None) -> str:
    secret = password(environ)
    if not secret:
        raise ValueError('login is not configured')
    body = f'{int(time.time() if now is None else now) + TTL}.{secrets.token_urlsafe(12)}'
    return f'{body}.{_sign(secret, body)}'


def session_ok(cookie, environ=None, now=None) -> bool:
    secret = password(environ)
    if not secret or not isinstance(cookie, str) or not 0 < len(cookie) <= 200:
        return False
    body, _, signature = cookie.rpartition('.')
    expiry = body.split('.', 1)[0]
    if not (expiry.isascii() and expiry.isdigit()):
        return False
    if not hmac.compare_digest(signature.encode('utf-8'), _sign(secret, body).encode('utf-8')):
        return False
    return int(expiry) > (time.time() if now is None else now)


def password_ok(supplied, environ=None) -> bool:
    secret = password(environ)
    return bool(secret) and isinstance(supplied, str) and hmac.compare_digest(supplied.encode('utf-8'), secret.encode('utf-8'))


def bearer_ok(authorization, environ=None) -> bool:
    env = os.environ if environ is None else environ
    token = env.get('V2_INTERVENTION_TOKEN', '')
    parts = (authorization or '').split()
    return bool(token) and len(parts) == 2 and parts[0].lower() == 'bearer' and hmac.compare_digest(parts[1].encode('utf-8'), token.encode('utf-8'))


def allowed(cookie, authorization, environ=None) -> bool:
    """这个请求能不能读看板：没开登录、已登录，或带着写入令牌。"""
    return not required(environ) or session_ok(cookie, environ) or bearer_ok(authorization, environ)


class Throttle:
    """输错密码的次数限制：同一个来源 5 分钟内 8 次，所有来源合计 60 次，超了就先拒绝。已登录的设备不受影响。"""

    def __init__(self, window=WINDOW, per_client=8, overall=60):
        self.window, self.per_client, self.overall = window, per_client, overall
        self.fails: list[tuple[float, str]] = []

    def _recent(self, now):
        self.fails = [item for item in self.fails if now - item[0] < self.window]
        return self.fails

    def blocked(self, client: str, now=None) -> bool:
        recent = self._recent(time.monotonic() if now is None else now)
        return len(recent) >= self.overall or sum(1 for _, who in recent if who == client) >= self.per_client

    def fail(self, client: str, now=None) -> None:
        now = time.monotonic() if now is None else now
        self._recent(now).append((now, client))
