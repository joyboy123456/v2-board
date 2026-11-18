import { useEffect, useMemo, useRef, useState, useCallback, Fragment, type WheelEvent, type PointerEvent } from 'react';
import { Minus, Plus, Maximize2, X, ArrowUpLeft, ArrowDownRight, AlertTriangle, ExternalLink, Map as MapIcon, GitMerge, PauseCircle, Unlink } from 'lucide-react';
import type { Board, Card, Link } from './types';
import { LABEL, ago, short, freshness, shortTitle, inline, flowColumns, STAGES, stageIndex, RUN_STATE, runReason } from './lib';

/* 任务地图（参考 Archify 的泳道 + 正交连线）：每条泳道是一个冲刺，从上到下是合并顺序
   （功能分支 → 集成验收 → 主干），未进合并链与搁置收在泳道底部的场外区。
   连线只画任务卡上登记的 depends（推断的用虚线）。 */

const NW = 204, NH = 116, MH = 68, CG = 18, G = 24, RG = 44, LG = 18, HEAD = 64;
const LW = 2 * G + 2 * NW + CG;
const PADX = 14, PADY = 30; // 泳道外留出跨冲刺连线的走线空间

interface Pos { c: Card; x: number; y: number; w: number; h: number; mini: boolean; side: 'L' | 'R' | 'C'; row: number; lane: number }
interface Edge { from: Card; to: Card; inferred: boolean }
interface Cross { keys: string[]; to: string; inferred: boolean; note: string; d: string; port?: { x: number; y: number; label: string } }
interface RowLab { x: number; y: number; text: string }
interface Lane { id: string; name: string; desc: string; x: number; y: number; w: number; h: number; sub: string; side?: { x: number; y: number; w: number; h: number; labs: RowLab[] } }

function layout(b: Board, perRow: number) {
  const pos = new Map<string, Pos>(); const lanes: Lane[] = []; const edges: Edge[] = []; const labs: RowLab[] = [];
  const geo = b.sprints.map((sp, li) => {
    const cards = b.cards.filter(c => c.sprint === sp.id);
    const f = flowColumns(cards);
    let y = HEAD + 14; const local: Pos[] = []; const llabs: RowLab[] = []; let row = 0;
    const names = f.cols.length === 1 ? ['任务'] : f.cols.map((_, i) => i === 0 ? '功能分支' : i === f.cols.length - 1 ? '主干收口' : '集成验收');
    f.cols.forEach((cs, d) => {
      if (!cs.length) return;
      llabs.push({ x: G, y: y - 4, text: names[d] });
      y += 12;
      for (let i = 0; i < cs.length; i += 2) {
        const pair = cs.slice(i, i + 2);
        pair.forEach((c, k) => {
          const single = pair.length === 1;
          local.push({ c, x: single ? (LW - NW) / 2 : G + k * (NW + CG), y, w: NW, h: NH, mini: false, side: single ? 'C' : k ? 'R' : 'L', row, lane: li });
        });
        row++; y += NH + (i + 2 < cs.length ? 12 : 0);
      }
      y += RG;
    });
    const out = [...f.loose, ...f.shelved];
    let side: Lane['side'];
    if (out.length) {
      const sy = y - RG + 26; let yy = sy + 10; const slabs: RowLab[] = [];
      const group = (list: Card[], text: string) => {
        if (!list.length) return;
        slabs.push({ x: G + 10, y: yy, text }); yy += 18;
        list.forEach((c, i) => local.push({ c, x: G + 10 + (i % 2) * (NW + CG - 10), y: yy + Math.floor(i / 2) * (MH + 8), w: NW - 10, h: MH, mini: true, side: 'C', row: 99, lane: li }));
        yy += Math.ceil(list.length / 2) * (MH + 8) + 6;
      };
      group(f.loose, '未进合并链');
      group(f.shelved, '搁置 · 分支保留，本轮不合并');
      side = { x: G, y: sy, w: LW - 2 * G, h: yy - sy + 4, labs: slabs };
      y = yy + 18;
    } else y = y - RG + 18;
    const n = (st: string) => cards.filter(c => c.st === st).length;
    const sub = [n('in_progress') && `进行中 ${n('in_progress')}`, n('review') && `待验收 ${n('review')}`, n('blocked') && `阻塞 ${n('blocked')}`, n('accepted') && `已验收 ${n('accepted')}`].filter(Boolean).join(' · ');
    const ids = new Map(cards.map(c => [c.id, c]));
    cards.forEach(c => c.depends.forEach(d => { const from = ids.get(d); if (from && from.st !== 'shelved' && c.st !== 'shelved') edges.push({ from, to: c, inferred: c.dependsInferred }); }));
    return { sp, li, local, llabs, side, h: y, sub };
  });
  // 泳道排布：宽屏横排，窄屏竖排
  let x0 = PADX, y0 = PADY, rowH = 0, width = 0;
  geo.forEach((g, i) => {
    if (i && i % perRow === 0) { y0 += rowH + LG; x0 = PADX; rowH = 0; }
    const h = perRow > 1 ? Math.max(...geo.slice(Math.floor(i / perRow) * perRow, Math.floor(i / perRow) * perRow + perRow).map(z => z.h)) : g.h;
    g.local.forEach(p => { p.x += x0; p.y += y0; pos.set(p.c.key, p); });
    g.llabs.forEach(l => labs.push({ ...l, x: l.x + x0, y: l.y + y0 }));
    lanes.push({ id: g.sp.id, name: g.sp.name, desc: g.sp.desc, sub: g.sub, x: x0, y: y0, w: LW, h,
      side: g.side && { ...g.side, x: g.side.x + x0, y: g.side.y + y0, labs: g.side.labs.map(l => ({ ...l, x: l.x + x0, y: l.y + y0 })) } });
    rowH = Math.max(rowH, h); x0 += LW + LG; width = Math.max(width, x0 - LG);
  });
  // 跨冲刺合并关系
  const laneIdx = new Map(b.sprints.map((sp, i) => [sp.id, i]));
  const card = new Map(b.cards.map(c => [c.key, c]));
  const cross: Cross[] = [];
  const groups = new Map<string, Link[]>();
  (b.links || []).forEach(l => { const a = card.get(l.from), t = card.get(l.to); if (!a || !t || !pos.has(l.from) || !pos.has(l.to)) return; const k = `${a.sprint}>${l.to}`; groups.set(k, [...(groups.get(k) || []), l]); });
  const horiz = perRow > 1;
  let gi = 0;
  groups.forEach(ls => {
    const a0 = card.get(ls[0].from)!, t = pos.get(ls[0].to)!;
    const sl = lanes[laneIdx.get(a0.sprint)!], tl = lanes[laneIdx.get(t.c.sprint)!];
    const fromRight = horiz && sl.x > tl.x;
    const gx = horiz ? (fromRight ? tl.x + tl.w + LG / 2 : tl.x - LG / 2) : PADX / 2;
    const off = (gi - 0.5) * 8; gi++;
    const ty = t.y + t.h / 2 + off;
    const tx = horiz && fromRight ? t.x + t.w + 7 : t.x - 7;
    const inferred = ls.every(l => l.inferred);
    const keys = ls.map(l => l.from);
    if (ls.length >= 3) {
      const ids = ls.map(l => card.get(l.from)!.id);
      const label = `${ids[0]}–${ids[ids.length - 1]} 并入 ${t.c.branch || t.c.id}${inferred ? '（推断）' : ''}`;
      const px = sl.x + 40, py = sl.y;
      const yb = horiz ? PADY / 2 - 2 : sl.y - LG / 2;
      cross.push({ keys, to: t.c.key, inferred, note: ls[0].note, port: { x: px, y: py, label },
        d: ortho([[px + 10, py], [px + 10, yb], [gx, yb], [gx, ty], [tx, ty]]) });
    } else ls.forEach(l => {
      const a = pos.get(l.from)!; const sy = a.y + a.h / 2;
      const sx = horiz && fromRight ? a.x : (horiz ? a.x + a.w : a.x);
      cross.push({ keys: [l.from], to: t.c.key, inferred: l.inferred, note: l.note,
        d: ortho([[sx, sy], [gx, sy], [gx, ty], [tx, ty]]) });
    });
  });
  return { pos, lanes, edges, labs, cross, width: width + PADX, height: y0 + rowH + 8 };
}

function reach(edges: Edge[], key: string, dir: 'up' | 'down') {
  const out = new Set<string>(); const stack = [key];
  while (stack.length) {
    const k = stack.pop()!;
    edges.forEach(e => {
      const [a, b] = dir === 'up' ? [e.to.key, e.from.key] : [e.from.key, e.to.key];
      if (a === k && !out.has(b)) { out.add(b); stack.push(b); }
    });
  }
  return out;
}

/** 圆角正交折线 */
function ortho(pts: [number, number][], r = 7) {
  const p = pts.filter((q, i) => !i || q[0] !== pts[i - 1][0] || q[1] !== pts[i - 1][1]);
  let d = `M${p[0][0]},${p[0][1]}`;
  for (let i = 1; i < p.length - 1; i++) {
    const [ax, ay] = p[i - 1], [bx, by] = p[i], [cx, cy] = p[i + 1];
    const r1 = Math.min(r, Math.hypot(bx - ax, by - ay) / 2, Math.hypot(cx - bx, cy - by) / 2);
    const ux = Math.sign(bx - ax), uy = Math.sign(by - ay), vx = Math.sign(cx - bx), vy = Math.sign(cy - by);
    d += ` L${bx - ux * r1},${by - uy * r1} Q${bx},${by} ${bx + vx * r1},${by + vy * r1}`;
  }
  const z = p[p.length - 1]; return d + ` L${z[0]},${z[1]}`;
}

function route(a: Pos, t: Pos, lane: Lane, i: number, n: number, j: number, m: number) {
  const tx = t.x + t.w * (j + 1) / (m + 1), tt = t.y - 7;
  if (t.row - a.row === 1) {
    const sx = a.x + a.w / 2 + (i - (n - 1) / 2) * 8, sb = a.y + a.h;
    const ym = (sb + t.y) / 2 + (j - (m - 1) / 2) * 4;
    return ortho([[sx, sb], [sx, ym], [tx, ym], [tx, tt]]);
  }
  // 跨层：从外侧出到泳道边沟，沿边沟下行，在目标上方的空隙里横到目标
  const right = a.side === 'R';
  const gx = right ? lane.x + lane.w - G / 2 + (j % 3) * 2 : lane.x + G / 2 - (j % 3) * 2;
  const ya = t.y - 16 - (j % 4) * 3;
  if (a.side === 'C') {
    const sx = a.x + a.w / 2, sb = a.y + a.h, ye = sb + 14;
    return ortho([[sx, sb], [sx, ye], [gx, ye], [gx, ya], [tx, ya], [tx, tt]]);
  }
  const sy = a.y + a.h / 2, sx = right ? a.x + a.w : a.x;
  return ortho([[sx, sy], [gx, sy], [gx, ya], [tx, ya], [tx, tt]]);
}

export default function TaskMap({ b, open, focus, setFocus }: { b: Board; open: (k: string) => void; focus: string; setFocus: (k: string) => void }) {
  const [narrow, setNarrow] = useState(() => innerWidth < 900);
  useEffect(() => { const r = () => setNarrow(innerWidth < 900); addEventListener('resize', r); return () => removeEventListener('resize', r); }, []);
  const L = useMemo(() => layout(b, narrow ? 1 : 3), [b, narrow]);
  const laneOf = (p: Pos) => L.lanes[p.lane];
  const box = useRef<HTMLDivElement>(null);
  const [view, setView] = useState({ s: 1, x: 0, y: 0 });
  const [mode, setMode] = useState<'all' | 'up' | 'down'>('all');
  const drag = useRef<{ x: number; y: number; vx: number; vy: number; moved: boolean } | null>(null);

  const fit = useCallback(() => {
    const el = box.current; if (!el) return;
    const w = el.clientWidth;
    const s = Math.min(1.15, (w - 24) / L.width);
    setView({ s, x: (w - L.width * s) / 2, y: 12 });
  }, [L.width]);
  useEffect(() => { fit(); addEventListener('resize', fit); return () => removeEventListener('resize', fit); }, [fit]);
  useEffect(() => { setMode('all'); }, [focus]);
  // 聚焦的卡不在可视区时，平移过去（手机上泳道竖排时常见）
  useEffect(() => {
    const p = focus ? L.pos.get(focus) : undefined; const el = box.current;
    if (!p || !el) return;
    setView(v => {
      const top = p.y * v.s + v.y, bottom = (p.y + p.h) * v.s + v.y;
      if (top >= 8 && bottom <= el.clientHeight * (narrow ? 0.45 : 1) - 8) return v;
      return { ...v, y: Math.min(12, 24 - p.y * v.s + (narrow ? 40 : 0)) };
    });
  }, [focus, L, narrow]);

  const allEdges = useMemo(() => {
    const ck = new Map(b.cards.map(c => [c.key, c]));
    return [...L.edges, ...L.cross.flatMap(x => x.keys.map(k => ({ from: ck.get(k)!, to: ck.get(x.to)!, inferred: x.inferred })))];
  }, [L, b.cards]);
  const up = useMemo(() => focus ? reach(allEdges, focus, 'up') : new Set<string>(), [allEdges, focus]);
  const down = useMemo(() => focus ? reach(allEdges, focus, 'down') : new Set<string>(), [allEdges, focus]);
  const lit = focus ? new Set([focus, ...(mode !== 'down' ? up : []), ...(mode !== 'up' ? down : [])]) : null;

  const zoom = (k: number, cx?: number, cy?: number) => setView(v => {
    const s = Math.min(2, Math.max(0.3, v.s * k));
    const el = box.current!; const px = cx ?? el.clientWidth / 2, py = cy ?? el.clientHeight / 2;
    return { s, x: px - (px - v.x) * (s / v.s), y: py - (py - v.y) * (s / v.s) };
  });
  const onWheel = (e: WheelEvent) => {
    if (!e.ctrlKey && !e.metaKey) return; // 普通滚轮留给页面滚动
    e.preventDefault(); const r = box.current!.getBoundingClientRect();
    zoom(e.deltaY < 0 ? 1.12 : 1 / 1.12, e.clientX - r.left, e.clientY - r.top);
  };
  const onDown = (e: PointerEvent) => { if ((e.target as HTMLElement).closest('button')) return; drag.current = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y, moved: false }; (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId); };
  const onMove = (e: PointerEvent) => { const d = drag.current; if (!d) return; const dx = e.clientX - d.x, dy = e.clientY - d.y; if (Math.abs(dx) + Math.abs(dy) > 3) d.moved = true; setView(v => ({ ...v, x: d.vx + dx, y: d.vy + dy })); };
  const onUp = () => { const d = drag.current; drag.current = null; if (d && !d.moved) setFocus(''); };

  // 每个节点的出入边序号，用于错开端点
  const outs = new Map<string, Edge[]>(), ins = new Map<string, Edge[]>();
  L.edges.forEach(e => { outs.set(e.from.key, [...(outs.get(e.from.key) || []), e]); ins.set(e.to.key, [...(ins.get(e.to.key) || []), e]); });
  const f = focus ? b.cards.find(c => c.key === focus) : undefined;
  const now = b.now;
  const fitS = Math.min(1.15, ((box.current?.clientWidth ?? 1200) - 24) / L.width);
  const heightPx = narrow ? Math.min(L.height * fitS + 24, 640) : L.height * fitS + 24;

  return (
    <section className="panel mapp">
      <div className="ph">
        <h2><MapIcon size={16} />任务地图</h2>
        <small>每条泳道是一批任务，从左到右按批次并入主干；点卡片看上下游</small>
        <div className="legend">
          {(['in_progress', 'review', 'blocked', 'shelved', 'accepted'] as const).map(st => <span key={st} className={`lg s-${st}`}><i />{LABEL[st]}</span>)}
          <span className="lg dash"><i />推断依赖</span>
        </div>
      </div>
      <div className="mapbox" ref={box} style={{ height: heightPx }} onWheel={onWheel} onPointerDown={onDown} onPointerMove={onMove} onPointerUp={onUp}>
        <div className="canvas" style={{ width: L.width, height: L.height, transform: `translate(${view.x}px,${view.y}px) scale(${view.s})` }}>
          {L.lanes.map(l => (
            <div key={l.id} className="lane" style={{ left: l.x, top: l.y, width: l.w, height: l.h }}>
              <div className="lane-h"><div className="lane-tt"><span className="lane-n">{l.name}</span><span className="lane-d">{l.desc}</span></div><span className="lane-sub">{l.sub}</span></div>
            </div>
          ))}
          {L.labs.map((r, i) => <div key={i} className="rowlab" style={{ left: r.x, top: r.y }}>{r.text === '主干收口' ? <GitMerge size={11} /> : null}{r.text}</div>)}
          {L.lanes.map(l => l.side && (
            <Fragment key={l.id + 's'}>
              <div className="sidebox" style={{ left: l.side.x, top: l.side.y, width: l.side.w, height: l.side.h }} />
              {l.side.labs.map((r, i) => <div key={i} className="sidelab" style={{ left: r.x, top: r.y }}>{r.text.startsWith('搁置') ? <PauseCircle size={11} /> : <Unlink size={11} />}{r.text}</div>)}
            </Fragment>
          ))}
          <svg className="edges" width={L.width} height={L.height}>
            <defs>
              {['on', 'off', 'dim', 'lit', 'x'].map(k => <marker key={k} id={`ah-${k}`} viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" className={`ah ${k}`} /></marker>)}
            </defs>
            {L.edges.map((e, k) => {
              const a = L.pos.get(e.from.key)!, t = L.pos.get(e.to.key)!;
              const o = outs.get(e.from.key)!, ii = ins.get(e.to.key)!;
              const d = route(a, t, laneOf(a), o.indexOf(e), o.length, ii.indexOf(e), ii.length);
              const done = ['review', 'accepted'].includes(e.from.st);
              const on = lit ? lit.has(e.from.key) && lit.has(e.to.key) : false;
              const cls = `edge ${done ? 'done' : 'todo'}${e.inferred ? ' inferred' : ''}${lit ? (on ? ' lit' : ' dim') : ''}${e.to.st === 'in_progress' ? ' flow' : ''}`;
              return <path key={k} d={d} className={cls} style={{ ['--c' as string]: `var(--s-${e.from.st})` }} markerEnd={`url(#ah-${lit ? (on ? 'lit' : 'dim') : done ? 'on' : 'off'})`} />;
            })}
            {L.cross.map((x, k) => {
              const on = lit ? lit.has(x.to) && x.keys.some(q => lit.has(q)) : false;
              return <path key={'x' + k} d={x.d} className={`edge cross${x.inferred ? ' inferred' : ''}${lit ? (on ? ' lit' : ' dim') : ''}`} markerEnd={`url(#ah-${lit ? (on ? 'lit' : 'dim') : 'x'})`} />;
            })}
          </svg>
          {L.cross.filter(x => x.port).map((x, k) => {
            const on = lit ? lit.has(x.to) && x.keys.some(q => lit.has(q)) : false;
            return <div key={'p' + k} className={`port${lit ? (on ? ' lit' : ' dim') : ''}`} style={{ left: x.port!.x, top: x.port!.y }} title={`依据：${x.note}`}><GitMerge size={11} />{x.port!.label}</div>;
          })}
          {[...L.pos.values()].map(p => {
            const c = p.c, idx = stageIndex(c.st), fr = freshness(c.stamp, now);
            const dim = lit && !lit.has(c.key), sel = focus === c.key;
            const why = c.needsAttention ? runReason(c.runReason) || c.decisions[0]?.text || c.last : c.st === 'blocked' || c.st === 'rejected' ? (c.decisions[0]?.text || (c.last !== '暂无汇报' ? c.last : '') || '没有写明原因') : '';
            if (p.mini) return (
              <button key={c.key} className={`mnode s-${c.st}${c.needsAttention ? " needs-attention" : ""}${dim ? ' dim' : ''}${sel ? ' sel' : ''}`} style={{ left: p.x, top: p.y, width: p.w, height: p.h }}
                onClick={() => setFocus(sel ? '' : c.key)}>
                <span className="cid">{c.id}</span><span className="nt">{shortTitle(c)}</span><span className="mst">{c.latestRun ? RUN_STATE[c.latestRun.state] : LABEL[c.st]}</span>{c.missingFields.length > 0 && <small className="incomplete">字段不全：{c.missingFields.join("、")}</small>}
              </button>
            );
            return (
              <button key={c.key} className={`mapnode s-${c.st}${c.needsAttention ? " needs-attention" : ""}${dim ? ' dim' : ''}${sel ? ' sel' : ''}`} style={{ left: p.x, top: p.y, width: p.w, height: p.h }}
                onClick={() => setFocus(sel ? '' : c.key)} aria-label={`${c.id} ${c.title}，${LABEL[c.st]}`}>
                <div className="mn-h"><span className="cid">{c.id}</span>{c.benchBrands?.length > 0 && <span className="mn-b" title={c.bench}>学 {c.benchBrands.join(" / ")}</span>}<span className="mn-st"><i />{LABEL[c.st]}</span></div>
                <div className="mn-t">{shortTitle(c)}</div>{c.missingFields.length > 0 && <small className="incomplete">字段不全：{c.missingFields.join("、")}</small>}{c.latestRun && <small className={`node-run run-${c.latestRun.state}`}>{RUN_STATE[c.latestRun.state]} · {c.latestRun.agent}</small>}
                {why ? <div className="mn-why"><AlertTriangle size={11} /><span dangerouslySetInnerHTML={{ __html: inline(why) }} /></div>
                  : <div className="mn-f"><span className="mn-stage">{STAGES.map((s, i) => <i key={s.label} className={i < idx ? 'done' : i === idx ? 'cur' : ''} />)}</span><span className={`fr ${c.st === 'in_progress' ? fr : ''}`}>{c.st === 'in_progress' ? `${ago(c.stamp, now)}汇报` : c.stamp.replace(/^\d{4}-(\d\d)-(\d\d) (\d\d:\d\d).*/, '$1/$2 $3')}</span></div>}
              </button>
            );
          })}
        </div>
        <div className="mapctl" onPointerDown={e => e.stopPropagation()}>
          <button onClick={() => zoom(1 / 1.15)} aria-label="缩小"><Minus size={14} /></button>
          <span>{Math.round(view.s * 100)}%</span>
          <button onClick={() => zoom(1.15)} aria-label="放大"><Plus size={14} /></button>
          <button onClick={fit} aria-label="适应宽度"><Maximize2 size={14} /></button>
        </div>
        {f && (
          <div className={`passport${(() => { const p = L.pos.get(f.key); return p && (p.x + p.w / 2) * view.s + view.x < (box.current?.clientWidth ?? 0) / 2 ? ' right' : ''; })()}`} onPointerDown={e => e.stopPropagation()}>
            <div className="pp-h"><span className="cid">{(b.sprints.find(x => x.id === f.sprint)?.name || f.sprint).split(' · ')[0]} · {f.id}</span><span className={`pill sm s-${f.st}`}><i />{LABEL[f.st]}</span><button className="icon" onClick={() => setFocus('')} aria-label="关闭"><X size={16} /></button></div>
            <h4>{f.title}</h4>{f.latestRun && <p className="node-run">{RUN_STATE[f.latestRun.state]} · {runReason(f.runReason)}<a className="dep" href={`?view=agents&run=${encodeURIComponent(f.latestRun.id)}`}>查看对应 run 日志 →</a></p>}
            <div className="pp-reach">
              <button className={mode === 'up' ? 'on' : ''} onClick={() => setMode(mode === 'up' ? 'all' : 'up')}><ArrowUpLeft size={13} />上游<b>{up.size}</b></button>
              <button className={mode === 'down' ? 'on' : ''} onClick={() => setMode(mode === 'down' ? 'all' : 'down')}><ArrowDownRight size={13} />下游<b>{down.size}</b></button>
            </div>
            <dl>
              <div><dt>{f.st === 'blocked' ? '卡在' : '最近'}</dt><dd className="clamp3" dangerouslySetInnerHTML={{ __html: inline(f.st === 'blocked' ? (f.decisions[0]?.text || f.last) : (f.reports[f.reports.length - 1]?.text || f.last)) }} /></div>
              {f.bench && <div><dt>对标</dt><dd className="bench">{f.bench}</dd></div>}
              <div><dt>时间</dt><dd>{short(f.stamp)}（{ago(f.stamp, now)}）</dd></div>
              <div><dt>执行</dt><dd>{f.assignee || '未指派'}</dd></div>
              {f.depends.length > 0 && <div><dt>等待</dt><dd>{f.depends.map(d => { const c = b.cards.find(x => x.sprint === f.sprint && x.id === d); return c ? <button key={d} className={`chip s-${c.st}`} onClick={() => setFocus(c.key)}>{d} · {LABEL[c.st]}</button> : <span key={d}>{d}</span>; })}{f.dependsInferred && <small className="muted">推断</small>}</dd></div>}
              {(() => { const ins = (b.links || []).filter(l => l.to === f.key); if (!ins.length) return null;
                return <div><dt>并入</dt><dd>{ins.map(l => { const c = b.cards.find(x => x.key === l.from)!; return <button key={l.from} className={`chip s-${c.st}`} onClick={() => setFocus(c.key)}>{c.sprint !== f.sprint ? (c.sprint === 'parity' ? '第 2 批 ' : c.sprint === 'wiring' ? '第 1 批 ' : '') : ''}{c.id}</button>; })}{ins.some(l => l.inferred) && <small className="muted">部分推断，依据 {ins.find(l => l.inferred)!.note}</small>}</dd></div>; })()}
              {(() => { const outs = (b.links || []).filter(l => l.from === f.key); if (!outs.length) return null;
                return <div><dt>去向</dt><dd>{outs.map(l => { const c = b.cards.find(x => x.key === l.to)!; return <button key={l.to} className={`chip s-${c.st}`} onClick={() => setFocus(c.key)}>{c.branch || c.key}</button>; })}{outs.some(l => l.inferred) && <small className="muted">推断，依据 {outs[0].note}</small>}</dd></div>; })()}
              {down.size > 0 && <div><dt>影响</dt><dd>{[...down].map(k => { const c = b.cards.find(x => x.key === k)!; return <button key={k} className={`chip s-${c.st}`} onClick={() => setFocus(k)}>{c.sprint !== f.sprint ? (c.branch || c.key) : c.id}</button>; })}</dd></div>}
            </dl>
            <button className="primary" onClick={() => open(f.key)}><ExternalLink size={14} />查看详情</button>
          </div>
        )}
      </div>
    </section>
  );
}
