import { useCallback, useEffect, useRef, useState } from 'react';
import { Bot, CheckCircle2, Clock, GitBranch, GitCommitHorizontal, Radio, X } from 'lucide-react';
import type { Board } from './types';
import { MergeProgress } from './Progress';
import type { MergeState } from './types';
import { contiguousSeq, mergeBySeq } from './run-stream';

type Run = { merge?: MergeState; id: string; agent_id: string; card: string; agent: string; model: string; reasoning?: string; state: string; branch: string; started_at: string; ended_at?: string; duration_seconds?: number; commits?: number; changed_files?: number; final_summary?: string; current_activity?: string; last_message?: string; tool_calls?: number; recent_files?: string[]; backfilled?: boolean; reason?: string; exit_code?: number; progress?: { summary: string; step?: number; total?: number }; latest_commit?: { subject: string }; log_tail?: string };
type Agent = { id: string; name: string; model: string; reasoning: string; availability: string; workload: string; current_run: Run | null; latest_run: Run | null; recent_runs: Run[]; total_runs: number; success_rate: number | null; totals: { commits: number; changed_files: number; completed_cards: number }; latest_commit?: { subject: string } };
type Msg = { run_id: string; seq: number; ts: string; type: string; tool?: string; summary: string; content: string };
type Event = { id: string; seq: number; ts: string; occurred_at?: string; type: string; card?: string; branch?: string; sha?: string; subject?: string; summary?: string; state?: string; agent?: string; run_id?: string; backfilled?: boolean };
type Data = { agents: Agent[]; summary: { completed_today: number; new_commits: number; review_accepted: number; running_agents: number }; runs?: Run[] };
const STATE: Record<string, string> = { running: '运行中', queued: '排队中', completed: '成功', failed: '失败', cancelled: '已取消', timeout: '超时', lost: '失联', lost_ended: '失联结束', idle: '空闲' };
const TYPE: Record<string, string> = { text: '输出', thinking: '思考', tool_use: '调用工具', tool_result: '工具结果', file_change: '文件改动', error: '错误', progress: '进度' };
const ACT: Record<string, string> = { 'git:commit': '提交', 'git:push': '远端推送', 'run:status': '执行', 'card:status': '卡状态', 'card:report': '汇报', 'run:progress': '进度' };
const CARD_STATE: Record<string, string> = { review: '待验收', accepted: '已验收', in_progress: '进行中', blocked: '受阻', rejected: '退回', assigned: '已指派' };
const COLORS = ['#8d9bff', '#69d4c5', '#d5a5ed', '#efb574', '#84c89b', '#e78d9e', '#8ebff2', '#d7cc85'];
function cst(value?: string) { return value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '未记录'; }
function elapsed(run: Run, clock: number) {
  const seconds = Math.max(0, Math.floor(run.duration_seconds ?? ((run.ended_at ? Date.parse(run.ended_at) : clock) - Date.parse(run.started_at)) / 1000));
  return seconds < 60 ? `${seconds} 秒` : seconds < 3600 ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : `${Math.floor(seconds / 3600)} 小时 ${Math.floor(seconds / 60) % 60} 分`;
}
async function get<T,>(url: string, signal?: AbortSignal): Promise<T> { const response = await fetch(url, { signal }); if (!response.ok) throw new Error(`读取失败 (${response.status})`); return response.json(); }

function useAgentFeed() {
  const [data, setData] = useState<Data | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [events, setEvents] = useState<Event[]>([]);
  const [transcripts, setTranscripts] = useState<Record<string, Msg[]>>({});
  const [connection, setConnection] = useState('connecting');
  const [error, setError] = useState('');
  const [generation, setGeneration] = useState(0);
  const revision = useRef(0);
  const merge = useCallback((id: string, incoming: Msg[]) => setTranscripts(previous => ({ ...previous, [id]: mergeBySeq(previous[id] || [], incoming) })), []);
  useEffect(() => {
    let mounted = true;
    const es = new EventSource('/api/stream');
    const refresh = () => {
      setConnection('live'); setGeneration(n => n + 1);
      const requestedRevision = revision.current;
      Promise.all([get<Data>('/api/agents'), get<Run[]>('/api/runs'), get<Event[]>('/api/events')]).then(([a, r, e]) => {
        if (!mounted) return;
        if (revision.current === requestedRevision) { setData(a); setRuns(r); } setEvents(old => mergeBySeq(old, e).slice(-200)); setError('');
      }).catch(() => { if (mounted) setError('产出数据读取失败，正在重试'); });
    };
    refresh(); // do not depend on SSE opening: some proxies buffer the stream
    es.onopen = refresh;
    es.onerror = () => setConnection('retry');
    es.addEventListener('resync', refresh);
    es.addEventListener('runs', event => { revision.current += 1; const payload = JSON.parse((event as MessageEvent).data) as Data; setData(payload); if (payload.runs) setRuns(payload.runs); setError(''); });
    es.addEventListener('events', event => setEvents(old => mergeBySeq(old, JSON.parse((event as MessageEvent).data) as Event[]).slice(-200)));
    es.addEventListener('run:message', event => { const m = JSON.parse((event as MessageEvent).data) as Msg; merge(m.run_id, [m]); });
    return () => { mounted = false; es.close(); };
  }, [merge]);
  return { data, runs, events, transcripts, merge, generation, connection, error };
}

function AgentDrawer({ agent, runs, transcripts, merge, generation, close, clock, jump, initialRun }: { initialRun?: string; agent: Agent; runs: Run[]; transcripts: Record<string, Msg[]>; merge: (id: string, messages: Msg[]) => void; generation: number; close: () => void; clock: number; jump: (card: string) => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const scroll = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const [selected, setSelected] = useState(initialRun || agent.current_run?.id || agent.latest_run?.id || '');
  const [detail, setDetail] = useState<Run | null>(null);
  const [error, setError] = useState('');
  const transcriptRef = useRef(transcripts);
  transcriptRef.current = transcripts;
  const record = runs.find(r => r.id === selected) || detail;
  const items = transcripts[selected] || [];
  const liveState = record?.state;
  useEffect(() => {
    if (!selected || !['running', 'lost'].includes(liveState || '')) return;
    const controller = new AbortController();
    const timer = window.setInterval(() => get<Run>(`/api/runs/${selected}`, controller.signal).then(setDetail).catch(() => {}), 2000);
    return () => { clearInterval(timer); controller.abort(); };
  }, [selected, liveState]);
  useEffect(() => {
    const node = dialog.current!;
    node.showModal();
    return () => node.close();
  }, []);
  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    setError(''); setDetail(null); follow.current = true;
    const existing = transcriptRef.current[selected] || [];
    const after = contiguousSeq(existing);
    Promise.all([get<Run>(`/api/runs/${selected}`, controller.signal), get<Msg[]>(`/api/runs/${selected}/messages?after_seq=${after}`, controller.signal)]).then(([d, m]) => { setDetail(d); merge(selected, m); }).catch(e => { if (e.name !== 'AbortError') setError('日志读取失败，重连时将自动补齐'); });
    return () => controller.abort();
  }, [selected, generation, merge]);
  useEffect(() => {
    if (follow.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [items.length]);
  const history = runs.length ? runs : agent.recent_runs;
  return <dialog ref={dialog} className="agent-dialog" aria-label={`${agent.name} 执行记录`} onCancel={close} onClick={e => { if (e.target === e.currentTarget) close(); }}>
    <div className="agent-sheet">
      <div className="dr-h"><div className="dr-top"><span className="eyebrow">AGENT / RUNS</span><button className="icon" autoFocus onClick={close} aria-label="关闭执行记录"><X size={20} /></button></div><h2>{agent.name} <small>{agent.model}</small></h2><p className="hint">{agent.total_runs} 次执行 · 卡片验收状态单独记录</p></div>
      <div className="agent-drawer-body">
        <ol className="run-history" aria-label="执行历史">{history.map(r => <li key={r.id}><button className={`run-row ${selected === r.id ? 'selected' : ''}`} onClick={() => setSelected(r.id)} aria-pressed={selected === r.id}>
          <span className={`run-state ${r.state}`}>{STATE[r.state] || r.state}</span><b>{r.card} · {r.model}</b><time>{cst(r.started_at)}</time><small>{elapsed(r, clock)} · {r.commits || 0} 提交 · {r.changed_files || 0} 文件{r.backfilled ? ' · 历史回填' : ''}</small><p>{r.final_summary || r.current_activity || r.reason || '等待输出'}</p>
        </button><MergeProgress value={r.merge} /></li>)}</ol>
        {!history.length && <p className="empty">还没有执行记录</p>}
        {record && <section className="live-section"><div className="ph"><h3>实时执行日志</h3><button className="dep" onClick={() => { close(); jump(record.card); }}>{record.card} → 任务地图</button></div>
          <MergeProgress value={record.merge} /><p className="hint"><span className={`run-state ${record.state}`}>{STATE[record.state]}</span> · {record.branch || '未记录分支'} · {record.tool_calls || 0} 次工具调用 · 退出码 {record.exit_code ?? '—'}</p>
          {record.progress && <p className="explicit-progress">{record.progress.summary} {record.progress.total ? <progress value={record.progress.step || 0} max={record.progress.total} aria-label="执行进度" /> : null} {record.progress.step != null ? `${record.progress.step}/${record.progress.total ?? '?'}` : ''}</p>}
          {error && <p role="alert">{error}</p>}
          <label className="follow-output"><input type="checkbox" defaultChecked onChange={e => { follow.current = e.target.checked; if (follow.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight; }} />自动跟随输出</label>
          <div ref={scroll} className="transcript" role="log" aria-label="执行消息" aria-live="off">
            {items.map(m => <article key={m.seq} className={`message m-${m.type}`}><div><span>#{m.seq} · {TYPE[m.type] || m.type}</span><time>{cst(m.ts)}</time></div>{['tool_use', 'tool_result', 'file_change', 'thinking'].includes(m.type) ? <details><summary>{m.tool || TYPE[m.type]} · {m.summary.slice(0, 100)}</summary><pre>{m.content}</pre></details> : <pre>{m.content}</pre>}</article>)}
            {!items.length && <p className="empty">{record.backfilled ? '历史记录没有结构化日志' : '等待 CLI 输出…'}</p>}
          </div>
          <details className="raw-tail"><summary>原始输出尾部 · 最后 80 行</summary><pre>{detail?.log_tail || '暂无原始日志'}</pre></details>
        </section>}
      </div>
    </div>
  </dialog>;
}

export default function Agents({ board, jump }: { board: Board; jump: (card: string) => void }) {
  const feed = useAgentFeed();
  const [selected, setSelected] = useState('');
  const [initialRun, setInitialRun] = useState('');
  const [clock, setClock] = useState(Date.now());
  const [showAll, setShowAll] = useState(false);
  useEffect(() => { const timer = window.setInterval(() => setClock(Date.now()), 1000); return () => clearInterval(timer); }, []);
  const { data } = feed;
  useEffect(() => {
    const id = new URLSearchParams(location.search).get('run');
    const run = feed.runs.find(r => r.id === id);
    if (run) { setInitialRun(run.id); setSelected(run.agent_id); const params = new URLSearchParams(location.search); params.delete('run'); window.history.replaceState(null, '', '?' + params); }
  }, [feed.runs]);
  if (!data) return <div className="panel empty" role="status">{feed.error || '正在读取 Agent 产出…'}</div>;
  const agent = data.agents.find(a => a.id === selected);
  const summary = data.summary;
  const recent = [...feed.events].filter(e => ACT[e.type]).sort((a, b) => (b.occurred_at || b.ts).localeCompare(a.occurred_at || a.ts) || b.seq - a.seq).slice(0, showAll ? 60 : 15);
  const history = agent ? feed.runs.filter(r => r.agent_id === agent.id) : [];
  const close = () => { setSelected(''); setInitialRun(''); };
  return <main className="agent-page">
    <section className="result-strip" aria-label="今日产出汇总">{[
      [CheckCircle2, summary.completed_today, '今日成功执行'], [GitCommitHorizontal, summary.new_commits, '新增提交'], [CheckCircle2, `${board.summary.review_today} / ${board.summary.accepted_today}`, '今天新进入待验收 / 已验收'], [Bot, summary.running_agents, '正在运行的 Agent'],
    ].map(([Icon, count, label], i) => { const Symbol = Icon as typeof Bot; return <div key={i}><Symbol size={19} /><b>{String(count)}</b><span>{String(label)}</span></div>; })}</section>
    <p className="hint">累计待验收 {board.summary.review_total} · 已验收 {board.summary.accepted_total}</p><div className="agent-heading"><div><h2>Agent 产出</h2><p>看正在做什么，也看最终交付了什么</p></div><span className={`live ${feed.connection}`} role="status"><Radio size={13} />{feed.connection === 'live' ? '实时同步' : feed.connection === 'retry' ? '重连补齐中' : '连接中'}</span></div>
    {feed.error && <p role="alert">{feed.error}</p>}
    <section className="agent-grid" aria-label="Agent 列表">{data.agents.map((a, i) => {
      const r = a.current_run || a.latest_run;
      const active = a.workload === 'running';
      const subject = r?.latest_commit?.subject || a.latest_commit?.subject;
      return <button key={a.id} className={`agent-card ${active ? 'is-running' : ''}`} onClick={() => setSelected(a.id)} aria-haspopup="dialog" aria-expanded={selected === a.id}>
        <div className="agent-card-head"><span className="agent-avatar" style={{ background: `${COLORS[i % COLORS.length]}22`, color: COLORS[i % COLORS.length] }} aria-hidden="true"><Bot size={24} /></span><span className="agent-name"><b>{a.name}</b><small>{a.model}{a.reasoning ? ` · ${a.reasoning}` : ''}</small></span><span className={`run-state ${r?.state === 'lost' && active ? 'lost' : a.workload}`}>{r?.state === 'lost' && active ? '失联' : STATE[a.workload]}</span></div>
        <div className="agent-availability"><i className={a.availability} />CLI {a.availability === 'available' ? '可执行' : a.availability === 'unavailable' ? '不可执行' : '未探测'}</div>
        {r ? <><div className="agent-task"><b>{r.card}</b><span><Clock size={12} />{elapsed(r, clock)}</span></div><p className="agent-branch"><GitBranch size={12} />{r.branch || '未记录分支'}</p>
          <span className="agent-result-state">{r && !active ? STATE[r.state] : ""}</span><p className="agent-activity">{active ? r.current_activity || r.last_message || '等待首条输出…' : `${STATE[r.state] || r.state} · ${r.final_summary || r.reason || '执行已结束'}`}</p>
          {subject && <p className="agent-commit"><GitCommitHorizontal size={12} />{subject}</p>}
          {active && <p className="agent-tools">{r.tool_calls || 0} 次工具调用{r.recent_files?.length ? ` · ${r.recent_files.slice(-2).join('、')}` : ''}</p>}
        </> : <div className="agent-no-run">等待首次执行</div>}
        <div className="agent-totals"><span><b>{a.totals.commits}</b> 提交</span><span><b>{a.totals.changed_files}</b> 文件</span><span><b>{a.totals.completed_cards}</b> 已验收卡</span><span>{a.success_rate == null ? '—' : `${Math.round(a.success_rate * 100)}%`} 成功率</span></div>
      </button>;
    })}</section>
    <section className="panel activity-panel"><div className="ph"><h2>执行记录</h2><small>北京时间 · 提交 / 推送 / 执行 / 卡状态</small></div><ol className="activity-feed">{recent.map(e => {
      const card = board.cards.find(c => c.key === e.card || c.id === e.card);
      return <li key={e.id}><time>{cst(e.occurred_at || e.ts)}</time><span className={`activity-kind ${e.type.replace(':', '-')}`}>{ACT[e.type]}</span><div>{e.card && <button className="dep" onClick={() => jump(card?.key || e.card!)}>{e.card}</button>} <strong>{e.subject || e.summary || e.branch}</strong><small>{e.agent || e.branch || ''} {e.sha?.slice(0, 8)} {STATE[e.state || ''] || CARD_STATE[e.state || ''] || ''}{e.backfilled ? ' · 历史回填' : ''}</small></div></li>;
    })}</ol>{!recent.length && <p className="empty">新的执行、提交和推送会自动出现在这里</p>}{!showAll && recent.length >= 15 && <button className="dep feed-more" onClick={() => setShowAll(true)}>显示更多动态</button>}</section>
    {agent && <AgentDrawer initialRun={initialRun} key={agent.id} agent={agent} runs={history} transcripts={feed.transcripts} merge={feed.merge} generation={feed.generation} close={close} clock={clock} jump={jump} />}
  </main>;
}
