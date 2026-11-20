export type Status = 'unknown' | 'open' | 'assigned' | 'in_progress' | 'blocked' | 'review' | 'accepted' | 'rejected';
export type St = Status | 'shelved';

export interface Report { time: string; sha: string; who: string; text: string }
export interface Commit { sha: string; time: string; subject: string }
export interface Decision { text: string; kind: 'you' | 'hark' | 'note' }

export interface MergeState { submitted: boolean; integration: boolean; main: boolean; known: boolean }
export interface CardRun { id: string; state: string; agent: string; model: string; started_at: string; ended_at?: string; duration_seconds?: number; current_activity?: string; last_message?: string; final_summary?: string; reason?: string }
export interface Milestone { card: string; title: string; sprint: string; time: string; stage: string; source: string; summary?: string; run_id?: string }
export interface Card {
  missingFields: string[]; latestRun: CardRun | null; needsAttention: boolean; runReason: string;
  git: { commit_count: number; changed_files: number; inherited_count: number; parent: string; merge: MergeState };
  key: string; sprint: string; id: string; title: string; status: Status; st: St;
  assignee: string; branch: string; worktree: string; depends: string[]; dependsInferred: boolean; bench: string; benchBrands: string[];
  priority: string; created: string; updated: string; capability: string;
  last: string; stamp: string; stampFromFile: boolean; source: string;
  reports: Report[]; commits: Commit[]; gitNote: string; baseline: string;
  decisions: Decision[]; body: string; progress: string; legacy: boolean;
  dispatch?: string; stopping?: boolean; stopped?: boolean; executor?: string; questions?: Question[]; answerHistory: AnswerHistory[]; chatLog?: ChatEntry[]; cardRevision: string; repairSource?: string; repairReport?: string; repairItem?: string;
}
export interface Question { id: string; time: string; who: string; text: string; options: string[] }
export interface AnswerHistory { id: string; time: string; actor: string; answer: string }

export interface Sys { load: number[]; cores: number; memUsed: number; memTotal: number; diskUsed: number; diskTotal: number; diskPct: number }
export interface Sprint { id: string; name: string; desc: string; count: number }

export interface Link { from: string; to: string; inferred: boolean; note: string }

export interface Board {
  timeline: Milestone[]; summary: { review_today: number; accepted_today: number; review_total: number; accepted_total: number }; links: Link[]; cards: Card[]; errors: string[]; sprints: Sprint[]; version: string; now: string; sys: Sys; dispatch?: { slots: number }; reports?: AccReport[];
  intervention: { mode: 'readonly' | 'demo' | 'enabled'; reason?: string; repair?: boolean; executors?: Executor[]; repairBase?: string };
}

export type Verdict = 'pass' | 'fail' | 'partial' | 'pending' | 'note';
export interface AccShot { alt: string; src: string; missing: boolean; kind?: 'image' | 'video' }
export interface AccItem { title: string; cards: string[]; verdict: Verdict; verdictText: string; text: string; shots: AccShot[] }
export interface Executor { id: string; name: string; browser: 'verified' | 'unknown'; agent?: string; label?: string; resume?: boolean }
export interface ChatEntry { kind: 'question' | 'answer' | 'message'; id: string; time: string; actor: string; text: string; options?: string[]; open?: boolean; delivered?: boolean }
export interface AccReport { id: string; revision: string; title: string; date: string; by: string; site: string; shotsNote: string; intro: string; items: AccItem[]; counts: Partial<Record<Verdict, number>>; cards: string[] }
