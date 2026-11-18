#!/usr/bin/env python3
"""Idempotent install/uninstall in the repository common git directory."""
import argparse
import os
import shlex
import shutil
import subprocess
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('action', choices=['install', 'uninstall'])
p.add_argument('repo', nargs='?', default='/srv/v0-ai')
p.add_argument('--board', default=os.environ.get('BOARD_ROOT', '/srv/v2-wiring-board'))
a = p.parse_args()
repo = Path(a.repo).resolve()
git = ['nice', '-n', '19', 'git', '-c', f'safe.directory={repo}', '-C', str(repo)]
common = subprocess.check_output([*git, 'rev-parse', '--path-format=absolute', '--git-common-dir'], text=True).strip()
custom = subprocess.run([*git, 'config', '--get', 'core.hooksPath'], capture_output=True, text=True).stdout.strip()
if custom:
    p.error('检测到 core.hooksPath；请先将仓库恢复为共享 .git/hooks 后安装，避免钩子无效')
hooks = Path(common) / 'hooks'
hooks.mkdir(exist_ok=True)
managed = hooks / '.v2-board'
marker = '# v2-board-managed-v1'
if a.action == 'install':
    managed.mkdir(exist_ok=True)
    for src, dest in ((Path(__file__).with_name('git-hook.py'), managed / 'collector.py'), (Path(__file__).resolve().parents[1] / 'backend/signals.py', managed / 'signals.py')):
        shutil.copy2(src, dest)
for kind in ('post-commit', 'post-merge'):
    path = hooks / kind
    backup = hooks / (kind + '.bak-20261117')
    active = path.exists() and marker in path.read_text(errors='replace')
    if a.action == 'uninstall':
        if active:
            path.unlink()
            if backup.exists():
                shutil.copy2(backup, path)
        print('restored', path)
        continue
    if path.exists() and not active:
        if backup.exists():
            p.error(f'{backup} 已存在且当前钩子不是看板版本；请人工核对，未覆盖')
        shutil.copy2(path, backup)
    q = shlex.quote
    lines = ['#!/bin/sh', marker]
    if backup.exists():
        # Detached, bounded original hook preserves previous side effects without blocking Git.
        lines += [f'nice -n 19 timeout -k 0.02s 0.15s {q(str(backup))} "$@" </dev/null >/dev/null 2>&1 &']
    lines += [f'nice -n 19 timeout -k 0.02s 0.15s python3 {q(str(managed / "collector.py"))} {q(str(Path(a.board).resolve()))} {q(kind)} "$@" </dev/null >/dev/null 2>&1 &', 'exit 0', '']
    path.write_text('\n'.join(lines))
    path.chmod(0o755)
    print('installed', path)
if a.action == 'uninstall' and managed.exists():
    shutil.rmtree(managed)
