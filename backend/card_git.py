"""Read-only per-card Git provenance, cached by resolved refs (including local commits)."""
import os
import re
import subprocess
import time
from pathlib import Path

CACHE = {}
REFS = {}


def git(repo, *args):
    try:
        r = subprocess.run(['nice', '-n', '19', 'git', '-c', f'safe.directory={repo}', '-C', str(repo), '--no-pager', *args], capture_output=True, text=True, timeout=3, env=dict(os.environ, GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0'))
        return r.stdout.strip() if r.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        return ''


def refs(repo):
    cached = REFS.get(str(repo))
    if cached and time.monotonic() - cached[0] < .4:
        return cached[1]
    lines = git(repo, 'for-each-ref', '--format=%(refname) %(objectname)')
    value = dict(line.split(' ', 1) for line in lines.splitlines() if ' ' in line)
    REFS[str(repo)] = (time.monotonic(), value)
    return value


def inspect_card(data):
    repo = os.environ.get('V2_GIT_REPO') or data.get('worktree', '')
    branch = str(data.get('branch', ''))
    empty = dict(commits=[], note='分支或仓库未登记', baseline='', commit_count=0, changed_files=0, inherited_count=0, parent='', merge=dict(submitted=False, integration=False, main=False, known=False), milestones=[])
    if not repo or not Path(repo).is_dir():
        return empty
    known = refs(repo)
    def resolve(name):
        if not name or not re.fullmatch(r'[A-Za-z0-9_./-]+', str(name)) or str(name).startswith('-') or '..' in str(name):
            return ''
        return next((known[r] for r in ('refs/heads/'+name, 'refs/remotes/origin/'+name.removeprefix('origin/'), name) if r in known), '') or (git(repo, 'rev-parse', '--verify', str(name)+'^{commit}') if re.fullmatch(r'[a-fA-F0-9]{7,40}', str(name)) else '')
    head = resolve(branch) if branch else git(repo, 'rev-parse', 'HEAD')
    if not head:
        return empty
    explicit = data.get('run_base') or data.get('baseline') or data.get('base_commit')
    if not explicit:
        match = re.match(r'^([a-fA-F0-9]{7,40})(?:\s|$)', str(data.get('base', '')))
        explicit = match[1] if match else ''
    # Parent is explicit when supplied; otherwise the closest shared ancestor of
    # another registered branch is the conservative fork point (never inherited log).
    parent = str(data.get('parent_branch') or data.get('parent') or '')
    if not parent:
        match = re.search(r'([\w-]+/[\w.-]+)', str(data.get('base', '')))
        parent = match[1] if match else ''
    fingerprint = tuple(sorted(known.items()))
    key = (str(repo), branch, head, str(explicit), parent, fingerprint)
    if key in CACHE:
        return CACHE[key]
    base = resolve(str(explicit)) if explicit else ''
    if not base and branch:
        birth = git(repo, 'reflog', 'show', '--format=%H|%gs', branch).splitlines()
        if birth:
            sha, _, msg = birth[-1].partition('|')
            if msg.startswith('branch: Created from '):
                base = sha
                parent = msg.removeprefix('branch: Created from ')
                if parent == 'HEAD':
                    parent = ''
    if not base:
        candidates = []
        names = [parent] if parent else list(dict.fromkeys(['h/lovart-shell', 'h/trunk', 'integration/v2-wiring', 'main'] + [r.removeprefix('refs/heads/') for r, sha in known.items() if sha == head and r.startswith('refs/heads/') and r != 'refs/heads/'+branch]))
        for name in names:
            sha = resolve(name)
            common = git(repo, 'merge-base', head, sha) if sha else ''
            if common:
                distance = git(repo, 'rev-list', '--count', common+'..'+head)
                candidates.append((int(distance or 0), name, common))
        if candidates:
            _, parent, base = min(candidates)
    if not base:
        return dict(empty, note='基线未知，等待开工记录', merge=dict(empty['merge'], known=True))
    if not parent:
        aliases = [r.removeprefix('refs/heads/') for r, sha in known.items() if sha == base and r.startswith('refs/heads/') and r != 'refs/heads/'+branch]
        parent = next((n for n in aliases if n == 'h/lovart-shell'), aliases[0] if aliases else '基线 '+base[:8])
    def ancestor(a, b):
        # merge-base --is-ancestor uses exit status, not stdout.
        try:
            return subprocess.run(['nice', '-n', '19', 'git', '-c', f'safe.directory={repo}', '-C', str(repo), 'merge-base', '--is-ancestor', a, b], capture_output=True, timeout=3).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    own = git(repo, 'log', '--no-show-signature', '--format=%h|%cI|%s', '-100', base+'..'+head, '--')
    commits = [line.split('|', 2) for line in own.splitlines() if line.count('|') >= 2]
    count = int(git(repo, 'rev-list', '--count', base+'..'+head) or 0)
    changed = len(git(repo, 'diff', '--name-only', base, head, '--').splitlines())
    inherited = int(git(repo, 'rev-list', '--count', base) or 0)
    integrated = any(ancestor(head, sha) for n in ('integration/v2-wiring', 'h/trunk') if (sha := resolve(n)))
    main = any(ancestor(head, sha) for n in ('main', 'origin/main') if (sha := resolve(n)))
    milestones = []
    if count:
        for n in ('integration/v2-wiring', 'h/trunk', 'main'):
            if sha := resolve(n):
                # Exact branch-head merge evidence, not current ref modification time.
                for line in git(repo, 'log', '--merges', '--format=%H|%cI|%P', '--ancestry-path', head+'..'+sha).splitlines()[::-1]:
                    parts = line.split('|')
                    if len(parts) == 3 and head in parts[2].split():
                        milestones.append(dict(time=parts[1], stage='合入 '+n, source='git merge '+parts[0][:8]))
                        break
    value = dict(commits=commits, note='还没有新提交' if not count else '本卡自己的提交（最多展示 100 条）', baseline=base, head=head, commit_count=count, changed_files=changed, inherited_count=inherited, parent=parent, merge=dict(submitted=count > 0, integration=integrated, main=main, known=True), milestones=milestones)
    if len(CACHE) > 500:
        CACHE.clear()
    CACHE[key] = value
    return value
