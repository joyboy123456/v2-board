"""验收报告解析与证据文件放行的边界。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

import acceptance  # noqa: E402

REPORT = """---
title: 复验
date: 2026-01-01 10:00
---
## 画布节点

卡：B11
结论：未能通过
- 还是红框。
![截图](shots/a.png)

```md
## 这不是标题
```

## 生成记录
卡：B12
结论：通过
"""


class AcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'board'
        (self.root / 'reports' / 'shots').mkdir(parents=True)
        (self.root / 'reports' / 'r.md').write_text(REPORT, encoding='utf-8')
        (self.root / 'reports' / 'shots' / 'a.png').write_bytes(b'png')
        (self.root / 'reports' / 'shots' / 'unreferenced.png').write_bytes(b'png')

    def test_blank_line_negated_verdict_and_fenced_heading(self):
        reports, errors = acceptance.collect(self.root)
        self.assertEqual(errors, [])
        items = reports[0]['items']
        self.assertEqual([it['title'] for it in items], ['画布节点', '生成记录'])
        self.assertEqual((items[0]['cards'], items[0]['verdict']), (['B11'], 'fail'))
        self.assertIn('## 这不是标题', items[0]['text'])
        self.assertEqual(items[1]['verdict'], 'pass')
        for phrase in ('无法通过', '没有通过', '没通过', '不通过'):
            self.assertEqual(acceptance.verdict_of(phrase), 'fail')

    def test_symlinked_reports_are_not_read(self):
        secret = Path(self.tmp.name) / 'outside.md'
        secret.write_text('## 外部\n卡：X\n结论：未通过\nTOP-SECRET\n', encoding='utf-8')
        (self.root / 'reports' / 'link.md').symlink_to(secret)
        reports, _ = acceptance.collect(self.root)
        self.assertEqual([r['id'] for r in reports], ['r'])
        other = Path(self.tmp.name) / 'other'
        other.mkdir()
        (other / 'reports').symlink_to(self.root / 'reports', target_is_directory=True)
        self.assertEqual(acceptance.collect(other), ([], []))

    def test_only_referenced_evidence_is_served(self):
        self.assertEqual(acceptance.safe_file(self.root, 'reports/shots/a.png'), (self.root / 'reports/shots/a.png').resolve())
        self.assertIsNone(acceptance.safe_file(self.root, 'reports/shots/unreferenced.png'))
        self.assertIsNone(acceptance.safe_file(self.root, '../outside.md'))


if __name__ == '__main__':
    unittest.main()
