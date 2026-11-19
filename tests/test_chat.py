import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
import chat  # noqa: E402
import interventions as iv  # noqa: E402


def card(dispatch='done', extra='', body='实现功能。\n'):
    return f'---\nid: "F1"\ntitle: "测试"\nstatus: "review"\nexecutor: "codex-astra"\ndispatch: "{dispatch}"\n{extra}---\n\n{body}'


class ChatTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'fix').mkdir()
        self.path = self.root / 'fix' / 'F1.md'
        self.config = self.root / 'dispatch.json'
        self.env = {'V2_INTERVENTION_MODE': 'enabled', 'V2_BOARD_DIR': str(self.root), 'V2_INTERVENTION_TOKEN': 'tok',
                    'V2_REPAIR_ENABLED': '1', 'V2_REPAIR_BASE': 'main', 'V2_REPAIR_REPO': '/srv/app', 'DISPATCH_CONFIG': str(self.config)}
        self.config.write_text(json.dumps({'board': str(self.root), 'executors': {
            'codex-astra': {'agent': 'Codex gpt-6.2-astra high', 'cmd': ['codex', 'exec', '-m', 'gpt-6.2-astra', '{prompt}'], 'resume': ['codex', 'exec', 'resume', '{session}', '{prompt}']},
            'kiro': {'agent': 'Kiro', 'cmd': ['kiro-cli', 'chat', '{prompt}']}}}))

    def send(self, text='把按钮改成蓝色', **over):
        payload = dict(card_key='fix/F1', message=text, expected_revision=hashlib.sha256(self.path.read_bytes()).hexdigest())
        payload.update(over)
        return chat.write_message(self.root, payload, 'enabled', self.env)

    def code(self, call):
        with self.assertRaises(iv.InterventionError) as caught:
            call()
        return caught.exception.code

    def test_message_reopens_finished_session(self):
        self.path.write_text(card('done'))
        result = self.send('把按钮\n改成  蓝色')
        self.assertEqual((result['message_id'], result['dispatch']), ('M1', 'answered'))
        text = self.path.read_text()
        self.assertIn('dispatch: "answered"', text)
        self.assertRegex(text, r'## 留言\n- \[M1\] \d{4}-\d\d-\d\d \d\d:\d\d:\d\d CST \| user: 把按钮 改成 蓝色\n')
        self.assertEqual(result['revision'], hashlib.sha256(self.path.read_bytes()).hexdigest())
        log = chat.chat_log(result['card_meta'], result['card_body'])
        self.assertEqual([(x['kind'], x['id'], x['delivered']) for x in log], [('message', 'M1', False)])

    def test_states(self):
        for state, after in (('queued', 'queued'), ('running', 'running'), ('asking', 'asking'), ('answered', 'answered'), ('held', 'held'), ('failed', 'answered')):
            self.path.write_text(card(state))
            self.assertEqual(self.send()['dispatch'], after, state)
        self.path.write_text(card('closed'))
        self.assertEqual(self.code(self.send), 'session_closed')
        self.path.write_text('---\nid: "F1"\ntitle: "旧卡"\nstatus: "review"\n---\n\n正文\n')
        self.assertEqual(self.code(self.send), 'not_dispatched')
        self.assertNotIn('留言', self.path.read_text())

    def stop(self, **over):
        payload = dict(card_key='fix/F1')
        payload.update(over)
        return chat.request_stop(self.root, payload, 'enabled', self.env)

    def test_stop_records_a_request_and_leaves_the_state_to_the_dispatcher(self):
        for state in ('running', 'queued', 'answered'):
            self.path.write_text(card(state))
            result = self.stop()
            self.assertEqual((result['dispatch'], result['stopping']), (state, True), state)
            text = self.path.read_text()
            self.assertRegex(text, r'dispatch: "%s"\nstop: "\d{4}-\d\d-\d\d \d\d:\d\d:\d\d CST"\n---\n\n实现功能。\n$' % state)
            self.assertEqual(result['revision'], hashlib.sha256(self.path.read_bytes()).hexdigest())
            # 再点一次不会再写一遍
            again = self.stop()
            self.assertEqual((again['revision'], again['stopping']), (result['revision'], True))
            self.assertEqual(self.path.read_text(), text)

    def test_stop_refuses_sessions_that_are_not_running(self):
        for state in ('asking', 'done', 'failed', 'held'):
            self.path.write_text(card(state))
            before = self.path.read_bytes()
            self.assertEqual(self.code(self.stop), 'not_running', state)
            self.assertEqual(self.path.read_bytes(), before)
        self.path.write_text(card('closed'))
        self.assertEqual(self.code(self.stop), 'session_closed')
        self.path.write_text('---\nid: "F1"\ntitle: "旧卡"\nstatus: "review"\n---\n\n正文\n')
        self.assertEqual(self.code(self.stop), 'not_dispatched')
        self.path.write_text(card('running'))
        before = self.path.read_bytes()
        self.assertEqual(self.code(lambda: self.stop(expected_revision='0' * 64)), 'invalid_request')
        self.assertEqual(self.code(lambda: self.stop(card_key='../fix/F1')), 'invalid_card_key')
        self.assertEqual(self.code(lambda: self.stop(card_key='fix/NOPE')), 'card_not_found')
        self.assertEqual(self.code(lambda: chat.request_stop(self.root, dict(card_key='fix/F1'), 'readonly', self.env)), 'writes_disabled')
        self.assertEqual(self.path.read_bytes(), before)

    def test_stop_in_demo_only_holds_demo_cards(self):
        (self.root / iv.DEMO_MARKER).write_bytes(iv.DEMO_MARKER_CONTENT)
        env = {'V2_INTERVENTION_MODE': 'demo', 'V2_BOARD_DIR': str(self.root)}
        self.path.write_text(card('running'))
        self.assertEqual(self.code(lambda: chat.request_stop(self.root, dict(card_key='fix/F1'), 'demo', env)), 'not_demo_card')
        self.path.write_text(card('running', 'demo: true\n'))
        result = chat.request_stop(self.root, dict(card_key='fix/F1'), 'demo', env)
        self.assertEqual((result['dispatch'], result['stopping']), ('held', False))
        self.assertNotIn('stop:', self.path.read_text())

    def test_message_reopens_a_stopped_session_but_not_a_hand_held_one(self):
        self.path.write_text(card('held', 'stopped: "2026-11-19 23:00:00 CST"\n'))
        self.assertEqual(self.send()['dispatch'], 'answered')
        self.path.write_text(card('held'))
        self.assertEqual(self.send()['dispatch'], 'held')

    def test_appends_in_order_and_keeps_other_sections(self):
        self.path.write_text(card('running', 'delivered: "1"\n', '正文\n\n## 留言\n- [M1] 2026-11-19 10:00:00 CST | user: 第一条\n\n## 回答\n- [Q1] 2026-11-19 09:00:00 CST | user: 用乙\n\n## 待回答\n- [Q1] 2026-11-19 08:00:00 CST | Codex | 用哪种？ | 选项：甲 / 乙\n'))
        result = self.send('第二条')
        text = self.path.read_text()
        self.assertIn('| user: 第一条\n- [M2] ', text)
        self.assertIn('user: 第二条\n\n## 回答\n- [Q1]', text)
        log = chat.chat_log(result['card_meta'], result['card_body'])
        self.assertEqual([(x['kind'], x['id']) for x in log], [('question', 'Q1'), ('answer', 'Q1'), ('message', 'M1'), ('message', 'M2')])
        self.assertEqual(log[0]['options'], ['甲', '乙'])
        self.assertEqual((log[0]['open'], log[0]['actor'], log[0]['text']), (False, 'Codex', '用哪种？'))
        self.assertEqual([x['delivered'] for x in log[2:]], [True, False])

    def test_rejects_bad_input(self):
        self.path.write_text(card('done'))
        before = self.path.read_bytes()
        self.assertEqual(self.code(lambda: self.send(expected_revision='0' * 64)), 'stale_revision')
        self.assertEqual(self.code(lambda: self.send('   \n ')), 'empty_message')
        self.assertEqual(self.code(lambda: self.send('字' * 4001)), 'message_too_long')
        self.assertEqual(self.code(lambda: self.send(card_key='../etc/passwd')), 'invalid_card_key')
        self.assertEqual(self.code(lambda: self.send(card_key='fix/NOPE')), 'card_not_found')
        self.assertEqual(self.code(lambda: chat.write_message(self.root, {'card_key': 'fix/F1'}, 'enabled', self.env)), 'invalid_request')
        self.assertEqual(self.code(lambda: chat.write_message(self.root, {}, 'enabled', dict(self.env, V2_INTERVENTION_MODE='readonly'))), 'writes_disabled')
        self.assertEqual(self.path.read_bytes(), before)

    def test_demo_only_touches_demo_cards(self):
        (self.root / iv.DEMO_MARKER).write_bytes(iv.DEMO_MARKER_CONTENT)
        env = {'V2_INTERVENTION_MODE': 'demo', 'V2_BOARD_DIR': str(self.root)}
        self.path.write_text(card('done'))
        payload = lambda: dict(card_key='fix/F1', message='你好', expected_revision=hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(self.code(lambda: chat.write_message(self.root, payload(), 'demo', env)), 'not_demo_card')
        self.path.write_text(card('done', 'demo: true\n'))
        self.assertEqual(chat.write_message(self.root, payload(), 'demo', env)['dispatch'], 'done')  # 演示不唤醒任何 Agent

    def test_executor_agents(self):
        agents = chat.executor_agents(self.root, 'enabled', self.env)
        self.assertEqual(agents['codex-astra'], dict(agent='codex-gpt-6.2-astra', label='Codex gpt-6.2-astra high', resume=True))
        self.assertEqual((agents['kiro']['agent'], agents['kiro']['resume']), ('kiro', False))
        self.assertEqual(chat.executor_agents(self.root, 'demo', self.env), {})
        self.config.write_text(json.dumps({'board': '/elsewhere', 'executors': {'x': {'cmd': []}}}))
        self.assertEqual(chat.executor_agents(self.root, 'enabled', self.env), {})

    def test_create_session(self):
        made = chat.create_session(self.root, {'executor': 'kiro', 'message': '检查首页为什么白屏\n先别改代码'}, 'enabled', self.env)
        self.assertRegex(made['card_key'], r'^chat/CHAT-\d{4}-\d{4}-[0-9A-F]{6}$')
        text = (self.root / (made['card_key'] + '.md')).read_text()
        for part in ('dispatch: "queued"', 'executor: "kiro"', 'repo: "/srv/app"', 'base: "main"', 'title: "对话：检查首页为什么白屏"', '## 任务\n检查首页为什么白屏\n先别改代码\n'):
            self.assertIn(part, text)
        self.assertEqual(self.code(lambda: chat.create_session(self.root, {'executor': 'nope', 'message': 'x'}, 'enabled', self.env)), 'unknown_executor')
        self.assertEqual(self.code(lambda: chat.create_session(self.root, {'executor': 'kiro', 'message': ' '}, 'enabled', self.env)), 'empty_message')
        self.assertEqual(self.code(lambda: chat.create_session(self.root, {'executor': 'kiro', 'message': 'x'}, 'enabled', dict(self.env, V2_REPAIR_BASE=''))), 'missing_base')
        self.assertEqual(self.code(lambda: chat.create_session(self.root, {'executor': 'kiro', 'message': 'x'}, 'enabled', dict(self.env, V2_REPAIR_ENABLED='0'))), 'sessions_disabled')
        self.assertEqual(len(list((self.root / 'chat').glob('*'))), 1)


if __name__ == '__main__':
    unittest.main()
