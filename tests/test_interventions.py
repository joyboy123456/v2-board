"""Regression coverage for the answer-only card intervention slice."""
import asyncio
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import board_parse as bp  # noqa: E402
import interventions as iv  # noqa: E402
import setup_intervention_demo as demo_setup  # noqa: E402


CARD = '''---
id: TEST-CARD
title: Original title
status: in_progress
dispatch: "asking"
executor: Worker A
# preserve this metadata comment
---
# Original body

Keep this unrelated content.

## 待回答
- [Q1] 2026-01-01 09:00:00 CST | Worker A | First question?
- [Q2] 2026-01-01 09:01:00 CST | Worker A | Second question?

## Notes
Do not change this section.
'''


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class InterventionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'board'
        self.sprint = self.root / 'h'
        self.sprint.mkdir(parents=True)
        self.card = self.sprint / '01.md'
        self.card.write_text(CARD, encoding='utf-8')
        (self.root / iv.DEMO_MARKER).write_bytes(iv.DEMO_MARKER_CONTENT)
        self.demo_env = {'V2_BOARD_DIR': str(self.root), 'V2_INTERVENTION_MODE': 'demo'}

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, question='Q1', answer='A useful answer', revision=None, **extra):
        value = {
            'card_key': 'h/01',
            'question_id': question,
            'answer': answer,
            'expected_revision': revision or digest(self.card),
        }
        value.update(extra)
        return value

    def write(self, payload=None, mode='demo', environ=None):
        return iv.write_answer(self.root, payload or self.payload(), mode, environ or self.demo_env)

    def error(self, expected_code, callback, expected_status=409):
        with self.assertRaises(iv.InterventionError) as caught:
            callback()
        self.assertEqual(caught.exception.code, expected_code)
        self.assertEqual(caught.exception.status, expected_status)

    def test_capabilities_default_and_missing_configuration_fail_closed(self):
        self.assertEqual(iv.capabilities(self.root, {})['mode'], 'readonly')
        self.assertEqual(iv.capabilities(self.root, {'V2_INTERVENTION_MODE': 'enabled', 'V2_INTERVENTION_TOKEN': 'set'})['reason'], 'board-root-required')
        self.assertEqual(iv.capabilities(self.root, {'V2_BOARD_DIR': str(self.root), 'V2_INTERVENTION_MODE': 'enabled'})['reason'], 'token-not-configured')
        self.assertEqual(iv.capabilities(self.root, {
            'V2_BOARD_DIR': str(self.root.parent), 'V2_INTERVENTION_MODE': 'enabled', 'V2_INTERVENTION_TOKEN': 'write-token',
        })['reason'], 'board-root-mismatch')
        enabled = {'V2_BOARD_DIR': str(self.root), 'V2_INTERVENTION_MODE': 'enabled', 'V2_INTERVENTION_TOKEN': 'write-token'}
        self.assertEqual(iv.capabilities(self.root, enabled), {'mode': 'enabled'})

    def test_demo_marker_and_explicit_root_guard(self):
        self.assertEqual(iv.capabilities(self.root, self.demo_env), {'mode': 'demo'})
        (self.root / iv.DEMO_MARKER).unlink()
        self.assertEqual(iv.capabilities(self.root, self.demo_env)['reason'], 'demo-marker-missing')
        self.error('writes_disabled', lambda: self.write(environ=self.demo_env), 403)

    def test_single_answer_preserves_content_and_remains_asking(self):
        result = self.write()
        text = self.card.read_text(encoding='utf-8')
        self.assertEqual((result['remaining'], result['dispatch']), (1, 'asking'))
        self.assertEqual(result['message'], '演示答案已记录；不会连接或启动 Agent。')
        self.assertIn('# preserve this metadata comment', text)
        self.assertIn('Keep this unrelated content.', text)
        self.assertIn('## Notes\nDo not change this section.', text)
        self.assertIn('## 回答\n- [Q1] ', text)
        self.assertIn(' | user: A useful answer\n', text)
        self.assertIn('dispatch: "asking"', text)

    def test_dispatch_changes_only_after_all_pending_questions_are_answered(self):
        first = self.write(self.payload('Q1', 'one'))
        self.assertEqual((first['remaining'], first['dispatch']), (1, 'asking'))
        second = self.write(self.payload('Q2', 'two', revision=first['revision']))
        text = self.card.read_text(encoding='utf-8')
        self.assertEqual((second['remaining'], second['dispatch']), (0, 'answered'))
        self.assertIn('dispatch: "answered"', text)
        self.assertEqual(text.count(' | user:'), 2)

    def test_duplicate_retry_is_never_appended_twice(self):
        old_revision = digest(self.card)
        first = self.write(self.payload('Q1', 'once', old_revision))
        self.error('stale_revision', lambda: self.write(self.payload('Q1', 'once', old_revision)))
        self.error('already_answered', lambda: self.write(self.payload('Q1', 'once', first['revision'])))
        self.assertEqual(self.card.read_text(encoding='utf-8').count(' | user: once'), 1)

    def test_stale_revision_wrong_state_and_missing_question_conflict(self):
        payload = self.payload()
        self.card.write_text(CARD + '\nExternal update.\n', encoding='utf-8')
        self.error('stale_revision', lambda: self.write(payload))
        self.card.write_text(CARD.replace('"asking"', '"running"'), encoding='utf-8')
        self.error('wrong_dispatch_state', lambda: self.write(self.payload()))
        self.card.write_text(CARD, encoding='utf-8')
        self.error('question_not_found', lambda: self.write(self.payload('Q3')))

    def test_invalid_keys_and_ambiguous_keys_are_rejected(self):
        self.error('invalid_card_key', lambda: self.write(self.payload(**{'card_key': 'h/../outside'})), 422)
        self.error('invalid_card_key', lambda: self.write(self.payload(**{'card_key': 'h\\01'})), 422)
        (self.root / '01.md').write_text(CARD, encoding='utf-8')
        wiring = self.root / 'wiring'
        wiring.mkdir()
        (wiring / '01.md').write_text(CARD, encoding='utf-8')
        self.error('ambiguous_card_key', lambda: self.write(self.payload(**{'card_key': 'wiring/01'})))

    def test_symlinks_and_fifo_are_not_discovered_or_written(self):
        outside = Path(self.temp.name) / 'outside.md'
        outside.write_text(CARD, encoding='utf-8')
        linked = self.sprint / '02.md'
        linked.symlink_to(outside)
        self.error('card_not_found', lambda: self.write(self.payload(**{'card_key': 'h/02'})), 404)
        outside_dir = Path(self.temp.name) / 'outside-dir'
        outside_dir.mkdir()
        (outside_dir / '01.md').write_text(CARD, encoding='utf-8')
        (self.root / 'linked-sprint').symlink_to(outside_dir, target_is_directory=True)
        self.error('card_not_found', lambda: self.write(self.payload(**{'card_key': 'linked-sprint/01'})), 404)
        fifo = self.sprint / '03.md'
        os.mkfifo(fifo)
        self.error('card_not_found', lambda: self.write(self.payload(**{'card_key': 'h/03'})), 404)
        self.assertEqual(outside.read_text(encoding='utf-8'), CARD)

    def test_malformed_and_duplicate_protocol_sections_or_ids_conflict(self):
        for body, code in (
            (CARD + '\n## 待回答\n- [Q3] Another question\n', 'malformed_protocol'),
            (CARD.replace('## 待回答', '##  待回答'), 'malformed_protocol'),
            (CARD.replace('## 待回答', '## 待回答\n- [Q1] Duplicate question'), 'ambiguous_question_ids'),
        ):
            self.card.write_text(body, encoding='utf-8')
            self.error(code, lambda: self.write())

    def test_multiline_and_control_injection_is_flattened(self):
        answer = '  safe\n## 回答\n- [Q2] injected\x00\t尾部  '
        self.write(self.payload(answer=answer))
        text = self.card.read_text(encoding='utf-8')
        self.assertIn('- [Q1] ', text)
        self.assertIn(' | user: safe ## 回答 - [Q2] injected 尾部\n', text)
        self.assertEqual(sum(line.startswith('## 回答') for line in text.splitlines()), 1)
        self.assertNotIn('\x00', text)
        self.assertNotIn('\n- [Q2] injected', text)

    def test_answer_validation_and_snapshot_revision_use_same_bytes(self):
        with self.assertRaises(iv.InterventionError) as empty:
            self.write(self.payload(answer=' \n\t '))
        self.assertEqual(empty.exception.code, 'empty_answer')
        with self.assertRaises(iv.InterventionError) as long:
            self.write(self.payload(answer='x' * 4001))
        self.assertEqual(long.exception.code, 'answer_too_long')
        self.error('invalid_request', lambda: self.write(self.payload(actor='Hark')), 422)
        data, body, progress, revision = bp.metadata(self.card, 'h', include_revision=True)
        self.assertEqual(revision, digest(self.card))
        self.assertIn('First question?', body)

    def test_dispatcher_parser_reads_board_written_answers(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dispatcher'))
        import cards as dispatcher_cards
        iv.write_answer(self.root, self.payload('Q1', 'first | with pipe'), 'demo', self.demo_env)
        meta, body = dispatcher_cards.parse(self.card.read_text(encoding='utf-8'))
        self.assertEqual(meta['dispatch'], 'asking')
        self.assertEqual(list(dispatcher_cards.open_questions(body)), ['Q2'])
        iv.write_answer(self.root, self.payload('Q2', 'second'), 'demo', self.demo_env)
        meta, body = dispatcher_cards.parse(self.card.read_text(encoding='utf-8'))
        self.assertEqual(meta['dispatch'], 'answered')
        self.assertFalse(dispatcher_cards.open_questions(body))
        self.assertEqual(set(dispatcher_cards.answers(body)), {'Q1', 'Q2'})

    def test_answer_heading_at_end_of_file_without_newline(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dispatcher'))
        import cards as dispatcher_cards
        self.card.write_text(CARD.split('## Notes')[0].rstrip('\n') + '\n\n## 回答', encoding='utf-8')
        iv.write_answer(self.root, self.payload('Q1', 'first'), 'demo', self.demo_env)
        text = self.card.read_text(encoding='utf-8')
        self.assertIn('## 回答\n- [Q1] ', text)
        self.assertEqual(set(dispatcher_cards.answers(dispatcher_cards.parse(text)[1])), {'Q1'})

    def test_answer_history_is_visible_after_questions_are_answered(self):
        result = self.write(self.payload('Q1', 'saved answer'))
        history = iv.answer_history(self.card.read_text(encoding='utf-8').split('---\n', 2)[-1])
        self.assertEqual(history[0]['id'], 'Q1')
        self.assertEqual(history[0]['actor'], 'user')
        self.assertEqual(history[0]['answer'], 'saved answer')
        self.assertEqual(result['revision'], digest(self.card))

    def test_legacy_answer_containing_separator_is_not_cut(self):
        body = '## 回答\n- [Q1] 用方案 A | 不要方案 B\n- [Q2] 2026-01-01 09:05:00 CST | user: 好 | 就这样\n'
        history = iv.answer_history(body)
        self.assertEqual((history[0]['time'], history[0]['answer']), ('', '用方案 A | 不要方案 B'))
        self.assertEqual((history[1]['time'], history[1]['actor'], history[1]['answer']),
                         ('2026-01-01 09:05:00 CST', 'user', '好 | 就这样'))

    def test_failed_write_restores_the_original_card(self):
        original = self.card.read_bytes()
        real_write, calls = os.write, []

        def flaky(fd, data):
            calls.append(len(data))
            if len(calls) == 1:
                real_write(fd, bytes(data[:40]))
                raise OSError('disk full')
            return real_write(fd, data)

        with patch.object(iv.os, 'write', flaky):
            self.error('card_unavailable', self.write, 400)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.card.read_bytes(), original)


class DemoSetupGuardTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / 'home'
        self.home.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_custom_root_needs_explicit_force_and_unknown_files_are_never_overwritten(self):
        root = self.home / 'preview'
        with patch('pathlib.Path.home', return_value=self.home):
            with self.assertRaises(SystemExit):
                demo_setup.validate_root(root, custom=True, force=False)
            root.mkdir()
            (root / 'user-data.json').write_text('untouched', encoding='utf-8')
            with self.assertRaises(SystemExit):
                demo_setup.validate_root(root, custom=True, force=True)
            self.assertEqual((root / 'user-data.json').read_text(encoding='utf-8'), 'untouched')

    def test_only_supported_marked_demo_roots_can_be_rebuilt(self):
        root = self.home / 'preview'
        (root / 'h').mkdir(parents=True)
        (root / demo_setup.MARKER).write_bytes(demo_setup.MARKER_CONTENT)
        (root / 'h/01.md').write_text('synthetic', encoding='utf-8')
        with patch('pathlib.Path.home', return_value=self.home):
            self.assertEqual(demo_setup.validate_root(root, custom=True, force=True), root)
            (root / 'h/unexpected.md').write_text('user data', encoding='utf-8')
            with self.assertRaises(SystemExit):
                demo_setup.validate_root(root, custom=True, force=True)

    def test_setup_generates_only_synthetic_preview_cards(self):
        root = self.home / 'preview'
        output = io.StringIO()
        with patch('pathlib.Path.home', return_value=self.home), \
                patch('sys.argv', ['setup_intervention_demo.py', '--root', str(root), '--force']), \
                contextlib.redirect_stdout(output):
            demo_setup.main()
        self.assertEqual((root / demo_setup.MARKER).read_bytes(), demo_setup.MARKER_CONTENT)
        self.assertEqual(len(list((root / 'h').glob('*.md'))), 5)
        self.assertTrue((root / demo_setup.SHOT).read_bytes().startswith(b'\x89PNG'))
        self.assertEqual([p.name for p in (root / 'reports').glob('*.md')], ['2026-01-01-1000-demo.md'])
        self.assertIn('## 待回答', (root / 'h/01.md').read_text(encoding='utf-8'))
        self.assertIn('## 回答', (root / 'h/04.md').read_text(encoding='utf-8'))
        self.assertIn('ISOLATED_DEMO_ROOT=', output.getvalue())

    def test_unmarked_nonempty_roots_and_special_files_are_rejected(self):
        root = self.home / 'preview'
        (root / 'h').mkdir(parents=True)
        (root / 'h/01.md').write_text('could be real data', encoding='utf-8')
        with patch('pathlib.Path.home', return_value=self.home):
            with self.assertRaises(SystemExit):
                demo_setup.validate_root(root, custom=True, force=True)
            (root / demo_setup.MARKER).write_bytes(b'wrong marker\n')
            with self.assertRaises(SystemExit):
                demo_setup.validate_root(root, custom=True, force=True)
            (root / demo_setup.MARKER).write_bytes(demo_setup.MARKER_CONTENT)
            self.assertEqual(demo_setup.validate_root(root, custom=True, force=True), root)
            os.mkfifo(root / 'h/02.md')
            with self.assertRaises(SystemExit):
                demo_setup.validate_root(root, custom=True, force=True)
        self.assertEqual((root / 'h/01.md').read_text(encoding='utf-8'), 'could be real data')

    def test_roots_outside_home_or_inside_repository_are_rejected(self):
        with patch('pathlib.Path.home', return_value=self.home):
            with self.assertRaises(SystemExit):
                demo_setup.validate_root(Path(self.temp.name) / 'outside', custom=True, force=True)
        with patch('pathlib.Path.home', return_value=demo_setup.REPO_ROOT.parent):
            repo_child = demo_setup.REPO_ROOT / 'not-a-demo'
            with self.assertRaises(SystemExit):
                demo_setup.validate_root(repo_child, custom=True, force=True)


class InterventionApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as board_app
        cls.app = board_app

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'board'
        sprint = self.root / 'h'
        sprint.mkdir(parents=True)
        self.card = sprint / '01.md'
        self.card.write_text(CARD, encoding='utf-8')
        (self.root / iv.DEMO_MARKER).write_bytes(iv.DEMO_MARKER_CONTENT)
        self.original_board = self.app.BOARD
        self.app.BOARD = self.root
        bp.BOARD = self.root
        self.app.hub.board = {
            'cards': [{
                'key': 'h/01', 'dispatch': 'asking', 'questions': [], 'answerHistory': [],
                'cardRevision': digest(self.card), 'body': '',
            }],
            'version': 'before', 'dispatch': {'slots': 0}, 'intervention': {'mode': 'readonly'},
        }
        self.app.hub.tick = 0

    def tearDown(self):
        self.app.BOARD = self.original_board
        bp.BOARD = self.original_board
        self.temp.cleanup()

    def call(self, payload=None, headers=None, raw_body=None, content_type='application/json', scheme='http'):
        body = raw_body if raw_body is not None else json.dumps(payload or {}).encode()
        values = {'host': 'testserver', 'origin': 'http://testserver', 'sec-fetch-site': 'same-origin', 'content-type': content_type}
        if headers:
            values.update(headers)
        messages = []
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {'type': 'http.disconnect'}
            sent = True
            return {'type': 'http.request', 'body': body, 'more_body': False}

        async def send(message):
            messages.append(message)

        scope = {
            'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
            'method': 'POST', 'scheme': scheme, 'path': '/api/interventions/answer',
            'raw_path': b'/api/interventions/answer', 'query_string': b'', 'root_path': '',
            'headers': [(key.lower().encode(), str(value).encode()) for key, value in values.items()],
            'client': ('127.0.0.1', 50000), 'server': ('testserver', 80),
        }
        asyncio.run(self.app.app(scope, receive, send))
        start = next(message for message in messages if message['type'] == 'http.response.start')
        response = b''.join(message.get('body', b'') for message in messages if message['type'] == 'http.response.body')
        return start['status'], json.loads(response or b'{}'), dict(start['headers'])

    def request_payload(self):
        return {'card_key': 'h/01', 'question_id': 'Q1', 'answer': 'API answer', 'expected_revision': hashlib.sha256(self.card.read_bytes()).hexdigest()}

    def test_default_readonly_is_denied_and_snapshot_capability_is_reported(self):
        with patch.dict(os.environ, {'V2_BOARD_DIR': str(self.root)}, clear=True):
            status, body, _ = self.call(self.request_payload())
            self.assertEqual(status, 503)
            self.assertEqual(body['error'], 'writes_disabled')
            self.assertEqual(iv.capabilities(self.root)['mode'], 'readonly')
            with patch.dict(os.environ, {
                'V2_BOARD_DIR': str(self.root), 'V2_INTERVENTION_MODE': 'enabled',
            }, clear=True):
                status, body, _ = self.call(self.request_payload())
            self.assertEqual((status, body['error']), (503, 'writes_disabled'))
            with patch.object(self.app.bp, 'collect', return_value=([], [])), \
                    patch.object(self.app.rr, 'events', return_value=[]), \
                    patch.object(self.app.rr, 'card_summary', return_value={}), \
                    patch.object(self.app, 'project_timeline', return_value=[]), \
                    patch.object(self.app.acceptance, 'collect', return_value=([], [])), \
                    patch.object(self.app, 'cross_links', return_value=[]), \
                    patch.object(self.app, 'dispatch_slots', return_value=0):
                snapshot = self.app.snapshot()
            self.assertEqual(snapshot['intervention']['mode'], 'readonly')
            demo_env = {'V2_BOARD_DIR': str(self.root), 'V2_INTERVENTION_MODE': 'demo'}
            with patch.dict(os.environ, demo_env, clear=True):
                snapshot = self.app.snapshot()
            self.assertEqual(snapshot['intervention']['mode'], 'demo')

    def test_origin_fetch_metadata_content_type_and_forwarded_host_guards(self):
        env = {'V2_BOARD_DIR': str(self.root), 'V2_INTERVENTION_MODE': 'demo'}
        with patch.dict(os.environ, env, clear=True):
            status, _, _ = self.call(self.request_payload(), headers={'sec-fetch-site': 'cross-site'})
            self.assertEqual(status, 403)
            status, _, _ = self.call(self.request_payload(), headers={'origin': 'https://evil.example'})
            self.assertEqual(status, 403)
            status, _, _ = self.call(self.request_payload(), content_type='text/plain')
            self.assertEqual(status, 415)
            env.update(V2_INTERVENTION_TRUST_PROXY_HEADERS='1')
            with patch.dict(os.environ, env, clear=True):
                status, _, _ = self.call(self.request_payload(), headers={
                    'host': 'internal:8000', 'origin': 'https://preview.example',
                    'x-forwarded-host': 'preview.example', 'x-forwarded-proto': 'https',
                }, scheme='http')
                self.assertEqual(status, 200)

    def test_enabled_mode_token_failures_and_success(self):
        payload = self.request_payload()
        base = {'V2_BOARD_DIR': str(self.root), 'V2_INTERVENTION_MODE': 'enabled', 'V2_INTERVENTION_TOKEN': 'correct-token'}
        with patch.dict(os.environ, base, clear=True):
            status, body, _ = self.call(payload, headers={'authorization': 'Bearer wrong-token'})
            self.assertEqual((status, body['error']), (401, 'unauthorized'))
            status, body, _ = self.call(payload)
            self.assertEqual((status, body['error']), (401, 'unauthorized'))
            with patch.object(self.app, 'snapshot', side_effect=AssertionError('intervention must not launch snapshot subprocesses')):
                status, body, _ = self.call(payload, headers={'authorization': 'Bearer correct-token'})
            self.assertEqual(status, 200)
            self.assertEqual(body['dispatch'], 'asking')
            self.assertEqual(body['remaining'], 1)
            self.assertIn('等待 dispatcher', body['message'])
            self.assertEqual(body['mode'], 'enabled')
            card = self.app.hub.board['cards'][0]
            self.assertEqual(card['cardRevision'], body['revision'])
            self.assertEqual(card['dispatch'], 'asking')
            self.assertEqual(card['answerHistory'][0]['answer'], 'API answer')

    def test_demo_requires_marker_and_records_without_auth(self):
        env = {'V2_BOARD_DIR': str(self.root), 'V2_INTERVENTION_MODE': 'demo'}
        with patch.dict(os.environ, env, clear=True):
            status, body, _ = self.call(self.request_payload())
            self.assertEqual(status, 200)
            self.assertEqual(body['mode'], 'demo')
            self.assertIn('不会连接或启动 Agent', body['message'])
            marker = self.root / iv.DEMO_MARKER
            marker.unlink()
            status, body, _ = self.call(self.request_payload())
            self.assertEqual((status, body['error']), (503, 'writes_disabled'))

    def test_duplicate_json_keys_are_rejected(self):
        env = {'V2_BOARD_DIR': str(self.root), 'V2_INTERVENTION_MODE': 'demo'}
        raw = b'{"card_key":"h/01","card_key":"h/01"}'
        with patch.dict(os.environ, env, clear=True):
            status, body, _ = self.call(raw_body=raw)
            self.assertEqual((status, body['error']), (400, 'invalid_json'))

    def test_deeply_nested_json_is_a_client_error(self):
        env = {'V2_BOARD_DIR': str(self.root), 'V2_INTERVENTION_MODE': 'demo'}
        real_loads, raised = json.loads, []

        def too_deep(*args, **kwargs):
            if not raised:
                raised.append(True)
                raise RecursionError
            return real_loads(*args, **kwargs)

        # 3.12 的 json 解析在 16 KB 请求体内不会递归溢出，这里直接模拟旧版本的行为
        with patch.dict(os.environ, env, clear=True), patch.object(iv.json, 'loads', too_deep):
            status, body, _ = self.call(payload=self.request_payload())
            self.assertEqual((status, body['error']), (400, 'invalid_json'))


if __name__ == '__main__':
    unittest.main()
