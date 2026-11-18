"""Shared on-disk protocol. Standard library only, also installed beside host wrappers."""
from __future__ import annotations
import contextlib
import fcntl
import json
import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))
TERMINAL = {'completed', 'failed', 'cancelled', 'timeout'}
ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))')


def now():
    return datetime.now(CST).isoformat(timespec='milliseconds')


def epoch(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace(' CST', '+08:00').replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=CST)
        return parsed.timestamp()
    except (ValueError, TypeError):
        return 0


def root():
    return Path(os.environ.get('V2_BOARD_DIR', os.environ.get('BOARD_ROOT', '/srv/v2-wiring-board')))


def redact(value):
    if isinstance(value, dict):
        return {k: '[REDACTED]' if re.search(r'(?i)^(api[_-]?key|access[_-]?token|refresh[_-]?token|token|access[_-]?key|secret[_-]?access[_-]?key|private[_-]?key|client[_-]?secret|password|secret|authorization)$', k) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if not isinstance(value, str):
        return value
    value = ANSI.sub('', value)
    value = re.sub(r'(?is)-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)', '[REDACTED PRIVATE KEY]', value)
    value = re.sub(r'(?i)\b(?:sk-(?:proj-)?[\w-]{8,}|gh[pousr]_[\w]{8,}|github_pat_[\w]{8,}|AKIA[A-Z0-9]{16})\b', '[REDACTED]', value)
    value = re.sub(r'(?i)(\b(?:[\w-]*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|access[_-]?key|secret[_-]?access[_-]?key|private[_-]?key|client[_-]?secret|password|secret)|authorization)\b["\x27]?\s*[:=]\s*["\x27]?)(?:Bearer\s+)?[^\s"\x27,;}]+', r'\1[REDACTED]', value)
    value = re.sub(r'(?i)(Bearer\s+)[\w.\-+/=]+', r'\1[REDACTED]', value)
    value = re.sub(r'(https?://)[^/\s:@]+:[^/\s@]+@', r'\1[REDACTED]@', value)
    return value


@contextlib.contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+', encoding='utf-8') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield f


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, ensure_ascii=False)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o664)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def update_run(board, run_id, changes, only_active=False):
    path = Path(board) / 'runs' / (safe_id(run_id) + '.json')
    with locked(str(path) + '.lock'):
        record = load_json(path, {})
        if only_active and record.get('state') not in ('running', 'lost'):
            return record
        record.update(redact(changes))
        atomic_json(path, record)
        return record


def safe_id(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', value):
        raise ValueError('invalid run id')
    return value


def append_jsonl(path, record):
    # Byte offsets are essential: a tail seek may land inside a UTF-8 character.
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0, 2)
        size = f.tell()
        offset = max(0, size - 65536)
        f.seek(offset)
        tail = f.read()
        if tail and not tail.endswith(b'\n'):
            end = tail.rfind(b'\n') + 1
            f.truncate(offset + end)
            tail = tail[:end]
        previous = 0
        for line in reversed(tail.decode('utf-8', errors='replace').splitlines()):
            try:
                previous = int(json.loads(line)['seq'])
                break
            except (ValueError, KeyError, TypeError):
                continue
        record = redact(dict(record, seq=previous + 1))
        f.seek(0, 2)
        f.write((json.dumps(record, ensure_ascii=False) + '\n').encode('utf-8'))
        f.flush()
        return record


def event(board, kind, **fields):
    return append_jsonl(Path(board) / 'events' / 'events.jsonl', dict(id=uuid.uuid4().hex, ts=now(), type=kind, **fields))


def rows(path):
    try:
        with Path(path).open() as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def message(board, run_id, kind, content, tool='', **extra):
    content = redact(str(content))[:12000]
    record = append_jsonl(Path(board) / 'runs' / f'{safe_id(run_id)}.messages.jsonl', dict(run_id=run_id, ts=now(), type=kind, tool=tool, summary=content[:300], content=content, **extra))
    # One run lock protects counters and explicit-progress precedence.
    with locked(Path(board) / 'runs' / f'{run_id}.json.lock'):
        path = Path(board) / 'runs' / f'{run_id}.json'
        run = load_json(path, {})
        changes = {'last_message': record['summary'], 'last_message_seq': record['seq']}
        if kind == 'text' and not run.get('progress'):
            changes['current_activity'] = record['summary']
        if kind == 'file_change':
            changes['recent_files'] = extra.get('files', [])[:10]
        if kind == 'tool_use':
            changes['tool_calls'] = run.get('tool_calls', 0) + 1
        run.update(changes)
        atomic_json(path, run)
    return record


def parse_line(line):
    """Codex JSONL, Claude stream-json and generic text; never execute output."""
    try:
        obj = json.loads(line)
    except ValueError:
        return [('text', line.rstrip(), '', {})] if line.strip() else []
    if not isinstance(obj, dict):
        return [('text', line.rstrip(), '', {})]
    typ = obj.get('type', '')
    item = obj.get('item') or {}
    if typ in ('item.started', 'item.updated', 'item.completed'):
        it = item.get('type')
        if it == 'agent_message':
            return [('text', item.get('text', ''), '', {})] if typ != 'item.started' else []
        if it == 'reasoning':
            return [('thinking', item.get('text', ''), '', {})]
        if it in ('command_execution', 'mcp_tool_call', 'web_search'):
            tool = item.get('tool', it)
            content = item.get('command') or item.get('query') or json.dumps(item.get('arguments', {}), ensure_ascii=False)
            if typ == 'item.completed':
                content += '\n' + str(item.get('aggregated_output', item.get('result', '')))
            return [('tool_use' if typ == 'item.started' else 'tool_result', content, tool, {})]
        if it == 'file_change':
            files = [x.get('path', '') for x in item.get('changes', [])]
            return [('file_change', '\n'.join(files), 'file_change', {'files': files})]
        if it == 'error':
            return [('error', item.get('message', str(item)), '', {})]
    if typ in ('error', 'turn.failed'):
        return [('error', str(obj.get('message') or obj.get('error') or obj), '', {})]
    if typ in ('assistant', 'user'):
        content = obj.get('message', {}).get('content', [])
        if isinstance(content, str):
            return [('text', content, '', {})]
        out = []
        for block in content:
            bt = block.get('type')
            if bt in ('text', 'thinking'):
                out.append((bt, block.get(bt, ''), '', {}))
            elif bt in ('tool_use', 'tool_result'):
                out.append((bt, json.dumps(block.get('input', block.get('content', '')), ensure_ascii=False), block.get('name', block.get('tool_use_id', '')), {}))
        return out
    if typ == 'stream_event':
        delta = obj.get('event', {}).get('delta', {})
        text = delta.get('text', delta.get('thinking', ''))
        return [('thinking' if 'thinking' in delta else 'text', text, '', {})] if text else []
    if typ == 'result':
        return [('error' if obj.get('is_error') else 'text', obj.get('result', ''), '', {})]
    return []


# Product identity policy: only Codex is split by model. Shared by host and API.
AGENT_PRODUCTS = (
    ('codex', ('codex',), 'codex', 'Codex', 'gpt-6.1-sol', True),
    ('claude-code', ('claude', 'fable', 'opus'), 'claude', 'Claude Code', 'opus', False),
    ('kiro', ('kiro',), 'kiro-cli', 'Kiro', 'GPT 5.6', False),
    ('zcode', ('zcode', 'glm'), 'zcode', 'zcode', 'GLM-5.3', False),
    ('devin', ('devin',), 'devin', 'Devin', 'swe-2-high', False),
    ('grok', ('grok',), 'grok', 'Grok', 'grok', False),
)


def normalize(name, model=''):
    value = (str(model) + ' ' + str(name)).lower()
    reasoning = next((x for x in ('ultra', 'xhigh', 'high', 'medium', 'low') if re.search(r'\b' + x + r'\b', value)), '')
    product = next((p for p in AGENT_PRODUCTS if any(alias in value for alias in p[1])), None)
    if not product:
        return dict(id=re.sub(r'[^\w.-]+', '-', name or 'unknown').lower(), name=name or '未知 Agent', model=model, cli='', reasoning=reasoning)
    ident, _, cli, title, fallback, split = product
    found = re.search(r'gpt-[\d.]+-(?:astra|sol)|glm-[\d.]+|fable(?:-[\d.]+)?|opus(?:-[\d.]+)?|swe-[\d]+-high', value)
    selected = found.group() if found else fallback
    if ident == 'kiro':
        selected = 'GPT 5.6'
    elif selected.lower().startswith('glm'):
        selected = selected.upper()
    return dict(id=ident + ('-' + selected.lower() if split else ''), name=title, model=selected, cli=cli, reasoning=reasoning)
