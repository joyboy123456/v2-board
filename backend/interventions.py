"""Safe, answer-only writes for dispatcher question cards."""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import re
import stat
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import board_parse as bp

DEMO_MARKER = '.v2-board-intervention-demo'
DEMO_MARKER_CONTENT = b'v2-board-intervention-demo-v1\n'
MAX_CARD_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 16 * 1024
MAX_ANSWER_CHARS = 4000
SECTION_RE = re.compile(r'(?m)^## (待回答|回答)[ \t]*(?:\r?\n|$)')
OTHER_HEADING_RE = re.compile(r'(?m)^## ')
ITEM_RE = re.compile(r'^- \[(Q[1-9][0-9]*)\] (.+)$')
KEY_RE = re.compile(r'^[^/\\\x00-\x1f\x7f]+/[^/\\\x00-\x1f\x7f]+$')
QUESTION_ID_RE = re.compile(r'^Q[1-9][0-9]*$')
REVISION_RE = re.compile(r'^[0-9a-f]{64}$')
FRONT_RE = re.compile(r'\A---(?P<nl>\r?\n)(?P<front>.*?)(?P=nl)---(?:\r?\n|\Z)', re.S)
DISPATCH_RE = re.compile(
    r"(?P<prefix>[ \t]*dispatch[ \t]*:[ \t]*)(?:(?P<quote>[\"'])(?P<quoted>[^\"']*)(?P=quote)|(?P<bare>[^#\s]+))(?P<suffix>[ \t]*(?:#.*)?)"
)


class InterventionError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)


def _open_absolute_directory(path: Path) -> int:
    absolute = Path(os.path.abspath(os.fspath(path)))
    fd = os.open(os.sep, _directory_flags())
    try:
        for part in absolute.parts[1:]:
            if part in ('', '.', '..'):
                raise OSError('invalid directory path')
            next_fd = os.open(part, _directory_flags(), dir_fd=fd)
            os.close(fd)
            fd = next_fd
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError('not a directory')
        return fd
    except Exception:
        os.close(fd)
        raise


def _marker_present(root: Path) -> bool:
    root_fd = -1
    marker_fd = -1
    try:
        root_fd = _open_absolute_directory(root)
        marker_fd = os.open(
            DEMO_MARKER,
            os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0),
            dir_fd=root_fd,
        )
        st = os.fstat(marker_fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > 128:
            return False
        return os.read(marker_fd, 129) == DEMO_MARKER_CONTENT
    except OSError:
        return False
    finally:
        if marker_fd >= 0:
            os.close(marker_fd)
        if root_fd >= 0:
            os.close(root_fd)


def capabilities(root: Path, environ=None) -> dict:
    env = os.environ if environ is None else environ
    requested = env.get('V2_INTERVENTION_MODE', 'readonly').strip().lower()
    if requested in ('enabled', 'demo'):
        configured_root = env.get('V2_BOARD_DIR', '').strip()
        if not configured_root:
            return dict(mode='readonly', reason='board-root-required')
        if os.path.abspath(os.fspath(root)) != os.path.abspath(configured_root):
            return dict(mode='readonly', reason='board-root-mismatch')
    if requested == 'enabled':
        token = env.get('V2_INTERVENTION_TOKEN', '')
        if not token or token != token.strip() or any(char.isspace() for char in token):
            return dict(mode='readonly', reason='token-not-configured')
        return dict(mode='enabled')
    if requested == 'demo':
        if not env.get('V2_BOARD_DIR', '').strip():
            return dict(mode='readonly', reason='demo-root-required')
        if _marker_present(Path(root)):
            return dict(mode='demo')
        return dict(mode='readonly', reason='demo-marker-missing')
    return dict(mode='readonly', reason='writes-disabled')


def _host_parts(authority: str, scheme: str) -> tuple[str, str, int]:
    if not authority or ',' in authority or any(char.isspace() for char in authority):
        raise ValueError('invalid host')
    parsed = urlsplit('//' + authority)
    if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or not parsed.hostname:
        raise ValueError('invalid host')
    hostname = parsed.hostname
    try:
        hostname = str(ipaddress.ip_address(hostname)).lower()
    except ValueError:
        hostname = hostname.encode('idna').decode('ascii').lower().rstrip('.')
    port = parsed.port or (443 if scheme == 'https' else 80)
    return scheme, hostname, port


def _origin_parts(origin: str) -> tuple[str, str, int]:
    parsed = urlsplit(origin)
    scheme = parsed.scheme.lower()
    if scheme not in ('http', 'https') or not parsed.netloc or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        raise ValueError('invalid origin')
    if parsed.username or parsed.password or not parsed.hostname:
        raise ValueError('invalid origin')
    hostname = parsed.hostname
    try:
        hostname = str(ipaddress.ip_address(hostname)).lower()
    except ValueError:
        hostname = hostname.encode('idna').decode('ascii').lower().rstrip('.')
    port = parsed.port or (443 if scheme == 'https' else 80)
    return scheme, hostname, port


def validate_origin(headers, request_scheme: str, environ=None) -> None:
    env = os.environ if environ is None else environ
    if headers.get('sec-fetch-site', '').lower() == 'cross-site':
        raise InterventionError(403, 'cross_site_request', '跨站写入请求已拒绝。')
    origin = headers.get('origin', '')
    if not origin:
        raise InterventionError(403, 'origin_required', '写入请求必须来自看板同源页面。')
    scheme = request_scheme.lower()
    host = headers.get('host', '')
    if env.get('V2_INTERVENTION_TRUST_PROXY_HEADERS') == '1':
        forwarded_host = headers.get('x-forwarded-host', '')
        forwarded_proto = headers.get('x-forwarded-proto', '')
        if forwarded_host:
            host = forwarded_host
        if forwarded_proto:
            if ',' in forwarded_proto or forwarded_proto.strip().lower() not in ('http', 'https'):
                raise InterventionError(403, 'invalid_forwarded_origin', '代理来源信息不明确，写入请求已拒绝。')
            scheme = forwarded_proto.strip().lower()
    try:
        expected = _host_parts(host, scheme)
        supplied = _origin_parts(origin)
    except (UnicodeError, ValueError):
        raise InterventionError(403, 'invalid_origin', '写入请求来源无法验证。')
    if expected != supplied:
        raise InterventionError(403, 'cross_origin_request', '写入请求必须来自看板同源页面。')


def authorize(mode: str, authorization: str, environ=None) -> None:
    if mode == 'demo':
        return
    env = os.environ if environ is None else environ
    expected = env.get('V2_INTERVENTION_TOKEN', '')
    if mode != 'enabled' or not expected or expected != expected.strip() or any(char.isspace() for char in expected):
        raise InterventionError(503, 'writes_disabled', '看板写入未启用。')
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != 'bearer':
        raise InterventionError(401, 'unauthorized', '请输入有效的看板写入令牌。')
    if not hmac.compare_digest(parts[1].encode('utf-8'), expected.encode('utf-8')):
        raise InterventionError(401, 'unauthorized', '写入令牌无效，请检查后重试。')


def validate_content_type(value: str) -> None:
    if value.split(';', 1)[0].strip().lower() != 'application/json':
        raise InterventionError(415, 'json_required', '请求必须使用 application/json。')


async def request_json(request) -> dict:
    content_length = request.headers.get('content-length', '')
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BYTES:
                raise InterventionError(413, 'request_too_large', '请求内容过大。')
        except ValueError:
            raise InterventionError(400, 'invalid_content_length', '请求长度无效。')
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_REQUEST_BYTES:
            raise InterventionError(413, 'request_too_large', '请求内容过大。')
        chunks.append(chunk)
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('duplicate JSON key')
            value[key] = item
        return value

    try:
        value = json.loads(b''.join(chunks).decode('utf-8'), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, ValueError):
        raise InterventionError(400, 'invalid_json', '请求必须是有效的 JSON。')
    if not isinstance(value, dict):
        raise InterventionError(422, 'invalid_request', '请求内容必须是对象。')
    return value


def _sanitize_payload(payload: dict) -> tuple[str, str, str, str]:
    allowed = {'card_key', 'question_id', 'answer', 'expected_revision'}
    if set(payload) != allowed:
        raise InterventionError(422, 'invalid_request', '请求字段缺失或包含不支持的字段。')
    card_key = payload['card_key']
    question_id = payload['question_id']
    answer = payload['answer']
    revision = payload['expected_revision']
    if not isinstance(card_key, str) or len(card_key) > 256 or not KEY_RE.fullmatch(card_key):
        raise InterventionError(422, 'invalid_card_key', '任务卡标识无效。')
    if any(part in ('', '.', '..') for part in card_key.split('/')):
        raise InterventionError(422, 'invalid_card_key', '任务卡标识无效。')
    if not isinstance(question_id, str) or not QUESTION_ID_RE.fullmatch(question_id):
        raise InterventionError(422, 'invalid_question_id', '问题编号无效。')
    if not isinstance(revision, str) or not REVISION_RE.fullmatch(revision):
        raise InterventionError(422, 'invalid_revision', '卡片版本无效，请刷新后重试。')
    if not isinstance(answer, str):
        raise InterventionError(422, 'invalid_answer', '回答必须是文本。')
    flattened = ''.join(' ' if unicodedata.category(char) in ('Cc', 'Cf', 'Cs') else char for char in answer)
    flattened = ' '.join(flattened.split())
    if not flattened:
        raise InterventionError(422, 'empty_answer', '回答不能为空。')
    if len(flattened) > MAX_ANSWER_CHARS:
        raise InterventionError(422, 'answer_too_long', f'回答不能超过 {MAX_ANSWER_CHARS} 个字符。')
    return card_key, question_id, flattened, revision


def _protocol_sections(body: str) -> tuple[dict[str, str], dict[str, str], re.Match | None]:
    for line in body.splitlines():
        if line.startswith('##') and line[2:].strip() in ('待回答', '回答') and not re.fullmatch(r'## (?:待回答|回答)[ \t]*', line):
            raise InterventionError(409, 'malformed_protocol', '问题或回答分区标题与 dispatcher 协议不一致，未写入。')
    headings = list(SECTION_RE.finditer(body))
    asked = [heading for heading in headings if heading.group(1) == '待回答']
    answered = [heading for heading in headings if heading.group(1) == '回答']
    if len(asked) != 1 or len(answered) > 1:
        raise InterventionError(409, 'malformed_protocol', '问题或回答分区缺失、重复或不明确，未写入。')

    def entries(heading, section_name):
        if heading is None:
            return {}
        end_match = OTHER_HEADING_RE.search(body, heading.end())
        section = body[heading.end():end_match.start() if end_match else len(body)]
        found = {}
        for line in section.splitlines():
            if not line.strip():
                continue
            if not line.lstrip().startswith('- ['):
                continue
            match = ITEM_RE.fullmatch(line)
            if not match or not match.group(2).strip():
                raise InterventionError(409, 'malformed_protocol', f'{section_name}分区包含格式不明确的条目，未写入。')
            question_id, value = match.groups()
            if question_id in found:
                raise InterventionError(409, 'ambiguous_question_ids', f'{section_name}分区包含重复问题编号，未写入。')
            found[question_id] = value
        return found

    return entries(asked[0], '待回答'), entries(answered[0] if answered else None, '回答'), answered[0] if answered else None


def _dispatch_frontmatter(text: str) -> tuple[re.Match, str, str, int, str]:
    match = FRONT_RE.match(text)
    if not match:
        raise InterventionError(409, 'malformed_frontmatter', '任务卡 frontmatter 格式不明确，未写入。')
    front = match.group('front')
    lines = front.splitlines(keepends=True)
    fields = []
    offset = 0
    for index, line in enumerate(lines):
        content = line[:-2] if line.endswith('\r\n') else line[:-1] if line.endswith('\n') else line
        if re.match(r'^[ \t]*dispatch[ \t]*:', content):
            fields.append((index, offset, content, line[len(content):]))
        offset += len(line)
    if len(fields) != 1:
        raise InterventionError(409, 'malformed_frontmatter', '任务卡必须包含唯一的 dispatch 状态，未写入。')
    index, offset, content, ending = fields[0]
    field = DISPATCH_RE.fullmatch(content)
    if not field:
        raise InterventionError(409, 'malformed_frontmatter', '任务卡 dispatch 状态格式不明确，未写入。')
    value = field.group('quoted') if field.group('quote') else field.group('bare')
    if value != 'asking':
        raise InterventionError(409, 'wrong_dispatch_state', '任务卡已不在等待回答状态。')
    return match, front, content, offset, ending


def _append_answer(body: str, heading_match: re.Match | None, answer_line: str, newline: str) -> str:
    if heading_match is None:
        if body.endswith(newline * 2):
            separator = ''
        elif body.endswith(newline):
            separator = newline
        else:
            separator = newline * 2
        return body + separator + f'## 回答{newline}{answer_line}{newline}'
    end_match = OTHER_HEADING_RE.search(body, heading_match.end())
    end = end_match.start() if end_match else len(body)
    section = body[heading_match.end():end]
    lines = section.splitlines(keepends=True)
    last_content = -1
    for index, line in enumerate(lines):
        if line.strip():
            last_content = index
    split = sum(len(line) for line in lines[:last_content + 1])
    before, after = section[:split], section[split:]
    if before and not before.endswith(newline):
        before += newline
    updated = before + answer_line + newline + after
    return body[:heading_match.end()] + updated + body[end:]


def answer_history(body: str) -> list[dict]:
    headings = [heading for heading in SECTION_RE.finditer(body) if heading.group(1) == '回答']
    if len(headings) != 1:
        return []
    heading = headings[0]
    end_match = OTHER_HEADING_RE.search(body, heading.end())
    section = body[heading.end():end_match.start() if end_match else len(body)]
    history = []
    for line in section.splitlines():
        match = ITEM_RE.fullmatch(line)
        if not match:
            continue
        question_id, payload = match.groups()
        timestamp, separator, value = payload.partition(' | ')
        actor = '人工'
        if separator and value.startswith('user: '):
            actor, value = 'user', value[6:]
        elif not separator:
            value = timestamp
            timestamp = ''
        history.append(dict(id=question_id, time=timestamp, actor=actor, answer=value))
    return history


def _read_locked(fd: int) -> bytes:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise InterventionError(400, 'invalid_card_file', '任务卡必须是普通文件。')
    if st.st_size > MAX_CARD_BYTES:
        raise InterventionError(413, 'card_too_large', '任务卡超过可安全修改的大小限制。')
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    size = 0
    while True:
        chunk = os.read(fd, min(65536, MAX_CARD_BYTES + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_CARD_BYTES:
            raise InterventionError(413, 'card_too_large', '任务卡超过可安全修改的大小限制。')
    return b''.join(chunks)


def _open_card(root_fd: int, relative: Path) -> int:
    parts = relative.parts
    if not parts or any(part in ('', '.', '..') or '/' in part or '\\' in part for part in parts):
        raise InterventionError(400, 'invalid_card_path', '任务卡路径无效。')
    directory_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, _directory_flags(), dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        flags = os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0)
        fd = os.open(parts[-1], flags, dir_fd=directory_fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise InterventionError(400, 'invalid_card_file', '任务卡必须是普通文件。')
        return fd
    except OSError:
        raise InterventionError(400, 'invalid_card_path', '任务卡路径包含无效目录或符号链接。')
    finally:
        os.close(directory_fd)


def write_answer(root: Path, payload: dict, mode: str, environ=None) -> dict:
    env = os.environ if environ is None else environ
    current = capabilities(root, env)
    if current['mode'] != mode or mode == 'readonly':
        raise InterventionError(403, 'writes_disabled', '看板写入未启用。')
    card_key, question_id, answer_text, expected_revision = _sanitize_payload(payload)
    if not KEY_RE.fullmatch(card_key) or any(part in ('', '.', '..') for part in card_key.split('/')):
        raise InterventionError(422, 'invalid_card_key', '任务卡标识无效。')
    root_path = Path(os.path.abspath(os.fspath(root)))
    root_fd = -1
    card_fd = -1
    try:
        root_fd = _open_absolute_directory(root_path)
        try:
            hits = [path for sprint, path in bp.discover(root_path) if f'{sprint}/{path.stem}' == card_key]
        except OSError:
            raise InterventionError(503, 'board_unavailable', '看板数据目录暂不可用。')
        if not hits:
            raise InterventionError(404, 'card_not_found', '没有找到这张任务卡。')
        if len(hits) != 1:
            raise InterventionError(409, 'ambiguous_card_key', '任务卡标识不唯一，未写入。')
        try:
            relative = hits[0].relative_to(root_path)
        except ValueError:
            raise InterventionError(400, 'invalid_card_path', '任务卡路径不在看板目录中。')
        card_fd = _open_card(root_fd, relative)
        fcntl.flock(card_fd, fcntl.LOCK_EX)
        raw = _read_locked(card_fd)
        if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_revision):
            raise InterventionError(409, 'stale_revision', '卡片已更新，请刷新后核对问题再提交。')
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            raise InterventionError(409, 'malformed_card', '任务卡不是有效的 UTF-8 文本，未写入。')
        if b'\r' in raw and re.search(rb'(?<!\r)\n|\r(?!\n)', raw):
            raise InterventionError(409, 'malformed_card', '任务卡换行格式不一致，未写入。')
        newline = '\r\n' if b'\r\n' in raw else '\n'
        front_match, front, dispatch_line, dispatch_offset, dispatch_ending = _dispatch_frontmatter(text)
        body = text[front_match.end():]
        questions, answers, answer_heading = _protocol_sections(body)
        if not questions:
            raise InterventionError(409, 'malformed_protocol', '任务卡没有可回答的问题，未写入。')
        if any(answer_id not in questions for answer_id in answers):
            raise InterventionError(409, 'malformed_protocol', '回答分区包含未知问题编号，未写入。')
        if question_id not in questions:
            raise InterventionError(409, 'question_not_found', '该问题不在这张任务卡中。')
        if question_id in answers:
            raise InterventionError(409, 'already_answered', '该问题已经有回答，请刷新查看记录。')
        timestamp = datetime.now(bp.CST).strftime('%Y-%m-%d %H:%M:%S CST')
        answer_line = f'- [{question_id}] {timestamp} | user: {answer_text}'
        updated_body = _append_answer(body, answer_heading, answer_line, newline)
        remaining = len(set(questions) - set(answers) - {question_id})
        updated_front = front
        dispatch = 'asking'
        if remaining == 0:
            updated_line = DISPATCH_RE.fullmatch(dispatch_line)
            replacement = updated_line.group('prefix') + '"answered"' + updated_line.group('suffix') + dispatch_ending
            lines = front.splitlines(keepends=True)
            lines_with_dispatch = []
            offset = 0
            for line in lines:
                if offset == dispatch_offset:
                    lines_with_dispatch.append(replacement)
                else:
                    lines_with_dispatch.append(line)
                offset += len(line)
            updated_front = ''.join(lines_with_dispatch)
            dispatch = 'answered'
        updated_text = text[:front_match.start('front')] + updated_front + text[front_match.end('front'):]
        updated_match = FRONT_RE.match(updated_text)
        if not updated_match:
            raise InterventionError(409, 'malformed_frontmatter', '任务卡 frontmatter 无法安全更新。')
        updated_text = updated_text[:updated_match.end()] + updated_body
        updated_raw = updated_text.encode('utf-8')
        if len(updated_raw) > MAX_CARD_BYTES:
            raise InterventionError(413, 'card_too_large', '任务卡超过可安全修改的大小限制。')
        os.lseek(card_fd, 0, os.SEEK_SET)
        os.ftruncate(card_fd, 0)
        view = memoryview(updated_raw)
        while view:
            written = os.write(card_fd, view)
            view = view[written:]
        os.fsync(card_fd)
        revision = hashlib.sha256(updated_raw).hexdigest()
        return dict(card_key=card_key, question_id=question_id, revision=revision, card_body=updated_body,
                    remaining=remaining, dispatch=dispatch,
                    message='演示答案已记录；不会连接或启动 Agent。' if mode == 'demo' else '答案已记录，等待 dispatcher 检查。')
    except InterventionError:
        raise
    except (OSError, UnicodeError):
        raise InterventionError(400, 'card_unavailable', '任务卡无法安全读取或更新。')
    finally:
        if card_fd >= 0:
            os.close(card_fd)
        if root_fd >= 0:
            os.close(root_fd)
