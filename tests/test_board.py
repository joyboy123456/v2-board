"""看板缺口修复的单测：按卡隔离提交、合并状态、宽松解析、身份归并、失联判定。"""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import board_parse as bp  # noqa: E402
import card_git  # noqa: E402
from runs import effective_run  # noqa: E402
from signals import normalize  # noqa: E402


def git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()


def commit(repo, name):
    (Path(repo) / name).write_text(name)
    git(repo, 'add', name)
    git(repo, 'commit', '-qm', name)
    return git(repo, 'rev-parse', 'HEAD')


class CardGitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, 'init', '-q', '-b', 'main')
        commit(self.repo, 'base')
        git(self.repo, 'checkout', '-qb', 'h/lovart-shell')
        for i in range(5):
            commit(self.repo, f'shell{i}')
        self.fork = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'branch', 'integration/v2-wiring', 'main')
        git(self.repo, 'checkout', '-qb', 'h/mvp-canvas')
        commit(self.repo, 'canvas1')
        commit(self.repo, 'canvas2')
        git(self.repo, 'checkout', '-q', self.fork)
        git(self.repo, 'checkout', '-qb', 'h/mvp-library')
        os.environ['V2_GIT_REPO'] = str(self.repo)
        card_git.CACHE.clear()
        card_git.REFS.clear()

    def tearDown(self):
        os.environ.pop('V2_GIT_REPO', None)
        self.tmp.cleanup()

    def test_sibling_cards_do_not_share_inherited_commits(self):
        canvas = card_git.inspect_card(dict(branch='h/mvp-canvas', run_base=self.fork))
        library = card_git.inspect_card(dict(branch='h/mvp-library', run_base=self.fork))
        self.assertEqual([c[2] for c in canvas['commits']], ['canvas2', 'canvas1'])
        self.assertEqual(canvas['commit_count'], 2)
        self.assertEqual(library['commits'], [])
        self.assertEqual(library['note'], '还没有新提交')
        self.assertEqual(canvas['inherited_count'], 6)

    def test_fork_point_without_run_record(self):
        info = card_git.inspect_card(dict(branch='h/mvp-canvas', parent_branch='h/lovart-shell'))
        self.assertEqual(info['commit_count'], 2)
        self.assertEqual(info['parent'], 'h/lovart-shell')

    def test_merge_progress(self):
        info = card_git.inspect_card(dict(branch='h/mvp-canvas', run_base=self.fork))
        self.assertEqual(info['merge'], dict(submitted=True, integration=False, main=False, known=True))
        git(self.repo, 'checkout', '-q', 'integration/v2-wiring')
        git(self.repo, 'merge', '-q', '--no-ff', '-m', 'merge canvas', 'h/mvp-canvas')
        card_git.CACHE.clear(); card_git.REFS.clear()
        info = card_git.inspect_card(dict(branch='h/mvp-canvas', run_base=self.fork))
        self.assertTrue(info['merge']['integration'])
        self.assertFalse(info['merge']['main'])
        self.assertTrue(any(m['stage'] == '合入 integration/v2-wiring' for m in info['milestones']))


class LooseParseTest(unittest.TestCase):
    def test_new_card_without_title_or_id_is_on_board(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'fix').mkdir()
            (root / 'fix' / 'NEW-CARD.md').write_text('---\nstatus: in_progress\n---\n## 新卡标题\n正文')
            (root / 'fix' / 'NOTES.md').write_text('# 只是说明文档，没有 front matter\n')
            (root / 'fix' / 'NEW-CARD-progress.md').write_text('- 2026-11-18 01:00 | x | 开工')
            found = [p.name for _, p in bp.discover(root)]
            self.assertEqual(found, ['NEW-CARD.md'])
            data, _, _ = bp.metadata(root / 'fix' / 'NEW-CARD.md', 'fix')
            self.assertEqual(data['id'], 'NEW-CARD')
            self.assertEqual(data['title'], '新卡标题')
            self.assertEqual(data['status'], 'in_progress')
            self.assertEqual(data['missing_fields'], ['id', 'title'])

    def test_invalid_status_degrades_instead_of_hiding_card(self):
        with tempfile.TemporaryDirectory() as d:
            card = Path(d) / 'X.md'
            card.write_text('---\nid: X\ntitle: t\nstatus: doing\ndepends: oops\n---\n')
            data, _, _ = bp.metadata(card, 'fix')
            self.assertEqual(data['status'], 'unknown')
            self.assertIn('status（无效）', data['missing_fields'])
            self.assertEqual(data['depends'], [])


class IdentityAndLivenessTest(unittest.TestCase):
    def test_claude_is_one_product_codex_split_by_model(self):
        ids = {normalize(n)['id'] for n in ('Claude Code fable', 'claude opus', 'Claude Code fable-5 high')}
        self.assertEqual(ids, {'claude-code'})
        self.assertNotEqual(normalize('Codex gpt-6-astra')['id'], normalize('Codex gpt-6.1-sol')['id'])
        self.assertEqual(normalize('Kiro gpt-5.6-sol max')['model'], 'GPT 5.6')

    def test_stale_run_is_not_running(self):
        now = time.time()
        iso = lambda s: time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(s))
        base = dict(id='r', agent='Codex gpt-6.1-sol', state='running', started_at=iso(now - 4000))
        self.assertEqual(effective_run(dict(base, last_seen_at=iso(now - 30)), now)['state'], 'running')
        self.assertEqual(effective_run(dict(base, last_seen_at=iso(now - 600)), now)['state'], 'lost')
        ended = effective_run(dict(base, last_seen_at=iso(now - 3600)), now)
        self.assertEqual(ended['state'], 'lost_ended')
        self.assertTrue(ended['ended_at'])


if __name__ == '__main__':
    unittest.main()
