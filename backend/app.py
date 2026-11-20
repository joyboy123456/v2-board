"""V2 开发看板后端：只读 Markdown 任务卡与 git，提供 JSON 快照和 SSE 实时推送。

任务卡与业务仓库只读；运行信号写入独立数据目录，远端 fetch 只写独立 mirror。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.requests import HTTPConnection
import acceptance
import auth
import chat
import repairs
from fastapi.staticfiles import StaticFiles

import board_parse as bp
import interventions
import runs as rr
from signals import event, load_json, rows, safe_id

BOARD = Path(os.environ.get('V2_BOARD_DIR', '/srv/v2-wiring-board'))
bp.BOARD = BOARD
DIST = Path(__file__).resolve().parent.parent / 'frontend' / 'dist'
SPRINT_NAMES = {'wiring': '第 1 批 · 接真实后端', 'parity': '第 2 批 · 对标竞品', 'h': '第 3 批 · 主流程新功能', 'fix': '第 4 批 · 验收退回修复', 'chat': '对话窗口直接交办'}
SPRINT_DESC = {
    'wiring': '卡号 01–08：把 V2 界面接上真实账号、生成、资产和对话 agent',
    'parity': '卡号 G1–G7：照着 Lovart、即梦、椒图补齐体验，每张卡标明学哪家的哪个功能',
    'h': '卡号 H1–H10：在前两批合并结果上加批量、导出、重新生成等，最后并成唯一主干 h/trunk',
    'fix': '11-17 验收退回的 12 张卡不重写，按具体 bug 拆成前端、后端、案例三条修复分支，合回 h/trunk 后复验',
}
SPRINT_ORDER = ['wiring', 'parity', 'h', 'fix']
BRANDS = ['Lovart', '即梦', '椒图']


def benchmark(title: str, body: str):
    m = re.search(r'^\s*[-*]?\s*对标[^：:\n]{0,80}[：:]\s*(.+)$', body or '', re.M)
    text = m.group(1).strip() if m else ''
    if not text:
        t = re.search(r'[（(]\s*学\s*([^）)]+)[）)]', title or '')
        if t:
            first = next((ln.strip() for ln in (body or '').splitlines() if ln.strip() and not ln.lstrip().startswith('#')), '')
            first = re.sub(r'^用户\s*[\d-]+\s*[：:]\s*', '', first)
            text = '学 ' + t.group(1).strip() + '：' + (first or '同类功能')
    brands = [b for b in BRANDS if b.lower() in text.lower()]
    return text, brands
POLL_SECONDS = .5
MIRROR_DATA = Path(os.environ.get("V2_MIRROR_DIR", "/var/tmp/v2-board-mirror"))
mirror = rr.Mirror(os.environ.get("V2_REPO_DIR", "/srv/v0-ai"), MIRROR_DATA)
if os.environ.get("V2_CONTAINER") == "1":
    os.environ.setdefault("V2_GIT_REPO", os.environ.get("V2_REPO_DIR", "/srv/v0-ai"))

REPORT_RE = re.compile(r'^(?P<t>\d{4}-\d\d-\d\d[ T]\d\d:\d\d(?::\d\d)?)\s*(?:CST|\+08:00)?\s*\|\s*(?P<mid>.*?)\s*\|\s*(?P<text>.+)$')
SHELVE_RE = re.compile(r'^搁置|\|\s*搁置|搁置：')


def reports_of(progress: str):
    out = []
    for heading, lines in bp.sections(progress).items():
        if heading != '汇报':
            continue
        for line in lines:
            line = re.sub(r'^[-*]\s*', '', line)
            m = REPORT_RE.match(line)
            if m:
                sha = re.search(r'commit\s*`([0-9a-f]{6,40})`', m.group('mid'))
                who = 'Hark' if m.group('mid').strip() == 'Hark' else ''
                out.append(dict(time=m.group('t').replace('T', ' ')[:19], sha=sha.group(1)[:8] if sha else '',
                                who=who, text=m.group('text').strip()))
            elif line:
                out.append(dict(time='', sha='', who='', text=line))
    return out


def decision_kind(text: str) -> str:
    if re.search(r'^(无|暂无)[^。；]*(阻塞|决策|决定)', text) and '请' not in text:
        return 'note'
    if re.search(r'产品(确认|决定|拍板)|待产品|用户(确认|决定|选择)|PM|请[^。]*(选择|决定|拍板)|A/B', text):
        return 'you'
    if re.search(r'Hark|验收|云沙盒', text):
        return 'hark'
    return 'note'


def clean_agent(name: str) -> str:
    return re.sub(r'（Hark 指派）', '', name or '').strip()


def card_json(c):
    d = c['data']
    reps = reports_of(c['progress'])
    last_text = reps[-1]['text'] if reps else c['last']
    status = d['status']
    shelved = status == 'blocked' and bool(SHELVE_RE.search(last_text or ''))
    st = 'shelved' if shelved else status
    stamp = c['stamp']
    m = re.search(r'\d{4}-\d\d-\d\d[ T]\d\d:\d\d(?::\d\d)?', stamp or '')
    depends = [str(x) for x in d.get('depends') or []]
    inferred = False
    if c['sprint'] == 'h' and d['id'] == 'TRUNK' and not depends:
        depends, inferred = ['ACC'], True
    return dict(
        key=c['key'], sprint=c['sprint'], id=d['id'], title=d['title'], status=status, st=st,
        assignee=clean_agent(str(d.get('assignee', ''))), branch=d.get('branch', ''), worktree=d.get('worktree', ''),
        depends=depends, dependsInferred=inferred, priority=d.get('priority', ''), created=d.get('created', ''),
        updated=d.get('updated', ''), capability=d.get('capability', ''),
        last=last_text, stamp=m.group().replace('T', ' ') if m else '', stampFromFile='文件更新' in (stamp or ''), source=c['source'],
        reports=reps, commits=[dict(sha=s[:8], time=t, subject=sub) for s, t, sub in c['commits']],
        gitNote=c['git_note'], baseline=c['baseline'], missingFields=d.get('missing_fields', []),
        git=c['git_info'], latestRun=c['latest_run'],
        needsAttention=needs_attention(st, c),
        runReason=(c['latest_run'].get('reason') or c['latest_run'].get('final_summary') or '') if c['latest_run'] else '',
        decisions=[dict(text=t, kind=decision_kind(t)) for t in c['decisions']],
        bench=benchmark(d['title'], c['body'])[0], benchBrands=benchmark(d['title'], c['body'])[1],
        body=c['body'], progress='' if c['legacy'] else c['progress'], legacy=c['legacy'],
        dispatch=str(d.get('dispatch', '') or ''), executor=str(d.get('executor', '') or ''),
        stopping=bool(d.get('stop')), stopped=bool(d.get('stopped')),
        questions=open_questions(c['body']) if d.get('dispatch') == 'asking' else [],
        answerHistory=interventions.answer_history(c['body']), chatLog=chat.chat_log(d, c['body']), cardRevision=c['card_revision'],
        repairSource=str(d.get('repair_source', '') or ''), repairReport=str(d.get('repair_report', '') or ''), repairItem=str(d.get('repair_item', '') or ''),
    )


Q_SECTION = re.compile(r'^## (待回答|回答)\s*$\n(.*?)(?=^## |\Z)', re.M | re.S)
Q_ITEM = re.compile(r'^- \[(Q\d+)\] (.*)$', re.M)


def open_questions(body):
    """Questions an agent asked through board-ask that nobody answered yet."""
    asked, answered = {}, set()
    for head, text in Q_SECTION.findall(body or ''):
        for qid, line in Q_ITEM.findall(text):
            if head == '待回答':
                parts = [x.strip() for x in line.split(' | ')]
                opts = next((x[3:].split(' / ') for x in parts if x.startswith('选项：')), [])
                rest = [x for x in parts if not x.startswith('选项：')]
                asked[qid] = dict(id=qid, time=rest[0] if rest else '', who=rest[1] if len(rest) > 2 else '', text=rest[-1] if rest else line, options=opts)
            else:
                answered.add(qid)
    return [q for k, q in asked.items() if k not in answered]


def dispatch_slots():
    try:
        return int(json.loads(Path(os.environ.get('DISPATCH_CONFIG', '/etc/v2-dispatch.json')).read_text()).get('slots', 3))
    except (OSError, ValueError, AttributeError):
        return int(os.environ.get('DISPATCH_SLOTS', '3'))


def needs_attention(status, c):
    """Only nodes someone has to act on now: blocked, an unmerged rejection, or a broken latest run."""
    if status in ('accepted', 'shelved'):
        return False
    if status == 'blocked':
        return True
    if status == 'rejected':
        return not c['git_info']['merge'].get('integration')
    run = c['latest_run']
    return bool(run and run['state'] in ('failed', 'timeout', 'lost'))


def project_timeline(cards):
    milestones = []
    records = rr.all_runs(BOARD)
    for c in cards:
        points = []
        for r in records:
            if r.get('card') not in (c['key'], c['id']):
                continue
            if not r.get('started_unknown'):
                points.append(dict(time=r.get('started_at'), stage='开工', source='run '+r['id'], run_id=r['id']))
            if r['state'] == 'completed':
                points.append(dict(time=r.get('ended_at'), stage='开发完成', source='run '+r['id'], run_id=r['id']))
        for rep in c['reports']:
            text = rep['text']
            if re.search(r'未通过|不通过|未开工|未完成', text):
                continue
            stage = '合入' if re.search(r'已合入|合并完成|已合并', text) else '验收' if re.search(r'验收通过|已验收', text) else '开发完成' if re.search(r'开发完成|待.*验收', text) else '开工' if re.search(r'开工|【开始】', text) else ''
            if stage and rep['time']:
                points.append(dict(time=rep['time'], stage=stage, source='progress 汇报', summary=text))
        points.extend(c['git'].get('milestones', []))
        if c['created'] and not any(p['stage'] == '开工' for p in points):
            points.append(dict(time=c['created'], stage='开工', source='卡片 created'))
        if not points:
            points.append(dict(time='', stage={'accepted': '验收', 'review': '开发完成'}.get(c['status'], '任务登记'), source='卡片当前状态，历史时间未记录'))
        seen = set()
        for p in sorted(points, key=lambda p: rr.epoch(p.get('time'))):
            identity = (p['stage'], int(rr.epoch(p.get('time')) // 60))
            if identity in seen:
                continue
            seen.add(identity)
            milestones.append(dict(p, card=c['key'], title=c['title'], sprint=c['sprint']))
    return sorted(milestones, key=lambda p: (not bool(rr.epoch(p.get('time'))), rr.epoch(p.get('time')), p['card']))


def cross_links(items):
    """跨冲刺合并关系（卡上 depends 只登记本冲刺内的卡）。
    1) 卡标题写了另一冲刺卡的分支名（如 h/acc 标题里的 parity/acc）→ 那张卡并入本卡，来源：卡标题。
    2) 卡正文写「<分支> …包含真实接线 01–06」→ 接线这些卡已并入该分支对应的卡，推断。"""
    by_branch = {c['branch']: c for c in items if c['branch']}
    links, seen = [], set()

    def add(a, b, inferred, note):
        if a['key'] != b['key'] and (a['key'], b['key']) not in seen:
            seen.add((a['key'], b['key']))
            links.append(dict(**{'from': a['key']}, to=b['key'], inferred=inferred, note=note))

    for c in items:
        for br, src in by_branch.items():
            if src['sprint'] != c['sprint'] and re.search(r'(?<![\w/])' + re.escape(br) + r'(?![\w/])', c['title']):
                add(src, c, False, f'{c["sprint"]}/{c["id"]} 卡标题')
    for c in items:
        for m in re.finditer(r'([\w-]+/[\w-]+)[^\n]{0,40}?包含真实接线\s*(\d\d)\s*[–—-]\s*(\d\d)', c['body'] or ''):
            dst = by_branch.get(m.group(1))
            if not dst:
                continue
            for n in range(int(m.group(2)), int(m.group(3)) + 1):
                src = next((x for x in items if x['sprint'] == 'wiring' and x['id'] == f'{n:02d}'), None)
                if src:
                    add(src, dst, True, f'{c["sprint"]}/{c["id"]} 卡描述')
    # cross-sprint depends: a card may list "sprint/ID" in depends
    keys = {c['key']: c for c in items}
    for c in items:
        for d in c['depends']:
            if '/' in d and d in keys:
                add(keys[d], c, False, f'{c["sprint"]}/{c["id"]} depends')
    return links


def sysinfo():
    l1, l5, l15 = os.getloadavg()
    mem = {}
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            k, v = line.split(':', 1)
            mem[k] = int(v.strip().split()[0])
        total = mem['MemTotal'] / 1048576
        used = total - mem.get('MemAvailable', mem.get('MemFree', 0)) / 1048576
    except (OSError, ValueError, KeyError):
        total = used = 0.0
    du = shutil.disk_usage('/')
    return dict(load=[round(l1, 2), round(l5, 2), round(l15, 2)], cores=os.cpu_count() or 1,
                memUsed=round(used, 1), memTotal=round(total, 1),
                diskUsed=round(du.used / 2**30), diskTotal=round(du.total / 2**30), diskPct=round(du.used / du.total * 100))


def snapshot():
    cards, errors = bp.collect()
    items = [card_json(c) for c in cards]
    order = {s: i for i, s in enumerate(SPRINT_ORDER)}
    sprints = sorted({c['sprint'] for c in items} | set(SPRINT_ORDER), key=lambda s: (order.get(s, 99), s))
    reports, rerr = acceptance.collect(BOARD)
    errors = list(errors) + rerr
    board = dict(summary=rr.card_summary(items, rr.events(BOARD)), timeline=project_timeline(items), cards=items, errors=errors, links=cross_links(items), reports=reports,
                 intervention=intervention_state(),
                 sprints=[dict(id=s, name=SPRINT_NAMES.get(s, s), desc=SPRINT_DESC.get(s, ''), count=sum(c['sprint'] == s for c in items)) for s in sprints])
    version = hashlib.sha1(json.dumps(board, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
    board['version'] = version
    board['dispatch'] = dict(slots=dispatch_slots())
    return board


def decorate_runs(records, cards):
    for r in records:
        c = next((c for c in cards if r.get('card') in (c['key'], c['id']) and (not r.get('branch') or c['branch'] == r['branch'])), None)
        if c:
            r['merge'] = c['git']['merge']
    return records


class Hub:
    def __init__(self):
        self.board = None
        self.cond = asyncio.Condition()
        self.tick = 0
        self.signal_data = {}
        self.signal_hash = ''
        self.serial = 0
        from collections import deque
        self.dispatch = deque(maxlen=2000)
        self.tasks = []

    def publish(self, kind, data):
        self.serial += 1
        self.dispatch.append((self.serial, kind, data))

    async def signals_loop(self):
        cursor = max((e.get('seq', 0) for e in rr.events(BOARD)), default=0)
        msg_cursors = {r['id']: max((m.get('seq', 0) for m in rr.messages(BOARD, r['id'])), default=0) for r in rr.all_runs(BOARD)}
        while True:
            try:
                def collect_signals():
                    records = decorate_runs(rr.all_runs(BOARD), (self.board or {}).get('cards', []))
                    data = dict(rr.agents(BOARD, (self.board or {}).get('cards', [])), runs=records)
                    data.pop('now', None)
                    fresh_events = rr.events(BOARD, str(cursor))
                    fresh_messages = []
                    for r in records:
                        fresh_messages.extend(rr.messages(BOARD, r['id'], msg_cursors.get(r['id'], 0)))
                    return data, fresh_events, fresh_messages
                data, fresh_events, fresh_messages = await asyncio.to_thread(collect_signals)
                digest = hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest()
                if digest != self.signal_hash:
                    self.signal_hash, self.signal_data = digest, data
                    self.publish('runs', data)
                for e in fresh_events:
                    cursor = max(cursor, e['seq'])
                    self.publish(e['type'], e)
                if fresh_events:
                    self.publish('events', fresh_events)
                for m in fresh_messages:
                    msg_cursors[m['run_id']] = max(msg_cursors.get(m['run_id'], 0), m['seq'])
                    self.publish('run:message', m)
                if fresh_events or fresh_messages or digest != getattr(self, '_last_digest', None):
                    async with self.cond:
                        self.cond.notify_all()
                self._last_digest = digest
            except Exception as error:
                print('signals failed:', type(error).__name__, flush=True)
            await asyncio.sleep(.5)

    async def maintenance(self):
        await asyncio.to_thread(rr.backfill, BOARD)
        while True:
            try:
                await asyncio.to_thread(rr.reconcile, BOARD, os.environ.get('V2_HOST_PID_CHECK', '1') == '1')
            except Exception as error:
                print('maintenance failed:', type(error).__name__, flush=True)
            await asyncio.sleep(15)

    async def fetch_loop(self):
        while True:
            started = time.monotonic()
            try:
                if os.environ.get('V2_FETCH_ENABLED', '1') == '1':
                    await asyncio.to_thread(mirror.poll, BOARD)
            except Exception as error:
                print('fetch failed:', type(error).__name__, flush=True)
            await asyncio.sleep(max(1, 60 - (time.monotonic() - started)))


    async def loop(self):
        while True:
            try:
                board = await asyncio.to_thread(snapshot)
                if not self.board or board['version'] != self.board['version']:
                    previous = {c['key']: c for c in (self.board or {}).get('cards', [])}
                    self.board = board
                    for c in board['cards']:
                        old = previous.get(c['key'])
                        if old and old['status'] != c['status']:
                            await asyncio.to_thread(event, BOARD, 'card:status', card=c['key'], state=c['status'], previous_state=old['status'], summary=c['title'])
                    async with self.cond:
                        self.tick += 1
                        self.cond.notify_all()
            except Exception as error:  # 解析失败不中断推送，保留上一版
                print('snapshot failed:', type(error).__name__, error, flush=True)
            await asyncio.sleep(POLL_SECONDS)


hub = Hub()
app = FastAPI(title='V2 开发看板', docs_url=None, redoc_url=None)
login_throttle = auth.Throttle()
OPEN_API = ('/api/session', '/api/login', '/api/logout')


class LoginGate:
    """开了登录以后，没登录的请求读不到 /api/ 下的任何数据。页面本身和静态资源照常给，由前端显示登录页。

    /api/interventions/ 不在这里拦：写接口自己校验「已登录或带写入令牌」，报错格式保持原样。
    纯 ASGI 中间件，不包装响应，SSE 长连接不受影响。
    """

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '') if scope['type'] == 'http' else ''
        if path.startswith('/api/') and path not in OPEN_API and not path.startswith('/api/interventions/'):
            conn = HTTPConnection(scope)
            if not auth.allowed(conn.cookies.get(auth.COOKIE, ''), conn.headers.get('authorization', '')):
                denied = JSONResponse({'error': 'login_required', 'detail': '请先登录看板。'}, status_code=401, headers={'Cache-Control': 'no-store'})
                await denied(scope, receive, send)
                return
        await self.inner(scope, receive, send)


app.add_middleware(LoginGate)


@app.on_event('startup')
async def start():
    hub.board = await asyncio.to_thread(snapshot)
    hub.signal_data = dict(await asyncio.to_thread(rr.agents, BOARD, hub.board['cards']), runs=await asyncio.to_thread(rr.all_runs, BOARD))
    hub.tasks = [asyncio.create_task(hub.loop()), asyncio.create_task(hub.signals_loop()), asyncio.create_task(hub.maintenance()), asyncio.create_task(hub.fetch_loop())]


@app.on_event('shutdown')
async def stop():
    for task in hub.tasks:
        task.cancel()
    await asyncio.gather(*hub.tasks, return_exceptions=True)


def now_cst():
    return datetime.now(bp.CST).strftime('%Y-%m-%d %H:%M:%S')


def _https(request: Request) -> bool:
    if os.environ.get('V2_INTERVENTION_TRUST_PROXY_HEADERS') == '1' and request.headers.get('x-forwarded-proto', '').strip().lower() == 'https':
        return True
    return request.url.scheme == 'https'


def _authorize(mode: str, request: Request) -> None:
    """写操作：已登录的浏览器靠 cookie，脚本和没开登录的实例靠写入令牌。跨站请求在这之前已经被来源检查拒掉。"""
    if mode == 'enabled' and auth.session_ok(request.cookies.get(auth.COOKIE, '')):
        return
    interventions.authorize(mode, request.headers.get('authorization', ''))


@app.get('/api/session')
async def api_session(request: Request):
    need = auth.required()
    authed = not need or auth.session_ok(request.cookies.get(auth.COOKIE, ''))
    return JSONResponse({'login': need, 'authed': authed}, headers={'Cache-Control': 'no-store'})


@app.post('/api/login')
async def api_login(request: Request):
    try:
        interventions.validate_origin(request.headers, request.scope.get('scheme', 'http'))
        interventions.validate_content_type(request.headers.get('content-type', ''))
        payload = await interventions.request_json(request)
    except interventions.InterventionError as error:
        return JSONResponse({'error': error.code, 'detail': error.detail}, status_code=error.status)
    if not auth.required():
        return JSONResponse({'ok': True, 'login': False})
    client = request.client.host if request.client else ''
    if login_throttle.blocked(client):
        return JSONResponse({'error': 'too_many_attempts', 'detail': '密码输错的次数太多，请 5 分钟后再试。'}, status_code=429, headers={'Retry-After': str(auth.WINDOW)})
    if set(payload) != {'password'} or not auth.password_ok(payload['password']):
        login_throttle.fail(client)
        await asyncio.sleep(login_throttle.delay())
        return JSONResponse({'error': 'bad_password', 'detail': '密码不对。'}, status_code=401)
    response = JSONResponse({'ok': True, 'login': True}, headers={'Cache-Control': 'no-store'})
    response.set_cookie(auth.COOKIE, auth.issue(), max_age=auth.TTL, path='/', httponly=True, samesite='lax', secure=_https(request))
    return response


@app.post('/api/logout')
async def api_logout(request: Request):
    try:
        interventions.validate_origin(request.headers, request.scope.get('scheme', 'http'))
    except interventions.InterventionError as error:
        return JSONResponse({'error': error.code, 'detail': error.detail}, status_code=error.status)
    response = JSONResponse({'ok': True})
    response.delete_cookie(auth.COOKIE, path='/')
    return response


@app.get('/api/board')
async def api_board():
    return JSONResponse(dict(hub.board, now=now_cst(), sys=sysinfo()))


def intervention_state():
    state = dict(interventions.capabilities(BOARD))
    state['repair'] = repairs.enabled(state['mode'])
    state['executors'] = repairs.executors(BOARD, state['mode']) if state['repair'] else []
    state['repairBase'] = repairs.default_base(state['mode'])
    agents = chat.executor_agents(BOARD, state['mode'])
    for item in state['executors']:
        item.update(agents.get(item['id'], {}))
    return state


@app.post('/api/interventions/repair')
async def api_intervention_repair(request: Request):
    try:
        mode = interventions.capabilities(BOARD)['mode']
        if mode == 'readonly' or not repairs.enabled(mode):
            raise interventions.InterventionError(503, 'repair_disabled', '看板未开启指派修复。')
        interventions.validate_origin(request.headers, request.scope.get('scheme', 'http'))
        interventions.validate_content_type(request.headers.get('content-type', ''))
        _authorize(mode, request)
        payload = await interventions.request_json(request)
        result = await asyncio.to_thread(repairs.create, BOARD, payload, mode)
        return JSONResponse(result)
    except interventions.InterventionError as error:
        headers = {'WWW-Authenticate': 'Bearer'} if error.status == 401 else None
        return JSONResponse({'error': error.code, 'detail': error.detail}, status_code=error.status, headers=headers)


def _publish_card(key, **changes):
    if hub.board is None:
        return
    board = dict(hub.board)
    board['cards'] = [dict(card, **changes) if card.get('key') == key else card for card in board.get('cards', [])]
    version_input = {k: v for k, v in board.items() if k not in ('version', 'dispatch')}
    board['version'] = hashlib.sha1(json.dumps(version_input, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
    hub.board = board


async def _guarded(request: Request, work):
    try:
        mode = interventions.capabilities(BOARD)['mode']
        if mode == 'readonly':
            raise interventions.InterventionError(503, 'writes_disabled', '看板写入未启用。')
        interventions.validate_origin(request.headers, request.scope.get('scheme', 'http'))
        interventions.validate_content_type(request.headers.get('content-type', ''))
        _authorize(mode, request)
        payload = await interventions.request_json(request)
        result = await asyncio.to_thread(work, BOARD, payload, mode)
        return mode, result, None
    except interventions.InterventionError as error:
        headers = {'WWW-Authenticate': 'Bearer'} if error.status == 401 else None
        return None, None, JSONResponse({'error': error.code, 'detail': error.detail}, status_code=error.status, headers=headers)


@app.post('/api/interventions/message')
async def api_intervention_message(request: Request):
    mode, result, failure = await _guarded(request, chat.write_message)
    if failure:
        return failure
    body, meta = result.pop('card_body'), result.pop('card_meta')
    _publish_card(result['card_key'], dispatch=result['dispatch'], chatLog=chat.chat_log(meta, body), cardRevision=result['revision'], body=body)
    hub.tick += 1
    async with hub.cond:
        hub.cond.notify_all()
    return JSONResponse(dict(ok=True, mode=mode, **result))


@app.post('/api/interventions/stop')
async def api_intervention_stop(request: Request):
    mode, result, failure = await _guarded(request, chat.request_stop)
    if failure:
        return failure
    result.pop('card_body'), result.pop('card_meta')
    _publish_card(result['card_key'], dispatch=result['dispatch'], stopping=result['stopping'], cardRevision=result['revision'])
    hub.tick += 1
    async with hub.cond:
        hub.cond.notify_all()
    return JSONResponse(dict(ok=True, mode=mode, **result))


@app.post('/api/interventions/session')
async def api_intervention_session(request: Request):
    mode, result, failure = await _guarded(request, chat.create_session)
    if failure:
        return failure
    return JSONResponse(dict(ok=True, mode=mode, **result))


@app.post('/api/interventions/answer')
async def api_intervention_answer(request: Request):
    try:
        capability = interventions.capabilities(BOARD)
        mode = capability['mode']
        if mode == 'readonly':
            raise interventions.InterventionError(503, 'writes_disabled', '看板写入未启用。')
        interventions.validate_origin(request.headers, request.scope.get('scheme', 'http'))
        interventions.validate_content_type(request.headers.get('content-type', ''))
        _authorize(mode, request)
        payload = await interventions.request_json(request)
        result = await asyncio.to_thread(interventions.write_answer, BOARD, payload, mode)
        card_body, card_meta = result.pop('card_body'), result.pop('card_meta')
        if hub.board is not None:
            board = dict(hub.board)
            cards = []
            for card in board.get('cards', []):
                if card.get('key') == result['card_key']:
                    card = dict(card)
                    card.update(
                        dispatch=result['dispatch'],
                        questions=open_questions(card_body) if result['dispatch'] == 'asking' else [],
                        answerHistory=interventions.answer_history(card_body),
                        chatLog=chat.chat_log(card_meta, card_body),
                        cardRevision=result['revision'],
                        body=card_body,
                    )
                cards.append(card)
            board['cards'] = cards
            version_input = {key: value for key, value in board.items() if key not in ('version', 'dispatch')}
            board['version'] = hashlib.sha1(json.dumps(version_input, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
            hub.board = board
        hub.tick += 1
        async with hub.cond:
            hub.cond.notify_all()
        return JSONResponse(dict(ok=True, mode=mode, **result))
    except interventions.InterventionError as error:
        headers = {'WWW-Authenticate': 'Bearer'} if error.status == 401 else None
        return JSONResponse({'error': error.code, 'detail': error.detail}, status_code=error.status, headers=headers)


@app.get('/api/file/{rel:path}')
async def api_file(rel: str):
    target = acceptance.safe_file(BOARD, rel)
    if not target:
        raise HTTPException(404)
    return FileResponse(target, headers={'Cache-Control': 'no-cache'})


@app.get('/api/agents')
async def api_agents():
    return await asyncio.to_thread(rr.agents, BOARD, (hub.board or {}).get('cards', []))


@app.get('/api/runs')
async def api_runs(agent: str = '', card: str = '', state: str = ''):
    records = await asyncio.to_thread(rr.all_runs, BOARD)
    if agent:
        records = [r for r in records if agent in (r.get('agent'), rr.normalize(r.get('agent', ''), r.get('model', ''))['id'])]
    if card:
        records = [r for r in records if card in (r.get('card'), r.get('card', '').split('/')[-1])]
    if state:
        records = [r for r in records if r.get('state') == state]
    return decorate_runs(records, (hub.board or {}).get('cards', []))


def get_run(run_id):
    try:
        safe_id(run_id)
    except ValueError:
        raise HTTPException(404, '运行不存在')
    record = load_json(BOARD / 'runs' / f'{run_id}.json')
    if not record:
        raise HTTPException(404, '运行不存在')
    return decorate_runs([rr.effective_run(record)], (hub.board or {}).get('cards', []))[0]


@app.get('/api/runs/{run_id}')
async def api_run(run_id: str):
    record = await asyncio.to_thread(get_run, run_id)
    return rr.redact(dict(record, log_tail=await asyncio.to_thread(rr.log_tail, record, BOARD)))


@app.get('/api/runs/{run_id}/messages')
async def api_messages(run_id: str, after_seq: int = Query(0, ge=0)):
    await asyncio.to_thread(get_run, run_id)
    return await asyncio.to_thread(rr.messages, BOARD, run_id, after_seq)


@app.get('/api/events')
async def api_events(since: str = ''):
    return await asyncio.to_thread(rr.events, BOARD, since)


@app.get('/api/stream')
async def api_stream(request: Request):
    cookie, authorization = request.cookies.get(auth.COOKIE, ''), request.headers.get('authorization', '')

    async def gen():
        seen = -1
        serial = hub.serial
        last_sys = 0
        yield 'retry: 1000\n\n'
        yield 'event: runs\ndata: ' + json.dumps(hub.signal_data, ensure_ascii=False) + '\n\n'
        recent = await asyncio.to_thread(rr.events, BOARD)
        yield 'event: events\ndata: ' + json.dumps(recent[-200:], ensure_ascii=False) + '\n\n'
        # 登录门只在连上的那一刻检查；连着的时候 cookie 到期就在这里断开，浏览器重连时会拿到 401 回登录页
        while auth.allowed(cookie, authorization) and not await request.is_disconnected():
            if seen != hub.tick:
                seen = hub.tick
                yield 'event: board\ndata: ' + json.dumps(dict(hub.board, now=now_cst(), sys=sysinfo()), ensure_ascii=False) + '\n\n'
            if hub.dispatch and serial < hub.dispatch[0][0] - 1:
                # Tell clients to refetch messages if their connection was too slow.
                yield 'event: resync\ndata: {}\n\n'
                serial = hub.serial
                yield 'event: runs\ndata: ' + json.dumps(hub.signal_data, ensure_ascii=False) + '\n\n'
            for seq, kind, data in list(hub.dispatch):
                if seq > serial:
                    serial = seq
                    yield f'id: {seq}\nevent: {kind}\ndata: ' + json.dumps(data, ensure_ascii=False) + '\n\n'
            if time.monotonic() - last_sys >= 5:
                yield 'event: sys\ndata: ' + json.dumps(dict(now=now_cst(), sys=sysinfo())) + '\n\n'
                last_sys = time.monotonic()
            try:
                async with hub.cond:
                    await asyncio.wait_for(hub.cond.wait(), timeout=.5)
            except asyncio.TimeoutError:
                pass
    return StreamingResponse(gen(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


if DIST.exists():
    app.mount('/assets', StaticFiles(directory=DIST / 'assets'), name='assets')

    @app.get('/{path:path}')
    async def spa(path: str):
        target = DIST / path
        if path and target.is_file() and DIST in target.resolve().parents:
            return FileResponse(target)
        return FileResponse(DIST / 'index.html')
