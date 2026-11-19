"""Card files: Markdown with a front matter block. Standard library only.

The dispatcher owns one front matter key, `dispatch`, so it never fights
board-report over `status`:
  queued   -> waiting for a slot
  running  -> an executor is working on it
  asking   -> the executor asked a person a question and ended its turn
  answered -> a person answered; the dispatcher resumes the same session
  done     -> the executor finished (board status is review)
  failed   -> the executor failed or timed out
  held     -> a person paused it; the dispatcher ignores it
  closed   -> accepted or dropped by a person; the dispatcher ignores it

Two more keys carry the board's 停止 button: the board writes 'stop' (a request),
the dispatcher ends the turn, removes it and writes 'stopped' next to 'held'.
A message to a session held that way reopens it; one held by hand stays held.
"""
from __future__ import annotations

import fcntl
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
FM = re.compile(r'\A---\n(.*?)\n---\n?', re.S)
STATES = ('queued', 'running', 'asking', 'answered', 'done', 'failed', 'held', 'closed')


def stamp():
    return datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S CST')


def parse(text):
    m = FM.match(text)
    meta = {}
    if m:
        for line in m.group(1).splitlines():
            k = re.match(r'^([A-Za-z_][\w-]*):\s*(.*)$', line)
            if k:
                meta[k.group(1)] = k.group(2).strip().strip('"\'')
    body = text[m.end():] if m else text
    return meta, body


def set_keys(text, **values):
    m = FM.match(text)
    head = m.group(1) if m else ''
    for key, value in values.items():
        line = f'{key}: "{value}"'
        if re.search(rf'^{re.escape(key)}:.*$', head, re.M):
            head = re.sub(rf'^{re.escape(key)}:.*$', lambda _: line, head, count=1, flags=re.M)
        else:
            head = (head + '\n' if head else '') + line
    return f'---\n{head}\n---\n' + (text[m.end():] if m else text)


def drop_keys(text, *keys):
    m = FM.match(text)
    if not m:
        return text
    head = '\n'.join(line for line in m.group(1).split('\n') if line.split(':', 1)[0] not in keys)
    return f'---\n{head}\n---\n' + text[m.end():]


@contextmanager
def edit(path):
    """Locked read-modify-write. Yields a one-item list holding the text."""
    with open(path, 'r+', encoding='utf-8') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        box = [f.read()]
        before = box[0]
        yield box
        if box[0] != before:
            f.seek(0)
            f.truncate()
            f.write(box[0])


def update(path, **values):
    with edit(path) as box:
        box[0] = set_keys(box[0], **values)


def read(path):
    return parse(Path(path).read_text(encoding='utf-8'))


def find(board, card):
    hits = sorted(Path(board).rglob(f'{card}.md'))
    return hits[0] if hits else None


def scan(board):
    """Every card that opted in to dispatch."""
    out = []
    for path in sorted(Path(board).rglob('*.md')):
        if path.name.endswith('-progress.md'):
            continue
        try:
            meta, body = read(path)
        except (OSError, UnicodeDecodeError):
            continue
        if meta.get('dispatch') in STATES:
            out.append((path, meta, body))
    return out


# Questions and answers live in the card body, readable by people and by the board.
Q_HEAD = '## 待回答'
A_HEAD = '## 回答'
Q_LINE = re.compile(r'^- \[(Q\d+)\] (.*)$', re.M)


def _section(body, head):
    m = re.search(rf'^{re.escape(head)}\s*$\n(.*?)(?=^## |\Z)', body, re.M | re.S)
    return m.group(1) if m else ''


def questions(body):
    return {q: t for q, t in Q_LINE.findall(_section(body, Q_HEAD))}


def answers(body):
    return {q: t for q, t in Q_LINE.findall(_section(body, A_HEAD))}


def open_questions(body):
    a = answers(body)
    return {q: t for q, t in questions(body).items() if q not in a}


def _append(text, head, line):
    if re.search(rf'^{re.escape(head)}\s*$', text, re.M):
        return re.sub(rf'(^{re.escape(head)}\s*$\n(?:(?!^## ).*\n?)*)', lambda m: m.group(1).rstrip('\n') + '\n' + line + '\n', text, count=1, flags=re.M)
    return text.rstrip('\n') + f'\n\n{head}\n{line}\n'


# 留言：人在看板对话窗口里对 Agent 说的话。front matter 的 delivered 记已经送进会话的条数。
M_HEAD = '## 留言'
M_LINE = re.compile(r'^- \[(M\d+)\] (.*)$', re.M)


def messages(body):
    return M_LINE.findall(_section(body, M_HEAD))


def delivered(meta):
    try:
        return max(0, int(meta.get('delivered') or 0))
    except ValueError:
        return 0


def message_text(raw):
    """'2026-11-19 22:24:55 CST | user: 你好' -> '你好'（送给 Agent 的只要正文）。"""
    rest = raw.split(' | ', 1)[1] if ' | ' in raw else raw
    return rest.split(': ', 1)[1] if ': ' in rest else rest


def pending_messages(meta, body):
    return messages(body)[delivered(meta):]


def add_message(path, text, actor='user'):
    """Record a message for the agent. A finished session is reopened; a live one picks it up after its turn."""
    with edit(path) as box:
        meta, body = parse(box[0])
        mid = f'M{len(messages(body)) + 1}'
        box[0] = _append(box[0], M_HEAD, f'- [{mid}] {stamp()} | {actor}: {" ".join(str(text).split())}')
        if meta.get('dispatch') in ('done', 'failed') or (meta.get('dispatch') == 'held' and meta.get('stopped')):
            box[0] = set_keys(box[0], dispatch='answered')
    return mid


def add_question(path, question, options=(), agent=''):
    with edit(path) as box:
        meta, body = parse(box[0])
        qid = f'Q{len(questions(body)) + 1}'
        opts = f' | 选项：{" / ".join(options)}' if options else ''
        who = f' | {agent}' if agent else ''
        box[0] = _append(box[0], Q_HEAD, f'- [{qid}] {stamp()}{who} | {question}{opts}')
        box[0] = set_keys(box[0], dispatch='asking')
    return qid


def add_answer(path, qid, answer):
    with edit(path) as box:
        meta, body = parse(box[0])
        if qid not in questions(body):
            raise KeyError(qid)
        box[0] = _append(box[0], A_HEAD, f'- [{qid}] {stamp()} | {answer}')
        if not open_questions(parse(box[0])[1]):
            box[0] = set_keys(box[0], dispatch='answered')
