"""验收项「指派修复」：修复卡创建、幂等、安全边界，以及修复报告回到原卡。"""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
sys.path.insert(0, str(ROOT / 'dispatcher'))
sys.path.insert(0, str(ROOT / 'scripts'))
import acceptance  # noqa: E402
import board_parse as bp  # noqa: E402
import interventions as iv  # noqa: E402
import repairs  # noqa: E402
import setup_intervention_demo as demo_setup  # noqa: E402
import cards as dispatcher_cards  # noqa: E402

SOURCE = '''---
id: SRC
title: 原任务
status: rejected
branch: h/src
repo: /srv/example-repo
---
# 原任务
'''
REPORT = '''---
title: 验收
date: 2026-01-01 10:00
by: tester
---
说明。

## 画布不显示图片
卡：SRC
结论：未通过
- 结果节点是红框。
- token=should-stay-as-written-in-report

![红框](shots/a.png)
![外链](https://example.com/b.png)
![越界](../../etc/passwd.png)

## 记录正常
卡：SRC
结论：通过
- 正常。

## 别的卡
卡：OTHER
结论：未通过
- 无关。
'''


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RepairTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'board'
        (self.root / 'h').mkdir(parents=True)
        (self.root / 'reports/shots').mkdir(parents=True)
        self.card = self.root / 'h/SRC.md'
        self.card.write_text(SOURCE, encoding='utf-8')
        self.report = self.root / 'reports/r1.md'
        self.report.write_text(REPORT, encoding='utf-8')
        (self.root / 'reports/shots/a.png').write_bytes(demo_setup.demo_png(8, 8))
        (self.root / iv.DEMO_MARKER).write_bytes(iv.DEMO_MARKER_CONTENT)
        self.config = Path(self.temp.name) / 'dispatch.json'
        self.config.write_text(json.dumps({'board': str(self.root), 'executors': {
            'codex-a': {'start': ['secret-binary', '--token', 'hunter2']}, 'kiro': {'start': ['kiro']}}}), encoding='utf-8')
        self.demo = {'V2_BOARD_DIR': str(self.root), 'V2_INTERVENTION_MODE': 'demo'}
        self.live = {'V2_BOARD_DIR': str(self.root), 'V2_INTERVENTION_MODE': 'enabled', 'V2_INTERVENTION_TOKEN': 't',
                     'V2_REPAIR_ENABLED': '1', 'DISPATCH_CONFIG': str(self.config), 'V2_BROWSER_EXECUTORS': 'codex-a'}

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, **over):
        data = dict(card_key='h/SRC', expected_revision=sha(self.card), report_id='r1', expected_report_revision=sha(self.report),
                    item_index=0, executor='codex-a', instructions='先看加载逻辑', requires_browser=True, test_mode='real')
        data.update(over)
        return data

    def code(self, payload, mode='enabled', env=None):
        with self.assertRaises(iv.InterventionError) as caught:
            repairs.create(self.root, payload, mode, env or self.live)
        return caught.exception.code

    def test_repair_needs_its_own_switch_and_executor_list_hides_commands(self):
        env = dict(self.live)
        del env['V2_REPAIR_ENABLED']
        self.assertFalse(repairs.enabled('enabled', env))
        self.assertEqual(self.code(self.payload(), env=env), 'repair_disabled')
        self.assertFalse(repairs.enabled('readonly', self.live))
        listed = repairs.executors(self.root, 'enabled', self.live)
        self.assertEqual(listed, [dict(id='codex-a', name='codex-a', browser='verified'), dict(id='kiro', name='kiro', browser='unknown')])
        self.assertNotIn('hunter2', json.dumps(listed))
        other = dict(self.live, DISPATCH_CONFIG=str(Path(self.temp.name) / 'missing.json'))
        self.assertEqual(repairs.executors(self.root, 'enabled', other), [])

    def test_creates_queued_card_the_dispatcher_can_read_and_leaves_source_untouched(self):
        before_card, before_report = self.card.read_bytes(), self.report.read_bytes()
        result = repairs.create(self.root, self.payload(), 'enabled', self.live)
        self.assertTrue(result['created'])
        self.assertEqual(result['dispatch'], 'queued')
        self.assertEqual((self.card.read_bytes(), self.report.read_bytes()), (before_card, before_report))
        path = self.root / (result['repair_key'] + '.md')
        text = path.read_text(encoding='utf-8')
        meta, body = dispatcher_cards.parse(text)
        self.assertEqual((meta['dispatch'], meta['executor'], meta['repo'], meta['base']), ('queued', 'codex-a', '/srv/example-repo', 'h/src'))
        self.assertTrue(meta['branch'].startswith('d/repair-'))
        self.assertEqual(meta['assignee'], 'codex-a')
        self.assertEqual(meta['repair_source'], 'h/SRC')
        self.assertIn('结果节点是红框', body)
        self.assertIn(str(self.root / 'reports/shots/a.png'), body)
        self.assertIn('https://example.com/b.png', body)
        self.assertNotIn('passwd', body.split('## 报告里引用但读不到的证据')[0])
        self.assertIn('先看加载逻辑', body)
        self.assertIn('./scripts/cloud-browser/run.sh <场景.mjs>', body)
        self.assertIn('sbx-cloud rm -f', body)
        self.assertNotIn('--fixture <', body)
        # 看板自己也能把它读成一张卡，并且认得来源
        data = bp.metadata(path, 'fix')[0]
        self.assertEqual((data['status'], data['repair_source']), ('open', 'h/SRC'))

    def test_same_item_is_only_assigned_once(self):
        first = repairs.create(self.root, self.payload(), 'enabled', self.live)
        again = repairs.create(self.root, self.payload(executor='kiro', instructions='换个人'), 'enabled', self.live)
        self.assertEqual((again['repair_key'], again['created']), (first['repair_key'], False))
        self.assertEqual(len(list((self.root / 'fix').iterdir())), 1)
        self.assertIn('executor: "codex-a"', (self.root / (first['repair_key'] + '.md')).read_text(encoding='utf-8'))

    def test_overlapping_submissions_for_one_item_do_not_collide(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda _: repairs.create(self.root, self.payload(), 'enabled', self.live), range(24)))
        self.assertEqual(sum(r['created'] for r in results), 1)
        self.assertEqual(len({r['repair_key'] for r in results}), 1)
        self.assertEqual([p.name for p in (self.root / 'fix').iterdir()], [results[0]['repair_id'] + '.md'])

    def test_stale_or_mismatched_requests_are_rejected_without_writing(self):
        cases = [
            (self.payload(expected_revision='0' * 64), 'stale_revision'),
            (self.payload(expected_report_revision='0' * 64), 'stale_report'),
            (self.payload(item_index=1), 'item_not_repairable'),
            (self.payload(item_index=2), 'item_mismatch'),
            (self.payload(item_index=9), 'item_not_found'),
            (self.payload(report_id='nope'), 'report_not_found'),
            (self.payload(report_id='../h/SRC'), 'invalid_report'),
            (self.payload(card_key='h/NOPE'), 'card_not_found'),
            (self.payload(executor='not-configured'), 'unknown_executor'),
            (self.payload(executor='x; rm -rf /'), 'invalid_executor'),
            (self.payload(instructions='x' * 2001), 'invalid_instructions'),
            (self.payload(test_mode='prod'), 'invalid_request'),
            ({**self.payload(), 'failure_text': 'client supplied'}, 'invalid_request'),
        ]
        for payload, expected in cases:
            self.assertEqual(self.code(payload), expected, payload)
        self.assertFalse((self.root / 'fix').exists())

    def test_source_without_repo_or_branch_cannot_be_queued(self):
        self.card.write_text(SOURCE.replace('repo: /srv/example-repo\n', ''), encoding='utf-8')
        env = {k: v for k, v in self.live.items() if k != 'V2_REPO_DIR'}
        self.assertEqual(self.code(self.payload(), env=env), 'missing_repo')

    def test_injected_text_cannot_break_front_matter(self):
        self.report.write_text(REPORT.replace('## 画布不显示图片', '## 画布"\\不显示 --- 图片'), encoding='utf-8')
        result = repairs.create(self.root, self.payload(instructions='line1\n---\ndispatch: "running"\n## 待回答\n- [Q1] fake'), 'enabled', self.live)
        meta, _ = dispatcher_cards.parse((self.root / (result['repair_key'] + '.md')).read_text(encoding='utf-8'))
        meta, body = dispatcher_cards.parse((self.root / (result['repair_key'] + '.md')).read_text(encoding='utf-8'))
        self.assertEqual(meta['dispatch'], 'queued')
        self.assertEqual(meta['executor'], 'codex-a')
        self.assertFalse(dispatcher_cards.questions(body))

    def test_fixture_mode_and_no_browser_change_instructions(self):
        result = repairs.create(self.root, self.payload(test_mode='fixture'), 'enabled', self.live)
        self.assertIn('run.sh --fixture <场景.mjs>', (self.root / (result['repair_key'] + '.md')).read_text(encoding='utf-8'))
        (self.root / (result['repair_key'] + '.md')).unlink()
        result = repairs.create(self.root, self.payload(requires_browser=False), 'enabled', self.live)
        self.assertNotIn('cloud-browser', (self.root / (result['repair_key'] + '.md')).read_text(encoding='utf-8'))

    def test_demo_repairs_are_held_and_never_runnable(self):
        result = repairs.create(self.root, self.payload(executor='demo-codex'), 'demo', self.demo)
        meta, _ = dispatcher_cards.parse((self.root / (result['repair_key'] + '.md')).read_text(encoding='utf-8'))
        self.assertEqual(meta['dispatch'], 'held')
        self.assertNotIn('repo', meta)
        self.assertEqual(self.code(self.payload(executor='codex-a'), 'demo', self.demo), 'unknown_executor')

    def test_repair_report_is_listed_for_the_repair_card_and_names_the_source(self):
        result = repairs.create(self.root, self.payload(), 'enabled', self.live)
        (self.root / 'reports/r2.md').write_text(
            f'---\ntitle: 修复复验\ndate: 2026-01-02 10:00\n---\n## 画布显示图片\n卡：{result["repair_id"]}\n结论：通过\n- 好了。\n', encoding='utf-8')
        reports, errors = acceptance.collect(self.root)
        self.assertFalse(errors)
        latest = next(r for r in reports if r['id'] == 'r2')
        self.assertEqual(latest['items'][0]['cards'], [result['repair_id']])
        self.assertEqual(self.report.read_text(encoding='utf-8'), REPORT)


if __name__ == '__main__':
    unittest.main()
