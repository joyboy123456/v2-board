import concurrent.futures
import importlib.machinery
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'backend'))
from signals import atomic_json, append_jsonl, epoch, event, load_json, message, normalize, now, parse_line, redact, rows, update_run
from runs import agents, all_runs, backfill, log_tail, messages, reconcile, Mirror


def concurrent_writer(args):
    board, number = args
    for i in range(40):
        event(board, 'test', writer=number, index=i)


def command(*args, cwd=None, env=None, check=True):
    return subprocess.run(['nice', '-n', '19', *map(str, args)], cwd=cwd, env=env, text=True, capture_output=True, check=check)


def init_repo(path):
    path.mkdir(exist_ok=True)
    command('git', 'init', '-q', '-b', 'main', path)
    command('git', '-C', path, 'config', 'user.name', 'Test')
    command('git', '-C', path, 'config', 'user.email', 'test@example.test')
    (path / 'file').write_text('first')
    command('git', '-C', path, 'add', '.')
    command('git', '-C', path, 'commit', '-qm', '初始提交')


class SignalsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.board = self.path / 'board'
        self.board.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_events_and_partial_tail(self):
        with concurrent.futures.ProcessPoolExecutor(max_workers=6) as pool:
            list(pool.map(concurrent_writer, [(str(self.board), n) for n in range(6)]))
        path = self.board / 'events/events.jsonl'
        data = list(rows(path))
        self.assertEqual(len(data), 240)
        self.assertEqual([r['seq'] for r in data], list(range(1, 241)))
        self.assertEqual(len({r['id'] for r in data}), 240)
        with path.open('a') as f:
            f.write('{"partial')
        event(self.board, 'recovered')
        self.assertEqual(list(rows(path))[-1]['seq'], 241)

    def test_unicode_large_event_tail(self):
        for i in range(300):
            event(self.board, 'test', summary='正在修改功能' * 100)
        data = list(rows(self.board / 'events/events.jsonl'))
        self.assertEqual(len(data), 300)
        self.assertEqual(data[-1]['seq'], 300)

    def test_concurrent_heartbeat_message_progress(self):
        update_run(self.board, 'one', dict(id='one', state='running', tool_calls=0))
        def write(i):
            if i % 2:
                update_run(self.board, 'one', {'last_seen_at': now()})
            else:
                message(self.board, 'one', 'tool_use', 'test')
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(80)))
        self.assertEqual(load_json(self.board / 'runs/one.json')['tool_calls'], 40)
        self.assertEqual([r['seq'] for r in messages(self.board, 'one')], list(range(1, 41)))
        self.assertEqual(len(messages(self.board, 'one', 30)), 10)

    def test_explicit_progress_precedence(self):
        update_run(self.board, 'one', dict(id='one', state='running', progress={'summary': '明确进度'}, current_activity='明确进度'))
        message(self.board, 'one', 'text', '后续输出')
        run = load_json(self.board / 'runs/one.json')
        self.assertEqual(run['current_activity'], '明确进度')
        self.assertEqual(run['last_message'], '后续输出')

    def test_pid_and_stale_heartbeat(self):
        record = dict(id='one', state='running', pid=99999999, started_at=now(), last_seen_at=now())
        update_run(self.board, 'one', record)
        reconcile(self.board)
        self.assertEqual(load_json(self.board / 'runs/one.json')['reason'], 'process_gone')
        update_run(self.board, 'one', dict(record, pid=os.getpid()))
        reconcile(self.board)
        self.assertEqual(load_json(self.board / 'runs/one.json')['state'], 'running')
        reconcile(self.board, check_pid=False, current=time.time() + 181)
        self.assertEqual(load_json(self.board / 'runs/one.json')['state'], 'lost')
        update_run(self.board, 'one', {'last_seen_at': now()})
        reconcile(self.board, check_pid=False)
        self.assertEqual(load_json(self.board / 'runs/one.json')['state'], 'running')
        update_run(self.board, 'one', {'state': 'completed'})
        reconcile(self.board, current=time.time() + 1000)
        self.assertEqual(load_json(self.board / 'runs/one.json')['state'], 'completed')

    def test_normalization(self):
        for raw, expected in [('Codex gpt-6-astra xhigh', ('Codex', 'gpt-6-astra')), ('codex GPT-6.1-SOL（Hark 指派）', ('Codex', 'gpt-6.1-sol')), ('Claude Code fable high', ('Claude Code', 'fable')), ('claude opus', ('Claude Code', 'opus')), ('Kiro gpt-5.6-sol', ('Kiro', 'GPT 5.6')), ('ZCode glm-5.3', ('zcode', 'GLM-5.3')), ('Devin swe-2-high', ('Devin', 'swe-2-high')), ('Grok', ('Grok', 'grok'))]:
            result = normalize(raw)
            self.assertEqual((result['name'], result['model']), expected)
        self.assertEqual(normalize('Codex gpt-6-astra', 'gpt-6.1-sol')['model'], 'gpt-6.1-sol')
        self.assertEqual(normalize('Codex gpt-6-astra xhigh')['reasoning'], 'xhigh')

    def test_redact_and_log(self):
        raw = '\x1b[31mapi_key="TOP_SECRET" Authorization: Bearer abc.def.xyz sk-proj-123456789012 ghp_abcdef123456 https://name:password@example.com/path\x1b[0m'
        clean = redact(raw)
        for secret in ('TOP_SECRET', 'abc.def.xyz', 'sk-proj-123456789012', 'ghp_abcdef123456', 'name:password', '\x1b'):
            self.assertNotIn(secret, clean)
        self.assertEqual(redact({'password': 'value'})['password'], '[REDACTED]')
        for name in ('TOKEN', 'AWS_SECRET_ACCESS_KEY', 'CLIENT_SECRET', 'PRIVATE_KEY'):
            self.assertNotIn('sensitive_value', redact(f'{name}=sensitive_value'))
        self.assertNotIn('private-body', redact('-----BEGIN RSA PRIVATE KEY-----\nprivate-body\n-----END RSA PRIVATE KEY-----'))
        folder = self.board / 'runs'
        folder.mkdir()
        log = folder / 'one.log'
        log.write_text('\n'.join(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': f'line {i} api_key=SECRET'}}) for i in range(90)))
        tail = log_tail({'id': 'one', 'log': str(log)}, self.board)
        self.assertEqual(len(tail.splitlines()), 80)
        self.assertIn('line 89', tail)
        self.assertNotIn('SECRET', tail)
        self.assertNotIn('item.completed', tail)
        log.write_text('-----BEGIN RSA PRIVATE KEY-----\n' + 'private-body\n' * 90 + '-----END RSA PRIVATE KEY-----\n')
        self.assertNotIn('private-body', log_tail({'id': 'one'}, self.board))

    def test_structured_parsers(self):
        examples = [({'type': 'item.started', 'item': {'type': 'command_execution', 'command': 'git status'}}, 'tool_use'), ({'type': 'item.completed', 'item': {'type': 'file_change', 'changes': [{'path': 'app.py'}]}}, 'file_change'), ({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Read', 'input': {'path': 'x'}}]}}, 'tool_use'), ({'type': 'assistant', 'message': {'content': [{'type': 'thinking', 'thinking': 'plan'}]}}, 'thinking'), ({'type': 'turn.failed', 'error': {'message': 'bad'}}, 'error')]
        for obj, expected in examples:
            self.assertEqual(parse_line(json.dumps(obj))[0][0], expected)
        self.assertEqual(parse_line('plain line')[0][0], 'text')
        self.assertEqual(parse_line('{"type":"ignored"}'), [])

    def test_backfill_idempotent_pairing(self):
        (self.board / 'A.md').write_text('---\nassignee: "Codex gpt-6.1-sol"\nbranch: "test/a"\n---\n')
        (self.board / 'A-progress.md').write_text('## 汇报\n- 2026-11-17 10:00:00 CST | Codex gpt-6.1-sol | commit `123` | 【开始】开工\n- 2026-11-17 10:02:00 CST | Codex gpt-6.1-sol | commit `456` | 【开发完成（待 Hark 验收）】退出码 0，交付\n- 2026-11-17 11:00:00 CST | Codex gpt-6.1-sol | commit `456` | 【开始】又一次\n- 2026-11-17 11:01:00 CST | Codex gpt-6.1-sol | commit `456` | 【超时】退出码 124\n')
        (self.board / 'BOARD.md').write_text('## 自动汇报（Agent 钩子）\n- 2026-11-17 10:00:00 CST | A | Codex gpt-6.1-sol | commit `123` | 【开始】开工\n')
        self.assertEqual(backfill(self.board), 2)
        records = all_runs(self.board)
        self.assertEqual(records[1]['duration_seconds'], 120)
        self.assertEqual(records[0]['state'], 'timeout')
        self.assertTrue(all(r['backfilled'] for r in records))
        self.assertEqual(backfill(self.board), 0)

    def test_run_and_card_separate(self):
        update_run(self.board, 'one', dict(id='one', card='A', agent='Codex gpt-6.1-sol', state='completed', started_at=now(), ended_at=now(), commits=2, changed_files=3))
        card = dict(id='A', key='h/A', status='review', assignee='Codex gpt-6.1-sol')
        group = next(g for g in agents(self.board, [card])['agents'] if g['model'] == 'gpt-6.1-sol')
        self.assertEqual(group['totals']['completed_cards'], 0)
        self.assertEqual(group['success_rate'], 1)
        card['status'] = 'accepted'
        group = next(g for g in agents(self.board, [card])['agents'] if g['model'] == 'gpt-6.1-sol')
        self.assertEqual(group['totals']['completed_cards'], 1)


class HookAndWrapperTest(unittest.TestCase):
    setUp = SignalsTest.setUp
    tearDown = SignalsTest.tearDown
    # Real temporary repositories; never mutate business code or the live service.
    def test_hook_deadline_failure_chaining_and_restore(self):
        repo = self.path / 'repo'
        init_repo(repo)
        hook = repo / '.git/hooks/post-commit'
        hook.write_text('#!/bin/sh\necho original > original-called\nsleep 3\nexit 12\n')
        hook.chmod(0o755)
        command('sh', REPO / 'scripts/install-hooks.sh', repo, '--board', self.board)
        original = hook.read_text()
        command('sh', REPO / 'scripts/install-hooks.sh', repo, '--board', self.board)
        self.assertEqual(original, hook.read_text())
        started = time.monotonic()
        self.assertEqual(command(hook, cwd=repo).returncode, 0)
        self.assertLess(time.monotonic() - started, .2)
        (repo / 'file').write_text('second')
        command('git', '-C', repo, 'add', '.')
        command('git', '-C', repo, 'commit', '-qm', '来自钩子的提交')
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not any(e.get('subject') == '来自钩子的提交' for e in rows(self.board / 'events/events.jsonl')):
            time.sleep(.02)
        records = list(rows(self.board / 'events/events.jsonl'))
        self.assertTrue(any(e.get('subject') == '来自钩子的提交' and e.get('branch') == 'main' for e in records), records)
        self.assertTrue((repo / 'original-called').exists())
        # Broken collector storage never fails Git.
        command('sh', REPO / 'scripts/install-hooks.sh', repo, '--board', '/dev/null/unwritable')
        command('git', '-C', repo, 'commit', '--allow-empty', '-qm', '错误静默')
        command('sh', REPO / 'scripts/uninstall-hooks.sh', repo)
        self.assertIn('sleep 3', hook.read_text())
        self.assertFalse((repo / '.git/hooks/post-merge').exists())

    def run_agent(self, cmd, final=None, agent='Codex gpt-6.1-sol'):
        repo = self.path / 'repo'
        if not repo.exists():
            init_repo(repo)
        (self.board / 'A.md').write_text('---\nid: "A"\nstatus: "assigned"\nupdated: ""\n---\n任务\n')
        env = dict(os.environ, BOARD_ROOT=str(self.board), V2_BOARD_DIR=str(self.board))
        args = ['nice', '-n', '19', str(REPO / 'scripts/agent-run'), 'A', agent, str(repo)]
        if final:
            args.append('FINAL=' + str(final))
        args += ['--', *map(str, cmd)]
        return subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_sleep_two_and_timeout(self):
        proc = self.run_agent(['sleep', '2'])
        out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, err)
        record = all_runs(self.board)[0]
        self.assertEqual(record['state'], 'completed')
        for field in ('pid', 'base_commit', 'head_commit', 'last_seen_at', 'ended_at', 'exit_code', 'log', 'final_summary'):
            self.assertIn(field, record)
        self.assertGreaterEqual(record['duration_seconds'], 2)
        states = [e['state'] for e in rows(self.board / 'events/events.jsonl') if e['type'] == 'run:status']
        self.assertEqual(states, ['running', 'completed'])
        proc = self.run_agent(['timeout', '.1', 'sleep', '2'])
        proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 124)
        self.assertEqual(all_runs(self.board)[0]['state'], 'timeout')
        self.assertEqual(len(all_runs(self.board)), 2)

    def test_fake_codex_live_messages_and_explicit_progress(self):
        fake = self.path / 'codex'
        report = str(REPO / 'scripts/board-report')
        fake.write_text('''#!/usr/bin/env python3
import json, sys, time, subprocess
assert '--json' in sys.argv
print(json.dumps({'type':'item.started','item':{'type':'command_execution','command':'echo api_key=secret'}}), flush=True)
time.sleep(.7)
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'正在写功能'}}), flush=True)
time.sleep(.7)
print(json.dumps({'type':'item.completed','item':{'type':'file_change','changes':[{'path':'backend/app.py'}]}}), flush=True)
subprocess.run(['nice','-n','19',REPORT,'A','--progress','完成接线','--step','2','--total','3'], check=True)
time.sleep(1)
'''.replace('REPORT', repr(report)))
        fake.chmod(0o755)
        proc = self.run_agent([fake, 'exec', 'prompt'])
        deadline = time.monotonic() + 2
        received = []
        while time.monotonic() < deadline:
            records = all_runs(self.board)
            if records:
                received = messages(self.board, records[0]['id'])
                if any(m['type'] == 'text' for m in received):
                    break
            time.sleep(.03)
        self.assertTrue(any(m['type'] == 'text' for m in received), received)
        proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0)
        record = all_runs(self.board)[0]
        self.assertEqual(record['tool_calls'], 1)
        self.assertEqual(record['progress']['step'], 2)
        self.assertEqual(record['recent_files'], ['backend/app.py'])
        data = messages(self.board, record['id'])
        self.assertEqual([m['seq'] for m in data], list(range(1, len(data) + 1)))
        self.assertNotIn('api_key=secret', json.dumps(data))
        self.assertIn('api_key=secret', Path(record['log']).read_text())
        self.assertEqual(messages(self.board, record['id'], received[-1]['seq']), data[len(received):])

    def test_parser_failure_preserves_cli_exit(self):
        proc = self.run_agent(['sh', '-c', "echo '{bad json'; exit 7"])
        out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 7)
        self.assertIn(b'{bad json', out)
        self.assertEqual(all_runs(self.board)[0]['state'], 'failed')

    def test_cancellation(self):
        proc = self.run_agent(['sleep', '10'])
        for _ in range(100):
            records = all_runs(self.board)
            if records and records[0].get('pid'):
                break
            time.sleep(.02)
        proc.terminate()
        proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 143)
        self.assertEqual(all_runs(self.board)[0]['state'], 'cancelled')

    def test_hook_shared_worktree(self):
        repo = self.path / 'repo'
        init_repo(repo)
        worktree = self.path / 'worktree'
        command('git', '-C', repo, 'worktree', 'add', '-qb', 'parallel', worktree)
        command('sh', REPO / 'scripts/install-hooks.sh', worktree, '--board', self.board)
        (worktree / 'file').write_text('worktree change')
        command('git', '-C', worktree, 'commit', '-qam', '并行工作树提交')
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            records = list(rows(self.board / 'events/events.jsonl'))
            if any(e.get('branch') == 'parallel' for e in records):
                break
            time.sleep(.02)
        self.assertTrue(any(e.get('subject') == '并行工作树提交' for e in records))
        self.assertTrue((repo / '.git/hooks/post-commit').exists())

    def test_remote_fetch_only_changes_mirror(self):
        remote = self.path / 'remote.git'
        command('git', 'init', '--bare', '-q', remote)
        repo = self.path / 'repo'
        init_repo(repo)
        command('git', '-C', repo, 'remote', 'add', 'origin', remote)
        command('git', '-C', repo, 'push', '-qu', 'origin', 'main')
        mirror = Mirror(repo, self.path / 'mirror')
        mirror.poll(self.board)
        old = command('git', '-C', repo, 'rev-parse', 'origin/main').stdout.strip()
        other = self.path / 'other'
        command('git', 'clone', '-q', '-b', 'main', remote, other)
        command('git', '-C', other, 'config', 'user.name', 'Other')
        command('git', '-C', other, 'config', 'user.email', 'other@example.test')
        (other / 'file').write_text('remote change')
        command('git', '-C', other, 'commit', '-qam', '本地推送的功能')
        command('git', '-C', other, 'push', '-q')
        mirror.poll(self.board)
        push = [e for e in rows(self.board / 'events/events.jsonl') if e['type'] == 'git:push']
        self.assertTrue(any(e['subject'] == '本地推送的功能' and e['changed_files'] == 1 for e in push), push)
        self.assertEqual(command('git', '-C', repo, 'rev-parse', 'origin/main').stdout.strip(), old)
        self.assertFalse((repo / '.git/FETCH_HEAD').exists())


if __name__ == '__main__':
    unittest.main()
