export type MoveStatus = 'new' | 'pending' | 'excluded';
export type Job = {
  id: string;
  company: string;
  title: string;
  site: string;
  url: string;
  site_name?: string;
  score?: number | string;
  location?: string;
  reason?: string;
  updated_at?: string;
  decision_revision?: string;
  decision_status?: string;
  movement_status?: MoveStatus | null;
  movable?: boolean;
  allowed_moves?: MoveStatus[];
  application_stage_label?: string;
  canonical_status?: string;
  canonical_status_label?: string;
  tracker_id?: number | string;
  note?: string;
  notes?: string;
  first_seen?: string;
  posting_date?: string;
  date?: string;
  decided_at?: string;
  review_required?: boolean;
  needs_review?: boolean;
  mail_source?: string;
  [key: string]: unknown;
};
export type ArchiveSource = { id: string; name: string; total: number };
export type Archive = {
  items: Job[];
  total: number;
  sources?: ArchiveSource[];
};
export type MailSync = {
  status?: string;
  phase?: string;
  last_success_at?: string;
  started_at?: string;
  completed_at?: string;
  processed_threads?: number;
  applied_count?: number;
  review_count?: number;
  error?: string;
  error_message?: string;
  error_code?: string;
  message?: string;
  phase_detail?: string;
  phase_label?: string;
  window_start?: string;
  window_end?: string;
  total_threads?: number | null;
  remaining_messages?: number;
  remaining_known_threads?: number;
  elapsed_seconds?: number;
  duration_seconds?: number | null;
  phase_elapsed_seconds?: number;
  resumed?: boolean | null;
  run_mode?: string;
  provider?: string;
  sync_mode?: 'bootstrap' | 'incremental' | 'rescan';
  changed_threads?: number;
  history_pages?: number;
  cache_reused_threads?: number;
  last_full_run?: {
    duration_seconds?: number;
    completed_at?: string;
    total_threads?: number;
  } | null;
  progress?: {
    searched_windows?: number;
    total_windows?: number;
    found_messages?: number;
    read_messages?: number;
    [key: string]: unknown;
  };
  [key: string]: unknown;
};
export type Dashboard = {
  generated_at: string;
  source_run_at?: string;
  updated_at: string;
  served_at?: string;
  sites: { id: string; name: string; total: number; jobs: Job[] }[];
  applications: Job[];
  pending: Archive;
  excluded: Archive;
  stats: Record<string, number>;
  stale: boolean;
  warnings: string[];
  mail_sync?: MailSync;
};
export type Api = (
  path: string,
  method?: string,
  body?: unknown,
) => Promise<Record<string, unknown>>;
export const moveNames: Record<MoveStatus, string> = {
  new: '추천 공고',
  pending: '지원보류',
  excluded: '지원제외',
};
export const isMovable = (job: Job) =>
  job.movable === true &&
  ['new', 'pending', 'excluded'].includes(job.movement_status || '');

export const canMove = (job: Job, target: MoveStatus) =>
  isMovable(job) &&
  target !== job.movement_status &&
  (!job.allowed_moves || job.allowed_moves.includes(target));
