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
import json, os, subprocess, sys, time
from pathlib import Path
mode, card, final, prompt = sys.argv[1:5]
print(json.dumps({"type": "thread.started", "thread_id": "T-" + card}), flush=True)
Path(final).write_text("final " + mode)
Path(os.getcwd(), "prompt-" + mode + ".txt").write_text(prompt)
if mode == "ok":
    sys.exit(0)
if mode == "fail":
    sys.exit(3)
if mode == "sleep":
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
        for mode in ('ok', 'fail', 'ask', 'sleep'):
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
