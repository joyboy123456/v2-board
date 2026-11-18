import { useMemo, useState } from 'react';
import { GitMerge, Clock, AlertTriangle } from 'lucide-react';
import type { Board, Card, MergeState, Milestone } from './types';
import { LABEL, short, shortTitle, RUN_STATE, runReason } from './lib';

export function MergeProgress({ value }: { value?: MergeState }) {
  if (!value?.known) return <p className="hint">合并进度：Git 数据待同步</p>;
  return <p className="merge-progress" aria-label="合并进度"><GitMerge size={14} />{[['submitted', '已提交'], ['integration', '已合入集成分支'], ['main', '已进 main']].map(([k, label], i) => <span key={k} className={value[k as keyof MergeState] ? 'done' : ''}>{i > 0 ? ' → ' : ''}{value[k as keyof MergeState] ? '✓ ' : '○ '}{label}</span>)}</p>;
}

const STEPS = ['开工', '开发完成', '验收', '合入'] as const;
const t = (s?: string) => (s ? Date.parse(s) || 0 : 0);

// The furthest step a card has reached, from its status and Git merge state, even when no timestamp survived.
function reached(c: Card): number {
  if (c.git.merge.integration || c.git.merge.main) return 3;
  if (c.st === 'accepted') return 2;
  if (c.st === 'review' || c.st === 'rejected') return 1;
  if (['in_progress', 'blocked', 'shelved'].includes(c.st) || c.git.commit_count > 0 || c.latestRun) return 0;
  return -1;
}

interface Row { card: Card; steps: (Milestone | undefined)[]; start: number; step: number }

export default function ProjectProgress({ board, open }: { board: Board; open: (key: string) => void }) {
  const [onlyIssues, setOnlyIssues] = useState(false);
  const phases = useMemo(() => {
    const byCard = new Map<string, Milestone[]>();
    for (const m of board.timeline) byCard.set(m.card, [...(byCard.get(m.card) || []), m]);
    return board.sprints.map(s => {
      const rows: Row[] = board.cards.filter(c => c.sprint === s.id).map(card => {
        const ms = byCard.get(card.key) || [];
        const raw = STEPS.map(stage => ms.filter(m => (m.stage === stage || m.stage.startsWith(stage)) && m.time).sort((a, b) => t(a.time) - t(b.time))[0]);
        // A later step never shows a time earlier than the step before it (e.g. a merge found on an older base); hide it instead of misleading.
        let floor = 0;
        const steps = raw.map(m => { if (!m) return m; if (t(m.time) < floor) return undefined; floor = t(m.time); return m; });
        const times = ms.map(m => t(m.time)).filter(Boolean);
        return { card, steps, start: times.length ? Math.min(...times) : Number.MAX_SAFE_INTEGER, step: reached(card) };
      }).sort((a, b) => a.start - b.start || a.card.id.localeCompare(b.card.id, 'zh', { numeric: true }));
      const times = rows.map(r => r.start).filter(x => x < Number.MAX_SAFE_INTEGER);
      const done = rows.filter(r => r.step >= 2 || r.card.st === 'accepted').length;
      const issues = rows.filter(r => r.card.needsAttention).length;
      return { s, rows, from: times.length ? Math.min(...times) : 0, done, issues };
    }).filter(p => p.rows.length);
  }, [board]);
  const totalIssues = phases.reduce((n, p) => n + p.issues, 0);
  return <main className="panel project-progress">
    <div className="ph"><h2><Clock size={16} />项目进程 · 0 → 1</h2><small>按批次先后 · 每张卡 开工 → 开发完成 → 验收 → 合入 · 北京时间</small></div>
    <div className="pp-filter"><button className={!onlyIssues ? 'on' : ''} onClick={() => setOnlyIssues(false)}>全部节点</button><button className={onlyIssues ? 'on' : ''} onClick={() => setOnlyIssues(true)}><AlertTriangle size={13} />只看出问题的 {totalIssues}</button></div>
    <ol className="pp-phases">{phases.map((p, i) => {
      const rows = onlyIssues ? p.rows.filter(r => r.card.needsAttention) : p.rows;
      if (!rows.length) return null;
      return <li key={p.s.id} className="pp-phase">
        <header><span className="pp-no">{i + 1}</span><div><h3>{p.s.name}</h3><p>{p.s.desc}</p></div><small>{p.from ? short(new Date(p.from).toISOString().replace('Z', '')) + ' 起 · ' : ''}{p.done}/{p.rows.length} 已过验收{p.issues ? ` · ${p.issues} 个要处理` : ''}</small></header>
        <ol className="pp-cards">{rows.map(({ card: c, steps, step }) => <li key={c.key}>
          <button className={`pp-card${c.needsAttention ? ' needs-attention' : ''}`} onClick={() => open(c.key)}>
            <span className="pp-title"><span className="cid">{c.id}</span><b>{shortTitle(c)}</b><span className={`pill sm s-${c.st}`}><i />{LABEL[c.st]}</span></span>
            <span className="pp-steps" aria-label="进度">{STEPS.map((label, k) => {
              const m = steps[k];
              const state = k <= step ? 'done' : k === step + 1 && c.needsAttention ? 'stuck' : '';
              return <span key={label} className={`pp-step ${state}`}><i />{label}<small>{m?.time ? short(m.time) : k <= step ? '时间未记录' : ''}</small></span>;
            })}</span>
            <span className="pp-meta">{c.git.commit_count} 个本卡提交 · {c.git.changed_files} 个文件{c.latestRun ? ` · ${c.latestRun.agent}` : c.assignee ? ` · ${c.assignee}` : ''}</span>
            {c.needsAttention && <span className="pp-why"><AlertTriangle size={12} />{c.latestRun && ['failed', 'timeout', 'lost'].includes(c.latestRun.state) ? `${RUN_STATE[c.latestRun.state]}：${runReason(c.runReason).slice(0, 80)}` : `${LABEL[c.st]}：${(c.decisions[0]?.text || c.last || '没有写明原因').slice(0, 80)}`}</span>}
          </button>
        </li>)}</ol>
      </li>;
    })}</ol>
    {!phases.length && <p className="empty">暂无项目进程，任务与执行记录到达后自动更新</p>}
  </main>;
}
