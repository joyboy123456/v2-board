"""看板对话窗口：人对 Agent 说的话记进任务卡的「## 留言」，dispatcher 在下一轮把它送进同一个会话。

看板只写卡片，不启动、不停止任何 Agent；CLI 跑完一轮才会读到留言，做不到中途打断。
"""
from __future__ import annotations

from datetime import datetime
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import unicodedata

import board_parse as bp
import interventions as iv
import repairs
from signals import normalize

MAX_MESSAGE_CHARS = 4000
M_SECTION = re.compile(r'(?m)^## 留言[ \t]*(?:\r?\n|$)')
M_ITEM = re.compile(r'^- \[(M[1-9][0-9]*)\] (.+)$')
Q_SECTION = re.compile(r'(?m)^## 待回答[ \t]*(?:\r?\n|$)')
Q_ITEM = re.compile(r'^- \[(Q[1-9][0-9]*)\] (.+)$')
STATES = ('queued', 'running', 'asking', 'answered', 'done', 'failed', 'held', 'closed')
REOPEN = ('done', 'failed')  # 这一轮已经结束：留言让 dispatcher 接着原会话再跑一轮
NOTES = {
    'queued': '留言已记录，会随任务一起交给 Agent。',
    'running': '留言已记录。Agent 正在跑这一轮，这一轮结束后会接着同一个会话收到。',
    'asking': '留言已记录。Agent 在等上面的问题，问题答完后会和回答一起收到。',
    'answered': '留言已记录，Agent 下一轮开始时收到。',
    'held': '留言已记录。这个会话已暂停，恢复后才会送达。',
}


def _section(body: str, heading: re.Pattern) -> tuple[re.Match | None, str]:
    hits = list(heading.finditer(body))
    if len(hits) != 1:
        return None, ''
    end = iv.OTHER_HEADING_RE.search(body, hits[0].end())
    return hits[0], body[hits[0].end():end.start() if end else len(body)]


def _stamped(payload: str) -> tuple[str, str]:
    stamp, sep, rest = payload.partition(' | ')
    return (stamp.strip(), rest) if sep and iv.STAMP_RE.fullmatch(stamp.strip()) else ('', payload)


def delivered_count(meta: dict) -> int:
    try:
        return max(0, int(str(meta.get('delivered') or 0)))
    except ValueError:
        return 0


def chat_log(meta: dict, body: str) -> list[dict]:
    """卡片里的问答和留言，按写入顺序；前端再和执行记录按时间合在一起。"""
    out = []
    answered = {a['id'] for a in iv.answer_history(body or '')}
    for line in _section(body or '', Q_SECTION)[1].splitlines():
        m = Q_ITEM.fullmatch(line)
        if not m:
            continue
        time, rest = _stamped(m.group(2))
        parts = [x.strip() for x in rest.split(' | ')]
        options = next((x[3:].split(' / ') for x in parts if x.startswith('选项：')), [])
        text = [x for x in parts if not x.startswith('选项：')]
        out.append(dict(kind='question', id=m.group(1), time=time, actor=text[0] if len(text) > 1 else '', text=text[-1] if text else rest,
                        options=options, open=m.group(1) not in answered))
    for a in iv.answer_history(body or ''):
        out.append(dict(kind='answer', id=a['id'], time=a['time'], actor=a['actor'], text=a['answer']))
    sent = delivered_count(meta)
    index = 0
    for line in _section(body or '', M_SECTION)[1].splitlines():
        m = M_ITEM.fullmatch(line)
        if not m:
            continue
        time, rest = _stamped(m.group(2))
        actor, sep, text = rest.partition(': ')
        out.append(dict(kind='message', id=m.group(1), time=time, actor=actor if sep else '', text=text if sep else rest, delivered=index < sent))
        index += 1
    return out


def executor_agents(root: Path, mode: str, environ=None) -> dict[str, dict]:
    """执行器名字 → 它在「人与产出」里归到哪个 Agent。只读配置里的名字和模型，不暴露命令。"""
    env = os.environ if environ is None else environ
    if mode != 'enabled':
        return {}
    try:
        cfg = json.loads(Path(env.get('DISPATCH_CONFIG', '/etc/v2-dispatch.json')).read_text(encoding='utf-8'))
        if Path(cfg.get('board', '')).resolve() != Path(root).resolve():
            return {}
        out = {}
        for name, spec in (cfg.get('executors') or {}).items():
            if not isinstance(name, str) or not repairs.EXECUTOR_RE.fullmatch(name) or not isinstance(spec, dict):
                continue
            cmd = [str(x) for x in spec.get('cmd', [])]
            model = next((cmd[i + 1] for i, arg in enumerate(cmd[:-1]) if arg in ('-m', '--model')), '')
            label = str(spec.get('agent') or name)
            # resume 可用才算能续聊；没有的话 dispatcher 只能从头再给一遍任务
            out[name] = dict(agent=normalize(label, model)['id'], label=label, resume=isinstance(spec.get('resume'), list) and bool(spec['resume']))
        return out
    except (OSError, ValueError, AttributeError, TypeError):
        return {}


def _text(value, limit: int) -> str:
    if not isinstance(value, str):
        raise iv.InterventionError(422, 'invalid_message', '消息必须是文本。')
    flat = ' '.join(''.join(' ' if unicodedata.category(ch) in ('Cc', 'Cf', 'Cs') else ch for ch in value).split())
    if not flat:
        raise iv.InterventionError(422, 'empty_message', '消息不能为空。')
    if len(flat) > limit:
        raise iv.InterventionError(422, 'message_too_long', f'消息不能超过 {limit} 个字符。')
    return flat


def _dispatch_line(front: str):
    fields, offset = [], 0
    for line in front.splitlines(keepends=True):
        content = line[:-2] if line.endswith('\r\n') else line[:-1] if line.endswith('\n') else line
        if re.match(r'^[ \t]*dispatch[ \t]*:', content):
            fields.append((offset, content, line[len(content):]))
        offset += len(line)
    if len(fields) != 1:
        raise iv.InterventionError(409, 'not_dispatched', '这张卡不是通过看板调度器派出的，没有可以续上的会话。')
    offset, content, ending = fields[0]
    field = iv.DISPATCH_RE.fullmatch(content)
    if not field:
        raise iv.InterventionError(409, 'malformed_frontmatter', '任务卡 dispatch 状态格式不明确，未写入。')
    return offset, field, ending, field.group('quoted') if field.group('quote') else field.group('bare')


def _append(body: str, heading: re.Match | None, line: str, newline: str) -> str:
    if heading is None:
        gap = '' if body.endswith(newline * 2) else newline if body.endswith(newline) else newline * 2
        return body + gap + f'## 留言{newline}{line}{newline}'
    end_match = iv.OTHER_HEADING_RE.search(body, heading.end())
    end = end_match.start() if end_match else len(body)
    lines = body[heading.end():end].splitlines(keepends=True)
    keep = max((i for i, text in enumerate(lines) if text.strip()), default=-1) + 1
    before, after = ''.join(lines[:keep]), ''.join(lines[keep:])
    if before and not before.endswith(newline):
        before += newline
    head = body[:heading.end()]
    return (head if head.endswith('\n') else head + newline) + before + line + newline + after + body[end:]


def write_message(root: Path, payload: dict, mode: str, environ=None) -> dict:
    env = os.environ if environ is None else environ
    if iv.capabilities(root, env)['mode'] != mode or mode == 'readonly':
        raise iv.InterventionError(403, 'writes_disabled', '看板写入未启用。')
    if not isinstance(payload, dict) or set(payload) != {'card_key', 'message', 'expected_revision'}:
        raise iv.InterventionError(422, 'invalid_request', '请求字段缺失或包含不支持的字段。')
    key, revision = payload['card_key'], payload['expected_revision']
    if not isinstance(key, str) or len(key) > 256 or not iv.KEY_RE.fullmatch(key) or any(p in ('', '.', '..') for p in key.split('/')):
        raise iv.InterventionError(422, 'invalid_card_key', '任务卡标识无效。')
    if not isinstance(revision, str) or not iv.REVISION_RE.fullmatch(revision):
        raise iv.InterventionError(422, 'invalid_revision', '卡片版本无效，请刷新后重试。')
    message = _text(payload['message'], MAX_MESSAGE_CHARS)
    root_path = Path(os.path.abspath(os.fspath(root)))
    root_fd = card_fd = -1
    try:
        root_fd = iv._open_absolute_directory(root_path)
        try:
            hits = [path for sprint, path in bp.discover(root_path) if f'{sprint}/{path.stem}' == key]
        except OSError:
            raise iv.InterventionError(503, 'board_unavailable', '看板数据目录暂不可用。')
        if len(hits) != 1:
            raise iv.InterventionError(404 if not hits else 409, 'card_not_found', '没有找到唯一对应的任务卡。')
        card_fd = iv._open_card(root_fd, hits[0].relative_to(root_path))
        fcntl.flock(card_fd, fcntl.LOCK_EX)
        raw = iv._read_locked(card_fd)
        if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), revision):
            raise iv.InterventionError(409, 'stale_revision', '卡片已更新，请刷新后再发送。')
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            raise iv.InterventionError(409, 'malformed_card', '任务卡不是有效的 UTF-8 文本，未写入。')
        if b'\r' in raw and re.search(rb'(?<!\r)\n|\r(?!\n)', raw):
            raise iv.InterventionError(409, 'malformed_card', '任务卡换行格式不一致，未写入。')
        newline = '\r\n' if b'\r\n' in raw else '\n'
        front = iv.FRONT_RE.match(text)
        if not front:
            raise iv.InterventionError(409, 'malformed_frontmatter', '任务卡 frontmatter 格式不明确，未写入。')
        offset, field, ending, state = _dispatch_line(front.group('front'))
        if state not in STATES:
            raise iv.InterventionError(409, 'not_dispatched', '这张卡不是通过看板调度器派出的，没有可以续上的会话。')
        if state == 'closed':
            raise iv.InterventionError(409, 'session_closed', '这个会话已经关闭，不能再发消息。')
        try:
            meta = bp.split_front(text)[0]
        except (ValueError, TypeError):
            raise iv.InterventionError(409, 'malformed_frontmatter', '任务卡 frontmatter 格式不明确，未写入。')
        if mode == 'demo' and not iv.is_demo_card(meta):
            raise iv.InterventionError(403, 'not_demo_card', '演示模式只能改写合成的演示卡片，这张卡没有改动。')
        body = text[front.end():]
        headings = list(M_SECTION.finditer(body))
        if len(headings) > 1 or any(line.startswith('##') and line[2:].strip() == '留言' and not re.fullmatch(r'## 留言[ \t]*', line) for line in body.splitlines()):
            raise iv.InterventionError(409, 'malformed_protocol', '留言分区重复或格式不明确，未写入。')
        count = sum(1 for line in _section(body, M_SECTION)[1].splitlines() if M_ITEM.fullmatch(line))
        message_id = f'M{count + 1}'
        stamp = datetime.now(bp.CST).strftime('%Y-%m-%d %H:%M:%S CST')
        body = _append(body, headings[0] if headings else None, f'- [{message_id}] {stamp} | user: {message}', newline)
        head = front.group('front')
        dispatch = state
        if mode == 'enabled' and state in REOPEN:
            dispatch = 'answered'
            head = head[:offset] + field.group('prefix') + '"answered"' + field.group('suffix') + ending + head[offset + len(field.group(0)) + len(ending):]
        updated = text[:front.start('front')] + head + text[front.end('front'):front.end()] + body
        if not iv.FRONT_RE.match(updated):
            raise iv.InterventionError(409, 'malformed_frontmatter', '任务卡 frontmatter 无法安全更新。')
        data = updated.encode('utf-8')
        if len(data) > iv.MAX_CARD_BYTES:
            raise iv.InterventionError(413, 'card_too_large', '任务卡超过可安全修改的大小限制。')
        try:
            iv._overwrite(card_fd, data)
        except OSError:
            try:
                iv._overwrite(card_fd, raw)
            except OSError:
                pass
            raise
        if mode == 'demo':
            note = '演示留言已记录；不会连接或启动 Agent。'
        elif dispatch != state:
            note = '留言已记录，dispatcher 有空槽位时会接着原来的会话让 Agent 继续。'
        else:
            note = NOTES.get(state, '留言已记录。')
        return dict(card_key=key, message_id=message_id, revision=hashlib.sha256(data).hexdigest(), card_body=body,
                    card_meta=bp.split_front(updated)[0], dispatch=dispatch, message=note)
    except iv.InterventionError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise iv.InterventionError(400, 'card_unavailable', '任务卡无法安全读取或更新。')
    finally:
        for fd in (card_fd, root_fd):
            if fd >= 0:
                os.close(fd)


def create_session(root: Path, payload: dict, mode: str, environ=None) -> dict:
    """在对话窗口里给某个 Agent 开一个新会话：写一张排队的任务卡，由 dispatcher 建 worktree 并启动。"""
    env = os.environ if environ is None else environ
    if iv.capabilities(root, env)['mode'] != mode or mode != 'enabled' or not repairs.enabled(mode, env):
        raise iv.InterventionError(403, 'sessions_disabled', '看板没有开启新建会话。')
    if not isinstance(payload, dict) or set(payload) != {'executor', 'message'}:
        raise iv.InterventionError(422, 'invalid_request', '请求字段缺失或包含不支持的字段。')
    executor = payload['executor']
    if not isinstance(executor, str) or executor not in {x['id'] for x in repairs.executors(root, mode, env)}:
        raise iv.InterventionError(422, 'unknown_executor', '这个执行器不在可指派列表里。')
    if not isinstance(payload['message'], str):
        raise iv.InterventionError(422, 'invalid_message', '消息必须是文本。')
    task = repairs._clean(payload['message'], MAX_MESSAGE_CHARS + 1)
    if not task:
        raise iv.InterventionError(422, 'empty_message', '消息不能为空。')
    if len(task) > MAX_MESSAGE_CHARS:
        raise iv.InterventionError(422, 'message_too_long', f'消息不能超过 {MAX_MESSAGE_CHARS} 个字符。')
    repo, base = repairs._repo(env), repairs.default_base(mode, env)
    if not base:
        raise iv.InterventionError(409, 'missing_base', '看板没有配置默认分支（V2_REPAIR_BASE），无法为新会话建独立 worktree。')
    root = Path(os.path.abspath(os.fspath(root)))
    now = datetime.now(bp.CST)
    token = secrets.token_hex(3)
    card_id = f'CHAT-{now.strftime("%m%d-%H%M")}-{token.upper()}'
    title = repairs._one_line(task.split('\n')[0], 48)
    text = '\n'.join([
        '---', f'id: "{card_id}"', f'title: "对话：{title}"', 'status: "open"', f'assignee: "{executor}"', f'executor: "{executor}"',
        'dispatch: "queued"', f'branch: "d/chat-{now.strftime("%m%d%H%M")}-{token}"', f'repo: "{repo}"', f'base: "{base}"',
        f'created: "{now.strftime("%Y-%m-%d %H:%M")}"', '---', '',
        f'# 对话：{title}', '', '这张卡由看板的对话窗口生成，下面是用户直接交给你的任务。', '',
        '## 任务', task, '',
        '## 要求',
        f'1. 你在从 {base} 分支开出的独立 worktree 里工作；只在本卡的分支上提交，不要自行合并、推送或部署。',
        '2. 只是提问或查看的话直接回答，不需要改代码。',
        '3. 密钥、令牌不得写进卡片、报告、日志或提交。',
        '4. 改了代码的话，做完用 board-report 在本卡汇报并把状态置为待验收（review）。', ''])
    folder = root / 'chat'
    if folder.is_symlink():
        raise iv.InterventionError(409, 'unsafe_target', '对话目录不安全，未写入。')
    folder.mkdir(exist_ok=True)
    final, temp = folder / f'{card_id}.md', folder / f'.{card_id}.{os.getpid()}.tmp'
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o644)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(text.encode('utf-8'))
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temp, final)
    except FileExistsError:
        raise iv.InterventionError(409, 'retry', '会话编号冲突，请再发一次。')
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass
    return dict(card_key=f'chat/{card_id}', card_id=card_id, executor=executor, dispatch='queued',
                message='会话已排队，dispatcher 有空槽位时启动这个 Agent；看板没有直接启动它。')
