#!/usr/bin/env python3
"""Create guarded synthetic cards for the isolated intervention preview."""
from __future__ import annotations

import argparse
import os
import re
import stat
import struct
import zlib
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
    'h/05.md': '''---
id: DEMO-REJECTED
title: 验收未通过的合成任务（可指派修复）
status: rejected
assignee: Server Worker Alpha
branch: demo/rejected
---
# 验收未通过的合成任务（可指派修复）

此卡片完全合成，用于体验「指派修复」：打开验收页签，在未通过的检查项上选择 Agent。
''',
    'reports/2026-01-01-1000-demo.md': '''---
title: 合成验收报告（演示）
date: 2026-01-01 10:00
by: 演示验收员
site: 合成数据，没有真实站点
---
这份报告完全合成，只用于体验把未通过的检查项指派给 Agent 修复。

## 结果节点显示「图片暂未载入」
卡：DEMO-REJECTED
结论：未通过
- 生成完成后，对话里能看到图片，画布上的结果节点是红框。
- 点「重新加载图片」后仍然不显示。

![画布结果节点红框（合成截图）](shots/demo/canvas-error.png)

## 生成记录可以打开
卡：DEMO-REJECTED
结论：通过
- 生成记录列表正常显示。
''',
}
SHOT = 'reports/shots/demo/canvas-error.png'
REPAIR_CARD = re.compile(r'fix/REPAIR-[0-9A-F]{10}\.md')


def demo_png(width: int = 480, height: int = 270) -> bytes:
    """一张合成的示意截图：深色画布上一个红框节点。"""
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            inside = 150 <= x < 330 and 70 <= y < 200
            edge = inside and (x < 154 or x >= 326 or y < 74 or y >= 196)
            rows += bytes((226, 86, 86) if edge else (42, 30, 30) if inside else (26, 26, 28))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))

    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(bytes(rows), 9)) + chunk(b'IEND', b''))


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
        marker = absolute / MARKER
        if entries:
            # 非空目录必须已经带有效的演示标记，否则它可能是别人的数据
            if marker.is_symlink() or not marker.exists() or not stat.S_ISREG(marker.lstat().st_mode) \
                    or marker.read_bytes() != MARKER_CONTENT:
                raise SystemExit('目标目录非空且没有有效的演示标记，拒绝覆盖。')
        allowed_files = set(FIXTURES) | {MARKER, SHOT}
        allowed_dirs = {parent.as_posix() for name in allowed_files | {'fix/x'} for parent in Path(name).parents if parent != Path('.')}
        for entry in entries:
            relative = entry.relative_to(absolute).as_posix()
            mode = entry.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise SystemExit('拒绝覆盖包含符号链接的目标目录。')
            if stat.S_ISDIR(mode):
                if relative not in allowed_dirs:
                    raise SystemExit(f'拒绝覆盖未知目录：{relative}')
            elif not stat.S_ISREG(mode):
                raise SystemExit(f'拒绝覆盖非普通文件：{relative}')
            elif relative not in allowed_files and not REPAIR_CARD.fullmatch(relative):
                raise SystemExit(f'拒绝覆盖未知文件：{relative}')
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
    (root / SHOT).parent.mkdir(parents=True, exist_ok=True)
    (root / SHOT).write_bytes(demo_png())
    if (root / 'fix').is_dir():
        for stale in (root / 'fix').glob('REPAIR-*.md'):  # 上一轮演示生成的合成修复卡
            stale.unlink()
    (root / MARKER).write_bytes(MARKER_CONTENT)
    cards = sum(1 for name in FIXTURES if not name.startswith('reports/'))
    print(f'ISOLATED_DEMO_ROOT={root}')
    print(f'SYNTHETIC_CARDS={cards} MARKER={MARKER}')


if __name__ == '__main__':
    main()
