import { useEffect, useMemo, useState, useCallback, useRef, type ReactNode } from 'react';
import {
  Activity, Cpu, MemoryStick, HardDrive, GitBranch, GitCommitHorizontal, GitMerge, ChevronRight,
  AlertTriangle, Gavel, Bot, X, FileText, MessageSquareText, Clock, Layers, Archive, Filter, Radio, WifiOff, Link2,
} from 'lucide-react';
import type { Board, Card, St, Decision } from './types';
import TaskMap from './Map';
import Agents from './Agents';
import ProjectProgress, { MergeProgress } from './Progress';
import { LABEL, ORDER, COUNT_ORDER, ago, short, full, freshness, shortTitle, md, inline, flowColumns, STAGES, stageIndex, RUN_STATE, runReason } from './lib';

/* ---------- 数据：首屏拉取 + SSE 推送 ---------- */
type Conn = 'connecting' | 'live' | 'retry';
function useBoard() {
  const [board, setBoard] = useState<Board | null>(null);
  const [conn, setConn] = useState<Conn>('connecting');
  const [error, setError] = useState('');
  useEffect(() => {
    fetch('/api/board').then(r => r.json()).then(setBoard).catch(() => setError('无法读取看板数据'));
    const es = new EventSource('/api/stream');
    es.onopen = () => setConn('live');
    es.onerror = () => setConn('retry');
    es.addEventListener('board', e => { setBoard(JSON.parse((e as MessageEvent).data)); setConn('live'); setError(''); });
    es.addEventListener('sys', e => { const d = JSON.parse((e as MessageEvent).data); setBoard(b => b ? { ...b, ...d } : b); setConn('live'); });
    return () => es.close();
  }, []);
  return { board, conn, error };
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

function TopBar({ b, conn }: { b: Board; conn: Conn }) {
  const s = b.sys;
  return (
    <header className="top">
      <div className="brand">
        <div className="logo"><Layers size={18} strokeWidth={2} /></div>
        <div>
          <h1>V2 开发看板</h1>
          <p>电商 AI 生图 · 多 agent 并行开发 · 只读</p>
        </div>
      </div>
      <div className={`live ${conn}`} role="status">
        {conn === 'retry' ? <WifiOff size={14} /> : <Radio size={14} />}
        <span>{conn === 'live' ? '实时' : conn === 'retry' ? '重连中' : '连接中'}</span>
        <time>北京时间 {b.now.slice(0, 16)}</time>
      </div>
      <div className="meters">
        <Meter icon={<Cpu size={13} />} label="负载" value={`${s.load[0].toFixed(2)} / ${s.cores} 核`} pct={s.load[0] / s.cores * 100} />
        <Meter icon={<MemoryStick size={13} />} label="内存" value={`${s.memUsed} / ${s.memTotal} G`} pct={s.memUsed / (s.memTotal || 1) * 100} />
        <Meter icon={<HardDrive size={13} />} label="磁盘" value={`${s.diskPct}%`} pct={s.diskPct} />
      </div>
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
function Decisions({ cards, open }: { cards: Card[]; open: (k: string) => void }) {
  const [view, setView] = useState<'need' | 'all'>('need');
  const activeCards = cards.filter(c => !['accepted', 'shelved'].includes(c.st));
  const archived = cards.filter(c => ['accepted', 'shelved'].includes(c.st)).reduce((a, c) => a + c.decisions.length, 0);
  const all = activeCards.map(c => ({ c, ds: c.decisions.map(d => ({ ...d, kind: c.st === 'blocked' && d.kind !== 'you' && d.kind !== 'note' ? 'block' : d.kind })) })).filter(x => x.ds.length);
  const youN = all.reduce((a, x) => a + x.ds.filter(d => d.kind === 'you').length, 0);
  const harkN = all.reduce((a, x) => a + x.ds.filter(d => d.kind === 'hark').length, 0);
  const items = (view === 'need' ? all.map(x => ({ ...x, ds: x.ds.filter(d => d.kind === 'you' || d.kind === 'block') })).filter(x => x.ds.length) : all)
    .sort((a, b) => ORDER.indexOf(a.c.st) - ORDER.indexOf(b.c.st));
  return (
    <section className="panel decisions">
      <div className="ph">
        <h2><Gavel size={16} />待你拍板</h2>
        <small>要你定 {youN}</small>
        <div className="seg" role="tablist">
          <button role="tab" aria-selected={view === 'need'} className={view === 'need' ? 'on' : ''} onClick={() => setView('need')}>需处理</button>
          <button role="tab" aria-selected={view === 'all'} className={view === 'all' ? 'on' : ''} onClick={() => setView('all')}>全部</button>
        </div>
      </div>
      <div className="dlist">
        {items.map(x => (
          <button key={x.c.key} className="dgroup" onClick={() => open(x.c.key)}>
            <div className="dg-h"><span className="cid">{x.c.sprint === 'wiring' ? '第 1 批' : x.c.sprint === 'parity' ? '第 2 批' : x.c.sprint === 'fix' ? '第 4 批' : '第 3 批'} · {x.c.id}</span><span className="dg-t">{shortTitle(x.c)}</span><Pill st={x.c.st} small /></div>
            {x.ds.slice(0, 3).map((d, i) => (
              <div key={i} className="ditem"><span className={`tag k-${d.kind}`}>{KIND[d.kind]}</span><Html className="clamp2" html={inline(d.text)} /></div>
            ))}
            {x.ds.length > 3 && <div className="more">还有 {x.ds.length - 3} 条</div>}
          </button>
        ))}
        {!items.length && <div className="empty">没有需要你处理的事项。</div>}
      </div>
      <div className="foot"><Archive size={13} />{view === 'need' ? `Hark 验收 ${harkN} 条由 Hark 处理，已收起；` : ''}已验收或搁置卡的 {archived} 条已归档</div>
    </section>
  );
}

/* ---------- 卡住了 ---------- */
function Blocked({ cards, now, open }: { cards: Card[]; now: string; open: (k: string) => void }) {
  const list = cards.filter(c => c.st === 'blocked' || c.st === 'rejected');
  return (
    <section className="panel blocked">
      <div className="ph"><h2><AlertTriangle size={16} />卡住了</h2><small>{list.length} 张</small></div>
      {list.map(c => (
        <button key={c.key} className="brow" onClick={() => open(c.key)}>
          <span className="cid">{c.id}</span>
          <div>
            <div className="bt">{shortTitle(c)}<Pill st={c.st} small /></div>
            <Html className="why clamp2" html={inline(c.decisions[0]?.text || (c.last !== '暂无汇报' ? c.last : '') || '没有写明原因，也没有汇报')} />
            <div className="bmeta">{c.assignee || '未指派'} · 最近更新 {short(c.stamp)}（{ago(c.stamp, now)}）</div>
          </div>
        </button>
      ))}
      {!list.length && <div className="empty">没有卡住的任务。</div>}
    </section>
  );
}

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
      <div className="rows" role="list">
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
function Drawer({ c, now, close, open }: { c: Card; now: string; close: () => void; open: (k: string) => void }) {
  const [tab, setTab] = useState<'rep' | 'dec' | 'git' | 'card'>('rep');
  useEffect(() => { setTab('rep'); }, [c.key]);
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { const node = dialog.current!; node.showModal(); return () => node.close(); }, []);
  const reps = [...c.reports].reverse();
  const tabs: [typeof tab, string, number | null][] = [['rep', '汇报', c.reports.length], ['dec', '待拍板', c.decisions.length], ['git', '提交', c.git.commit_count], ['card', '任务卡', null]];
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

/* ---------- 页面 ---------- */
export default function App() {
  const { board, conn, error } = useBoard();
  const [tab, setTab] = useParam('tab', 'h');
  const [view, setView] = useParam('view', 'agents');
  const [filter, setFilterRaw] = useParam('f', '');
  const [openKey, setOpen] = useParam('open', '');
  const [focus, setFocus] = useParam('focus', '');
  const byKey = useMemo(() => new Map((board?.cards || []).map(c => [c.key, c])), [board]);
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
      <TopBar b={board} conn={conn} />
      {board.errors.length > 0 && <div className="errors">{board.errors.map(e => <p key={e}><AlertTriangle size={14} />{e}</p>)}</div>}
      <nav className="page-tabs" aria-label="看板视图"><button className={view === 'agents' ? 'on' : ''} aria-current={view === 'agents' ? 'page' : undefined} onClick={() => setView('agents')}><Bot size={16} />Agent 产出</button><button className={view === 'tasks' ? 'on' : ''} aria-current={view === 'tasks' ? 'page' : undefined} onClick={() => setView('tasks')}><Layers size={16} />任务地图</button><button className={view === "progress" ? "on" : ""} aria-current={view === "progress" ? "page" : undefined} onClick={() => setView("progress")}><Clock size={16} />项目进程</button></nav>{board.cards.some(c => c.needsAttention) && <section className="attention-bar" aria-label="需要处理的节点"><AlertTriangle size={16} /><b>需要处理 {board.cards.filter(c => c.needsAttention).length} 个节点</b><div>{board.cards.filter(c => c.needsAttention).map(c => <button key={c.key} onClick={() => { setView("tasks"); setFocus(c.key); setTab(c.sprint); document.querySelector(".mapp")?.scrollIntoView(); }}>{c.id} · {c.latestRun ? RUN_STATE[c.latestRun.state] : LABEL[c.st]}</button>)}</div></section>}
      {view === 'agents' ? <Agents board={board} jump={card => {
        const target = board.cards.find(c => c.key === card || c.id === card);
        setView('tasks'); if (target) { setFocus(target.key); setTab(target.sprint); setOpen(target.key); }
      }} /> : view === "progress" ? <ProjectProgress board={board} open={setOpen} /> : <>
      <div className="hero">
        <Overview cards={board.cards} sprints={board.sprints} filter={filter} setFilter={setFilter} />
        <Working cards={board.cards} now={board.now} open={setOpen} />
      </div>
      <TaskMap b={board} open={setOpen} focus={focus} setFocus={setFocus} />
      <div className="main">
        <SprintPanel b={board} tab={tab} setTab={setTab} filter={filter} setFilter={setFilter} open={setOpen} now={board.now} />
        <div className="side">
          <Decisions cards={board.cards} open={setOpen} />
          <Blocked cards={board.cards} now={board.now} open={setOpen} />
        </div>
      </div>
      </>}
      <footer>只读看板 · 数据来自任务卡与 git · 有变化时自动推送 · 北京时间（UTC+8）</footer>
      {current && <Drawer c={current} now={board.now} close={close} open={setOpen} />}
    </div>
  );
}
