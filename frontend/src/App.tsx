import { useEffect, useMemo, useState, useCallback, useRef, type CSSProperties, type ReactNode } from 'react';
import {
  Activity, Cpu, MemoryStick, HardDrive, GitBranch, GitCommitHorizontal, GitMerge, ChevronRight,
  AlertTriangle, Gavel, ClipboardCheck, Bot, X, MessageCircleQuestion, CircleCheckBig, Inbox, Gauge, ListOrdered, FileText, MessageSquareText, Clock, Layers, Archive, Filter, Radio, WifiOff, Link2, Moon, Sun,
} from 'lucide-react';
import { applyTheme, storedTheme, type Theme } from './theme';
import type { Board, Card, St, Decision } from './types';
import TaskMap from './Map';
import Agents from './Agents';
import Reports, { CardAcceptance, reportsFor } from './Reports';
import ProjectProgress, { MergeProgress } from './Progress';
import { LABEL, ORDER, COUNT_ORDER, ago, short, full, freshness, shortTitle, md, inline, flowColumns, STAGES, stageIndex, RUN_STATE, runReason } from './lib';

/* ---------- 数据：首屏拉取 + SSE 推送 ---------- */
type Conn = 'connecting' | 'live' | 'retry';
function useBoard() {
  const [board, setBoard] = useState<Board | null>(null);
  const [conn, setConn] = useState<Conn>('connecting');
  const [error, setError] = useState('');
  const refresh = useCallback(async () => {
    try {
      const response = await fetch('/api/board', { cache: 'no-store' });
      if (!response.ok) throw new Error('board request failed');
      const next = await response.json() as Board;
      setBoard(next);
      setError('');
      return next;
    } catch {
      setError('无法读取看板数据');
      return undefined;
    }
  }, []);
  useEffect(() => {
    void refresh();
    const es = new EventSource('/api/stream');
    es.onopen = () => setConn('live');
    es.onerror = () => setConn('retry');
    es.addEventListener('board', e => { setBoard(JSON.parse((e as MessageEvent).data)); setConn('live'); setError(''); });
    es.addEventListener('sys', e => { const d = JSON.parse((e as MessageEvent).data); setBoard(b => b ? { ...b, ...d } : b); setConn('live'); });
    return () => es.close();
  }, [refresh]);
  return { board, conn, error, refresh };
}

/* ---------- URL 状态，便于分享与截图 ---------- */
function useParam(name: string, init: string): [string, (v: string) => void] {
  const [v, setV] = useState(() => new URLSearchParams(location.search).get(name) ?? init);
  const set = useCallback((nv: string) => {
    setV(nv);
    const p = new URLSearchParams(location.search);
    if (nv) p.set(name, nv); else p.delete(name);
    history.replaceState(null, '', p.toString() ? '?' + p : location.pathname);
  }, [name]);
  return [v, set];
}

const Html = ({ html, className }: { html: string; className?: string }) => <span className={className} dangerouslySetInnerHTML={{ __html: html }} />;

function Pill({ st, small }: { st: St; small?: boolean }) {
  return <span className={`pill s-${st}${small ? ' sm' : ''}`}><i />{LABEL[st]}</span>;
}

/* ---------- 顶栏 ---------- */
function Meter({ icon, label, value, pct }: { icon: ReactNode; label: string; value: string; pct: number }) {
  const level = pct > 85 ? 'bad' : pct > 65 ? 'warn' : 'ok';
  return (
    <div className="meter" title={`${label} ${value}`}>
      <div className="meter-row"><span className="meter-label">{icon}{label}</span><b>{value}</b></div>
      <div className="meter-bar"><span className={level} style={{ width: `${Math.min(100, pct)}%` }} /></div>
    </div>
  );
}

function TopBar({ b, conn, view, setView, theme, toggleTheme }: { b: Board; conn: Conn; view: string; setView: (v: string) => void; theme: Theme; toggleTheme: () => void }) {
  const other = theme === 'dark' ? '亮色' : '暗色';
  return (
    <header className="top">
      <div className="brand">
        <div className="logo"><Layers size={18} strokeWidth={2} /></div>
        <div>
          <h1>V2 开发看板</h1>
          <p>电商 AI 生图 · 多 Agent 并行开发 · 回答按模式开放</p>
        </div>
      </div>
      <div className={`live ${conn}`} role="status">
        {conn === 'retry' ? <WifiOff size={14} /> : <Radio size={14} />}
        <span>{conn === 'live' ? '实时' : conn === 'retry' ? '重连中' : '连接中'}</span>
        <time>北京时间 {b.now.slice(0, 16)}</time>
      </div>
      <button className="theme-toggle" onClick={toggleTheme} aria-label={`切换到${other}模式`} title={`切换到${other}模式`}>
        {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
      </button>
      <nav className="page-tabs" aria-label="看板视图">
        {([['agents', '人与产出', <Bot size={15} key="a" />], ['tasks', '任务地图', <Layers size={15} key="t" />], ['progress', '项目进程', <Clock size={15} key="p" />], ['reports', '验收报告', <ClipboardCheck size={15} key="r" />]] as const).map(([k, l, icon]) =>
          <button key={k} className={view === k ? 'on' : ''} aria-current={view === k ? 'page' : undefined} onClick={() => setView(k)}>{icon}{l}</button>)}
      </nav>
    </header>
  );
}

/* ---------- 总体进度 ---------- */
function Overview({ cards, sprints, filter, setFilter }: { cards: Card[]; sprints: Board['sprints']; filter: string; setFilter: (v: string) => void }) {
  const n = (st: St) => cards.filter(c => c.st === st).length;
  const active = cards.filter(c => c.st !== 'shelved');
  const done = n('accepted'), review = n('review');
  const total = active.length || 1;
  const R = 46, C = 2 * Math.PI * R;
  const a = done / total * C, r = review / total * C;
  return (
    <section className="panel overview">
      <div className="ph"><h2>总体进度</h2><small>不含搁置 {n('shelved')} 张</small></div>
      <div className="ov-body">
        <svg className="ring" viewBox="0 0 120 120" role="img" aria-label={`已验收 ${done} 张，待验收 ${review} 张，共 ${active.length} 张`}>
          <circle cx="60" cy="60" r={R} className="ring-bg" />
          <circle cx="60" cy="60" r={R} className="ring-acc" strokeDasharray={`${a} ${C}`} />
          <circle cx="60" cy="60" r={R} className="ring-rev" strokeDasharray={`${r} ${C}`} strokeDashoffset={-a} />
          <text x="60" y="56" className="ring-n">{done + review}<tspan className="ring-d">/{active.length}</tspan></text>
          <text x="60" y="76" className="ring-l">开发完成</text>
        </svg>
        <div className="ov-side">
          <div className="stack" aria-hidden>
            {ORDER.filter(st => st !== 'shelved').map(st => n(st) ? <span key={st} className={`s-${st}`} style={{ flex: n(st) }} /> : null)}
          </div>
          <div className="counts">
            {COUNT_ORDER.map(st => (
              <button key={st} className={`count s-${st}${filter === st ? ' on' : ''}${n(st) ? '' : ' zero'}`}
                aria-pressed={filter === st} onClick={() => setFilter(filter === st ? '' : st)}>
                <b>{n(st)}</b><span><i />{LABEL[st]}</span>
              </button>
            ))}
          </div>
        </div>
      </div>
      <div className="sp-list">
        {sprints.map(sp => {
          const g = cards.filter(c => c.sprint === sp.id && c.st !== 'shelved');
          const acc = g.filter(c => c.st === 'accepted').length, rev = g.filter(c => c.st === 'review').length;
          const t = g.length || 1;
          return (
            <div key={sp.id} className="sp">
              <span className="sp-n">{sp.name}</span>
              <span className="sp-bar"><i className="s-accepted" style={{ width: `${acc / t * 100}%` }} /><i className="s-review" style={{ width: `${rev / t * 100}%` }} /></span>
              <span className="sp-v">已验收 {acc} · 待验收 {rev} / {g.length}</span>
            </div>
          );
        })}
      </div>
    </section>
  );
}

/* ---------- 正在干活 + 最近动态 ---------- */
function Working({ cards, now, open }: { cards: Card[]; now: string; open: (k: string) => void }) {
  const working = cards.filter(c => c.latestRun?.state === 'running');
  const raw = cards.flatMap(c => c.reports.filter(r => r.time).map(r => ({ c, r })))
    .sort((x, y) => y.r.time.localeCompare(x.r.time) || x.c.id.localeCompare(y.c.id, 'zh', { numeric: true }));
  // 同一时刻同一句话（比如 Hark 批量搁置）合成一行
  const feed: { cs: Card[]; r: Card['reports'][number] }[] = [];
  for (const { c, r } of raw) {
    const prev = feed[feed.length - 1];
    if (prev && prev.r.time.slice(0, 16) === r.time.slice(0, 16) && prev.r.text === r.text) prev.cs.push(c);
    else if (feed.length < 5) feed.push({ cs: [c], r });
  }
  return (
    <section className="panel working">
      <div className="ph"><h2>正在干活</h2><small>{working.length} 张</small></div>
      {working.length ? working.map(c => {
        const latest = c.reports[c.reports.length - 1];
        const f = freshness(c.stamp, now);
        return (
          <button key={c.key} className="spot" onClick={() => open(c.key)}>
            <div className="spot-h">
              <span className="cid">{c.id}</span>
              <span className="spot-title">{shortTitle(c)}</span>
              <span className={`fresh ${f}`}><i />{ago(c.stamp, now)}汇报</span>
            </div>
            <Html className="spot-now" html={inline(c.latestRun?.last_message || c.latestRun?.current_activity || (latest ? latest.text : c.last))} />
            <div className="spot-meta">
              <span><Bot size={13} />{c.latestRun?.agent || c.assignee || '未指派'}</span>
              <span><GitBranch size={13} />{c.branch || '未登记'}</span>
              <span><GitCommitHorizontal size={13} />提交 {c.git.commit_count}</span>
              <span><Clock size={13} />开工 {short(c.latestRun?.started_at || c.created)} · 用时 {Math.max(0, Math.round((c.latestRun?.duration_seconds ?? ((Date.now() - Date.parse(c.latestRun?.started_at || '')) / 1000)) / 60)) || 0} 分钟</span>
            </div>
          </button>
        );
      }) : <div className="idle"><Activity size={16} />现在没有 agent 在干活</div>}
      <div className="feed-h">最近动态</div>
      <ol className="feed">
        {feed.map(({ cs, r }, i) => (
          <li key={cs[0].key + i}>
            <button onClick={() => open(cs[0].key)}>
              <time>{short(r.time)}</time>
              <span className="cid" title={cs.map(c => c.id).join('、')}>{cs.length > 1 ? `${cs[0].id} 等 ${cs.length} 张` : cs[0].id}</span>
              {r.who ? <span className="who">Hark</span> : r.sha ? <code>{r.sha.slice(0, 7)}</code> : null}
              <Html className="clamp1" html={inline(r.text)} />
            </button>
          </li>
        ))}
        {!feed.length && <li className="empty">还没有汇报。</li>}
      </ol>
    </section>
  );
}

/* ---------- 待拍板 ---------- */
const KIND: Record<string, string> = { you: '要你定', hark: 'Hark 验收', note: '备注', block: '卡点' };
/* ---------- 冲刺：合并路线 + 任务列表 ---------- */
function Stage({ st }: { st: St }) {
  const idx = stageIndex(st);
  if (idx < 0) return <span className="stage none">{st === 'shelved' ? '本轮不合并' : '未开始'}</span>;
  return (
    <span className="stage" aria-label={`阶段：${STAGES[idx].label}`}>
      {STAGES.map((s, i) => <i key={s.label} className={i < idx ? 'done' : i === idx ? `cur s-${st}` : ''} />)}
      <em>{STAGES[idx].label}</em>
    </span>
  );
}

function Flow({ cards, open }: { cards: Card[]; open: (k: string) => void }) {
  const { cols, loose, shelved, maxd } = flowColumns(cards);
  const names = maxd === 0 ? ['任务'] : cols.map((_, i) => i === 0 ? '功能分支' : i === maxd ? '主干' : '合并');
  const node = (c: Card, i: number) => (
    <button key={c.key} className={`node s-${c.st}${i === maxd && maxd > 0 ? ' trunk' : ''}`} onClick={() => open(c.key)}>
      <span className="cid">{c.id}</span><span className="nt">{shortTitle(c)}</span><Pill st={c.st} small />
    </button>
  );
  return (
    <div className="flowwrap">
      <div className="flow">
        {cols.map((cs, i) => (
          <div className="fcol-wrap" key={i}>
            {i > 0 && <div className="arrow"><ChevronRight size={18} /></div>}
            <div className="fcol"><h4>{i === maxd && maxd > 0 ? <GitMerge size={13} /> : <GitBranch size={13} />}{names[i]}</h4>{cs.map(c => node(c, i))}</div>
          </div>
        ))}
      </div>
      {(loose.length > 0 || shelved.length > 0) && (
        <div className="shelf">
          {loose.length > 0 && <div><span className="lab">未进合并链</span>{loose.map(c => <button key={c.key} className={`chip s-${c.st}`} onClick={() => open(c.key)}>{c.id} · {LABEL[c.st]}</button>)}</div>}
          {shelved.length > 0 && <div><span className="lab">搁置 · 分支保留，本轮不合并</span>{shelved.map(c => <button key={c.key} className="chip s-shelved" onClick={() => open(c.key)}>{c.id} {shortTitle(c)}</button>)}</div>}
        </div>
      )}
      {cards.some(c => c.dependsInferred) && <p className="hint">TRUNK 承接 ACC 为看板推断，任务卡上未登记依赖。</p>}
    </div>
  );
}

function SprintPanel({ b, tab, setTab, filter, setFilter, open, now }: {
  b: Board; tab: string; setTab: (v: string) => void; filter: string; setFilter: (v: string) => void; open: (k: string) => void; now: string;
}) {
  const group = b.cards.filter(c => c.sprint === tab);
  const rows = group.filter(c => !filter || c.st === filter)
    .sort((x, y) => ORDER.indexOf(x.st) - ORDER.indexOf(y.st) || x.id.localeCompare(y.id, 'zh', { numeric: true }));
  return (
    <section className="panel sprint">
      <div className="tabs" role="tablist">
        {b.sprints.map(s => (
          <button key={s.id} role="tab" aria-selected={tab === s.id} className={tab === s.id ? 'on' : ''} onClick={() => setTab(s.id)}>
            {s.name}<span>{s.count}</span>
          </button>
        ))}
      </div>
      <div className="sub-h">
        <FileText size={14} />任务 {rows.length} 张
        {filter && <span className="filter"><Filter size={12} />只看{LABEL[filter as St]}<button onClick={() => setFilter('')} aria-label="清除筛选"><X size={12} /></button></span>}
      </div>
      <div className="rows" role="list" style={{ '--id-ch': Math.min(Math.max(4, ...rows.map(c => c.id.length)), 18) } as CSSProperties}>
        {rows.map(c => (
          <button key={c.key} role="listitem" className={`row s-${c.st}`} onClick={() => open(c.key)}>
            <span className="cid">{c.id}</span>
            <span className="r-title"><b>{shortTitle(c)}</b>{c.bench && <em className="r-bench">对标：{c.bench}</em>}<small>{c.assignee || '未指派'}{c.priority ? ` · ${c.priority}` : ''}</small></span>
            <span><Pill st={c.st} />{c.missingFields.length > 0 && <small className="incomplete" title={`缺少：${c.missingFields.join("、")}`}>字段不全：{c.missingFields.join("、")}</small>}</span>
            <Stage st={c.st} />
            <Html className="r-last clamp1" html={inline(c.last)} />
            <span className="r-time" title={full(c.stamp)}>{short(c.stamp)}<small>{ago(c.stamp, now)}</small></span>
            <span className="r-commits"><GitCommitHorizontal size={13} />{c.git.commit_count}</span>
          </button>
        ))}
        {!rows.length && <div className="empty">{filter ? `${LABEL[filter as St]}的卡在这个冲刺里没有。` : '暂无任务。'}</div>}
      </div>
    </section>
  );
}

/* ---------- 详情抽屉 ---------- */
function InterventionPanel({ b, c, refresh }: { b: Board; c: Card; refresh: () => Promise<Board | undefined> }) {
  const mode = b.intervention?.mode || 'readonly';
  const mounted = useRef(true);
  const [token, setToken] = useState('');
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [pending, setPending] = useState('');
  const [error, setError] = useState<{ status: number; code: string; detail: string } | null>(null);
  const [success, setSuccess] = useState('');
  const questions = c.questions || [];
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const setAnswer = (id: string, value: string) => {
    setAnswers(current => ({ ...current, [id]: value }));
    setError(null);
    setSuccess('');
  };
  const submit = async (questionId: string) => {
    const answer = answers[questionId] || '';
    if (!answer.trim() || pending || mode === 'readonly' || (mode === 'enabled' && !token.trim())) return;
    const confirmation = mode === 'demo'
      ? '确认记录这条演示回答？只会更新合成卡片，不会连接、启动或恢复真实 Agent。'
      : '确认提交这条回答？答案会写入卡片；dispatcher 之后检查时可能恢复原 worker 会话。看板不会调用 Hark API，也不会停止 worker。';
    if (!window.confirm(confirmation)) return;
    setPending(questionId);
    setError(null);
    setSuccess('');
    try {
      const headers: Record<string, string> = { 'Content-Type': 'application/json' };
      if (mode === 'enabled') headers.Authorization = `Bearer ${token}`;
      const response = await fetch('/api/interventions/answer', {
        method: 'POST',
        headers,
        body: JSON.stringify({ card_key: c.key, question_id: questionId, answer, expected_revision: c.cardRevision }),
      });
      const payload = await response.json().catch(() => ({})) as { error?: string; detail?: string; message?: string };
      if (!response.ok) {
        if (mounted.current) setError({ status: response.status, code: payload.error || '', detail: payload.detail || '' });
        return;
      }
      if (mounted.current) {
        setAnswers(current => { const next = { ...current }; delete next[questionId]; return next; });
        setSuccess(payload.message || '答案已记录。');
      }
      await refresh();
    } catch {
      if (mounted.current) setError({ status: 0, code: 'network', detail: '提交未确认成功。保留了输入；刷新看板核对回答记录后再决定是否重试。' });
    } finally {
      if (mounted.current) setPending('');
    }
  };
  const errorText = error?.status === 401
    ? '令牌缺失或无效。检查写入令牌后可重试；如果状态不确定，先刷新核对卡片。'
    : error?.status === 409
      ? `${error.detail || '卡片或问题状态已变化。'}请刷新看板核对回答记录后再操作。`
      : error?.code === 'not_demo_card'
        ? error.detail
        : error?.status === 403
          ? '来源检查拒绝了此次请求。请从看板同源页面重试。'
          : error?.code === 'writes_disabled'
            ? '此实例未启用写入，当前操作保持只读。'
          : error?.detail || '提交失败；输入仍保留。刷新看板后再重试。';
  return (
    <section className="intervention-panel" aria-label="回答 Agent 问题">
      <p className="intervention-model">个人 Agent 与 Hark 负责路由，服务器 Agent 负责执行；你在这里直接回答卡片问题。看板不会调用真实 Hark API，也不提供 worker 停止控制。</p>
      {mode === 'readonly' && <p className="intervention-readonly">只读模式：回答操作已禁用。需要显式启用写入，或使用带演示标记的隔离数据。</p>}
      {mode === 'demo' && <p className="intervention-note">演示回答只修改合成任务卡；这里不会启动 dispatcher 或 worker，因此不代表真实恢复执行。</p>}
      {mode === 'enabled' && <label className="intervention-token">写入令牌 <small>仅保存在当前页面内存，不写入存储或 URL</small>
        <input type="password" autoComplete="new-password" value={token} onChange={e => setToken(e.target.value)} />
      </label>}
      {questions.map(question => (
        <article className="intervention-question" key={question.id}>
          <div className="intervention-qhead"><b>{c.key} · {question.id}</b><span>{question.who || c.executor || c.assignee || 'worker 未标注'}</span></div>
          <p>{question.text}</p>
          {question.options.length > 0 && <div className="intervention-options" role="group" aria-label="可选回答">
            {question.options.map(option => <button type="button" key={option} disabled={mode === 'readonly' || Boolean(pending)} onClick={() => setAnswer(question.id, option)}>{option}</button>)}
          </div>}
          <label className="intervention-answer">你的回答
            <textarea value={answers[question.id] || ''} maxLength={4000} rows={3} disabled={mode === 'readonly' || Boolean(pending)} onChange={e => setAnswer(question.id, e.target.value)} placeholder="可选择上方选项，也可自由输入" />
          </label>
          <div className="intervention-submit"><small>{(answers[question.id] || '').length}/4000</small><button type="button" disabled={mode === 'readonly' || Boolean(pending) || !c.cardRevision || !(answers[question.id] || '').trim() || (mode === 'enabled' && !token.trim())} onClick={() => void submit(question.id)}>{pending === question.id ? '正在记录…' : '确认并记录回答'}</button></div>
        </article>
      ))}
      {error && <div className="intervention-error" role="alert"><p>{errorText}</p><button type="button" onClick={() => void refresh()}>刷新看板</button></div>}
      {success && <p className="intervention-success" role="status">{success}</p>}
      {c.answerHistory.length > 0 && <section className="answer-history" aria-label="已记录回答">
        <h4>已记录回答</h4>
        {c.answerHistory.map((record, index) => <article key={`${record.id}-${index}`}><header><b>{record.id} · {record.actor}</b><time>{record.time || '未注明时间'}</time></header><p>{record.answer}</p></article>)}
      </section>}
      {questions.length === 0 && c.answerHistory.length === 0 && <p className="empty">这张卡没有等待回答的问题。</p>}
    </section>
  );
}

function Drawer({ b, c, now, close, open, refresh }: { b: Board; c: Card; now: string; close: () => void; open: (k: string) => void; refresh: () => Promise<Board | undefined> }) {
  const accN = reportsFor(b.reports, c).reduce((a, x) => a + x.items.length, 0);
  const hasIntervention = Boolean(c.questions?.length || c.answerHistory.length);
  const firstTab = hasIntervention ? 'intervene' : accN && ['rejected', 'review', 'accepted'].includes(c.st) ? 'acc' : c.repairSource && !c.reports.length ? 'card' : 'rep';
  type DrawerTab = 'acc' | 'rep' | 'dec' | 'git' | 'card' | 'intervene';
  const [tab, setTab] = useState<DrawerTab>(firstTab);
  useEffect(() => { setTab(firstTab); }, [c.key]); // eslint-disable-line react-hooks/exhaustive-deps
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { const node = dialog.current!; node.showModal(); return () => node.close(); }, []);
  const reps = [...c.reports].reverse();
  const tabs: [DrawerTab, string, number | null][] = [...(hasIntervention ? [['intervene', '回答', (c.questions?.length || 0) + c.answerHistory.length] as [DrawerTab, string, number]] : []), ...(accN ? [['acc', '验收', accN] as [DrawerTab, string, number]] : []), ['rep', '汇报', c.reports.length], ['dec', '待拍板', c.decisions.length], ['git', '提交', c.git.commit_count], ['card', '任务卡', null]];
  return (
    <dialog ref={dialog} className="card-dialog" aria-label={c.title} onCancel={close} onClick={e => { if (e.target === e.currentTarget) close(); }}>
      <aside className="drawer" onClick={e => e.stopPropagation()}>
        <div className="dr-h">
          <div className="dr-top"><span className="cid">{c.sprint} · {c.id}</span><Pill st={c.st} /><button className="icon" autoFocus onClick={close} aria-label="关闭"><X size={18} /></button></div>
          <h3>{c.title}</h3>{c.missingFields.length > 0 && <p className="incomplete">字段不全 · 缺少：{c.missingFields.join("、")}</p>}<MergeProgress value={c.git.merge} />{c.latestRun && <p className="node-run"><span className={`run-state ${c.latestRun.state}`}>{RUN_STATE[c.latestRun.state]}</span> {runReason(c.runReason)} <a className="dep" href={`?view=agents&run=${encodeURIComponent(c.latestRun.id)}`}>查看对应 run 日志 →</a></p>}
          <dl className="dr-meta">
            <div><dt>执行</dt><dd>{c.assignee || '未指派'}</dd></div>
            {c.bench && <div><dt>对标</dt><dd>{c.bench}</dd></div>}
            <div><dt>分支</dt><dd><code>{c.branch || '未登记'}</code></dd></div>
            {c.repairSource && <div><dt>修复</dt><dd><button className="dep" onClick={() => open(c.repairSource!)}><Link2 size={11} />原卡 {b.cards.find(x => x.key === c.repairSource)?.id || c.repairSource}</button></dd></div>}
            <div><dt>依赖</dt><dd>{c.depends.length ? c.depends.map(d => <button key={d} className="dep" onClick={() => open(d.includes('/') ? d : `${c.sprint}/${d}`)}><Link2 size={11} />{d}</button>) : '无'}{c.dependsInferred && <small>（推断）</small>}</dd></div>
            <div><dt>最近</dt><dd>{full(c.stamp)}（{ago(c.stamp, now)}）</dd></div>
            <div><dt>开工</dt><dd>{full(c.latestRun?.started_at || c.created)}</dd></div>
            <div><dt>阶段</dt><dd><Stage st={c.st} /></dd></div>
          </dl>
          <div className="dr-tabs" role="tablist">
            {tabs.map(([k, l, n]) => <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? 'on' : ''} onClick={() => setTab(k)}>{l}{n != null && <span>{n}</span>}</button>)}
          </div>
        </div>
        <div className="dr-body">
          {hasIntervention && <div hidden={tab !== 'intervene'}><InterventionPanel key={c.key} b={b} c={c} refresh={refresh} /></div>}
          {tab === 'acc' && <CardAcceptance key={c.key} b={b} c={c} open={open} />}
          {tab === 'rep' && (reps.length ? <ol className="tl">{reps.map((r, i) => (
            <li key={i}><div className="tl-h"><time>{r.time ? full(r.time) : '未注明时间'}</time>{r.who && <span className="who">Hark</span>}{r.sha && <code>{r.sha.slice(0, 8)}</code>}</div><Html className="tl-t" html={inline(r.text)} /></li>
          ))}</ol> : <div className="empty"><MessageSquareText size={16} />没有按格式写的汇报。最近摘要：<Html html={inline(c.last)} /></div>)}
          {tab === 'dec' && (c.decisions.length ? <ul className="dec">{c.decisions.map((d: Decision, i) => <li key={i}><span className={`tag k-${d.kind}`}>{KIND[d.kind]}</span><Html html={inline(d.text)} /></li>)}</ul> : <div className="empty">这张卡没有待拍板事项。</div>)}
          {tab === 'git' && <>
            <p className="hint">相对基线 <code>{c.baseline}</code> · {c.gitNote}</p>
            <p className="hint">本卡 {c.git.commit_count} 提交 · {c.git.changed_files} 个改动文件</p><details className="inherited"><summary>继承自 {c.git.parent || "未记录父分支"} {c.git.inherited_count} 条</summary><p>继承历史不计入本卡产出。基线 {c.baseline}</p></details>{!c.git.commit_count && <p className="empty">还没有新提交</p>}<ol className="commits">{c.commits.map(m => <li key={m.sha}><code>{m.sha}</code><span>{m.subject}</span><time>{full(m.time.replace(/^(\d{4}-\d\d-\d\d)T/, '$1 '))}</time></li>)}</ol>
          </>}
          {tab === 'card' && <div className="md"><Html html={md(c.body)} />{c.progress && <><hr /><h3>进度文件</h3><Html html={md(c.progress)} /></>}</div>}
        </div>
      </aside>
    </dialog>
  );
}

/* ---------- 左栏：需要你 ---------- */
const BATCH: Record<string, string> = { wiring: '第 1 批', parity: '第 2 批', h: '第 3 批', fix: '第 4 批' };
function RailSection({ icon, title, n, tone, children, empty }: { icon: ReactNode; title: string; n: number; tone: string; children: ReactNode; empty: string }) {
  return (
    <section className={`rsec t-${tone}`}>
      <h3>{icon}<span>{title}</span><b>{n}</b></h3>
      {n ? children : <p className="rempty">{empty}</p>}
    </section>
  );
}
function NeedYou({ b, open }: { b: Board; open: (k: string) => void }) {
  const live = b.cards.filter(c => !['accepted', 'shelved'].includes(c.st));
  const asking = live.filter(c => c.questions?.length);
  const stuck = live.filter(c => c.needsAttention && !c.questions?.length);
  const decide = live.map(c => ({ c, ds: c.decisions.filter(d => d.kind === 'you') })).filter(x => x.ds.length);
  const review = live.filter(c => c.st === 'review');
  const total = asking.length + stuck.length + decide.length + review.length;
  const [showAll, setShowAll] = useState(false);
  const reasonOf = (c: Card) => (c.runReason || c.decisions[0]?.text || (c.last !== '暂无汇报' ? c.last : '') || '没有写明原因')
    .replace(/^commit\s+\S+\s*\|\s*/, '').replace(/\s*\d{2}-\d{2} \d{2}:\d{2}\s*$/, '');
  const stuckGroups: { reason: string; cs: Card[] }[] = [];
  for (const c of stuck) {
    const reason = reasonOf(c);
    const g = stuckGroups.find(x => x.reason === reason && x.cs[0].st === c.st);
    if (g) g.cs.push(c); else stuckGroups.push({ reason, cs: [c] });
  }
  stuckGroups.sort((x, y) => y.cs.length - x.cs.length);
  return (
    <aside className="rail rail-l" aria-label="需要你处理">
      <div className="rail-h"><Inbox size={16} /><h2>需要你</h2><b className={total ? 'hot' : ''}>{total}</b></div>
      <RailSection icon={<MessageCircleQuestion size={14} />} title="等你回答" n={asking.reduce((a, c) => a + (c.questions?.length || 0), 0)} tone="ask" empty="没有 Agent 在等你回答">
        {asking.map(c => c.questions!.map(q => (
          <button key={c.key + q.id} className="ritem ask" onClick={() => open(c.key)}>
            <div className="ri-h"><span className="cid">{c.id}</span><span className="ri-t">{shortTitle(c)}</span><span className="ri-q">{q.id}</span></div>
            <p>{q.text}</p>
            {q.options.length > 0 && <div className="opts">{q.options.map(o => <span key={o}>{o}</span>)}</div>}
            <div className="ri-m">{q.who || c.executor || c.assignee} · {q.time.slice(5, 16)}</div>
          </button>
        )))}
      </RailSection>
      <RailSection icon={<AlertTriangle size={14} />} title="卡住或失败" n={stuck.length} tone="bad" empty="没有卡住的任务">
        {stuckGroups.slice(0, showAll ? 99 : 4).map(g => (
          <div key={g.reason + g.cs[0].key} className="ritem">
            <div className="ri-h">
              {g.cs.length === 1
                ? <button className="ri-link" onClick={() => open(g.cs[0].key)}><span className="cid">{g.cs[0].id}</span><span className="ri-t">{shortTitle(g.cs[0])}</span></button>
                : <span className="ri-t">{g.cs.length} 张同一原因</span>}
              <Pill st={g.cs[0].st} small />
            </div>
            <Html className="clamp2 ri-p" html={inline(g.reason)} />
            {g.cs.length > 1 && <div className="chips mini">{g.cs.map(c => <button key={c.key} onClick={() => open(c.key)} title={c.title}><span className="cid">{c.id}</span></button>)}</div>}
          </div>
        ))}
        {stuckGroups.length > 4 && <button className="rmore" onClick={() => setShowAll(!showAll)}>{showAll ? '收起' : `还有 ${stuckGroups.length - 4} 组`}</button>}
      </RailSection>
      <RailSection icon={<Gavel size={14} />} title="要你拍板" n={decide.reduce((a, x) => a + x.ds.length, 0)} tone="you" empty="没有要你定的事">
        {decide.map(({ c, ds }) => (
          <button key={c.key} className="ritem" onClick={() => open(c.key)}>
            <div className="ri-h"><span className="cid">{BATCH[c.sprint] || c.sprint} · {c.id}</span><span className="ri-t">{shortTitle(c)}</span></div>
            {ds.slice(0, 2).map((d, i) => <Html key={i} className="clamp2 ri-p" html={inline(d.text)} />)}
            {ds.length > 2 && <div className="ri-m">还有 {ds.length - 2} 条</div>}
          </button>
        ))}
      </RailSection>
      <RailSection icon={<CircleCheckBig size={14} />} title="待验收" n={review.length} tone="rev" empty="没有待验收的卡">
        <div className="chips">{review.map(c => <button key={c.key} onClick={() => open(c.key)} title={c.title}><span className="cid">{c.id}</span>{shortTitle(c)}</button>)}</div>
      </RailSection>
    </aside>
  );
}

/* ---------- 右栏：现场 ---------- */
function LiveRail({ b, open }: { b: Board; open: (k: string) => void }) {
  const s = b.sys;
  const slots = b.dispatch?.slots || 3;
  const running = b.cards.filter(c => c.latestRun?.state === 'running');
  const queued = b.cards.filter(c => c.dispatch === 'queued' || c.dispatch === 'answered');
  const raw = b.cards.flatMap(c => c.reports.filter(r => r.time).map(r => ({ c, r })))
    .sort((x, y) => y.r.time.localeCompare(x.r.time));
  const feed: { cs: Card[]; r: Card['reports'][number] }[] = [];
  for (const { c, r } of raw) {
    const prev = feed[feed.length - 1];
    if (prev && prev.r.time.slice(0, 16) === r.time.slice(0, 16) && prev.r.text === r.text) { if (!prev.cs.includes(c)) prev.cs.push(c); }
    else if (prev && prev.cs[0] === c && prev.cs.length === 1 && prev.r.time.slice(0, 13) === r.time.slice(0, 13)) continue;
    else if (feed.length < 14) feed.push({ cs: [c], r });
  }
  return (
    <aside className="rail rail-r" aria-label="现场">
      <section className="rsec">
        <h3><ListOrdered size={14} /><span>调度槽位</span><b>{running.length} / {slots}</b></h3>
        <div className="slots">{Array.from({ length: Math.max(slots, running.length) }, (_, i) => {
          const c = running[i];
          return c ? <button key={i} className="slot on" onClick={() => open(c.key)}><i /><span className="cid">{c.id}</span><span className="slot-a">{c.latestRun?.agent || c.assignee}</span></button>
            : <div key={i} className="slot"><i />空闲</div>;
        })}</div>
        <p className="rnote">{queued.length ? `排队 ${queued.length} 张：${queued.map(c => c.id).join('、')}` : '队列为空'}</p>
      </section>
      <section className="rsec">
        <h3><Gauge size={14} /><span>服务器</span></h3>
        <div className="meters">
          <Meter icon={<Cpu size={13} />} label="负载" value={`${s.load[0].toFixed(2)} / ${s.cores} 核`} pct={s.load[0] / s.cores * 100} />
          <Meter icon={<MemoryStick size={13} />} label="内存" value={`${s.memUsed} / ${s.memTotal} G`} pct={s.memUsed / (s.memTotal || 1) * 100} />
          <Meter icon={<HardDrive size={13} />} label="磁盘" value={`${s.diskUsed} / ${s.diskTotal} G`} pct={s.diskPct} />
        </div>
      </section>
      <section className="rsec grow">
        <h3><Activity size={14} /><span>最新动态</span></h3>
        <ol className="rfeed">
          {feed.map(({ cs, r }, i) => (
            <li key={cs[0].key + i}><button onClick={() => open(cs[0].key)}>
              <div><time>{r.time.slice(5, 16)}</time><span className="cid">{cs.length > 1 ? `${cs[0].id} 等 ${cs.length} 张` : cs[0].id}</span></div>
              <Html className="clamp2" html={inline(r.text)} />
            </button></li>
          ))}
          {!feed.length && <li className="rempty">还没有汇报</li>}
        </ol>
      </section>
    </aside>
  );
}

/* ---------- 页面 ---------- */
export default function App() {
  const { board, conn, error, refresh } = useBoard();
  const [tab, setTab] = useParam('tab', 'h');
  const [view, setView] = useParam('view', 'agents');
  const [theme, setTheme] = useState<Theme>(storedTheme);
  const toggleTheme = () => {
    const next: Theme = theme === 'dark' ? 'light' : 'dark';
    applyTheme(next, true);
    setTheme(next);
  };
  const [filter, setFilterRaw] = useParam('f', '');
  const [openKey, setOpen] = useParam('open', '');
  const [focus, setFocus] = useParam('focus', '');
  const [report, setReport] = useParam('report', '');
  const demoDefaultApplied = useRef(false);
  const byKey = useMemo(() => new Map((board?.cards || []).map(c => [c.key, c])), [board]);
  useEffect(() => {
    if (board?.intervention.mode !== 'demo' || demoDefaultApplied.current) return;
    demoDefaultApplied.current = true;
    const params = new URLSearchParams(window.location.search);
    if (!params.has('view')) setView('tasks');
    if (!params.has('open')) {
      const asking = board.cards.find(card => card.questions?.length);
      if (asking) {
        if (!params.has('tab')) setTab(asking.sprint);
        setOpen(asking.key);
      }
    }
  }, [board, setOpen, setTab, setView]);
  const setFilter = (v: string) => {
    setFilterRaw(v);
    if (v && board && !board.cards.some(c => c.sprint === tab && c.st === v)) {
      const hit = board.sprints.find(s => board.cards.some(c => c.sprint === s.id && c.st === v));
      if (hit) setTab(hit.id);
    }
  };
  const close = useCallback(() => setOpen(''), [setOpen]);
  if (!board) return <div className="loading">{error || '正在读取看板…'}</div>;
  const current = openKey ? byKey.get(openKey) : undefined;
  return (
    <div className="app">
      <TopBar b={board} conn={conn} view={view} setView={setView} theme={theme} toggleTheme={toggleTheme} />
      {board.intervention.mode === 'demo' && <div className="intervention-banner demo-banner demo-global-banner">隔离演示 · 不连接真实 Agent</div>}
      {error && <div className="errors" role="alert"><p><AlertTriangle size={14} />{error}，页面上是上一次读到的内容。</p></div>}
      {board.errors.length > 0 && <div className="errors">{board.errors.map(e => <p key={e}><AlertTriangle size={14} />{e}</p>)}</div>}
      <div className="cols">
        <NeedYou b={board} open={setOpen} />
        <main className="center">
      {view === 'agents' ? <Agents board={board} jump={card => {
        const target = board.cards.find(c => c.key === card || c.id === card);
        setView('tasks'); if (target) { setFocus(target.key); setTab(target.sprint); setOpen(target.key); }
      }} /> : view === 'reports' ? <Reports b={board} open={setOpen} sel={report} setSel={setReport} /> : view === "progress" ? <ProjectProgress board={board} open={setOpen} /> : <>
      <div className={board.cards.some(c => c.latestRun?.state === 'running') ? 'hero' : 'hero hero-idle'}>
        <Overview cards={board.cards} sprints={board.sprints} filter={filter} setFilter={setFilter} />
        <Working cards={board.cards} now={board.now} open={setOpen} />
      </div>
      <TaskMap b={board} open={setOpen} focus={focus} setFocus={setFocus} />
      <SprintPanel b={board} tab={tab} setTab={setTab} filter={filter} setFilter={setFilter} open={setOpen} now={board.now} />
      </>}
        </main>
        <LiveRail b={board} open={setOpen} />
      </div>
      <footer>Agent 协作看板 · 回答操作按实例模式开放 · 数据来自任务卡与 git · 有变化时自动推送 · 北京时间（UTC+8）</footer>
      {current && <Drawer b={board} c={current} now={board.now} close={close} open={setOpen} refresh={refresh} />}
    </div>
  );
}
