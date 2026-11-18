"""Run queries, history import, process reconciliation and isolated remote mirror."""
from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from signals import CST, TERMINAL, atomic_json, epoch, event, load_json, locked, normalize, now, parse_line, redact, rows, safe_id, update_run

DEFAULT_AGENTS = ['Codex gpt-6-astra', 'Codex gpt-6.1-sol', 'Claude Code fable', 'Claude Code opus', 'Kiro gpt-5.6-sol', 'zcode GLM-5.3', 'Devin', 'Grok']


def effective_run(record, current=None):
    r = dict(record)
    current = time.time() if current is None else current
    seen = epoch(r.get('last_seen_at') or r.get('started_at'))
    age = current - seen
    if r.get('state') == 'running' and age > 180:
        r.update(state='lost', reason='heartbeat_stale')
    if r.get('state') == 'lost' and age > 1800:
        r.update(state='lost_ended', ended_at=r.get('ended_at') or datetime.fromtimestamp(seen + 1800, CST).isoformat(), duration_seconds=max(0, seen + 1800 - epoch(r.get('started_at'))))
    ident = normalize(r.get('agent', ''), r.get('model', ''))
    r.update(agent_id=ident['id'], model=ident['model'])
    return redact(r)


def all_runs(board):
    return sorted((effective_run(r) for p in (Path(board) / 'runs').glob('*.json') if (r := load_json(p)) and r.get('id')), key=lambda r: r.get('started_at', ''), reverse=True)


def messages(board, run_id, after_seq=0):
    return [redact(r) for r in rows(Path(board) / 'runs' / f'{safe_id(run_id)}.messages.jsonl') if r.get('seq', 0) > after_seq]


def events(board, since=''):
    items = list(rows(Path(board) / 'events' / 'events.jsonl'))
    if since:
        if str(since).isdigit():
            items = [r for r in items if r.get('seq', 0) > int(since)]
        else:
            idx = next((i for i, r in enumerate(items) if r.get('id') == since), None)
            items = items[idx + 1:] if idx is not None else [r for r in items if r.get('ts', '') > since]
    return redact(items)


def log_tail(record, board=None):
    # Never follow arbitrary paths supplied through a run record.
    base = (Path(board) / 'runs').resolve() if board else Path(record.get('log', '/nonexistent')).parent.resolve()
    path = Path(record.get('log') or base / (record['id'] + '.log'))
    if board:
        path = base / (record['id'] + '.log')
    try:
        if not path.resolve().is_relative_to(base):
            return ''
        with path.open('rb') as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 262144))
            raw = f.read().decode('utf-8', errors='replace')
        output = []
        for line in raw.splitlines():
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    output.extend(content for kind, content, _, _ in parse_line(line) if kind in ('text', 'error'))
                else:
                    output.append(line)
            except ValueError:
                output.append(line)
        return '\n'.join(redact('\n'.join(output)).splitlines()[-80:])
    except OSError:
        return ''


def pid_alive(pid):
    try:
        pid = int(pid)
        if pid <= 1:
            return False
        os.kill(pid, 0)
        # kill(0) treats zombies as alive.
        stat = Path(f'/proc/{pid}/stat').read_text()
        return stat.rsplit(')', 1)[1].strip().split()[0] != 'Z'
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError, IndexError):
        return False


def reconcile(board, check_pid=True, current=None):
    current = current or time.time()
    changed = []
    for path in (Path(board) / 'runs').glob('*.json'):
        r = load_json(path, {})
        if r.get('state') not in ('running', 'lost') or r.get('backfilled'):
            continue
        patch = None
        if check_pid and r.get('pid') and not pid_alive(r['pid']):
            patch = dict(state='failed', reason='process_gone', ended_at=now(), duration_seconds=max(0, current - epoch(r['started_at'])))
        elif current - epoch(r.get('last_seen_at', r.get('started_at'))) > 180 and r['state'] != 'lost':
            patch = dict(state='lost', reason='heartbeat_stale')
        elif r['state'] == 'lost' and current - epoch(r.get('last_seen_at')) <= 180:
            patch = dict(state='running', reason='')
        if patch:
            path = Path(board) / 'runs' / f'{r["id"]}.json'
            with locked(str(path) + '.lock'):
                latest = load_json(path, {})
                # A late finish/heartbeat wins over a stale reconciliation snapshot.
                if latest.get('state') != r['state'] or latest.get('last_seen_at') != r.get('last_seen_at'):
                    continue
                latest.update(patch)
                atomic_json(path, latest)
                event(board, 'run:status', run_id=r['id'], card=r.get('card'), agent=r.get('agent'), **patch)
                changed.append(latest)
    return changed


def agents(board, cards):
    grouped = {normalize(a)['id']: dict(normalize(a), runs=[]) for a in DEFAULT_AGENTS}
    runs = all_runs(board)
    feed = events(board)
    for run in runs:
        ident = normalize(run.get('agent', ''), run.get('model', ''))
        group = grouped.setdefault(ident['id'], dict(ident, runs=[]))
        group['runs'].append(run)
    for group in grouped.values():
        records = group.pop('runs')
        running = [r for r in records if r['state'] == 'running']
        queued = [r for r in records if r['state'] == 'queued']
        ended = [r for r in records if r['state'] in TERMINAL]
        current = (running or queued or [None])[0]
        latest = current or next((r for r in records if r['state'] not in ('lost', 'lost_ended')), records[0] if records else None)
        card_keys = {str(r.get('card')) for r in records}
        complete_cards = [c for c in cards if c['status'] == 'accepted' and (c['key'] in card_keys or c['id'] in card_keys) and normalize(c.get('assignee', ''))['id'] == group['id']]
        commit = next((e for e in reversed(feed) if e.get('type') == 'git:commit' and latest and e.get('branch') == latest.get('branch') and epoch(e.get('ts')) >= epoch(latest.get('started_at'))), None)
        availability_path = load_json(Path(board) / 'agents.json', {})
        # Host wrappers publish CLI availability; containers do not probe their own PATH.
        availability = availability_path.get(group['cli'])
        if isinstance(availability, dict):
            availability = availability.get('available')
        if any(r['state'] == 'completed' and time.time() - epoch(r.get('ended_at')) < 86400 for r in records):
            availability = True
        if latest:
            group['model'] = normalize(latest.get('agent', ''), latest.get('model', ''))['model']
        group.update(availability='available' if availability else 'unavailable' if availability is False else 'unknown', workload='running' if running else 'queued' if queued else 'idle', current_run=current, latest_run=latest, recent_runs=records[:10], active_runs=running, success_rate=round(sum(r['state'] == 'completed' for r in ended) / len(ended), 3) if ended else None, total_runs=len(records), reasoning=latest.get('reasoning', '') if latest else group['reasoning'], totals=dict(commits=sum(r.get('commits', 0) for r in records), changed_files=sum(r.get('changed_files', 0) for r in records), completed_cards=len(complete_cards)), latest_commit=commit)
    today = datetime.now(CST).date()
    def is_today(ts):
        return bool(epoch(ts)) and datetime.fromtimestamp(epoch(ts), CST).date() == today
    commits = {e['sha'] for e in feed if e['type'] == 'git:commit' and e.get('sha') and is_today(e.get('ts'))}
    entered = {e['card'] for e in feed if e['type'] == 'card:status' and e.get('state') in ('review', 'accepted') and is_today(e.get('ts'))}
    return dict(agents=list(grouped.values()), summary=dict(completed_today=sum(r['state'] == 'completed' and is_today(r.get('ended_at')) for r in runs), new_commits=len(commits), review_accepted=len(entered), **card_summary(cards, feed), running_agents=sum(g['workload'] == 'running' for g in grouped.values())), now=now())


def card_summary(cards, feed, current=None):
    today = datetime.fromtimestamp(current or time.time(), CST).date()
    def is_today(ts):
        return bool(epoch(ts)) and datetime.fromtimestamp(epoch(ts), CST).date() == today
    result = {}
    for state in ('review', 'accepted'):
        entered = {e.get('card') for e in feed if e.get('type') == 'card:status' and e.get('state') == state and is_today(e.get('occurred_at') or e.get('ts'))}
        # Historical reports provide evidence when collection began after the transition.
        for c in cards:
            for rep in c.get('reports', []):
                pattern = r'已验收|验收通过' if state == 'accepted' else r'开发完成|待.*验收'
                if is_today(rep.get('time')) and re.search(pattern, rep.get('text', '')):
                    if state != 'accepted' or not re.search(r'未通过|不通过', rep.get('text', '')):
                        entered.add(c['key'])
        result[state + '_today'] = len({next((c['key'] for c in cards if e in (c['id'], c['key'])), e) for e in entered})
        result[state + '_total'] = sum(c['status'] == state for c in cards)
    return result


def backfill(board):
    """Import explicit timestamped reports only; deterministic IDs make startup idempotent."""
    board = Path(board)
    native = [r for r in all_runs(board) if not r.get('backfilled')]
    records = []
    for path in sorted(board.rglob('*-progress.md')) + [board / 'BOARD.md']:
        if not path.exists():
            continue
        text = path.read_text(errors='replace')
        is_board = path.name == 'BOARD.md'
        if is_board:
            if '自动汇报（Agent 钩子）' not in text:
                continue
            text = text.split('自动汇报（Agent 钩子）', 1)[1]
        card_path = path.with_name(path.name.replace('-progress.md', '.md'))
        meta = {}
        if card_path.exists() and not is_board:
            for m in re.finditer(r'^(assignee|branch|worktree):\s*["\x27]?(.*?)["\x27]?\s*$', card_path.read_text(), re.M):
                meta[m[1]] = m[2]
        for line in text.splitlines():
            match = re.match(r'^\s*[-*]?\s*(\d{4}-\d\d-\d\d[ T]\d\d:\d\d(?::\d\d)?)\s*(?:CST|\+08:00)?\s*\|\s*(.*)$', line)
            if not match:
                continue
            ts = match[1].replace(' ', 'T') + '+08:00'
            fields = [p.strip() for p in match[2].split('|')]
            card = fields.pop(0) if is_board else str(card_path.relative_to(board).with_suffix(''))
            if '/' not in card and not is_board:
                card = card_path.stem
            agent = fields[0] if fields and not fields[0].startswith(('commit', '无项目')) else meta.get('assignee', '')
            if not agent or agent.lower() in ('hark', 'agent', '—'):
                continue
            body = fields[-1]
            bracket = re.search(r'【([^】]+)】', body)
            status = bracket[1] if bracket else body
            # Unstructured prose may mention failures/tests; only import clear state reports.
            if bracket:
                state = 'timeout' if '超时' in status else 'failed' if re.search('失败|受阻|阻塞', status) else 'cancelled' if '取消' in status else 'completed' if re.search('开发完成|待验收', status) else 'running' if re.search('开始|开工', status) else None
            else:
                state = 'completed' if re.search(r'开发完成|待 Hark 验收', body) else 'running' if re.search(r'开工|开始实现', body) else None
            if state:
                records.append(dict(card=card, agent=agent, ts=ts, state=state, summary=redact(body)[:600], source=str(path.relative_to(board)), **meta))
    pending = {}
    imported = 0
    seen = set()
    for record in sorted(records, key=lambda r: r['ts']):
        identity = normalize(record['agent'])
        short_card = record['card'].split('/')[-1]
        dedup = (short_card, identity['id'], record['ts'], record['state'])
        if dedup in seen:
            continue
        seen.add(dedup)
        if any(r.get('card', '').split('/')[-1] == short_card and normalize(r.get('agent', ''), r.get('model', ''))['id'] == identity['id'] and (abs(epoch(r['started_at']) - epoch(record['ts'])) < 30 or abs(epoch(r.get('ended_at')) - epoch(record['ts'])) < 30) for r in native):
            continue
        key = (short_card, identity['id'])
        if record['state'] == 'running':
            if key in pending:
                imported += save_history(board, pending[key], None)
            pending[key] = record
        else:
            imported += save_history(board, pending.pop(key, record), record)
    for start in pending.values():
        imported += save_history(board, start, None)
    return imported


def save_history(board, start, end):
    run_id = 'history-' + hashlib.sha256(f'{start["card"].split("/")[-1]}|{normalize(start["agent"])["id"]}|{start["ts"]}'.encode()).hexdigest()[:24]
    path = board / 'runs' / f'{run_id}.json'
    existing = load_json(path)
    if existing and (not end or existing.get('state') != 'lost' or not existing.get('backfilled')):
        return 0
    ident = normalize(start['agent'])
    changes = dict(id=run_id, run_id=run_id, card=start['card'], agent=start['agent'], model=ident['model'], reasoning=ident['reasoning'], cli=ident['cli'], branch=start.get('branch', ''), worktree=start.get('worktree', ''), state=end['state'] if end else 'lost', reason='' if end else 'historical_open', started_at=start['ts'], ended_at=end['ts'] if end else None, final_summary=end['summary'] if end else start['summary'], duration_seconds=max(0, epoch(end['ts']) - epoch(start['ts'])) if end else None, backfilled=True, source=start['source'], commits=0, changed_files=0, pid=None, started_unknown=start is end)
    rc = re.search(r'退出码\s*(\d+)', end['summary']) if end else None
    changes['exit_code'] = int(rc[1]) if rc else None
    update_run(board, run_id, changes)
    event(board, 'run:status', run_id=run_id, card=start['card'], agent=start['agent'], state=changes['state'], backfilled=True, summary=changes['final_summary'], occurred_at=(end or start)['ts'])
    return 1


class Mirror:
    def __init__(self, source, data):
        self.source = Path(source)
        self.data = Path(data)
        self.path = self.data / 'repo.git'

    def git(self, cwd, *args, timeout=40):
        env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0')
        config = self.data / 'gitconfig'
        if config.exists():
            env['GIT_CONFIG_GLOBAL'] = str(config)
        env.setdefault('GIT_SSH_COMMAND', 'ssh -o BatchMode=yes -o ConnectTimeout=10')
        return subprocess.run(['nice', '-n', '19', 'git', '-c', f'safe.directory={cwd}', '-c', f'safe.directory={self.source}', '-c', f'safe.directory={self.source / ".git"}', '-C', str(cwd), *args], capture_output=True, text=True, timeout=timeout, env=env, check=True).stdout.strip()

    def poll(self, board):
        self.data.mkdir(parents=True, exist_ok=True)
        # Multiple previews/production instances share a lock, but never block each other.
        with (self.data / 'fetch.lock').open('a') as lock:
            try:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            try:
                # upload-pack only trusts protected config. Use a private global config,
                # including the user's read-only credential configuration, never edit it.
                inherited = os.environ.get('GIT_CONFIG_GLOBAL', str(Path.home() / '.gitconfig'))
                config = '[include]\n path = ' + json.dumps(inherited) + '\n[safe]\n'
                config += ''.join(' directory = ' + json.dumps(str(p)) + '\n' for p in (self.source, self.source / '.git', self.path))
                (self.data / 'gitconfig').write_text(config)
                remote = self.git(self.source, 'remote', 'get-url', 'origin')
                if not (self.path / 'board-mirror-ready').exists():
                    staging = self.data / 'repo.init.git'
                    if staging.exists():
                        shutil.rmtree(staging)
                    self.git(self.data, 'clone', '--mirror', '--no-hardlinks', str(self.source), str(staging), timeout=180)
                    (staging / 'board-mirror-ready').write_text('v1\n')
                    if self.path.exists():
                        shutil.rmtree(self.path)
                    staging.replace(self.path)
                self.git(self.path, 'remote', 'set-url', 'origin', remote)
                self.git(self.path, 'config', 'remote.origin.mirror', 'false')
                self.git(self.path, 'config', '--replace-all', 'remote.origin.fetch', '+refs/heads/*:refs/remotes/origin/*')
                before = load_json(self.data / 'remote-refs.json')
                if before is None:
                    before = self.refs()
                self.git(self.path, 'fetch', '--prune', 'origin', '+refs/heads/*:refs/remotes/origin/*')
                after = self.refs()
                for branch in sorted(before.keys() | after.keys()):
                    if before.get(branch) == after.get(branch):
                        continue
                    sha = after.get(branch, '')
                    meta = self.git(self.path, 'show', '-s', '--format=%s%n%an', sha).splitlines() if sha else []
                    changed = len(self.git(self.path, 'diff-tree', '--root', '--no-commit-id', '--name-only', '-r', sha).splitlines()) if sha else 0
                    event(board, 'git:push', branch=branch, sha=sha, previous_sha=before.get(branch, ''), subject=meta[0] if meta else '远端分支已删除', author=meta[1] if len(meta) > 1 else '', changed_files=changed, summary=meta[0] if meta else '远端分支已删除')
                atomic_json(self.data / 'remote-refs.json', after)
                atomic_json(self.data / 'fetch-status.json', {'last_ok_at': now(), 'error': None})
            except (OSError, subprocess.SubprocessError) as error:
                # Don't expose remote URLs or credentials in API/logs.
                detail = getattr(error, 'stderr', '') or ''
                if isinstance(detail, bytes):
                    detail = detail.decode('utf-8', errors='replace')
                atomic_json(self.data / 'fetch-status.json', {'last_attempt_at': now(), 'error': type(error).__name__, 'detail': redact(detail)[-1000:]})

    def refs(self):
        output = self.git(self.path, 'for-each-ref', '--format=%(refname:strip=3) %(objectname)', 'refs/remotes/origin/')
        return dict(line.split(' ', 1) for line in output.splitlines() if line and not line.startswith('HEAD '))
