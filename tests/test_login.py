import asyncio
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

import auth
import board_parse as bp

TOKEN = 'board-token-for-tests-0123456789'
CARD = '---\nid: "01"\ntitle: "卡"\nstatus: "open"\ndispatch: "asking"\n---\n\n正文\n\n## 待回答\n- [Q1] 2026-11-19 10:00 | 用哪个接口？\n'


class AuthTest(unittest.TestCase):
    env = {'V2_INTERVENTION_TOKEN': TOKEN}

    def test_login_is_only_required_when_a_password_is_configured(self):
        self.assertFalse(auth.required({}))
        self.assertTrue(auth.required(self.env))
        self.assertTrue(auth.required({'V2_BOARD_PASSWORD': 'pw-for-people'}))
        self.assertFalse(auth.required(dict(self.env, V2_BOARD_LOGIN='0')))
        self.assertTrue(auth.allowed('', '', {}))
        self.assertFalse(auth.allowed('', '', self.env))

    def test_board_password_wins_over_the_token(self):
        env = dict(self.env, V2_BOARD_PASSWORD='pw-for-people')
        self.assertTrue(auth.password_ok('pw-for-people', env))
        self.assertFalse(auth.password_ok(TOKEN, env))
        self.assertTrue(auth.password_ok(TOKEN, self.env))
        for wrong in ('', None, 123, TOKEN + 'x', ['x']):
            self.assertFalse(auth.password_ok(wrong, self.env), wrong)
        self.assertTrue(auth.bearer_ok('Bearer ' + TOKEN, env))  # 脚本仍然用写入令牌
        self.assertFalse(auth.bearer_ok('Bearer pw-for-people', env))

    def test_session_cookie(self):
        cookie = auth.issue(self.env, now=1000)
        self.assertTrue(auth.session_ok(cookie, self.env, now=1000))
        self.assertTrue(auth.session_ok(cookie, self.env, now=1000 + auth.TTL - 1))
        self.assertFalse(auth.session_ok(cookie, self.env, now=1000 + auth.TTL))  # 过期
        self.assertFalse(auth.session_ok(cookie, {'V2_INTERVENTION_TOKEN': TOKEN + 'x'}, now=1000))  # 改了密码
        self.assertFalse(auth.session_ok(cookie, {}, now=1000))
        expiry, nonce, signature = cookie.split('.')
        forged = (
            f'{int(expiry) + 1}.{nonce}.{signature}', f'{expiry}.{nonce}x.{signature}', f'{expiry}.{nonce}.' + '0' * 64,
            f'{expiry}.{nonce}', '', 'x', '..', '９９９９９９９９９９.a.b', 'é.a.b', cookie + 'x' * 200, None, 5,
        )
        for bad in forged:
            self.assertFalse(auth.session_ok(bad, self.env, now=1000), bad)
        self.assertNotEqual(cookie, auth.issue(self.env, now=1000))
        with self.assertRaises(ValueError):
            auth.issue({})

    def test_bearer(self):
        self.assertTrue(auth.allowed('', 'bearer ' + TOKEN, self.env))
        for bad in ('', TOKEN, 'Bearer', 'Bearer ' + TOKEN + 'x', 'Basic ' + TOKEN, 'Bearer ' + TOKEN + ' x'):
            self.assertFalse(auth.bearer_ok(bad, self.env), bad)
        self.assertFalse(auth.bearer_ok('Bearer ', {}))

    def test_throttle(self):
        t = auth.Throttle(window=300, per_client=3, overall=5)
        for i in range(3):
            self.assertFalse(t.blocked('a', now=i))
            t.fail('a', now=i)
        self.assertTrue(t.blocked('a', now=3))
        self.assertFalse(t.blocked('b', now=3))  # 别的来源不受影响
        self.assertFalse(t.blocked('a', now=300))  # 窗口过了一条
        for who in ('b', 'c', 'e'):
            t.fail(who, now=4)
        self.assertTrue(t.blocked('d', now=5))  # 合计上限
        self.assertFalse(t.blocked('d', now=400))
        self.assertEqual(t.fails, [])


class LoginApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as board_app
        cls.app = board_app

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'board'
        (self.root / 'h').mkdir(parents=True)
        self.card = self.root / 'h' / '01.md'
        self.card.write_text(CARD, encoding='utf-8')
        self.saved = (self.app.BOARD, bp.BOARD, self.app.hub.board, self.app.hub.tick, self.app.login_throttle.fails)
        self.app.BOARD = bp.BOARD = self.root
        self.app.hub.board = {'cards': [{'key': 'h/01', 'dispatch': 'asking', 'questions': [], 'answerHistory': [], 'cardRevision': '', 'body': ''}], 'version': 'v', 'dispatch': {'slots': 0}}
        self.app.hub.tick = 0
        self.app.login_throttle.fails = []
        self.env = {'V2_BOARD_DIR': str(self.root), 'V2_INTERVENTION_MODE': 'enabled', 'V2_INTERVENTION_TOKEN': TOKEN}

    def tearDown(self):
        self.app.BOARD, bp.BOARD, self.app.hub.board, self.app.hub.tick, self.app.login_throttle.fails = self.saved
        self.temp.cleanup()

    def call(self, method, path, payload=None, headers=None, env=None, scheme='http'):
        body = b'' if payload is None else json.dumps(payload).encode()
        values = {'host': 'testserver'}
        if method == 'POST':
            values.update({'origin': f'{scheme}://testserver', 'sec-fetch-site': 'same-origin', 'content-type': 'application/json'})
        values.update(headers or {})
        messages, sent = [], False

        async def receive():
            nonlocal sent
            if sent:
                return {'type': 'http.disconnect'}
            sent = True
            return {'type': 'http.request', 'body': body, 'more_body': False}

        async def send(message):
            messages.append(message)

        scope = {
            'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': method, 'scheme': scheme,
            'path': path, 'raw_path': path.encode(), 'query_string': b'', 'root_path': '',
            'headers': [(key.lower().encode(), str(value).encode()) for key, value in values.items() if value is not None],
            'client': ('203.0.113.9', 50000), 'server': ('testserver', 80),
        }
        with patch.dict(os.environ, self.env if env is None else env, clear=True):
            asyncio.run(self.app.app(scope, receive, send))
        start = next(m for m in messages if m['type'] == 'http.response.start')
        raw = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')
        try:
            data = json.loads(raw or b'{}')
        except ValueError:
            data = raw
        return start['status'], data, [(k.decode(), v.decode()) for k, v in start['headers']]

    def login(self, password=TOKEN, **kw):
        status, data, headers = self.call('POST', '/api/login', {'password': password}, **kw)
        cookies = [v for k, v in headers if k == 'set-cookie']
        return status, data, cookies

    def test_reads_need_a_login(self):
        for path in ('/api/board', '/api/agents', '/api/runs', '/api/events', '/api/stream', '/api/file/reports/x.png', '/api/runs/abc/messages', '/api/nope'):
            status, data, _ = self.call('GET', path)
            self.assertEqual((status, data.get('error')), (401, 'login_required'), path)
        self.assertEqual(self.call('GET', '/api/session')[:2], (200, {'login': True, 'authed': False}))
        # 页面本身不拦（前端显示登录页）；接口路径的各种写法都不会绕过去
        for path in ('//api/board', '/api/board/', '/API/board', '/api/./board'):
            status, data, _ = self.call('GET', path)
            self.assertFalse(isinstance(data, dict) and 'cards' in data, path)

    def test_login_then_read_and_logout(self):
        status, data, cookies = self.login()
        self.assertEqual((status, data), (200, {'ok': True, 'login': True}))
        self.assertEqual(len(cookies), 1)
        flags = cookies[0].lower()
        self.assertIn('httponly', flags)
        self.assertIn('samesite=lax', flags)
        self.assertIn(f'max-age={auth.TTL}', flags)
        self.assertNotIn('secure', flags)  # 本地 http
        self.assertNotIn(TOKEN, cookies[0])
        cookie = {'cookie': cookies[0].split(';', 1)[0]}
        status, data, _ = self.call('GET', '/api/board', headers=cookie)
        self.assertEqual((status, data['cards'][0]['key']), (200, 'h/01'))
        self.assertEqual(self.call('GET', '/api/session', headers=cookie)[1], {'login': True, 'authed': True})
        # 改了密码：旧 cookie 立刻失效
        changed = dict(self.env, V2_BOARD_PASSWORD='a-new-password')
        self.assertEqual(self.call('GET', '/api/board', headers=cookie, env=changed)[0], 401)
        status, _, headers = self.call('POST', '/api/logout', {}, headers=cookie)
        self.assertEqual(status, 200)
        self.assertIn('max-age=0', next(v for k, v in headers if k == 'set-cookie').lower())

    def test_https_cookie_is_secure(self):
        _, _, cookies = self.login(scheme='https')
        self.assertIn('secure', cookies[0].lower())
        proxied = dict(self.env, V2_INTERVENTION_TRUST_PROXY_HEADERS='1')
        _, _, cookies = self.login(env=proxied, headers={'x-forwarded-proto': 'https', 'origin': 'https://testserver'})
        self.assertIn('secure', cookies[0].lower())

    def test_wrong_password_and_throttle(self):
        status, data, cookies = self.login('wrong')
        self.assertEqual((status, data['error'], cookies), (401, 'bad_password', []))
        self.assertEqual(len(self.app.login_throttle.fails), 1)
        for payload in ({}, {'password': TOKEN, 'extra': 1}, {'password': None}, {'password': [TOKEN]}):
            with patch.object(self.app.asyncio, 'sleep', new=_no_sleep):
                self.assertEqual(self.call('POST', '/api/login', payload)[0], 401, payload)
        with patch.object(self.app.asyncio, 'sleep', new=_no_sleep):
            for _ in range(3):
                self.login('wrong')
        status, data, cookies = self.login()  # 第 9 次：密码对也先拒绝
        self.assertEqual((status, data['error'], cookies), (429, 'too_many_attempts', []))
        self.assertEqual(len(self.app.login_throttle.fails), 8)

    def test_login_checks_the_origin(self):
        for headers in ({'origin': 'https://evil.example'}, {'origin': None}, {'sec-fetch-site': 'cross-site'}):
            status, _, cookies = self.login(headers=headers)
            self.assertEqual((status, cookies), (403, []), headers)
        self.assertEqual(self.login(headers={'content-type': 'text/plain'})[0], 415)
        self.assertEqual(self.app.login_throttle.fails, [])
        self.assertEqual(self.call('POST', '/api/logout', {}, headers={'origin': 'https://evil.example'})[0], 403)

    def test_scripts_keep_using_the_token(self):
        status, data, _ = self.call('GET', '/api/board', headers={'authorization': 'Bearer ' + TOKEN})
        self.assertEqual((status, len(data['cards'])), (200, 1))
        self.assertEqual(self.call('GET', '/api/board', headers={'authorization': 'Bearer nope'})[0], 401)

    def test_writes_accept_the_login_instead_of_the_token(self):
        payload = lambda: {'card_key': 'h/01', 'question_id': 'Q1', 'answer': '用 v2', 'expected_revision': hashlib.sha256(self.card.read_bytes()).hexdigest()}
        status, data, headers = self.call('POST', '/api/interventions/answer', payload())
        self.assertEqual((status, data['error']), (401, 'unauthorized'))  # 没登录也没令牌：原来的报错不变
        self.assertIn(('www-authenticate', 'Bearer'), headers)
        cookie = {'cookie': self.login()[2][0].split(';', 1)[0]}
        # 登录了但请求来自别的站：来源检查先拒绝
        self.assertEqual(self.call('POST', '/api/interventions/answer', payload(), headers=dict(cookie, origin='https://evil.example'))[0], 403)
        self.assertNotIn('用 v2', self.card.read_text(encoding='utf-8'))
        status, data, _ = self.call('POST', '/api/interventions/answer', payload(), headers=cookie)
        self.assertEqual(status, 200, data)
        self.assertIn('用 v2', self.card.read_text(encoding='utf-8'))
        status, data, _ = self.call('POST', '/api/interventions/stop', {'card_key': 'h/01'}, headers=cookie)
        self.assertNotEqual(status, 401, data)

    def test_no_login_without_a_password(self):
        env = {'V2_BOARD_DIR': str(self.root)}
        self.assertEqual(self.call('GET', '/api/board', env=env)[0], 200)
        self.assertEqual(self.call('GET', '/api/session', env=env)[1], {'login': False, 'authed': True})
        status, data, cookies = self.login(env=env)
        self.assertEqual((status, data, cookies), (200, {'ok': True, 'login': False}, []))
        off = dict(self.env, V2_BOARD_LOGIN='0')
        self.assertEqual(self.call('GET', '/api/board', env=off)[0], 200)
        # 没开登录时写操作照旧要令牌
        self.assertEqual(self.call('POST', '/api/interventions/stop', {'card_key': 'h/01'}, env=off)[0], 401)


async def _no_sleep(_seconds):
    return None


if __name__ == '__main__':
    unittest.main()
