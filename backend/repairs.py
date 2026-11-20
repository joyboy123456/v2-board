"""验收项「指派修复」：把失败原因和截图整理成一张独立修复卡，交给 dispatcher 排队。

只写新卡，不改原卡、不启动或停止任何 Agent；执行器只暴露名字，不暴露命令和配置。
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import unicodedata
from urllib.parse import urlsplit

import acceptance
import board_parse as bp
import interventions as iv

MAX_INSTRUCTIONS = 2000
REPAIRABLE = ('fail', 'partial', 'pending')
EXECUTOR_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
REPORT_ID_RE = re.compile(r'^[^/\\\x00-\x1f\x7f]{1,200}$')
BRANCH_RE = re.compile(r'[\w/][\w./-]*')
REPO_RE = re.compile(r'/[\w./-]+')
DEMO_EXECUTORS = [
    dict(id='demo-codex', name='Codex（演示）', browser='unknown'),
    dict(id='demo-claude', name='Claude Code（演示）', browser='unknown'),
]
FIELDS = {'card_key', 'expected_revision', 'report_id', 'expected_report_revision', 'item_index',
          'executor', 'instructions', 'requires_browser', 'test_mode'}


def enabled(mode: str, environ=None) -> bool:
    env = os.environ if environ is None else environ
    return mode == 'demo' or (mode == 'enabled' and env.get('V2_REPAIR_ENABLED') == '1')


def _repo(env) -> str:
    """不挂原卡的修复用哪个仓库：dispatcher 所在机器上的路径；看板跑在容器里时用 V2_REPAIR_REPO 写宿主路径。"""
    return str(env.get('V2_REPAIR_REPO') or env.get('V2_REPO_DIR') or '').strip()


def _board_repo(env) -> str:
    """看板自己的仓库在 dispatcher 所在机器上的路径；容器里挂载的路径和宿主不一样时用 V2_REPO_HOST_DIR 写宿主路径。"""
    return str(env.get('V2_REPO_HOST_DIR') or env.get('V2_REPO_DIR') or '').strip()


def default_base(mode: str, environ=None) -> str:
    """验收项没写任务卡时修复分支的起点；管理员没配 V2_REPAIR_BASE 就不能这样指派。"""
    env = os.environ if environ is None else environ
    base = str(env.get('V2_REPAIR_BASE') or '').strip()
    if mode != 'enabled' or not enabled(mode, env) or not BRANCH_RE.fullmatch(base) or not REPO_RE.fullmatch(_repo(env)):
        return ''
    return base


def executors(root: Path, mode: str, environ=None) -> list[dict]:
    """可指派的执行器。浏览器能力只有管理员显式声明过才标「已确认」，否则一律「未确认」。"""
    env = os.environ if environ is None else environ
    if mode == 'demo':
        return [dict(x) for x in DEMO_EXECUTORS]
    if mode != 'enabled':
        return []
    try:
        cfg = json.loads(Path(env.get('DISPATCH_CONFIG', '/etc/v2-dispatch.json')).read_text(encoding='utf-8'))
        if Path(cfg.get('board', '')).resolve() != Path(root).resolve():
            return []
        listed = cfg.get('executors')
        names = [k for k in listed if isinstance(k, str) and EXECUTOR_RE.fullmatch(k)] if isinstance(listed, dict) else []
    except (OSError, ValueError, AttributeError, TypeError):
        return []
    verified = {x.strip() for x in env.get('V2_BROWSER_EXECUTORS', '').split(',') if x.strip()}
    return [dict(id=k, name=k, browser='verified' if k in verified else 'unknown') for k in sorted(names)]


def _clean(value: str, limit: int) -> str:
    text = ''.join(' ' if unicodedata.category(ch) in ('Cc', 'Cf', 'Cs') and ch != '\n' else ch for ch in value)
    return '\n'.join(' '.join(line.split()) for line in text.split('\n')).strip()[:limit]


def _one_line(value: str, limit: int = 120) -> str:
    return ' '.join(_clean(value, limit * 2).replace('"', '”').replace('\\', '/').split())[:limit]


def _payload(payload: dict):
    if not isinstance(payload, dict) or set(payload) != FIELDS:
        raise iv.InterventionError(422, 'invalid_request', '请求字段缺失或包含不支持的字段。')
    key, rev, rid, rrev = (payload[k] for k in ('card_key', 'expected_revision', 'report_id', 'expected_report_revision'))
    if key == '' and rev == '':
        pass  # 验收项没写任务卡：不基于任何卡，从默认分支修
    elif not isinstance(key, str) or len(key) > 256 or not iv.KEY_RE.fullmatch(key) or any(p in ('', '.', '..') for p in key.split('/')):
        raise iv.InterventionError(422, 'invalid_card_key', '任务卡标识无效。')
    elif not isinstance(rev, str) or not iv.REVISION_RE.fullmatch(rev):
        raise iv.InterventionError(422, 'invalid_revision', '版本无效，请刷新后重试。')
    if not isinstance(rrev, str) or not iv.REVISION_RE.fullmatch(rrev):
        raise iv.InterventionError(422, 'invalid_revision', '版本无效，请刷新后重试。')
    if not isinstance(rid, str) or not REPORT_ID_RE.fullmatch(rid) or rid in ('.', '..'):
        raise iv.InterventionError(422, 'invalid_report', '验收报告标识无效。')
    index = payload['item_index']
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < 1000:
        raise iv.InterventionError(422, 'invalid_item', '验收项序号无效。')
    executor = payload['executor']
    if not isinstance(executor, str) or not EXECUTOR_RE.fullmatch(executor):
        raise iv.InterventionError(422, 'invalid_executor', '执行器无效。')
    notes = payload['instructions']
    if not isinstance(notes, str) or len(notes) > MAX_INSTRUCTIONS:
        raise iv.InterventionError(422, 'invalid_instructions', f'补充说明不能超过 {MAX_INSTRUCTIONS} 个字符。')
    if not isinstance(payload['requires_browser'], bool) or payload['test_mode'] not in ('real', 'fixture'):
        raise iv.InterventionError(422, 'invalid_request', '验收方式无效。')
    return key, rev, rid, rrev, index, executor, _clean(notes, MAX_INSTRUCTIONS), payload['requires_browser'], payload['test_mode']


def _shots(root: Path, item: dict) -> tuple[list[str], list[str], list[str]]:
    local, links, missing = [], [], []
    for shot in item.get('shots', []):
        src, alt = shot.get('src', ''), _one_line(shot.get('alt', '') or '截图', 80)
        if src.startswith('/api/file/'):
            rel = src[len('/api/file/'):]
            target = acceptance.safe_file(root, rel)
            if target and not (root / rel).is_symlink():
                local.append(f'- {alt}：`{target}`')
            else:
                missing.append(f'- {alt}（文件缺失或不可读）')
        elif src.startswith(('http://', 'https://')) and '@' not in urlsplit(src).netloc:
            links.append(f'- {alt}：{src}')
        else:
            missing.append(f'- {alt}（文件缺失或不可读）')
    return local, links, missing


def _body(source, key: str, base: str, report: dict, index: int, item: dict, shots, notes: str, browser: bool, test_mode: str, repair_id: str) -> str:
    local, links, missing = shots
    unknown = '、'.join(_one_line(str(c), 60) for c in item.get('cards', []))
    origin = ([f'- 原任务卡：{key}（{_one_line(source.get("title", ""))}）', f'- 原分支：{source.get("branch") or "未登记"}'] if source else
              ['- 原任务卡：' + (f'验收报告写的任务卡（{unknown}）在看板上找不到' if unknown else '验收报告没有写这一项对应哪张任务卡') + '，需要你自己定位相关代码',
               f'- 起点分支：{base}（看板配置的默认修复分支）'])
    context = [f'- {label}：{_one_line(report.get(field, ""), 200)}' for label, field in (('验收人', 'by'), ('验收环境', 'site')) if report.get(field)]
    out = [f'# 修复：{source["id"] + " · " if source else ""}{_one_line(item["title"])}', '',
           '这张卡由看板「指派修复」生成。只修下面这一项验收问题，不要顺手改无关内容。', '',
           '## 来源', *origin,
           f'- 验收报告：{report["id"]}（{_one_line(report.get("title", ""))}，{report.get("date", "")}）', *context,
           f'- 验收项：第 {index + 1} 项「{_one_line(item["title"])}」，结论：{item.get("verdictText") or item["verdict"]}', '',
           '## 报错与现象', item.get('text', '').strip() or '（报告没有写文字说明，请看截图）', '']
    if local:
        out += ['## 截图与录屏（服务器本地文件，可直接读取）', *local, '']
    if links:
        out += ['## 外部链接（未下载到本地）', *links, '']
    if missing:
        out += ['## 报告里引用但读不到的证据', *missing, '']
    if notes:
        out += ['## 用户补充说明', *(f'> {line}' for line in notes.split('\n')), '']
    out += ['## 验收要求',
            '1. 先复现问题，再修复；在本卡的独立 worktree 和分支上提交，不要自行合并。']
    if browser:
        mode = '默认真实接口模式（8 核 16G 云沙盒，会产生云沙盒和模型供应商费用）' if test_mode == 'real' else '`--fixture` 假接口模式（4 核 8G 云沙盒）'
        out += [
            '2. 用云浏览器验收：在 worktree 根目录先读 `.agents/skills/aora-cloud-browser/SKILL.md` 和 `scripts/cloud-browser/README.md`；'
            '旧 worktree 里没有这些文件时，改用原仓库里的同名绝对路径。',
            f'3. 为这个问题写一个场景脚本并运行 `./scripts/cloud-browser/run.sh{" --fixture" if test_mode == "fixture" else ""} <场景.mjs>`，使用{mode}，沙盒存活上限 40 分钟。',
            '4. 证据在 `.tmp/cloud-browser/<任务>/`；核对退出码、截图和状态，不要编造证据，也不要跳过断言。',
            '5. 跑完只回收自己的沙盒：`sbx-cloud rm -f <名称>`，再用 `sbx-cloud ls` 复核已删除。',
            '6. 密钥、令牌不得写进卡片、报告、日志或提交。']
    else:
        out += ['2. 这次不要求浏览器验收；运行相关自动化测试并写明结果。']
    named = f'同时写 {repair_id} 和 {source["id"]}' if source else f'写 {repair_id}'
    out += [f'{7 if browser else 3}. 完成后用 `board-report` 在本卡汇报，并在 `reports/` 写一份验收报告，「卡：」一行{named}，'
            '截图放进看板 `shots/` 目录。' + ('看板会把这份报告显示回原任务卡的验收页签。' if source else '看板会把这份报告显示在本卡的验收页签。'),
            f'{8 if browser else 4}. 汇报后把本卡状态置为待验收（review），等用户在看板上人工验收；测试通过不等于验收通过。', '']
    return '\n'.join(out)


def _names_known_card(root: Path, names: list[str]) -> bool:
    """验收项写的卡号里有没有看板上真实存在的卡；认法与指派时校验原卡一致：卡的 id 或「批次/文件名」。"""
    wanted = set(names)
    if not wanted:
        return False
    for sprint, path in bp.discover(root):
        if f'{sprint}/{path.stem}' in wanted:
            return True
        try:
            if str(bp.metadata(path, sprint)[0].get('id', '')) in wanted:
                return True
        except (OSError, ValueError):
            continue
    return False


def create(root: Path, payload: dict, mode: str, environ=None) -> dict:
    env = os.environ if environ is None else environ
    if iv.capabilities(root, env)['mode'] != mode or not enabled(mode, env):
        raise iv.InterventionError(403, 'repair_disabled', '看板未开启指派修复。')
    key, rev, rid, rrev, index, executor, notes, browser, test_mode = _payload(payload)
    if executor not in {x['id'] for x in executors(root, mode, env)}:
        raise iv.InterventionError(422, 'unknown_executor', '这个执行器不在可指派列表里。')
    root = Path(os.path.abspath(os.fspath(root)))
    source = None
    if key:
        hits = [path for sprint, path in bp.discover(root) if f'{sprint}/{path.stem}' == key]
        if len(hits) != 1:
            raise iv.InterventionError(404 if not hits else 409, 'card_not_found', '没有找到唯一对应的任务卡。')
        try:
            source, _, _, revision = bp.metadata(hits[0], key.split('/')[0], include_revision=True)
        except (OSError, ValueError):
            raise iv.InterventionError(409, 'malformed_card', '原任务卡读不出来，未创建修复卡。')
        if mode == 'demo' and not iv.is_demo_card(source):
            raise iv.InterventionError(403, 'not_demo_card', '演示模式只能从合成的演示卡片指派修复，没有建卡。')
        if not hmac.compare_digest(revision, rev):
            raise iv.InterventionError(409, 'stale_revision', '任务卡已更新，请刷新后再指派。')
    elif mode != 'enabled':
        raise iv.InterventionError(403, 'not_demo_card', '演示模式只能从合成的演示卡片指派修复，没有建卡。')
    paths = [p for p in sorted((root / 'reports').glob('*.md')) if p.stem == rid and not p.is_symlink() and p.is_file()] if (root / 'reports').is_dir() else []
    if len(paths) != 1:
        raise iv.InterventionError(404, 'report_not_found', '没有找到这份验收报告。')
    report = acceptance.parse(root, paths[0])
    if not hmac.compare_digest(report['revision'], rrev):
        raise iv.InterventionError(409, 'stale_report', '验收报告已更新，请刷新后再指派。')
    if index >= len(report['items']):
        raise iv.InterventionError(409, 'item_not_found', '这份报告里没有这一项。')
    item = report['items'][index]
    if source is not None and not any(c in (source['id'], key) for c in item['cards']):
        raise iv.InterventionError(409, 'item_mismatch', '这一项验收不属于这张任务卡。')
    if source is None and _names_known_card(root, item['cards']):
        raise iv.InterventionError(409, 'card_required', '这一项写了对应的任务卡，请选一张任务卡再指派。')
    if item['verdict'] not in REPAIRABLE:
        raise iv.InterventionError(409, 'item_not_repairable', '只有未通过、部分通过或未完成的验收项可以指派修复。')
    if source is None:
        repo, base = _repo(env), default_base(mode, env)
        if not base:
            raise iv.InterventionError(409, 'missing_base', '这一项没有对应看板上的任务卡，看板也没有配置默认修复分支（V2_REPAIR_BASE），无法为修复建独立 worktree。')
    else:
        # 原卡的分支在看板自己的仓库里；V2_REPAIR_REPO 只管不挂原卡的修复
        repo = str(source.get('repo') or '').strip() or _board_repo(env)
        base = str(source.get('branch') or source.get('base') or '')
        if mode == 'enabled' and (not repo or not base or not BRANCH_RE.fullmatch(base) or not REPO_RE.fullmatch(repo)):
            raise iv.InterventionError(409, 'missing_repo', '原任务卡没有登记仓库或分支，无法为修复建独立 worktree。')
    # 按验收项的内容而不是整份报告的版本定 id：报告别处改动后再指派同一项，仍是同一张卡
    content = '\0'.join((item['title'], item['verdictText'], item['text'], *(shot['src'] for shot in item['shots'])))
    digest = hashlib.sha256('\0'.join((key, rid, hashlib.sha256(content.encode()).hexdigest())).encode()).hexdigest()[:10]
    repair_id = f'REPAIR-{digest.upper()}'
    dispatch = 'queued' if mode == 'enabled' else 'held'
    stamp = datetime.now(bp.CST).strftime('%Y-%m-%d %H:%M')
    front = ['---', f'id: "{repair_id}"', f'title: "修复：{_one_line(str(source["id"]), 40) + " · " if source else ""}{_one_line(item["title"], 60)}"', 'status: "open"',
             f'assignee: "{executor}"', f'executor: "{executor}"', f'dispatch: "{dispatch}"', f'branch: "d/repair-{digest}"',
             *([f'repo: "{repo}"', f'base: "{base}"'] if mode == 'enabled' else ['demo: true']),
             f'created: "{stamp}"', *([f'repair_source: "{_one_line(key, 256)}"'] if source else []), f'repair_report: "{_one_line(rid, 200)}"', f'repair_item: "{index}"', '---', '']
    text = '\n'.join(front) + _body(source, key, base, report, index, item, _shots(root, item), notes, browser, test_mode, repair_id)
    folder = root / 'fix'
    if folder.is_symlink():
        raise iv.InterventionError(409, 'unsafe_target', '修复目录不安全，未写入。')
    folder.mkdir(exist_ok=True)
    final, temp = folder / f'{repair_id}.md', folder / f'.{repair_id}.{os.getpid()}.{secrets.token_hex(6)}.tmp'
    created = True
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o644)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(text.encode('utf-8'))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, final)  # 原子发布；同一验收项重复提交只会有一张卡
        except FileExistsError:
            created = False
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass
    if not created:
        message = '这一项已经指派过修复，没有重复建卡。'
    elif mode == 'demo':
        message = '演示：已生成合成修复卡（搁置状态，不会被任何 Agent 执行）。'
    else:
        message = '修复卡已排队，等 dispatcher 有空槽位时启动；看板没有直接启动 Agent。'
    return dict(repair_key=f'fix/{repair_id}', repair_id=repair_id, created=created, dispatch=dispatch, executor=executor, message=message)
