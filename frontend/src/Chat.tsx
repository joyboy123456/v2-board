import { useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, Bot, CornerDownLeft, ExternalLink, GitBranch, MessageSquarePlus, Radio, Square } from 'lucide-react';
import type { Board, Card, ChatEntry, Executor } from './types';
import { COLORS, STATE, TYPE, cst, get, useAgentFeed } from './Agents';
import type { Agent, Msg, Run } from './Agents';
import { contiguousSeq } from './run-stream';

const DISPATCH: Record<string, string> = { queued: '排队中', running: '运行中', asking: '等你回答', answered: '马上继续', done: '本轮完成', failed: '失败', held: '已暂停', closed: '已关闭' };
const COMPOSER: Record<string, string> = {
  queued: '还在排队，你说的话会随任务一起交给它。',
  running: '它正在跑这一轮。你说的话会在这一轮结束后接着同一个会话送达；要马上打断，点上面的「停止」。',
  asking: '它在等你回答上面的问题。也可以另外留言，会和回答一起送达。',
  answered: '它马上开始下一轮，你说的话下一轮开始时送达。',
  done: '这一轮已经结束。发送后它会接着这个会话继续。',
  failed: '这一轮失败了。发送后它会接着这个会话再试。',
  held: '这个会话已暂停，留言会记下，恢复后才送达。',
};
// 「停止」之后：已停止的会话发消息就能接着聊；手工暂停的（held 但不是被停止的）要人恢复
const stateOf = (card: Card) => card.stopping ? '正在停止' : card.dispatch === 'held' && card.stopped ? '已停止' : DISPATCH[card.dispatch!] || card.dispatch!;
const STOPPABLE = ['queued', 'running', 'answered'];
// CLI 自己打的提示行，不是 Agent 说的话
const NOISE = /^(ZCode Built-in|Reading additional input from stdin|Warning: Kiro CLI is running as root|Hide this warning:)/;
const spoken = (content: string) => content.replace(/\[INFO\] \[KRS\][^\n]*/g, '').split('\n').filter(line => !NOISE.test(line.trim())).join('\n').trim();
type Session = { key: string; card?: Card; cardId: string; title: string; runs: Run[]; state: string; live: boolean; last: number };
type Item = { t: number; order: number; kind: 'run' | 'msg' | 'entry'; run?: Run; msg?: Msg; entry?: ChatEntry };
type Failure = { status: number; detail: string };

function when(value?: string) {
  if (!value) return 0;
  const iso = /^\d{4}-\d\d-\d\d \d\d:\d\d/.test(value) ? value.replace(' CST', '').replace(' ', 'T') + (value.length < 17 ? ':00' : '') + '+08:00' : value;
  const parsed = Date.parse(iso.replace(/:00:00\+08:00$/, ':00+08:00'));
  return Number.isNaN(parsed) ? 0 : parsed;
}
function clock(ms: number) { return ms ? new Date(ms).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : ''; }

function sessionsOf(agent: Agent, board: Board, runs: Run[], execs: Executor[]): Session[] {
  const mine = new Set(execs.filter(e => e.agent === agent.id).map(e => e.id));
  const found = new Map<string, Session>();
  const add = (key: string, card: Card | undefined, cardId: string) => {
    if (!found.has(key)) found.set(key, { key, card, cardId, title: card?.title || '', runs: [], state: '', live: false, last: when(card?.created) });
    return found.get(key)!;
  };
  for (const card of board.cards) if (card.dispatch && card.executor && mine.has(card.executor)) add(card.key, card, card.id);
  for (const run of runs) {
    if (run.agent_id !== agent.id) continue;
    const card = board.cards.find(c => c.key === run.card || c.id === run.card);
    const session = add(card?.key || 'run:' + run.card, card, card?.id || run.card);
    session.runs.push(run);
    session.last = Math.max(session.last, when(run.ended_at || run.started_at));
  }
  for (const session of found.values()) {
    session.runs.sort((a, b) => when(a.started_at) - when(b.started_at));
    const latest = session.runs[session.runs.length - 1];
    session.live = !!session.card?.dispatch && session.card.dispatch !== 'closed' && !!session.card.executor && mine.has(session.card.executor);
    session.state = session.live || session.card?.dispatch === 'closed' ? stateOf(session.card!) : latest ? STATE[latest.state] || latest.state : '未开始';
    for (const entry of session.card?.chatLog || []) session.last = Math.max(session.last, when(entry.time));
  }
  return [...found.values()].sort((a, b) => b.last - a.last);
}

async function post(url: string, token: string, body: unknown): Promise<{ ok: boolean; status: number; data: Record<string, string> }> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (token) headers.Authorization = 'Bearer ' + token;
  const response = await fetch(url, { method: 'POST', headers, body: JSON.stringify(body) });
  return { ok: response.ok, status: response.status, data: await response.json().catch(() => ({})) };
}
function explain(failure: Failure) {
  if (failure.status === 401) return '令牌不对或没填，没有发送。';
  if (failure.status === 0) return '没有确认发送成功。输入还在，刷新后核对记录再决定是否重发。';
  return failure.detail || '发送失败 (' + failure.status + ')';
}

export default function Chat({ board, jump, refresh, agentId, setAgentId, sessionKey, setSessionKey }: { board: Board; jump: (card: string) => void; refresh: () => Promise<Board | undefined>; agentId: string; setAgentId: (v: string) => void; sessionKey: string; setSessionKey: (v: string) => void }) {
  const feed = useAgentFeed();
  const [token, setToken] = useState('');
  const mode = board.intervention.mode;
  const execs = board.intervention.executors || [];
  const agents = feed.data?.agents || [];
  const lists = useMemo(() => new Map(agents.map(a => [a.id, sessionsOf(a, board, feed.runs, execs)])), [agents, board, feed.runs, execs]);
  // 没指定时：先看谁在等你回答，其次谁在跑，再其次最近有会话的
  const recent = (a: Agent) => lists.get(a.id)?.[0]?.last || 0;
  const agent = agents.find(a => a.id === agentId)
    || agents.find(a => lists.get(a.id)?.some(x => x.card?.dispatch === 'asking'))
    || agents.find(a => a.workload === 'running')
    || [...agents].sort((a, b) => recent(b) - recent(a))[0];
  if (!feed.data) return <div className="panel empty" role="status">{feed.error || '正在读取 Agent…'}</div>;
  if (!agent) return <div className="panel empty">还没有登记任何 Agent</div>;
  const sessions = lists.get(agent.id) || [];
  const creating = sessionKey === 'new';
  const session = creating ? undefined : sessions.find(s => s.key === sessionKey) || sessions[0];
  const usable = execs.filter(e => e.agent === agent.id);
  const canCreate = mode === 'enabled' && !!board.intervention.repair && !!board.intervention.repairBase && usable.length > 0;
  return <section className="chat-page" aria-label="Agent 对话">
    <nav className="chat-agents" aria-label="Agent 列表">
      <div className="chat-side-h"><b>Agent</b><span className={'live ' + feed.connection} role="status"><Radio size={12} />{feed.connection === 'live' ? '实时' : feed.connection === 'retry' ? '重连中' : '连接中'}</span></div>
      {agents.map((a, i) => {
        const list = lists.get(a.id) || [];
        const asking = list.filter(s => s.card?.dispatch === 'asking').length;
        const wired = execs.some(e => e.agent === a.id);
        return <div key={a.id} className={'chat-agent' + (a.id === agent.id ? ' on' : '')}>
          <button className="chat-agent-row" aria-current={a.id === agent.id ? 'true' : undefined} onClick={() => { setAgentId(a.id); setSessionKey(''); }}>
            <span className="agent-avatar" style={{ background: COLORS[i % COLORS.length] + '22', color: COLORS[i % COLORS.length] }} aria-hidden="true"><Bot size={18} /></span>
            <span className="chat-agent-name"><b>{a.name}</b><small>{a.model}{a.reasoning ? ' · ' + a.reasoning : ''}</small><small>{a.totals.commits} 提交 · {a.total_runs} 次执行{wired ? '' : ' · 未接入调度'}</small></span>
            {asking > 0 ? <span className="chat-badge ask" title="有问题等你回答">{asking}</span> : a.workload === 'running' ? <span className="chat-badge run" title="运行中" /> : null}
          </button>
          {a.id === agent.id && <ol className="chat-sessions" aria-label={a.name + ' 的会话'}>
            {canCreate && <li><button className={'chat-session new' + (creating ? ' on' : '')} onClick={() => setSessionKey('new')}><MessageSquarePlus size={14} />新会话</button></li>}
            {sessions.map(s => <li key={s.key}><button className={'chat-session' + (!creating && session?.key === s.key ? ' on' : '')} onClick={() => setSessionKey(s.key)}>
              <b>{s.cardId}</b><span className={'chat-state ' + (s.card?.dispatch || s.runs[s.runs.length - 1]?.state || '')}>{s.state}</span>
              <small>{s.title || '没有对应的任务卡'}</small><time>{clock(s.last)}</time>
            </button></li>)}
            {!sessions.length && <li className="hint">还没有会话{usable.length ? '' : '；这个 Agent 还没接进调度器，暂时不能从这里交办'}</li>}
          </ol>}
        </div>;
      })}
    </nav>
    {creating && canCreate
      ? <NewSession agent={agent} execs={usable} base={board.intervention.repairBase || ''} token={token} setToken={setToken} refresh={refresh} done={setSessionKey} />
      : session ? <Conversation key={session.key} agent={agent} session={session} mode={mode} feed={feed} token={token} setToken={setToken} refresh={refresh} jump={jump} resume={execs.find(e => e.id === session.card?.executor)?.resume !== false} />
        : <div className="chat-main chat-blank"><Bot size={30} /><p>{agent.name} 还没有会话</p><p className="hint">{canCreate ? '点左边的「新会话」直接给它交办一件事。' : usable.length ? '看板没有开启从这里交办。' : '它还没接进调度器，所以这里只能看它以前的执行记录。'}</p></div>}
  </section>;
}

function TokenField({ token, setToken }: { token: string; setToken: (v: string) => void }) {
  return <label className="chat-token">写入令牌<input type="password" autoComplete="off" value={token} onChange={e => setToken(e.target.value)} placeholder="只留在这个页面里，刷新后要重填" /></label>;
}

function NewSession({ agent, execs, base, token, setToken, refresh, done }: { agent: Agent; execs: Executor[]; base: string; token: string; setToken: (v: string) => void; refresh: () => Promise<Board | undefined>; done: (key: string) => void }) {
  const [executor, setExecutor] = useState(execs[0]?.id || '');
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  const send = async () => {
    if (!text.trim() || !token.trim() || busy) return;
    if (!window.confirm('确认开这个新会话？调度器有空槽位时会启动真实的 ' + agent.name + '，并为它建一个独立的 worktree。')) return;
    setBusy(true); setFailure(null);
    try {
      const result = await post('/api/interventions/session', token, { executor, message: text });
      if (!result.ok) { setFailure({ status: result.status, detail: result.data.detail || '' }); return; }
      await refresh();
      done(result.data.card_key);
    } catch { setFailure({ status: 0, detail: '' }); } finally { setBusy(false); }
  };
  return <div className="chat-main">
    <header className="chat-head"><div><h2>给 {agent.name} 开一个新会话</h2><p className="hint">会生成一张任务卡，从 {base} 分支开独立的 worktree；它做完或提问都会回到这个窗口。</p></div></header>
    <div className="chat-new">
      {execs.length > 1 && <label>用哪个执行器<select value={executor} onChange={e => setExecutor(e.target.value)}>{execs.map(e => <option key={e.id} value={e.id}>{e.label || e.name}（{e.id}）</option>)}</select></label>}
      <label>要它做什么<textarea rows={7} value={text} maxLength={4000} onChange={e => setText(e.target.value)} placeholder="把事情说清楚：现象、想要的结果、不要动什么。" /></label>
      <TokenField token={token} setToken={setToken} />
      {failure && <p className="chat-error" role="alert"><AlertTriangle size={14} />{explain(failure)}</p>}
      <button className="chat-send" disabled={!text.trim() || !token.trim() || busy} onClick={send}>{busy ? '发送中…' : '开始会话'}</button>
    </div>
  </div>;
}

function Conversation({ agent, session, mode, feed, token, setToken, refresh, jump, resume }: { agent: Agent; session: Session; mode: string; feed: ReturnType<typeof useAgentFeed>; token: string; setToken: (v: string) => void; refresh: () => Promise<Board | undefined>; jump: (card: string) => void; resume: boolean }) {
  const { card, runs } = session;
  const scroll = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const [text, setText] = useState('');
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState('');
  const [failure, setFailure] = useState<Failure | null>(null);
  const [note, setNote] = useState('');
  const [all, setAll] = useState(false);
  const transcripts = useRef(feed.transcripts);
  transcripts.current = feed.transcripts;
  const ids = runs.map(r => r.id).join(',');
  const { merge, generation } = feed;
  useEffect(() => {
    const controller = new AbortController();
    for (const id of ids ? ids.split(',') : []) {
      get<Msg[]>('/api/runs/' + id + '/messages?after_seq=' + contiguousSeq(transcripts.current[id] || []), controller.signal).then(m => merge(id, m)).catch(() => {});
    }
    return () => controller.abort();
  }, [ids, generation, merge]);
  const items = useMemo(() => {
    const out: Item[] = [];
    let order = 0;
    runs.forEach(run => {
      out.push({ t: when(run.started_at), order: order++, kind: 'run', run });
      for (const raw of feed.transcripts[run.id] || []) { const content = raw.type === 'text' ? spoken(raw.content || '') : (raw.content || '').trim(); if (content) out.push({ t: when(raw.ts) || when(run.started_at), order: order++, kind: 'msg', msg: { ...raw, content } }); }
    });
    // 卡片里的时间只到秒：同一秒内，你说的话排在这一轮开始之前，它的提问排在这一轮输出之后
    for (const entry of card?.chatLog || []) { const t = when(entry.time); out.push({ t: !t ? Number.MAX_SAFE_INTEGER : entry.kind === 'question' ? t + 999 : t, order: entry.kind === 'question' ? order++ : -1e6 + order++, kind: 'entry', entry }); }
    return out.sort((a, b) => a.t - b.t || a.order - b.order);
  }, [runs, feed.transcripts, card?.chatLog]);
  const shown = all ? items : items.slice(-250);
  useEffect(() => { if (stick.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight; }, [items.length]);
  const writable = mode !== 'readonly' && session.live && !!card;
  const needToken = mode === 'enabled';
  const submit = async (url: string, body: Record<string, string>, id: string, after: () => void) => {
    if (busy || (needToken && !token.trim())) return;
    setBusy(id); setFailure(null); setNote('');
    try {
      const result = await post(url, needToken ? token : '', body);
      if (!result.ok) { setFailure({ status: result.status, detail: result.data.detail || '' }); return; }
      after(); setNote(result.data.message || '已记录。');
      await refresh();
    } catch { setFailure({ status: 0, detail: '' }); } finally { setBusy(''); }
  };
  const send = () => { if (card && text.trim()) submit('/api/interventions/message', { card_key: card.key, message: text, expected_revision: card.cardRevision }, 'message', () => setText('')); };
  const answer = (id: string) => { const value = answers[id] || ''; if (card && value.trim()) submit('/api/interventions/answer', { card_key: card.key, question_id: id, answer: value, expected_revision: card.cardRevision }, id, () => setAnswers(a => ({ ...a, [id]: '' }))); };
  const stop = () => {
    if (!card || busy) return;
    if (needToken && !token.trim()) { setNote(''); setFailure({ status: 400, detail: '先在下面填写入令牌，再点停止。' }); return; }
    if (!window.confirm('确认停止？会结束 ' + agent.name + ' 正在跑的这一轮。已经改的文件、分支和会话都保留，之后发消息可以接着聊。')) return;
    submit('/api/interventions/stop', { card_key: card.key }, 'stop', () => {});
  };
  const latest = runs[runs.length - 1];
  const hint = !card ? '' : card.stopping ? '正在停止这一轮，几秒内结束。之后发消息可以接着这个会话继续。' : card.dispatch === 'held' && card.stopped ? '你已经停止了这一轮，会话和 worktree 都留着。发送后它会接着这个会话继续。' : COMPOSER[card.dispatch!] || '';
  return <div className="chat-main">
    <header className="chat-head">
      <div><h2>{session.cardId} <small>{session.title}</small></h2>
        <p className="hint"><span className={'chat-state ' + (card?.dispatch || latest?.state || '')}>{session.state}</span> · {agent.name} {agent.model}{card?.executor ? ' · ' + card.executor : ''}{(card?.branch || latest?.branch) ? <> · <GitBranch size={11} />{card?.branch || latest?.branch}</> : null} · {runs.length} 轮</p></div>
      {card && <div className="chat-actions">
        {writable && (card.stopping || STOPPABLE.includes(card.dispatch || '')) && <button className="chat-stop" disabled={!!busy || card.stopping} onClick={stop} title="结束它正在跑的这一轮，会话和 worktree 保留"><Square size={10} />{card.stopping ? '正在停止…' : busy === 'stop' ? '提交中…' : '停止'}</button>}
        <button className="dep" onClick={() => jump(card.key)}><ExternalLink size={12} />打开任务卡</button>
      </div>}
    </header>
    <div ref={scroll} className="chat-log" role="log" aria-label="对话记录" onScroll={e => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80; }}>
      {!all && items.length > shown.length && <button className="dep chat-more" onClick={() => setAll(true)}>显示更早的 {items.length - shown.length} 条</button>}
      {card && <details className="chat-task"><summary>任务说明（交给 Agent 的原文）</summary><pre>{card.body}</pre></details>}
      {shown.map(item => {
        if (item.kind === 'run') { const r = item.run!; return <div key={'r' + r.id} className="chat-turn"><span>第 {runs.indexOf(r) + 1} 轮 · {cst(r.started_at)} · {STATE[r.state] || r.state}{r.backfilled ? ' · 历史回填，没有逐条记录' : ''}</span></div>; }
        if (item.kind === 'msg') {
          const m = item.msg!;
          if (m.type === 'text' || m.type === 'error') return <article key={m.run_id + m.seq} className={'bubble agent' + (m.type === 'error' ? ' error' : '')}><pre>{m.content}</pre><time>{clock(when(m.ts))}</time></article>;
          return <details key={m.run_id + m.seq} className="chat-step"><summary>{TYPE[m.type] || m.type}{m.tool ? ' · ' + m.tool : ''} · {(m.summary || m.content).slice(0, 90)}</summary><pre>{m.content}</pre></details>;
        }
        const e = item.entry!;
        if (e.kind === 'question') return <article key={'q' + e.id} className={'bubble agent question' + (e.open ? ' open' : '')}>
          <b>{e.open ? '它在问你' : '它问过'}（{e.id}）</b><pre>{e.text}</pre><time>{e.time}</time>
          {e.open && writable && <div className="chat-answer">
            {e.options?.length ? <div className="chat-options">{e.options.map(o => <button key={o} className={answers[e.id] === o ? 'on' : ''} onClick={() => setAnswers(a => ({ ...a, [e.id]: o }))}>{o}</button>)}</div> : null}
            <textarea rows={2} value={answers[e.id] || ''} maxLength={4000} onChange={ev => setAnswers(a => ({ ...a, [e.id]: ev.target.value }))} placeholder="选一个选项，或直接写你的回答" />
            <button className="chat-send" disabled={!(answers[e.id] || '').trim() || !!busy || (needToken && !token.trim())} onClick={() => answer(e.id)}>{busy === e.id ? '提交中…' : '回答'}</button>
          </div>}
        </article>;
        return <article key={e.kind + e.id} className="bubble me"><pre>{e.text}</pre><time>{e.kind === 'answer' ? '回答 ' + e.id : e.delivered ? '已送达' : '等它这一轮结束后送达'} · {e.time}</time></article>;
      })}
      {!items.length && <p className="empty">{card?.dispatch === 'queued' ? '排队中，还没开始执行' : '还没有任何记录'}</p>}
      {latest?.state === 'running' && <p className="chat-typing"><Radio size={12} />{latest.current_activity || '正在执行…'}</p>}
    </div>
    <footer className="chat-composer">
      {writable ? <>
        <p className="hint">{hint}{resume ? '' : ' 这个执行器没有配续聊命令，继续时会从头再给它一遍任务。'}</p>
        <div className="chat-input"><textarea rows={2} value={text} maxLength={4000} onChange={e => setText(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) send(); }} placeholder={'对 ' + agent.name + ' 说…（Ctrl+Enter 发送）'} />
          <button className="chat-send" disabled={!text.trim() || !!busy || (needToken && !token.trim())} onClick={send} aria-label="发送"><CornerDownLeft size={15} />{busy === 'message' ? '发送中…' : '发送'}</button></div>
        {needToken && <TokenField token={token} setToken={setToken} />}
        {mode === 'demo' && <p className="hint">隔离演示：只记录留言，不会连接真实 Agent。</p>}
      </> : <p className="hint">{mode === 'readonly' ? '看板现在是只读的，不能发消息。' : card?.dispatch === 'closed' ? '这个会话已经关闭，只能看记录。' : '这是以前不经调度器直接跑的任务，没有可以续上的会话，只能看记录。'}</p>}
      {failure && <p className="chat-error" role="alert"><AlertTriangle size={14} />{explain(failure)}</p>}
      {note && <p className="chat-note" role="status">{note}</p>}
    </footer>
  </div>;
}
