#!/usr/bin/env python3
"""Create guarded synthetic cards for the isolated intervention preview."""
from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path

MARKER = '.v2-board-intervention-demo'
MARKER_CONTENT = b'v2-board-intervention-demo-v1\n'
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path.home() / 'v2-board-intervention-demo'
FIXTURES = {
    'h/01.md': '''---
id: DEMO-ASK-MULTI
title: 选择合成卡片的视觉方向
status: in_progress
assignee: Server Worker Alpha
executor: Server Worker Alpha
dispatch: "asking"
---
# 选择合成卡片的视觉方向

此卡片完全合成，仅用于体验回答界面。

## 待回答
- [Q1] 2026-01-01 09:00:00 CST | Server Worker Alpha | 先采用哪种视觉方向？ | 选项：简洁产品图 / 生活方式场景
- [Q2] 2026-01-01 09:01:00 CST | Server Worker Alpha | 包装上的文字应该如何处理？ | 选项：保留清晰文字 / 不显示文字
''',
    'h/02.md': '''---
id: DEMO-ASK-SINGLE
title: 确认合成任务的输出比例
status: in_progress
assignee: Server Worker Beta
executor: Server Worker Beta
dispatch: "asking"
---
# 确认合成任务的输出比例

另一张合成提问卡，用于验证单问题回答。

## 待回答
- [Q1] 2026-01-01 09:05:00 CST | Server Worker Beta | 预览图使用哪种比例？ | 选项：方形 / 横向
''',
    'h/03.md': '''---
id: DEMO-RUNNING
title: 正在执行的合成任务（只读示例）
status: in_progress
assignee: Server Worker Gamma
executor: Server Worker Gamma
dispatch: "running"
---
# 正在执行的合成任务（只读示例）

此示例表示一个正在运行的 worker。当前切片只允许回答 asking 卡片，不提供运行中消息、暂停或终止操作。
''',
    'h/04.md': '''---
id: DEMO-ANSWERED
title: 已记录回答的合成历史
status: in_progress
assignee: Server Worker Delta
executor: Server Worker Delta
dispatch: "answered"
---
# 已记录回答的合成历史

此卡片展示问题退出 Needs You 后仍保留的回答历史。

## 待回答
- [Q1] 2026-01-01 09:10:00 CST | Server Worker Delta | 是否使用演示色板？ | 选项：使用 / 不使用

## 回答
- [Q1] 2026-01-01 09:12:00 CST | user: 使用演示色板
''',
}


def validate_root(root: Path, custom: bool, force: bool) -> Path:
    absolute = Path(os.path.abspath(os.fspath(root.expanduser())))
    resolved = absolute.resolve(strict=False)
    home = Path.home().resolve()
    repo = REPO_ROOT.resolve()
    if absolute != resolved:
        raise SystemExit('演示目录的路径组件不能是符号链接。')
    if absolute == home or not absolute.is_relative_to(home):
        raise SystemExit('演示目录必须位于当前用户 home 目录内，且不能是 home 本身。')
    if absolute.is_relative_to(repo) or repo.is_relative_to(absolute):
        raise SystemExit('演示目录必须位于仓库之外，并且不能包含仓库。')
    if custom and not force:
        raise SystemExit('自定义 --root 需要同时提供 --force 显式确认。')
    if absolute.is_symlink():
        raise SystemExit('演示目录不能是符号链接。')
    if absolute.exists():
        if not absolute.is_dir():
            raise SystemExit('演示目录必须是普通目录，不能是符号链接。')
        entries = list(absolute.rglob('*'))
        if not force:
            raise SystemExit('目标目录已存在；检查后使用 --force 才能创建或重建演示文件。')
        allowed_files = set(FIXTURES) | {MARKER}
        allowed_dirs = {str(Path(name).parent) for name in FIXTURES if Path(name).parent != Path('.')}
        for entry in entries:
            if entry.is_symlink():
                raise SystemExit('拒绝覆盖包含符号链接的目标目录。')
            relative = entry.relative_to(absolute).as_posix()
            if entry.is_dir() and relative not in allowed_dirs:
                raise SystemExit(f'拒绝覆盖未知目录：{relative}')
            if entry.is_file() and relative not in allowed_files:
                raise SystemExit(f'拒绝覆盖未知文件：{relative}')
        marker = absolute / MARKER
        if marker.exists():
            if not stat.S_ISREG(marker.lstat().st_mode) or marker.read_bytes() != MARKER_CONTENT:
                raise SystemExit('目标目录中的演示标记无效，拒绝覆盖。')
    return absolute


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, help=f'隔离数据目录（默认：{DEFAULT_ROOT}）')
    parser.add_argument('--force', action='store_true', help='显式允许重建受支持的演示文件；未知文件仍会拒绝')
    args = parser.parse_args()
    root = validate_root(args.root or DEFAULT_ROOT, custom=args.root is not None, force=args.force)
    root.mkdir(parents=True, exist_ok=True)
    for relative, content in FIXTURES.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8', newline='')
    (root / MARKER).write_bytes(MARKER_CONTENT)
    print(f'ISOLATED_DEMO_ROOT={root}')
    print(f'SYNTHETIC_CARDS={len(FIXTURES)} MARKER={MARKER}')


if __name__ == '__main__':
    main()
