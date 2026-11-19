#!/usr/bin/env python3
"""V2 中文 Issue 看板，单文件、标准库、只读。默认端口 3142，支持 --host/--port。"""
import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlsplit

BOARD = Path(os.environ.get('V2_BOARD_DIR', '/srv/v2-wiring-board'))
BASELINES = {'wiring': '921d21d5', 'parity': 'd82d9311'}
SPRINT_NAMES = {'wiring': '接线 01–08', 'parity': '对标 G', 'h': 'H 冲刺', 'fix': '验收修复'}
STATUSES = ('open', 'assigned', 'in_progress', 'blocked', 'review', 'accepted', 'rejected', 'unknown')
LABELS = dict(zip(STATUSES, ('待指派', '已指派', '进行中', '阻塞', '待验收', '已验收', '已退回', '未定')))
FIELDS = ('id', 'title', 'sprint', 'status', 'assignee', 'branch', 'worktree', 'depends', 'priority', 'created', 'updated')
CST = timezone(timedelta(hours=8), 'CST')


def now_cst():
    return datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S CST')


def scalar(value):
    value = value.strip()
    if value in ('', 'null', '~'):
        return ''
    if value.startswith('"'):
        return json.loads(value)
    if value.startswith("'"):
        return value[1:-1].replace("''", "'")
    if value.startswith('['):
        try:
            return json.loads(value)
        except ValueError:
            return [scalar(v) for v in value.strip('[]').split(',') if v.strip()]
    return value.split(' #', 1)[0].strip()


def split_front(text):
    """读取协议的扁平 YAML 子集（标量及 depends 列表），正文逐字保留。"""
    match = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)', text, re.S)
    if not match:
        return {}, text
    data, current = {}, None
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        item = re.match(r'^\s+-\s+(.+)$', line)
        if item and current == 'depends':
            if not isinstance(data[current], list):
                data[current] = []
            data[current].append(str(scalar(item.group(1))))
            continue
        field = re.match(r'^([a-z_]+):\s*(.*?)\s*$', line)
        if not field:
            raise ValueError('front matter 仅支持扁平字段和 depends 列表')
        current = field.group(1)
        data[current] = scalar(field.group(2))
    return data, text[match.end():]


def front(data, body):
    keys = list(FIELDS) + [k for k in data if k not in FIELDS]
    return '---\n' + ''.join(k + ': ' + json.dumps(data.get(k, [] if k == 'depends' else ''), ensure_ascii=False) + '\n' for k in keys) + '---\n' + body


def infer_status(text):
    match = re.search(r'^\s*(?:[-*]\s*)?(?:\*\*)?状态(?:\*\*)?\s*[:：]\s*(.+)', text, re.M)
    state = match.group(1).strip('* ') if match else ''
    if state in STATUSES:
        return state
    for pattern, status in ((r'验收未通过|退回|拒绝', 'rejected'), (r'验收通过|已验收', 'accepted'),
                            (r'卡住|阻塞|等凭据', 'blocked'), (r'开发完成|待.*验收|合并完成', 'review'),
                            (r'未开工|未开始|待指派|待领取|未领取', 'open'), (r'进行|开发中|合并中|准备合并|开工', 'in_progress'), (r'已指派|Hark.*指派|已分配', 'assigned')):
        if re.search(pattern, state):
            return status
    return 'open'


def read_text(path):
    try:
        with path.open(encoding='utf-8', newline='') as stream:
            return stream.read()
    except FileNotFoundError:
        return ''


MAX_CARD_BYTES = 1024 * 1024


def read_card_bytes(card):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0)
    fd = os.open(card, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError('card is not a regular file')
        if st.st_size > MAX_CARD_BYTES:
            raise ValueError('card exceeds the safe read limit')
        chunks = []
        size = 0
        while True:
            chunk = os.read(fd, min(65536, MAX_CARD_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_CARD_BYTES:
                raise ValueError('card exceeds the safe read limit')
        return b''.join(chunks)
    finally:
        os.close(fd)


def metadata(card, sprint, include_revision=False):
    raw = read_card_bytes(card)
    text = raw.decode('utf-8')
    try:
        data, body = split_front(text)
    except (ValueError, TypeError):
        data, body = {}, text
    progress = read_text(card.with_name(card.stem + '-progress.md'))
    title = re.search(r'^#{1,6}\s+(.+)', body, re.M)
    defaults = dict(id=card.stem, title=title.group(1) if title else card.stem, sprint=sprint,
                    status=infer_status(progress or body), assignee='', branch='', worktree='', depends=[],
                    priority='P1', created='', updated='', capability='')
    missing = [k for k in ('id', 'title', 'status') if not data.get(k)]
    defaults.update({k: v for k, v in data.items() if v != ''})
    defaults['missing_fields'] = missing
    if not data.get('status'):
        defaults['status'] = 'unknown'
    if not defaults.get('assignee') and data.get('agent'):
        defaults['assignee'] = data['agent']
    if defaults['status'] not in STATUSES:
        defaults['missing_fields'].append('status（无效）')
        defaults['status'] = 'unknown'
    if not isinstance(defaults['depends'], list):
        defaults['depends'] = []
        defaults['missing_fields'].append('depends（无效）')
    result = defaults, body, progress
    if include_revision:
        return (*result, hashlib.sha256(raw).hexdigest())
    return result


def _card_probe(card):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0)
    try:
        fd = os.open(card, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, 6)
    except OSError:
        return None
    finally:
        os.close(fd)


def discover(root):
    """Every Markdown document inside a batch is a card; root keeps legacy IDs only."""
    excluded = {'runs', 'events', 'shots'}
    groups = [('wiring', root)] + [(p.name, p) for p in sorted(root.iterdir()) if p.is_dir() and not p.is_symlink() and not p.name.startswith('.') and p.name not in excluded]
    for sprint, directory in groups:
        for card in sorted(directory.glob('*.md')):
            if card.is_symlink() or card.stem.endswith('-progress'):
                continue
            if directory == root and not re.fullmatch(r'\d{2}', card.stem):
                continue
            head = _card_probe(card)
            if head is None:
                continue
            # A card is any batch document with front matter (even if id/title/status are
            # missing); prompts, notes and QA findings without front matter are not cards.
            if re.fullmatch(r'\d{2}', card.stem) or (head[3:] if head.startswith(b'\xef\xbb\xbf') else head).startswith(b'---'):
                yield sprint, card


def inline(s):
    s = html.escape(s)
    s = re.sub(r'`([^`]+)`', r'<code>\1</code>', s)
    s = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', s)
    return s


def md(text):
    out, lines, i = [], text.split('\n'), 0
    while i < len(lines):
        l = lines[i]
        if l.strip().startswith('```'):
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith('```'):
                j += 1
            out.append('<pre tabindex="0" role="region" aria-label="代码块"><code>' + html.escape('\n'.join(lines[i + 1:j])) + '</code></pre>')
            i = j + 1; continue
        if l.startswith('|'):
            rows = []
            while i < len(lines) and lines[i].startswith('|'):
                if not re.match(r'^\|[\s\-:|]+\|$', lines[i]):
                    rows.append([inline(c.strip()) for c in lines[i].strip('|').split('|')])
                i += 1
            if rows:
                out.append('<div class="tw"><table><tr>' + ''.join(f'<th scope="col">{c}</th>' for c in rows[0]) + '</tr>' +
                           ''.join('<tr>' + ''.join(f'<td>{c}</td>' for c in r) + '</tr>' for r in rows[1:]) + '</table></div>')
            continue
        m = re.match(r'^(#{1,4})\s+(.*)', l)
        if m:
            n = min(len(m.group(1)) + 2, 6)
            out.append(f'<h{n}>{inline(m.group(2))}</h{n}>')
        elif l.startswith('>'):
            out.append('<blockquote>' + inline(l.lstrip('> ')) + '</blockquote>')
        elif re.match(r'^\s*[-*]\s+', l):
            items = []
            while i < len(lines) and re.match(r'^\s*[-*]\s+', lines[i]):
                items.append('<li>' + inline(re.sub(r'^\s*[-*]\s+', '', lines[i])) + '</li>'); i += 1
            out.append('<ul>' + ''.join(items) + '</ul>'); continue
        elif l.strip():
            out.append('<p>' + inline(l) + '</p>')
        i += 1
    return '\n'.join(out)


# git 查询串行且缓存 30 秒；不执行 checkout/status/fetch 等操作。
GIT_CACHE = {}


def git_commits(data, sprint):
    from card_git import inspect_card
    info = inspect_card(data)
    return info['commits'], info['note'], info['baseline']


def sections(text):
    result, heading = {}, ''
    fence = False
    for line in text.splitlines():
        if line.lstrip().startswith('```'):
            fence = not fence
            continue
        if fence:
            continue
        match = re.match(r'^#{1,6}\s+(.+)', line)
        if match:
            heading = match.group(1).strip()
            result.setdefault(heading, [])
        elif heading and line.strip():
            result[heading].append(line.strip())
    return result


def decisions(text):
    result = []
    for heading, lines in sections(text).items():
        if not re.search('卡住|需要决定', heading):
            continue
        for line in lines:
            line = re.sub(r'^(?:[-*]|\d+[.、])\s*', '', line).strip()
            if re.match(r'^(?:无|暂无|没有|已解决|无开发阻塞)(?:[。；;，,：:]|$)', line):
                # “无开发阻塞。请 Hark 选择…”仍需显示。
                if re.search(r'请.*(?:选择|决定|确认)|待.*决定', line):
                    result.append(line)
                continue
            if line and line not in ('—', '-'):
                result.append(line)
    return result


def latest_report(progress, path):
    parts = sections(progress)
    reports = [re.sub(r'^[-*]\s*', '', line) for heading, lines in parts.items() if heading == '汇报' for line in lines]
    stamp = datetime.fromtimestamp(path.stat().st_mtime, CST).strftime('%Y-%m-%d %H:%M:%S CST') if path.exists() else '—'
    if reports:
        last = reports[-1]
        match = re.search(r'\d{4}-\d\d-\d\d[ T]\d\d:\d\d(?::\d\d)?\s*(?:CST|\+08:00)', last)
        return last, match.group() if match else stamp + '（文件更新）', '最近汇报'
    candidates = [re.sub(r'^[-*]\s*', '', line) for heading, lines in parts.items()
                  if re.search('已完成|步骤进展|开发收尾|最终交接|总结', heading) for line in lines if not line.startswith('|')]
    return (candidates[-1] if candidates else '暂无汇报'), stamp + '（文件更新）', '旧进度摘要'


def collect():
    cards, errors = [], []
    from runs import all_runs
    from card_git import inspect_card
    records = all_runs(BOARD)
    for sprint, path in discover(BOARD):
        try:
            data, body, progress, card_revision = metadata(path, sprint, include_revision=True)
            progress_path = path.with_name(path.stem + '-progress.md')
            legacy = not progress_path.exists()
            display_progress = progress if not legacy else body
            last, stamp, source = latest_report(display_progress, progress_path if not legacy else path)
            related = [r for r in records if r.get('card') in (sprint+'/'+path.stem, str(data['id'])) and (not r.get('branch') or r.get('branch') == data.get('branch'))]
            bases = [r for r in related if r.get('base_commit')]
            if bases:
                data['run_base'] = min(bases, key=lambda r: r.get('started_at', ''))['base_commit']
            git_info = inspect_card(data)
            commits, git_note, baseline = git_info['commits'], git_info['note'], git_info['baseline']
            cards.append(dict(data=data, sprint=sprint, key=sprint+'/'+path.stem, body=body, card_revision=card_revision,
                              progress=display_progress, legacy=legacy, last=last, stamp=stamp, source=source,
                              commits=commits, git_note=git_note, baseline=baseline, git_info=git_info, latest_run=next((r for r in related if not r.get('backfilled')), related[0] if related else None),
                              decisions=list(dict.fromkeys(decisions(display_progress)+decisions(body)))))
        except (OSError, ValueError, TypeError) as error:
            errors.append(f'{sprint}/{path.stem}：元数据无法读取（{type(error).__name__}），请检查任务卡。')
    return cards, errors


def sysinfo():
    loads = ' / '.join(f'{v:.2f}' for v in os.getloadavg())
    mem = {}
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            mem[key] = int(value.strip().split()[0])
        total = mem['MemTotal']; used = total - mem.get('MemAvailable', mem.get('MemFree', 0))
        memory = f'{used/1048576:.1f} / {total/1048576:.1f} GiB'
    except (OSError, ValueError, KeyError):
        memory = '暂不可读'
    disk = shutil.disk_usage('/')
    return f'负载 {loads} · {os.cpu_count() or 1} 核', f'内存 {memory}', f'磁盘 {disk.used/disk.total:.0%}'


def esc(value):
    return html.escape(str(value), quote=True)


def issue_href(key):
    return '#' + quote(key, safe='/')


def render_card(card):
    data = card['data']; key = card['key']; status = data['status']
    depends = []
    for dep in data['depends']:
        target = str(dep) if '/' in str(dep) else card['sprint']+'/'+str(dep)
        depends.append(f'<a href="{esc(issue_href(target))}">{esc(dep)}</a>')
    commit_html = ''.join(f'<li><code>{esc(sha)}</code> <time>{esc(stamp)}</time><p>{esc(subject)}</p></li>' for sha, stamp, subject in card['commits'])
    progress_html = '<p class="muted">旧进度保留在任务卡正文中，见上文。</p>' if card['legacy'] else md(card['progress'])
    return f'''<details class="issue" id="{esc(key)}" data-key="{esc(key)}" data-status="{status}">
<summary><span class="issue-id">{esc(data['id'])}</span><strong>{esc(data['title'])}</strong>
<span class="badge {status}">{LABELS[status]}</span><span class="owner">Hark 指派：{esc(data['assignee'] or '尚未指派')}</span>
<span class="branch">分支：<code>{esc(data['branch'] or '未登记')}</code></span>
<span class="dependencies">依赖：{'、'.join(esc(dep) for dep in data['depends']) or '无'}</span>
<span class="report"><span class="muted">{esc(card['source'])} · {esc(card['stamp'])}</span><span class="last-report">{inline(card['last'])}</span></span>
<span class="muted">提交 {len(card['commits'])} · 优先级 {esc(data['priority'])} · <span class="disclosure">详情</span></span></summary>
<div class="detail"><aside><p><a href="{esc(issue_href(key))}">任务直达链接</a></p><p>工作树：<code>{esc(data['worktree'] or '未登记')}</code></p>
<p>依赖：{'、'.join(depends) or '无'}</p>
<p>指派理由：{esc(data.get('capability') or '未记录')}</p>
<p>创建：{esc(data['created'] or '未记录')}<br>更新：{esc(data['updated'] or '未记录')}</p>
<h3>提交列表</h3><p class="muted">相对基线 <code>{esc(card['baseline'])}</code> · {esc(card['git_note'])}</p><ul class="commits">{commit_html or '<li>没有可展示的提交</li>'}</ul></aside>
<div class="documents"><h3>完整任务卡</h3>{md(card['body'])}<hr><h3>进度</h3>{progress_html}</div></div></details>'''


def render_inner():
    cards, errors = collect()
    counts = {status: sum(c['data']['status'] == status for c in cards) for status in STATUSES}
    stat_html = ''.join(f'<div class="stat"><span class="badge {status}">{LABELS[status]}</span><b>{counts[status]}</b></div>' for status in STATUSES)
    decisions_html = ''.join(f'<li><a href="{esc(issue_href(c["key"]))}">{esc(c["key"])}</a> · {inline(item)}</li>' for c in cards for item in c['decisions'])
    sprints = list(dict.fromkeys(['wiring', 'parity', 'h']+[c['sprint'] for c in cards]))
    tabs, panels = [], []
    for sprint in sprints:
        group = [c for c in cards if c['sprint'] == sprint]
        tabs.append(f'<a id="nav-{esc(sprint)}" data-sprint-link="{esc(sprint)}" href="#{esc(quote(sprint))}"'+(' aria-current="page"' if sprint == 'wiring' else '')+f'>{esc(SPRINT_NAMES.get(sprint, sprint))} <span>{len(group)}</span></a>')
        panels.append(f'<section data-sprint="{esc(sprint)}" aria-labelledby="nav-{esc(sprint)}"'+(' hidden' if sprint != 'wiring' else '')+'>'+f'<h2>{esc(SPRINT_NAMES.get(sprint, sprint))}</h2>'+''.join(render_card(c) for c in group)+(f'<p class="empty">{esc(SPRINT_NAMES.get(sprint,sprint))}暂无任务；Hark 发布任务卡后会自动出现。</p>' if not group else '')+'</section>')
    metrics = ''.join(f'<span>{esc(item)}</span>' for item in sysinfo())
    error_html = ''.join(f'<p class="error">{esc(error)}</p>' for error in errors)
    return f'''<header><div><p class="muted">V2 开发协作 · Hark 指派与验收</p><h1>Issue 看板</h1></div><div class="metrics">{metrics}<span id="live" role="status">实时连接中</span></div></header>
<div class="stats" aria-label="全部冲刺状态汇总">{stat_html}</div>
<section class="decisions"><h2>待你拍板 <small>{sum(len(c['decisions']) for c in cards)} 条</small></h2><ul>{decisions_html or '<li>暂无需要决定的事项</li>'}</ul></section>
{error_html}<nav class="tabs" aria-label="冲刺标签页">{''.join(tabs)}</nav>{''.join(panels)}
<footer>只读 · CST（UTC+8）· 任务 {len(cards)} 张 · 每 5 秒刷新数据，git 查询缓存 30 秒</footer>'''


CSS = r'''
:root{color-scheme:dark;--bg:#181818;--panel:#222;--inset:#1b1b1b;--line:#414141;--ink:#eee;--sub:#b4b4b4;--blue:#68b5ff;--mono:ui-monospace,Consolas,monospace}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.6 system-ui,"PingFang SC","Microsoft YaHei",sans-serif}main{max-width:1480px;margin:auto;padding:28px 24px 60px}h1{font-size:26px;margin:0}h2{font-size:17px;margin:0 0 10px}h3{font-size:15px}h4,h5,h6{font-size:14px}p{margin:8px 0}a{color:var(--blue);text-underline-offset:3px}a:focus-visible,summary:focus-visible{outline:2px solid var(--blue);outline-offset:4px}header{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:20px}.muted,small,time,footer{color:var(--sub)}.metrics{display:flex;flex-wrap:wrap;gap:8px}.metrics span{border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px}.stats{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:10px;margin-bottom:16px}.stat{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px;display:flex;justify-content:space-between;align-items:center;gap:4px}.stat b{font:600 25px var(--mono)}.badge{font-size:12px;border:1px solid currentColor;border-radius:5px;padding:2px 7px;display:inline-block;white-space:nowrap}.open{color:#c2c2c2}.assigned{color:#b5aaff}.in_progress{color:#68b5ff}.blocked,.rejected{color:#ffad9d}.review{color:#ffd389}.accepted{color:#89dbac}.decisions{padding:16px 20px;background:var(--panel);border:1px solid #66543d;border-radius:9px;margin-bottom:20px}.decisions ul{margin:0;padding-left:20px}.decisions li{margin:5px 0;overflow-wrap:anywhere}.tabs{display:flex;gap:8px;overflow-x:auto;padding:4px 2px 10px;margin-bottom:8px}.tabs a{flex:none;position:relative;white-space:nowrap;color:var(--sub);text-decoration:none;padding:9px 16px;border:1px solid var(--line);border-radius:6px}.tabs a[aria-current]{color:var(--ink)}.tabs a[aria-current]::after{content:"";position:absolute;bottom:0;left:12px;right:12px;border-bottom:3px solid var(--blue)}.tabs span{font:12px var(--mono);margin-left:6px}.issue{border:1px solid var(--line);border-radius:8px;background:var(--panel);margin-bottom:10px;scroll-margin-top:12px}.issue>summary{cursor:pointer;display:grid;grid-template-columns:46px minmax(200px,1fr) auto minmax(190px,.65fr);gap:8px 14px;align-items:start;padding:14px 16px;list-style:none}.issue>summary::-webkit-details-marker{display:none}.disclosure::before{content:"▸ 展开";margin-right:4px}.issue[open] .disclosure::before{content:"▾ 收起"}.issue-id{font:600 14px/1.8 var(--mono);color:var(--blue)}.issue summary strong,.owner,.branch{overflow-wrap:anywhere}.branch{grid-column:2/4}.report{grid-column:2/5;display:flex;flex-direction:column;min-width:0}.last-report{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.issue>summary>.muted{grid-column:2/5;font-size:12px}.dependencies{font-size:12px;overflow-wrap:anywhere}.detail{display:grid;grid-template-columns:300px minmax(0,1fr);border-top:1px solid var(--line)}aside{padding:12px 16px;background:var(--inset);border-right:1px solid var(--line);overflow-wrap:anywhere;font-size:12px}.documents{padding:12px 24px;overflow-wrap:anywhere;min-width:0}.commits{padding-left:16px}.commits li{padding:5px 0;border-bottom:1px dashed var(--line)}.commits time{font-size:11px}.commits p{margin:2px 0}.empty{padding:28px;border:1px dashed var(--line);border-radius:8px;color:var(--sub)}code,pre{font-family:var(--mono);font-size:12px;background:var(--inset);border-radius:4px}code{padding:2px 4px}pre{padding:12px;overflow:auto;white-space:pre-wrap}blockquote{margin:12px 0;padding:8px 12px;border-left:2px solid #9d7548;background:#30291f}.tw{overflow:auto}table{border-collapse:collapse;width:100%;font-size:12px}th,td{border:1px solid var(--line);padding:6px 8px;text-align:left}hr{border:0;border-top:1px solid var(--line);margin:24px 0}footer{font-size:12px;text-align:center;margin-top:24px}.error{color:#ffad9d}[hidden]{display:none!important}
@media(max-width:1000px){.stats{grid-template-columns:repeat(4,1fr)}.detail{grid-template-columns:240px minmax(0,1fr)}.issue>summary{grid-template-columns:40px minmax(150px,1fr) auto}.owner,.branch,.dependencies{grid-column:2/4}.report,.issue>summary>.muted{grid-column:2/4}}
@media(max-width:650px){main{padding:16px 12px 40px}.stats{grid-template-columns:repeat(2,1fr)}.stat{padding:10px}.detail{grid-template-columns:1fr}aside{border-right:0;border-bottom:1px solid var(--line)}.documents{padding:10px 14px}.issue>summary{padding:12px;grid-template-columns:34px minmax(0,1fr) auto;gap:8px}.metrics{font-size:11px}}
'''

JS = r'''
const wrap = document.getElementById('wrap');
let version = wrap.dataset.version;
function route(scroll = false, openTarget = true) {
  let key;
  try { key = decodeURIComponent(location.hash.slice(1)); } catch { key = ''; }
  const sprint = key.split('/')[0] || 'wiring';
  const panels = [...wrap.querySelectorAll('[data-sprint]')];
  const selected = panels.some(p => p.dataset.sprint === sprint) ? sprint : 'wiring';
  for (const panel of panels) panel.hidden = panel.dataset.sprint !== selected;
  for (const link of wrap.querySelectorAll('[data-sprint-link]')) {
    if (link.dataset.sprintLink === selected) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  const target = document.getElementById(key);
  if (target && target.matches('details.issue')) {
    if (openTarget) target.open = true;
    if (scroll) target.scrollIntoView({block:'start'});
  }
}
function setLive(text) { const el = document.getElementById('live'); if (el) el.textContent = text; }
function apply(snapshot) {
  if (snapshot.v === version) return;
  const open = new Set([...wrap.querySelectorAll('details[open]')].map(e => e.id));
  const focus = document.activeElement;
  const focusKey = focus?.closest('details.issue')?.id;
  const focusTab = focus?.dataset?.sprintLink;
  // Markdown 在服务端全部转义；客户端额外限制可进入文档的标签与属性。
  const parsed = new DOMParser().parseFromString(snapshot.html, 'text/html');
  const allowed = new Set('HEADER DIV P H1 H2 H3 H4 H5 H6 SPAN B STRONG SMALL SECTION UL LI A NAV DETAILS SUMMARY ASIDE CODE TIME HR FOOTER BLOCKQUOTE PRE TABLE THEAD TBODY TFOOT TR TH TD BR'.split(' '));
  const attrs = new Set('id class href hidden role scope tabindex aria-label aria-labelledby aria-current data-key data-status data-sprint data-sprint-link'.split(' '));
  for (const el of [...parsed.body.querySelectorAll('*')]) {
    if (!allowed.has(el.tagName)) { el.remove(); continue; }
    for (const attr of [...el.attributes]) if (!attrs.has(attr.name)) el.removeAttribute(attr.name);
    if (el.hasAttribute('href') && !el.getAttribute('href').startsWith('#')) el.removeAttribute('href');
  }
  wrap.replaceChildren(...[...parsed.body.childNodes].map(node => document.importNode(node, true)));
  for (const el of wrap.querySelectorAll('details')) el.open = open.has(el.id);
  route(false, false);
  if (focusKey) document.getElementById(focusKey)?.querySelector('summary')?.focus({preventScroll:true});
  else if (focusTab) [...wrap.querySelectorAll('[data-sprint-link]')].find(e => e.dataset.sprintLink === focusTab)?.focus({preventScroll:true});
  version = snapshot.v;
  setLive('实时已连接');
}
window.addEventListener('hashchange', () => route(true));
route(Boolean(location.hash));
if ('EventSource' in window) {
  const stream = new EventSource('/events');
  stream.addEventListener('snap', event => { try { apply(JSON.parse(event.data)); setLive('实时已连接'); } catch { setLive('刷新失败，等待重试'); } });
  stream.onopen = () => setLive('实时已连接');
  stream.onerror = () => setLive('连接中断，自动重连');
} else {
  setLive('定时刷新');
  setInterval(async () => { try { const response = await fetch('/snapshot.json', {cache:'no-store'}); if(response.ok) apply(await response.json()); } catch {setLive('连接中断');} }, 15000);
}
'''

STATE = {'html': '', 'v': '', 'lock': threading.Condition()}


def refresh_once():
    content = render_inner()
    version = hashlib.sha256(content.encode()).hexdigest()[:16]
    with STATE['lock']:
        STATE.update(html=content, v=version)
        STATE['lock'].notify_all()


def refresher():
    while True:
        time.sleep(5)
        try:
            refresh_once()
        except Exception as error:
            # 仅记录异常类型，避免把底层内容或环境值写入日志。
            print('刷新失败，保留上一快照：'+type(error).__name__, flush=True)


def snapshot():
    with STATE['lock']:
        return {'v': STATE['v'], 'html': STATE['html']}


def full_page():
    data = snapshot()
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>V2 Issue 看板</title><style>'+CSS+'</style><noscript><style>section[hidden]{display:block!important}</style></noscript></head><body>'
            '<main id="wrap" data-version="'+data['v']+'">'+data['html']+'</main><script>'+JS+'</script></body></html>')


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def headers_for(self, code, ctype, length=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'SAMEORIGIN')
        self.send_header('Referrer-Policy', 'no-referrer')
        import base64
        digest = base64.b64encode(hashlib.sha256(JS.encode()).digest()).decode()
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'sha256-"+digest+"'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'self'")
        if length is not None:
            self.send_header('Content-Length', str(length))

    def send_body(self, code, ctype, body):
        self.headers_for(code, ctype, len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ('/', '/index.html'):
            return self.send_body(200, 'text/html; charset=utf-8', full_page().encode())
        if path == '/snapshot.json':
            return self.send_body(200, 'application/json; charset=utf-8', json.dumps(snapshot(), ensure_ascii=False).encode())
        if path == '/events':
            return self.sse()
        self.send_body(404, 'text/plain; charset=utf-8', '未找到页面'.encode())

    def do_POST(self):
        self.close_connection = True
        self.send_body(405, 'text/plain; charset=utf-8', '只读看板，不接受写入'.encode())

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

    def sse(self):
        self.headers_for(200, 'text/event-stream; charset=utf-8')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        sent = None
        try:
            self.wfile.write(b': '+b' '*2048+b'\nretry: 3000\n\n'); self.wfile.flush()
            while True:
                with STATE['lock']:
                    if sent == STATE['v']:
                        STATE['lock'].wait_for(lambda: STATE['v'] != sent, timeout=15)
                    data = {'v': STATE['v'], 'html': STATE['html']}
                if sent != data['v']:
                    payload = json.dumps(data, ensure_ascii=False)
                    self.wfile.write(f"event: snap\nid: {data['v']}\ndata: {payload}\n\n".encode())
                    sent = data['v']
                else:
                    self.wfile.write(b': keepalive\n\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def log_message(self, *args):
        pass


def main():
    global BOARD
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('legacy_port', nargs='?', type=int, help='兼容旧服务的位置参数端口')
    parser.add_argument('--port', type=int, default=None)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--board', type=Path, default=BOARD)
    args = parser.parse_args()
    BOARD = args.board
    port = args.port if args.port is not None else args.legacy_port if args.legacy_port is not None else 3142
    # 先绑定，端口占用时立即失败，不做额外扫描。
    server = ThreadingHTTPServer((args.host, port), Handler)
    server.daemon_threads = True
    refresh_once()
    threading.Thread(target=refresher, daemon=True).start()
    print(f'Issue 看板已启动：{args.host}:{port}（只读）', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
