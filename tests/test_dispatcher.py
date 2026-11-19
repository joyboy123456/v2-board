import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dispatcher'))
import cards  # noqa: E402
from dispatcher import Dispatcher  # noqa: E402

# A fake agent. First turn: prints a codex-style thread id, then either finishes,
# fails, asks a question, or sleeps. Resume turn: records the prompt and finishes.
FAKE = r'''
import json, os, signal, subprocess, sys, time
from pathlib import Path
mode, card, final, prompt = sys.argv[1:5]
if mode == "deaf":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if mode == "orphan":  # 另起会话的子进程：父进程退出后就不在进程树里了
    kid = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"], start_new_session=True)
    Path("orphan.pid").write_text(str(kid.pid))
print(json.dumps({"type": "thread.started", "thread_id": "T-" + card}), flush=True)
Path(final).write_text("final " + mode)
Path(os.getcwd(), "prompt-" + mode + ".txt").write_text(prompt)
if mode == "ok":
    sys.exit(0)
if mode == "fail":
    sys.exit(3)
if mode in ("sleep", "deaf", "orphan"):
    time.sleep(30)
if mode == "ask":
    subprocess.run([sys.executable, os.environ["BOARD_ASK"], card, "用哪种方案？", "--option", "甲", "--option", "乙"], check=True)
    sys.exit(0)
if mode == "resume":
    sys.exit(0)
'''


def card_text(card, executor, extra=''):
    return f'---\ntitle: "测试 {card}"\nstatus: "open"\ndispatch: "queued"\nexecutor: "{executor}"\n{extra}---\n\n实现 {card} 的功能。\n'


class DispatcherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.board = self.tmp / 'board'
        self.board.mkdir()
        fake = self.tmp / 'fake.py'
        fake.write_text(FAKE)
        py = sys.executable
        execs = {}
        for mode in ('ok', 'fail', 'ask', 'sleep', 'deaf', 'orphan'):
            execs[mode] = {'agent': f'fake {mode}', 'cmd': [py, str(fake), mode, '{card}', '{final}', '{prompt}'],
                           'resume': [py, str(fake), 'resume', '{card}', '{final}', 'RESUMED {session} {prompt}']}
        self.cfg = {'board': str(self.board), 'state': str(self.tmp / 'state'), 'slots': 2, 'min_free_mem_gb': 0,
                    'agent_run': None, 'executors': execs}
        os.environ['BOARD_ASK'] = str(ROOT / 'scripts' / 'board-ask')
        os.environ['V2_BOARD_DIR'] = str(self.board)

    def write(self, card, executor, extra=''):
        path = self.board / f'{card}.md'
        path.write_text(card_text(card, executor, extra))
        return path

    def settle(self, d, until, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            d.tick()
            if until():
                return
            time.sleep(.1)
        self.fail('did not settle: ' + json.dumps(d.status(), ensure_ascii=False))

    def state(self, card):
        return cards.read(self.board / f'{card}.md')[0]['dispatch']

    def test_ok_and_fail(self):
        self.write('A1', 'ok')
        self.write('A2', 'fail')
        d = Dispatcher(self.cfg)
        self.settle(d, lambda: self.state('A1') == 'done' and self.state('A2') == 'failed')
        rec = d.rec('A1')
        self.assertEqual(rec['session'], 'T-A1')
        prompt = Path(rec['worktree'], 'prompt-ok.txt').read_text()
        self.assertIn('实现 A1 的功能', prompt)
        self.assertIn('board-ask A1', prompt)
        st = d.status()
        self.assertEqual([i['card'] for i in st['done']], ['A1'])
        self.assertEqual(st['failed'][0]['final'], 'final fail')

    def test_ask_answer_resume(self):
        self.write('B1', 'ask')
        d = Dispatcher(self.cfg)
        self.settle(d, lambda: self.state('B1') == 'asking' and not d.children)
        st = d.status()
        self.assertEqual(st['asking'][0]['questions']['Q1'].split('| ')[-2:], ['用哪种方案？ ', '选项：甲 / 乙'])
        # board-ask also ran board-report: board status turns blocked
        self.assertEqual(cards.read(self.board / 'B1.md')[0]['status'], 'blocked')
        subprocess.run([sys.executable, str(ROOT / 'scripts' / 'board-dispatch'), 'answer', 'B1', 'q1', '用乙'], check=True, capture_output=True)
        self.assertEqual(self.state('B1'), 'answered')
        self.settle(d, lambda: self.state('B1') == 'done')
        rec = d.rec('B1')
        self.assertEqual(rec['mode'], 'resume')
        self.assertEqual(rec['attempt'], 2)
        resumed = Path(rec['worktree'], 'prompt-resume.txt').read_text()
        self.assertTrue(resumed.startswith('RESUMED T-B1'))
        self.assertIn('答：', resumed)
        self.assertIn('用乙', resumed)

    def test_message_reopens_same_session(self):
        path = self.write('M1', 'ok')
        d = Dispatcher(self.cfg)
        self.settle(d, lambda: self.state('M1') == 'done')
        self.assertEqual(cards.add_message(path, '把按钮  改成蓝色'), 'M1')
        self.assertEqual(self.state('M1'), 'answered')
        self.settle(d, lambda: self.state('M1') == 'done' and d.rec('M1')['attempt'] == 2)
        rec = d.rec('M1')
        self.assertEqual(rec['mode'], 'resume')
        resumed = Path(rec['worktree'], 'prompt-resume.txt').read_text()
        self.assertTrue(resumed.startswith('RESUMED T-M1'))
        self.assertIn('把按钮 改成蓝色', resumed)
        self.assertNotIn('有人回答了你的问题', resumed)
        meta, body = cards.read(path)
        self.assertEqual(meta['delivered'], '1')
        self.assertEqual(cards.pending_messages(meta, body), [])
        # 送过的留言不会再送一遍
        cards.add_message(path, '再加一个图标')
        self.settle(d, lambda: self.state('M1') == 'done' and d.rec('M1')['attempt'] == 3)
        again = Path(d.rec('M1')['worktree'], 'prompt-resume.txt').read_text()
        self.assertIn('再加一个图标', again)
        self.assertNotIn('改成蓝色', again)

    def test_message_before_start_and_during_run(self):
        path = self.write('M2', 'ok')
        cards.add_message(path, '先看 README')
        self.assertEqual(self.state('M2'), 'queued')
        d = Dispatcher(self.cfg)
        self.settle(d, lambda: self.state('M2') == 'done')
        first = Path(d.rec('M2')['worktree'], 'prompt-ok.txt').read_text()
        self.assertIn('先看 README', first)
        self.assertEqual(first.count('先看 README'), 1)  # 任务正文里不重复带留言分区
        # 跑着的时候来的留言：这一轮结束后接着同一个会话送达，而不是丢掉
        slow = self.write('M3', 'sleep')
        d.tick()
        self.assertEqual(self.state('M3'), 'running')
        cards.add_message(slow, '别动数据库')
        self.assertEqual(self.state('M3'), 'running')
        end = time.time() + 10  # 等它报出会话号再结束这一轮，否则续不上原会话
        while 'thread.started' not in Path(d.rec('M3')['log']).read_text() and time.time() < end:
            time.sleep(.05)
        os.killpg(d.children['M3'].pid, 15)
        self.settle(d, lambda: d.rec('M3').get('attempt') == 2 and self.state('M3') == 'done')
        self.assertIn('别动数据库', Path(d.rec('M3')['worktree'], 'prompt-resume.txt').read_text())

    def started(self, d, card):
        end = time.time() + 10  # 等它报出会话号，之后才续得上原会话
        while 'thread.started' not in Path(d.rec(card)['log']).read_text() and time.time() < end:
            time.sleep(.05)

    def test_stop_ends_the_turn_and_keeps_the_session(self):
        path = self.write('S1', 'sleep')
        d = Dispatcher(self.cfg)
        d.tick()
        self.assertEqual(self.state('S1'), 'running')
        self.started(d, 'S1')
        pid = d.children['S1'].pid
        cards.update(path, stop=cards.stamp())  # 看板的停止按钮只写这一个字段
        self.settle(d, lambda: self.state('S1') == 'held')
        meta = cards.read(path)[0]
        self.assertNotIn('stop', meta)
        self.assertIn('stopped', meta)
        self.assertFalse(Dispatcher.alive(pid))
        self.assertEqual(d.rec('S1')['session'], 'T-S1')
        self.assertTrue(Path(d.rec('S1')['worktree']).is_dir())
        self.assertIn('"kind": "stopped"', (self.tmp / 'state' / 'events.jsonl').read_text())
        d.tick()
        self.assertEqual(self.state('S1'), 'held')  # 停下以后不会自己再跑
        # 之后发消息，接着同一个会话继续
        cards.add_message(path, '换个思路再试')
        self.assertEqual(self.state('S1'), 'answered')
        self.settle(d, lambda: self.state('S1') == 'done' and d.rec('S1')['attempt'] == 2)
        self.assertEqual(d.rec('S1')['mode'], 'resume')
        self.assertIn('RESUMED T-S1', Path(d.rec('S1')['worktree'], 'prompt-resume.txt').read_text())
        self.assertNotIn('stopped', cards.read(path)[0])

    def test_stop_kills_an_agent_that_ignores_term(self):
        self.cfg['stop_grace_seconds'] = .3
        path = self.write('S2', 'deaf')
        d = Dispatcher(self.cfg)
        d.tick()
        self.started(d, 'S2')
        pids = Dispatcher.tree(d.children['S2'].pid)
        self.assertGreater(len(pids), 1)  # timeout 和它下面的 Agent
        cards.update(path, stop=cards.stamp())
        d.tick()
        self.assertEqual(self.state('S2'), 'running')  # 先发 TERM，它不理
        self.settle(d, lambda: self.state('S2') == 'held')
        self.assertFalse(any(Dispatcher.alive(p) for p in pids))

    def test_stop_also_ends_detached_children_and_never_touches_a_recycled_pid(self):
        self.cfg['stop_grace_seconds'] = .5
        path = self.write('S5', 'orphan')
        d = Dispatcher(self.cfg)
        d.tick()
        self.started(d, 'S5')
        orphan = int(Path(d.rec('S5')['worktree'], 'orphan.pid').read_text())
        cards.update(path, stop=cards.stamp())
        self.settle(d, lambda: self.state('S5') == 'held')
        self.assertTrue(Dispatcher.alive(orphan))  # 父进程已经退了，它不理 TERM
        cards.add_message(path, '继续')
        d.tick()
        self.assertEqual(self.state('S5'), 'answered')  # 残留没清完之前不重新开跑
        end = time.time() + 10
        while Dispatcher.alive(orphan) and time.time() < end:
            d.tick()
            time.sleep(.1)
        self.assertFalse(Dispatcher.alive(orphan))
        self.settle(d, lambda: self.state('S5') == 'done')
        self.assertNotIn('stop_pids', {k: v for k, v in d.rec('S5').items() if v})
        # 重启后接手的记录里，pid 已经被别的进程占用：不发信号，只把卡收尾
        other = subprocess.Popen(['sleep', '30'])
        try:
            stale = self.write('S6', 'sleep', 'stop: "2026-11-19 23:00:00 CST"\n')
            cards.update(stale, dispatch='running')
            d.save('S6', pid=other.pid, pid_start='1', attempt=1)
            d.tick()
            self.assertEqual(self.state('S6'), 'held')
            self.assertIsNone(other.poll())
        finally:
            other.kill()
            other.wait()

    def test_stop_before_start_and_stale_requests(self):
        queued = self.write('S3', 'ok', 'stop: "2026-11-19 23:00:00 CST"\n')
        done = self.write('S4', 'ok')
        d = Dispatcher(self.cfg)
        self.settle(d, lambda: self.state('S4') == 'done')
        self.assertEqual(self.state('S3'), 'held')  # 还在排队就被停止：不启动
        self.assertEqual(d.rec('S3'), {})
        self.assertIn('stopped', cards.read(queued)[0])
        # 没跑过的卡被停止后收到留言：没有会话可续，按第一轮给完整任务
        d.cfg['executors']['ok']['resume'] = [part for part in d.cfg['executors']['ok']['resume'] if '{session}' not in part]
        cards.add_message(queued, '现在可以开始了')
        self.settle(d, lambda: self.state('S3') == 'done')
        self.assertNotEqual(d.rec('S3')['mode'], 'resume')
        self.assertNotIn('stopped', cards.read(queued)[0])
        # 这一轮自己跑完之后才到的停止请求：什么都不停，只把请求清掉
        cards.update(done, stop=cards.stamp())
        d.tick()
        meta = cards.read(done)[0]
        self.assertEqual((meta['dispatch'], 'stop' in meta, 'stopped' in meta), ('done', False, False))

    def test_resume_without_session_id(self):
        # 有的 CLI 在同一个目录里「接着上一次」，续聊命令不带会话号
        self.cfg['executors']['ok']['resume'] = [sys.executable, str(self.tmp / 'fake.py'), 'resume', '{card}', '{final}', 'CONTINUED {prompt}']
        path = self.write('M4', 'ok')
        d = Dispatcher(self.cfg)
        self.settle(d, lambda: self.state('M4') == 'done')
        d.save('M4', session='')
        cards.add_message(path, '继续')
        self.settle(d, lambda: self.state('M4') == 'done' and d.rec('M4')['attempt'] == 2)
        self.assertEqual(d.rec('M4')['mode'], 'resume')
        self.assertTrue(Path(d.rec('M4')['worktree'], 'prompt-resume.txt').read_text().startswith('CONTINUED 用户在看板的对话窗口里对你说'))

    def test_slots_and_priority(self):
        self.write('C1', 'sleep', 'priority: "90"\n')
        self.write('C2', 'sleep')
        self.write('C3', 'sleep')
        d = Dispatcher(self.cfg)
        d.tick()
        self.assertEqual(sorted(d.children), ['C2', 'C3'])
        self.assertEqual(self.state('C1'), 'queued')
        for c in d.children.values():
            c.kill()

    def test_restart_adopts_running(self):
        self.write('D1', 'sleep')
        d = Dispatcher(self.cfg)
        d.tick()
        pid = d.rec('D1')['pid']
        d2 = Dispatcher(self.cfg)  # dispatcher restarted, child keeps running
        d2.tick()
        self.assertEqual(d2.running(), ['D1'])
        self.assertEqual(d2.rec('D1')['attempt'], 1)
        os.kill(pid, 9)
        d.children['D1'].wait()
        self.settle(d2, lambda: self.state('D1') == 'failed')

    def test_unknown_executor_and_hold(self):
        self.write('E1', 'nope')
        self.write('E2', 'ok')
        subprocess.run([sys.executable, str(ROOT / 'scripts' / 'board-dispatch'), 'hold', 'E2'], check=True, capture_output=True)
        d = Dispatcher(self.cfg)
        d.tick()
        self.assertEqual(self.state('E1'), 'failed')
        self.assertEqual(self.state('E2'), 'held')
        self.assertEqual(d.children, {})


class CardsTest(unittest.TestCase):
    def test_set_keys_keeps_body(self):
        t = '---\ntitle: "x"\nstatus: "open"\n---\n正文\n'
        t2 = cards.set_keys(t, dispatch='queued', status='review')
        meta, body = cards.parse(t2)
        self.assertEqual(meta, {'title': 'x', 'status': 'review', 'dispatch': 'queued'})
        self.assertEqual(body, '正文\n')

    def test_questions_sections(self):
        with tempfile.NamedTemporaryFile('w+', suffix='.md', delete=False) as f:
            f.write('---\ndispatch: "running"\n---\n任务\n\n## 背景\n说明\n')
        q1 = cards.add_question(f.name, '问一')
        q2 = cards.add_question(f.name, '问二')
        self.assertEqual((q1, q2), ('Q1', 'Q2'))
        cards.add_answer(f.name, 'Q1', '答一')
        meta, body = cards.read(f.name)
        self.assertEqual(meta['dispatch'], 'asking')
        self.assertEqual(list(cards.open_questions(body)), ['Q2'])
        cards.add_answer(f.name, 'Q2', '答二')
        meta, body = cards.read(f.name)
        self.assertEqual(meta['dispatch'], 'answered')
        self.assertIn('## 背景\n说明', body)


if __name__ == '__main__':
    unittest.main()
