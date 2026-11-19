import type React from 'react';
import { useEffect, useState } from 'react';
import { Wrench, ClipboardCheck, CircleCheck, CircleX, CircleDashed, CircleAlert, ImageOff, X, Globe, User, Camera, Video } from 'lucide-react';
import type { AccItem, AccReport, AccShot, Board, Card, Verdict } from './types';
import { md } from './lib';

export const VERDICT_LABEL: Record<Verdict, string> = { pass: '通过', fail: '未通过', partial: '部分通过', pending: '未完成', note: '说明' };
const VERDICT_ORDER: Verdict[] = ['fail', 'partial', 'pending', 'pass', 'note'];

export function VerdictTag({ v, text }: { v: Verdict; text?: string }) {
  const Icon = v === 'pass' ? CircleCheck : v === 'fail' ? CircleX : v === 'partial' ? CircleAlert : CircleDashed;
  return <span className={`vtag v-${v}`}><Icon size={13} strokeWidth={2} />{text || VERDICT_LABEL[v]}</span>;
}

/** 卡片 id 与报告里写的卡号是否对应（支持 h/ACC 这种带批次的写法） */
export function itemHits(it: AccItem, c: Card) {
  return it.cards.some(x => x === c.id || x === c.key);
}

export function reportsFor(reports: AccReport[] | undefined, c: Card, children: Card[] = []) {
  return (reports || []).map(r => ({ r, items: r.items.filter(it => itemHits(it, c) || children.some(x => itemHits(it, x))) })).filter(x => x.items.length);
}

function Lightbox({ shot, close }: { shot: AccShot; close: () => void }) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); } };
    window.addEventListener('keydown', k, true);
    return () => window.removeEventListener('keydown', k, true);
  }, [close]);
  return (
    <div className="lightbox" role="dialog" aria-label={shot.alt} onClick={close}>
      <figure onClick={e => e.stopPropagation()}>
        <img src={shot.src} alt={shot.alt} />
        <figcaption>{shot.alt}<button className="icon" onClick={close} aria-label="关闭大图"><X size={18} /></button></figcaption>
      </figure>
    </div>
  );
}

function Shots({ shots }: { shots: AccShot[] }) {
  const [big, setBig] = useState<AccShot | null>(null);
  if (!shots.length) return null;
  return (
    <>
      <div className="shots">
        {shots.map((s, i) => s.missing
          ? <div key={i} className="shot missing"><ImageOff size={18} /><span>{s.kind === 'video' ? '录屏文件不在了' : '截图文件不在了'}</span><small>{s.alt}</small></div>
          : s.kind === 'video'
          ? <figure key={i} className="shot video">
              <video src={s.src} controls preload="metadata" playsInline aria-label={`录屏：${s.alt}`} />
              <span><Video size={13} />{s.alt}</span>
            </figure>
          : <button key={i} className="shot" onClick={() => setBig(s)} aria-label={`放大截图：${s.alt}`}>
              <img src={s.src} alt={s.alt} loading="lazy" />
              <span>{s.alt}</span>
            </button>)}
      </div>
      {big && <Lightbox shot={big} close={() => setBig(null)} />}
    </>
  );
}

export function ItemView({ it, cards, open, hideCards, repair }: { it: AccItem; cards?: Card[]; open?: (k: string) => void; hideCards?: boolean; repair?: React.ReactNode }) {
  return (
    <article className={`acc-item v-${it.verdict}`}>
      <header>
        <VerdictTag v={it.verdict} text={it.verdictText || undefined} />
        <h4>{it.title}</h4>
        {!hideCards && it.cards.length > 0 && <span className="acc-cards">{it.cards.map(id => {
          const c = cards?.find(c => c.id === id || c.key === id);
          return c && open ? <button key={id} className="cid" onClick={() => open(c.key)} title={c.title}>{id}</button> : <span key={id} className="cid">{id}</span>;
        })}</span>}
      </header>
      {it.text && <div className="md acc-text" dangerouslySetInnerHTML={{ __html: md(it.text) }} />}
      <Shots shots={it.shots} />
      {repair}
    </article>
  );
}

function Counts({ r }: { r: AccReport }) {
  return <span className="acc-counts">{VERDICT_ORDER.filter(v => v !== 'note' && r.counts[v]).map(v => <VerdictTag key={v} v={v} text={`${VERDICT_LABEL[v]} ${r.counts[v]}`} />)}</span>;
}

function Meta({ r }: { r: AccReport }) {
  const shots = r.items.reduce((a, it) => a + it.shots.filter(s => !s.missing && s.kind !== 'video').length, 0);
  const videos = r.items.reduce((a, it) => a + it.shots.filter(s => !s.missing && s.kind === 'video').length, 0);
  return (
    <div className="acc-meta">
      <span><User size={13} />{r.by || '未注明'}</span>
      {r.site && <span><Globe size={13} />{r.site}</span>}
      <span><Camera size={13} />截图 {shots} 张</span>
      {videos > 0 && <span><Video size={13} />录屏 {videos} 段</span>}
    </div>
  );
}

/** 抽屉里「验收」页签：只列这张卡相关的检查项 */
export function CardAcceptance({ b, c, open }: { b: Board; c: Card; open: (k: string) => void }) {
  // 修复卡（以及修复的修复）的验收报告也显示在原卡上
  const children: Card[] = [];
  for (let from = [c.key]; from.length;) {
    const next = b.cards.filter(x => x.repairSource && from.includes(x.repairSource) && !children.includes(x) && x !== c);
    children.push(...next);
    from = next.map(x => x.key);
  }
  const hits = reportsFor(b.reports, c, children);
  if (!hits.length) return <div className="empty"><ClipboardCheck size={16} />这张卡还没有验收报告。</div>;
  return (
    <div className="acc-card">
      {hits.map(({ r, items }) => (
        <section key={r.id} className="acc-block">
          <div className="acc-bh">
            <h4>{r.title}</h4>
            <time>{r.date}</time>
            <a className="dep" href={`?view=reports&report=${encodeURIComponent(r.id)}`}>看整份报告 →</a>
          </div>
          {r.shotsNote && items.every(it => !it.shots.length) && <p className="hint">{r.shotsNote}</p>}
          {items.map((it, i) => <ItemView key={i} it={it} cards={b.cards} open={open} repair={['fail', 'partial', 'pending'].includes(it.verdict) ? <RepairForm b={b} cands={[[...children].reverse().find(x => itemHits(it, x)) || c]} r={r} index={r.items.indexOf(it)} it={it} open={open} /> : undefined} />)}
        </section>
      ))}
    </div>
  );
}

/** 验收项上的「指派修复」：选执行器，把报错和截图整理成独立修复卡 */
const REPAIRABLE: Verdict[] = ['fail', 'partial', 'pending'];

/** 整份报告页上，一个验收项可以基于哪些任务卡修：报告里写了卡号的那些，最近的修复卡排最前 */
export function repairCands(b: Board, it: AccItem) {
  const hits = b.cards.filter(c => itemHits(it, c));
  return [...hits.filter(c => c.repairSource).reverse(), ...hits.filter(c => !c.repairSource)];
}

/** cands 为空表示报告没写这一项对应哪张任务卡：不关联任务卡，从看板配置的默认分支修 */
function RepairForm({ b, cands, r, index, it, open }: { b: Board; cands: Card[]; r: AccReport; index: number; it: AccItem; open: (k: string) => void }) {
  const iv = b.intervention;
  const list = iv?.executors || [];
  const preferred = list.find(x => x.browser === 'verified') || list.find(x => /codex/i.test(x.id)) || list[0];
  const [on, setOn] = useState(false);
  const [pick, setPick] = useState('');
  const c = cands.find(x => x.key === pick) || cands[0] as Card | undefined;
  const [executor, setExecutor] = useState(preferred?.id || '');
  const [notes, setNotes] = useState('');
  const [browser, setBrowser] = useState(true);
  const [mode, setMode] = useState<'real' | 'fixture'>('real');
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [done, setDone] = useState<{ repair_key: string; message: string } | null>(null);
  const existing = c
    ? b.cards.filter(x => x.repairSource === c.key)
    : b.cards.filter(x => !x.repairSource && x.repairReport === r.id && x.repairItem === String(index));
  if (!iv?.repair) return iv?.mode === 'readonly' ? null : <p className="hint">这个看板没有开启「指派修复」。</p>;
  const links = existing.length > 0 && <p className="repair-links">已有修复卡：{existing.map(x => <button key={x.key} className="cid" onClick={() => open(x.key)}>{x.id}</button>)}</p>;
  if (!c && !iv.repairBase) return (
    <div className="repair">
      {links}
      <p className="hint">{it.cards.length
        ? `这一项写的任务卡（${it.cards.join('、')}）在看板上找不到，`
        : '这份报告没有写这一项对应哪张任务卡，'}{iv.mode === 'demo' ? '演示模式下不能指派。' : '看板也还没有配置默认修复分支，暂时不能从这里指派。'}</p>
    </div>
  );
  const picked = list.find(x => x.id === executor);
  const submit = async () => {
    const warn = iv.mode === 'demo'
      ? '确认生成演示修复卡？只写合成数据，不会启动任何 Agent。'
      : `确认把这一项指派给 ${executor} 修复？会新建一张排队的修复卡，dispatcher 有空槽位时会启动 Agent${browser && mode === 'real' ? '，真实接口验收会产生云沙盒和模型费用' : ''}。`;
    if (!window.confirm(warn)) return;
    setBusy(true); setError('');
    try {
      const res = await fetch('/api/interventions/repair', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(iv.mode === 'enabled' ? { Authorization: `Bearer ${token}` } : {}) },
        body: JSON.stringify({ card_key: c?.key || '', expected_revision: c?.cardRevision || '', report_id: r.id, expected_report_revision: r.revision, item_index: index, executor, instructions: notes, requires_browser: browser, test_mode: mode }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) setError(data.detail || `提交失败（${res.status}）`);
      else setDone(data);
    } catch { setError('网络错误，未提交。'); }
    setBusy(false);
  };
  return (
    <div className="repair">
      {links}
      {done ? <p className="repair-done">{done.message} <button className="cid" onClick={() => open(done.repair_key)}>{done.repair_key}</button></p>
        : !list.length ? <p className="hint">还没有可指派的执行器：需要在调度配置里登记执行器，并且该配置指向这个看板目录。</p>
        : !on ? <button className="repair-open" onClick={() => setOn(true)}><Wrench size={14} />指派修复</button>
        : <div className="repair-form">
          <p className="repair-sum">会把这一项的结论、报错说明{it.shots.length ? `和 ${it.shots.length} 个截图/录屏` : ''}{c
            ? `连同原卡 ${c.id} 的分支信息一起交给 Agent，原卡不会被改动。`
            : `一起交给 Agent。报告没写这一项对应哪张任务卡，Agent 会从 ${iv.repairBase} 分支开独立 worktree 自己定位代码，验收报告不会被改动。`}</p>
          {cands.length > 1 && <label>基于哪张任务卡
            <select value={c?.key} onChange={e => setPick(e.target.value)}>
              {cands.map(x => <option key={x.key} value={x.key}>{x.id} · {x.title}</option>)}
            </select>
          </label>}
          <label>交给谁
            <select value={executor} onChange={e => setExecutor(e.target.value)}>
              {list.map(x => <option key={x.id} value={x.id}>{x.name} · 浏览器验收{x.browser === 'verified' ? '已确认' : '未确认'}</option>)}
            </select>
          </label>
          {browser && picked?.browser !== 'verified' && <p className="repair-warn">这个执行器能否操作浏览器还没有确认，可能跑不了云浏览器验收。</p>}
          <label className="repair-check"><input type="checkbox" checked={browser} onChange={e => setBrowser(e.target.checked)} />修完后用云沙盒浏览器验收</label>
          {browser && <label>验收接口
            <select value={mode} onChange={e => setMode(e.target.value as 'real' | 'fixture')}>
              <option value="real">真实接口（8 核 16G，会产生费用）</option>
              <option value="fixture">假接口 --fixture（4 核 8G）</option>
            </select>
          </label>}
          <label>补充说明（可不填）
            <textarea value={notes} maxLength={2000} onChange={e => setNotes(e.target.value)} placeholder="比如：先看画布节点的图片加载逻辑" />
          </label>
          {iv.mode === 'enabled' && <label>写入令牌<input type="password" value={token} autoComplete="off" onChange={e => setToken(e.target.value)} /></label>}
          {error && <p className="repair-err">{error}</p>}
          <div className="repair-act">
            <button onClick={() => setOn(false)} disabled={busy}>取消</button>
            <button className="primary" onClick={submit} disabled={busy || !executor || (iv.mode === 'enabled' && !token)}>{busy ? '提交中…' : '确认指派'}</button>
          </div>
        </div>}
    </div>
  );
}

/** 「验收报告」主视图 */
export default function Reports({ b, open, sel, setSel }: { b: Board; open: (k: string) => void; sel: string; setSel: (v: string) => void }) {
  const reports = b.reports || [];
  const [only, setOnly] = useState<'all' | 'bad'>('all');
  const r = reports.find(x => x.id === sel) || reports[0];
  if (!r) return <section className="panel"><div className="empty"><ClipboardCheck size={16} />还没有验收报告。</div></section>;
  const items = [...r.items]
    .filter(it => only === 'all' || ['fail', 'partial', 'pending'].includes(it.verdict))
    .sort((x, y) => VERDICT_ORDER.indexOf(x.verdict) - VERDICT_ORDER.indexOf(y.verdict));
  return (
    <div className="reports">
      <nav className="rep-list panel" aria-label="验收报告列表">
        <div className="ph"><h2>验收报告</h2><small>{reports.length} 份</small></div>
        {reports.map(x => (
          <button key={x.id} className={`rep-li${x.id === r.id ? ' on' : ''}`} aria-current={x.id === r.id ? 'true' : undefined} onClick={() => setSel(x.id)}>
            <time>{x.date}</time>
            <b>{x.title}</b>
            <Counts r={x} />
          </button>
        ))}
      </nav>
      <section className="rep-doc panel">
        <header className="rep-h">
          <div className="rep-ht"><h2>{r.title}</h2><time>{r.date} 北京时间</time></div>
          <Meta r={r} />
          <div className="rep-sum">
            <Counts r={r} />
            <div className="seg" role="group" aria-label="筛选检查项">
              <button className={only === 'all' ? 'on' : ''} aria-pressed={only === 'all'} onClick={() => setOnly('all')}>全部 {r.items.length}</button>
              <button className={only === 'bad' ? 'on' : ''} aria-pressed={only === 'bad'} onClick={() => setOnly('bad')}>只看有问题</button>
            </div>
          </div>
          {r.intro && <div className="md acc-intro" dangerouslySetInnerHTML={{ __html: md(r.intro) }} />}
          {r.shotsNote && <p className="hint"><Camera size={13} />{r.shotsNote}</p>}
        </header>
        <div className="acc-items">
          {items.map(it => {
            const index = r.items.indexOf(it);
            return <ItemView key={r.id + index} it={it} cards={b.cards} open={open}
              repair={REPAIRABLE.includes(it.verdict) ? <RepairForm b={b} cands={repairCands(b, it)} r={r} index={index} it={it} open={open} /> : undefined} />;
          })}
          {!items.length && <div className="empty">这份报告里没有问题项。</div>}
        </div>
      </section>
    </div>
  );
}
