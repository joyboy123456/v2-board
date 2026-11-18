#!/usr/bin/env python3
"""Background collector. Hook callers always return success immediately."""
import os
import subprocess
import sys
from pathlib import Path
from signals import event, load_json, root


def git(*args):
    return subprocess.run(['nice', '-n', '19', 'git', '-c', f'safe.directory={os.getcwd()}', *args], capture_output=True, text=True, timeout=.08, check=True).stdout.strip()


try:
    board = Path(sys.argv[1])
    kind = sys.argv[2]
    branch = git('rev-parse', '--abbrev-ref', 'HEAD')
    info = git('show', '--root', '--format=%H%n%s%n%an', '--name-only', '--no-renames', 'HEAD', '--').splitlines()
    sha, subject, author = info[:3]
    files = [p for p in info[3:] if p.strip()]
    event(board, 'git:commit', branch=branch, sha=sha, subject=subject, author=author, changed_files=len(files), hook=kind, summary=subject)
    # Associate current run if this commit originated inside the wrapper.
    run_id = os.environ.get('BOARD_RUN_ID')
    if run_id:
        from signals import update_run
        update_run(board, run_id, {'latest_commit': {'sha': sha, 'subject': subject}, 'recent_files': files[:10]})
except Exception:
    pass
