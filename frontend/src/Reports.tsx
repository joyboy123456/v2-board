import { useEffect, useState } from 'react';
import { ClipboardCheck, CircleCheck, CircleX, CircleDashed, CircleAlert, ImageOff, X, Globe, User, Camera, Video } from 'lucide-react';
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

export function reportsFor(reports: AccReport[] | undefined, c: Card) {
  return (reports || []).map(r => ({ r, items: r.items.filter(it => itemHits(it, c)) })).filter(x => x.items.length);
}

function Lightbox({ shot, close }: { shot: AccShot; close: () => void }) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.stopPropagation(); close(); } };
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

export function ItemView({ it, cards, open, hideCards }: { it: AccItem; cards?: Card[]; open?: (k: string) => void; hideCards?: boolean }) {
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
  const hits = reportsFor(b.reports, c);
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
          {items.map((it, i) => <ItemView key={i} it={it} cards={b.cards} open={open} />)}
        </section>
      ))}
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
          {items.map((it, i) => <ItemView key={r.id + i} it={it} cards={b.cards} open={open} />)}
          {!items.length && <div className="empty">这份报告里没有问题项。</div>}
        </div>
      </section>
    </div>
  );
}
