import type { Card, St } from './types';

export const LABEL: Record<St, string> = {
  unknown: '未定', in_progress: '进行中', review: '待验收', blocked: '阻塞', shelved: '搁置',
  assigned: '已指派', open: '待指派', rejected: '已退回', accepted: '已验收',
};
export const ORDER: St[] = ['in_progress', 'review', 'blocked', 'rejected', 'assigned', 'open', 'shelved', 'accepted', 'unknown'];
export const COUNT_ORDER: St[] = ['in_progress', 'review', 'blocked', 'shelved', 'assigned', 'accepted'];

/** 解析看板里的北京时间字符串为毫秒（以 UTC+8 计） */
export function parseT(s?: string): number | null {
  const m = (s || '').match(/(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d)(?::(\d\d))?/);
  return m ? Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4] - 8, +m[5], +(m[6] || 0)) : null;
}

export function minutesAgo(s: string, now: string): number | null {
  const a = parseT(s), b = parseT(now);
  return a == null || b == null ? null : Math.max(0, (b - a) / 60000);
}

export function ago(s: string, now: string): string {
  const m = minutesAgo(s, now);
  if (m == null) return '时间未知';
  if (m < 1) return '刚刚';
  if (m < 60) return `${Math.round(m)} 分钟前`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} 小时前`;
  return `${Math.floor(h / 24)} 天前`;
}

/** 北京时间绝对日期：11月16日 22:33 */
export function short(s: string): string {
  const m = (s || '').match(/\d{4}-(\d\d)-(\d\d)[ T](\d\d:\d\d)/);
  return m ? `${+m[1]}月${+m[2]}日 ${m[3]}` : '未记录';
}
/** 北京时间绝对日期：2026-11-16 22:33 */
export function full(s: string): string {
  const m = (s || '').match(/(\d{4}-\d\d-\d\d)[ T](\d\d:\d\d)/);
  return m ? `${m[1]} ${m[2]}` : '未记录';
}

export function freshness(s: string, now: string): 'live' | 'warm' | 'stale' {
  const m = minutesAgo(s, now);
  if (m == null) return 'stale';
  return m < 15 ? 'live' : m < 45 ? 'warm' : 'stale';
}

export function shortTitle(c: Card): string {
  return c.title.replace(/[（(][^）)]*[）)]\s*$/, '').replace(new RegExp('^' + c.id + '\\s*'), '').split(/[：:]/)[0].trim() || c.title;
}

const escMap: Record<string, string> = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' };
export const esc = (s: string) => String(s ?? '').replace(/[&<>"]/g, c => escMap[c]);

export function inline(s: string): string {
  return esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
}

/** 只读 Markdown 渲染：先转义再加有限标签 */
export function md(text: string): string {
  const out: string[] = []; const lines = (text || '').split('\n'); let i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (l.trim().startsWith('```')) {
      let j = i + 1; while (j < lines.length && !lines[j].trim().startsWith('```')) j++;
      out.push('<pre><code>' + esc(lines.slice(i + 1, j).join('\n')) + '</code></pre>'); i = j + 1; continue;
    }
    if (l.startsWith('|')) {
      const rows: string[][] = [];
      while (i < lines.length && lines[i].startsWith('|')) {
        if (!/^\|[\s\-:|]+\|$/.test(lines[i])) rows.push(lines[i].replace(/^\||\|$/g, '').split('|').map(c => inline(c.trim())));
        i++;
      }
      if (rows.length) out.push('<div class="md-table"><table><tr>' + rows[0].map(c => `<th>${c}</th>`).join('') + '</tr>' +
        rows.slice(1).map(r => '<tr>' + r.map(c => `<td>${c}</td>`).join('') + '</tr>').join('') + '</table></div>');
      continue;
    }
    const h = l.match(/^(#{1,4})\s+(.*)/);
    if (h) out.push(`<h${Math.min(h[1].length + 2, 6)}>${inline(h[2])}</h${Math.min(h[1].length + 2, 6)}>`);
    else if (l.startsWith('>')) out.push('<blockquote>' + inline(l.replace(/^>\s?/, '')) + '</blockquote>');
    else if (/^\s*([-*]|\d+[.、])\s+/.test(l)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*([-*]|\d+[.、])\s+/.test(lines[i])) { items.push('<li>' + inline(lines[i].replace(/^\s*([-*]|\d+[.、])\s+/, '')) + '</li>'); i++; }
      out.push('<ul>' + items.join('') + '</ul>'); continue;
    } else if (l.trim()) out.push('<p>' + inline(l) + '</p>');
    i++;
  }
  return out.join('\n');
}

/** 合并路线：按依赖深度分列；没有上下游的卡单列，搁置单列 */
export function flowColumns(cards: Card[]) {
  const shelved = cards.filter(c => c.st === 'shelved');
  const live = cards.filter(c => c.st !== 'shelved');
  const ids = new Map(live.map(c => [c.id, c]));
  const depth = new Map<string, number>();
  const dep = (c: Card, seen = new Set<string>()): number => {
    if (depth.has(c.id)) return depth.get(c.id)!;
    if (seen.has(c.id)) return 0; seen.add(c.id);
    const ds = c.depends.filter(d => ids.has(d));
    const v = ds.length ? 1 + Math.max(...ds.map(d => dep(ids.get(d)!, seen))) : 0;
    depth.set(c.id, v); return v;
  };
  live.forEach(c => dep(c));
  const used = new Set(live.flatMap(c => c.depends));
  const maxd = Math.max(0, ...depth.values());
  const loose = maxd > 0 ? live.filter(c => depth.get(c.id) === 0 && !used.has(c.id)) : [];
  const cols: Card[][] = [];
  for (let d = 0; d <= maxd; d++) cols.push(live.filter(c => depth.get(c.id) === d && !loose.includes(c)));
  return { cols, loose, shelved, maxd };
}

export const STAGES: { st: St[]; label: string }[] = [
  { st: ['assigned'], label: '指派' },
  { st: ['in_progress', 'blocked', 'rejected'], label: '开发' },
  { st: ['review'], label: '验收' },
  { st: ['accepted'], label: '完成' },
];
export function stageIndex(st: St): number {
  if (st === 'open' || st === 'shelved') return -1;
  return STAGES.findIndex(s => s.st.includes(st));
}

export const RUN_STATE: Record<string, string> = { running: '运行中', lost: '失联', lost_ended: '失联结束', failed: '失败', timeout: '超时', blocked: '受阻', completed: '成功', cancelled: '已取消', queued: '排队中' };
export function runReason(value: string) { return ({ heartbeat_stale: '超过 3 分钟没有心跳', historical_open: '历史开工记录没有结束汇报', process_gone: '执行进程已退出' } as Record<string, string>)[value] || value; }
