'use client';
import { useEffect, useRef, useState } from 'react';
import {
  ArrowUpRight,
  Check,
  ChevronDown,
  ChevronRight,
  Clock3,
  History,
  Mail,
  RefreshCw,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
  DialogHeader,
} from '@/components/ui/dialog';
import type { Api, Job, MailSync } from './dashboard-types';

const formatDate = (value?: string) => {
  if (!value || Number.isNaN(Date.parse(value))) return '아직 기록 없음';
  return new Intl.DateTimeFormat('ko-KR', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    timeZone: 'Asia/Seoul',
  }).format(new Date(value));
};
// "오늘 09:34" for today in Seoul, otherwise "09. 21. 09:34".
const shortTime = (value?: string) => {
  const d = new Date(value || '');
  if (Number.isNaN(d.getTime())) return '기록 없음';
  const day = (x: Date) =>
    new Intl.DateTimeFormat('ko-KR', {
      dateStyle: 'short',
      timeZone: 'Asia/Seoul',
    }).format(x);
  const clock = new Intl.DateTimeFormat('ko-KR', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
    timeZone: 'Asia/Seoul',
  }).format(d);
  if (day(d) === day(new Date())) return `오늘 ${clock}`;
  return `${new Intl.DateTimeFormat('ko-KR', { month: '2-digit', day: '2-digit', timeZone: 'Asia/Seoul' }).format(d)} ${clock}`;
};
export const mailIsRunning = (sync: MailSync) =>
  [
    'running',
    'queued',
    'collecting',
    'applying',
    'backfill',
    'in_progress',
  ].includes(sync.status || '');
const formatDuration = (value: number) => {
  const seconds = Math.max(0, Math.floor(value));
  return seconds >= 3600
    ? `${Math.floor(seconds / 3600)}시간 ${Math.floor((seconds % 3600) / 60)}분`
    : seconds >= 60
      ? `${Math.floor(seconds / 60)}분 ${seconds % 60}초`
      : `${seconds}초`;
};
const syncProgress = (sync: MailSync) => {
  const count = Number(sync.processed_threads) || 0;
  if (
    sync.phase_detail === 'history' ||
    sync.phase_detail === 'read_history' ||
    sync.phase_detail === 'list_history'
  )
    return '마지막 확인 이후 바뀐 메일을 찾고 있습니다.';
  if (sync.phase === 'search')
    return `검색 구간 ${sync.progress?.searched_windows || 0}/${sync.progress?.total_windows || 0}개 확인 · 관련 메일 ${sync.progress?.found_messages || 0}건 발견`;
  if (typeof sync.total_threads === 'number')
    return `대화 ${count}/${sync.total_threads}개 확인 · ${Math.max(0, sync.total_threads - count)}개 남음`;
  return `대화 ${count}개 확인${typeof sync.remaining_messages === 'number' ? ` · 검색된 메일 중 ${sync.remaining_messages}건 조회 대기` : ''}`;
};
export function MailSyncBar({
  api,
  onChanged,
}: {
  api: Api;
  onChanged: () => void;
}) {
  const [sync, setSync] = useState<MailSync>({});
  const [requesting, setRequesting] = useState(false);
  const [error, setError] = useState('');
  const [pollGeneration, setPollGeneration] = useState(0);
  const [clock, setClock] = useState(Date.now);
  const [sampledAt, setSampledAt] = useState(0);
  const [connection, setConnection] = useState<{
    configured?: boolean;
    connected?: boolean;
    provider?: string;
    can_authorize?: boolean;
    message?: string;
  }>({});
  const [connecting, setConnecting] = useState(false);
  const [open, setOpen] = useState(false);
  const pollEpoch = useRef(0);
  const wasRunning = useRef(false);
  const running = mailIsRunning(sync);
  useEffect(() => {
    let alive = true;
    api('/api/mail-connection')
      .then((value) => {
        if (alive) setConnection(value);
      })
      .catch(() => {
        /* Older servers keep their existing synchronization flow. */
      });
    return () => {
      alive = false;
    };
  }, [api, pollGeneration]);
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setClock(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    let previousSuccess = '';
    const poll = async () => {
      let delay = 30000;
      const epoch = ++pollEpoch.current;
      try {
        const result = await api('/api/mail-sync');
        if (!alive || epoch !== pollEpoch.current) return;
        const next = (result.sync || result) as MailSync;
        setSync(next);
        setSampledAt(Date.now());
        setClock(Date.now());
        setError('');
        if (
          (previousSuccess &&
            next.last_success_at &&
            previousSuccess !== next.last_success_at) ||
          (wasRunning.current && !mailIsRunning(next))
        )
          onChanged();
        wasRunning.current = mailIsRunning(next);
        previousSuccess = next.last_success_at || previousSuccess;
        delay = mailIsRunning(next) ? 5000 : 30000;
      } catch {
        if (alive && epoch === pollEpoch.current)
          setError('메일 동기화 현황을 불러오지 못했습니다.');
      }
      if (alive) timer = setTimeout(() => void poll(), delay);
    };
    void poll();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [api, onChanged, pollGeneration]);
  const start = async () => {
    pollEpoch.current += 1;
    setRequesting(true);
    setError('');
    try {
      const result = await api('/api/mail-sync', 'POST', {});
      const next = (result.sync || result) as MailSync;
      wasRunning.current = mailIsRunning(next);
      setSync(next);
      setSampledAt(Date.now());
      setClock(Date.now());
      if (!mailIsRunning(next)) onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRequesting(false);
      setPollGeneration((value) => value + 1);
    }
  };
  const connect = async () => {
    setConnecting(true);
    setError('');
    try {
      const result = await api('/api/mail-connection/start', 'POST', {});
      const destination = new URL(String(result.authorization_url));
      if (
        destination.origin !== 'https://accounts.google.com' ||
        destination.pathname !== '/o/oauth2/v2/auth'
      )
        throw new Error('Google 연결 주소를 확인하지 못했습니다.');
      window.location.assign(destination.href);
    } catch (e) {
      setError((e as Error).message);
      setConnecting(false);
    }
  };
  const elapsed =
    typeof sync.elapsed_seconds === 'number'
      ? sync.elapsed_seconds +
        (running ? Math.max(0, clock - sampledAt) / 1000 : 0)
      : sync.started_at && Number.isFinite(Date.parse(sync.started_at))
        ? Math.max(
            0,
            ((running
              ? clock
              : Date.parse(sync.completed_at || sync.started_at)) -
              Date.parse(sync.started_at)) /
              1000,
          )
        : null;
  const duration =
    typeof sync.duration_seconds === 'number' ? sync.duration_seconds : elapsed;
  const modeLabel =
    sync.resumed === true
      ? '중단 지점부터 이어서'
      : sync.sync_mode === 'incremental'
        ? '변경된 메일 확인'
        : sync.sync_mode === 'bootstrap'
          ? '첫 메일 기록 수집'
          : sync.sync_mode === 'rescan'
            ? '메일 기록 다시 확인'
            : sync.resumed === false
              ? '새 동기화'
              : '이번 동기화';
  const failed = ['failed', 'partial'].includes(sync.status || '');
  const syncError =
    sync.error_message ||
    (sync.error && /[가-힣]/.test(sync.error)
      ? sync.error
      : '메일 조회 결과를 확인하지 못해 중단됐습니다. 다시 동기화하면 저장된 지점부터 이어서 처리합니다.');
  const reviewCount = Number(sync.review_count) || 0;
  const hasError = Boolean(error || sync.error || failed);
  // Details stay folded in the normal state and open by themselves on errors.
  const detailsOpen = open || hasError;
  return (
    <section
      className={`sync-status${hasError ? ' is-failed' : ''}${running ? ' is-running' : ''}`}
      aria-label="메일 동기화 현황"
    >
      <div className="sync-line">
        <span className="sync-icon" aria-hidden="true">
          <Mail size={17} />
        </span>
        <p className="sync-summary">
          <strong aria-live="polite">
            {running
              ? sync.phase_label || '지원 관련 메일을 확인하고 있습니다'
              : failed
                ? '메일 동기화가 중단됐습니다'
                : sync.status === 'complete'
                  ? '메일 동기화 완료'
                  : '메일에서 이어지는 지원 기록'}
          </strong>
          {running ? (
            <span>{syncProgress(sync)}</span>
          ) : sync.last_success_at ? (
            <span>마지막 반영 {shortTime(sync.last_success_at)}</span>
          ) : !connection.connected && connection.message ? (
            <span>{connection.message}</span>
          ) : null}
          {!running && reviewCount > 0 && (
            <span className="sync-review">확인 필요 {reviewCount}건</span>
          )}
        </p>
        <div className="sync-actions">
          {connection.configured &&
            !connection.connected &&
            connection.can_authorize && (
              <Button
                variant="outline"
                disabled={connecting || running}
                onClick={() => void connect()}
              >
                <Mail size={15} />
                {connecting ? 'Google로 이동 중' : 'Gmail 연결'}
              </Button>
            )}
          <Button
            variant="outline"
            className="sync-button"
            disabled={requesting || running || sync.enabled === false}
            onClick={() => void start()}
          >
            <RefreshCw
              size={15}
              className={running || requesting ? 'spin' : ''}
            />
            <span className="sync-button-label">
              {running || requesting
                ? '동기화 중'
                : failed
                  ? '이어서 동기화'
                  : '메일 동기화'}
            </span>
          </Button>
          <button
            type="button"
            className="sync-toggle"
            aria-expanded={detailsOpen}
            aria-controls="sync-details"
            disabled={hasError}
            onClick={() => setOpen((value) => !value)}
            title={detailsOpen ? '동기화 상세 접기' : '동기화 상세 보기'}
          >
            <ChevronDown size={17} className={detailsOpen ? 'rotated' : ''} />
            <span className="sr-only">
              {detailsOpen ? '동기화 상세 접기' : '동기화 상세 보기'}
            </span>
          </button>
        </div>
      </div>
      {detailsOpen && (
        <div className="sync-details" id="sync-details">
          {(sync.provider === 'gmail' || connection.connected) && (
            <span>
              Gmail 직접 연결 ·{' '}
              {sync.provider !== 'gmail'
                ? '연결 준비됨'
                : failed
                  ? '메일 확인을 마치지 못했습니다'
                  : sync.sync_mode === 'incremental'
                    ? '새로 바뀐 메일만 확인합니다'
                    : running
                      ? '기존 기록을 확인하고 있습니다'
                      : '기존 기록 확인 완료'}
            </span>
          )}
          {!connection.connected && connection.message && (
            <span>{connection.message}</span>
          )}
          {sync.window_start && sync.window_end && (
            <span>
              {sync.sync_mode === 'incremental'
                ? '변경 확인 기간'
                : '검색 기간'}{' '}
              · {formatDate(sync.window_start)} ~ {formatDate(sync.window_end)}
            </span>
          )}
          {(running || failed) && <span>{syncProgress(sync)}</span>}
          {(running || failed) && elapsed !== null && (
            <span aria-live="off">
              {modeLabel} · 경과 {formatDuration(elapsed)}
            </span>
          )}
          {running &&
            !sync.sync_mode &&
            sync.resumed !== true &&
            typeof sync.last_full_run?.duration_seconds === 'number' && (
              <span>
                직전 새 동기화 소요 ·{' '}
                {formatDuration(sync.last_full_run.duration_seconds)}
              </span>
            )}
          {!running && (
            <span>
              정상 반영된 메일 기준 · {formatDate(sync.last_success_at)}
            </span>
          )}
          {!running && !failed && sync.completed_at && (
            <span>
              작업 완료 · {formatDate(sync.completed_at)}
              {duration !== null ? ` · 소요 ${formatDuration(duration)}` : ''}
            </span>
          )}
          {!running &&
            sync.provider === 'gmail' &&
            sync.status === 'complete' && (
              <span>
                이번 확인 대화 {Number(sync.processed_threads) || 0}개
                {sync.sync_mode === 'incremental' &&
                Number(sync.processed_threads) === 0
                  ? ' · 새로 읽을 대화가 없습니다'
                  : ''}
              </span>
            )}
          {(sync.applied_count != null || sync.review_count != null) && (
            <small>
              {running
                ? '확인 후 결과 반영 예정'
                : `이번 실행 반영 ${Number(sync.applied_count) || 0}건`}{' '}
              · 누적 확인 필요 {Number(sync.review_count) || 0}건
            </small>
          )}
          {(error || sync.error || failed) && (
            <output>{error || syncError}</output>
          )}
        </div>
      )}
    </section>
  );
}

type Event = {
  id?: string;
  at?: string;
  occurred_at?: string;
  timestamp?: string;
  kind?: string;
  label?: string;
  note?: string;
  evidence?: string;
  url?: string;
  mail_url?: string;
  source?: string;
  subject?: string;
  identity_evidence?: string;
  posting_url?: string;
  [key: string]: unknown;
};
const labels: Record<string, string> = {
  candidate_received: '채용 제안 수신',
  proposal_received: '채용 제안 수신',
  candidate_declined: '지원제외 의사 확인',
  candidate_withdrawn: '지원철회 확인',
  candidate_hold: '지원보류',
  candidate_applied: '지원 의사 전달',
  application_received: '지원 접수',
  application_submitted: '지원 접수',
  interview: '면접 안내',
  offer: '오퍼 수신',
  rejected: '불합격 안내',
  manual_move: '직접 분류',
  move: '직접 분류',
  undo: '이동 실행 취소',
  note: '메모 수정',
};
export function HistoryButton({
  job,
  onOpen,
  compact = false,
}: {
  job: Job;
  onOpen: (job: Job) => void;
  compact?: boolean;
}) {
  const mails =
    typeof job.mail_count === 'number' && job.mail_count > 0
      ? job.mail_count
      : 0;
  const label = `세부 내역${mails ? ` · 메일 ${mails}건` : ''}: ${job.company} ${job.title}`;
  // In list rows this opens a detail view, so it reads as a link rather than an action.
  if (compact)
    return (
      <button
        type="button"
        className="history-button detail-link"
        onClick={() => onOpen(job)}
        aria-label={label}
      >
        <span className="history-label">세부 내역</span>
        {mails > 0 && <span className="history-count">메일 {mails}</span>}
        <ChevronRight size={15} aria-hidden="true" />
      </button>
    );
  return (
    <Button
      variant="ghost"
      className="history-button"
      onClick={() => onOpen(job)}
      aria-label={label}
      title="세부 내역과 메모"
    >
      <History size={15} />
      <span className="history-label">세부 내역</span>
      {mails > 0 && <span className="history-count">메일 {mails}</span>}
    </Button>
  );
}
export function JobHistory({
  job,
  api,
  onClose,
  onChanged,
}: {
  job: Job | null;
  api: Api;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [events, setEvents] = useState<Event[]>([]),
    [current, setCurrent] = useState<Job | null>(job);
  const [note, setNote] = useState(''),
    [loading, setLoading] = useState(false),
    [saving, setSaving] = useState(false),
    [error, setError] = useState(''),
    [saved, setSaved] = useState(false);
  useEffect(() => {
    if (!job) return;
    let active = true;
    queueMicrotask(() => {
      if (active) {
        setCurrent(job);
        setLoading(true);
        setError('');
        setEvents([]);
        setSaved(false);
        setNote(job.note || job.notes || '');
      }
    });
    api(`/api/jobs/${encodeURIComponent(job.id)}/history`)
      .then((result) => {
        if (!active) return;
        const next = (result.job || job) as Job;
        setCurrent(next);
        setNote(next.note || next.notes || '');
        setEvents((result.events || []) as Event[]);
      })
      .catch((e) => {
        if (active) setError((e as Error).message);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [job, api]);
  const save = async () => {
    if (!current) return;
    setSaving(true);
    setError('');
    setSaved(false);
    try {
      const result = await api(
        `/api/jobs/${encodeURIComponent(current.id)}/note`,
        'PATCH',
        {
          note,
          expected_updated_at:
            current.decision_revision || current.updated_at || '',
        },
      );
      setCurrent((result.job || current) as Job);
      setSaved(true);
      onChanged();
      try {
        const refreshed = await api(
          `/api/jobs/${encodeURIComponent(current.id)}/history`,
        );
        setCurrent((refreshed.job || result.job || current) as Job);
        setEvents((refreshed.events || []) as Event[]);
      } catch {
        setError(
          '메모는 저장했습니다. 변경 이력은 창을 다시 열면 확인할 수 있습니다.',
        );
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };
  return (
    <Dialog
      open={Boolean(job)}
      onOpenChange={(open) => {
        if (!open && !saving) onClose();
      }}
    >
      <DialogContent className="history-dialog">
        <DialogHeader>
          <DialogTitle>{job?.company}</DialogTitle>
          <DialogDescription>{job?.title}</DialogDescription>
        </DialogHeader>
        <div className="history-provenance">
          <span>
            공고 출처 <b>{job?.site_name || job?.site || '확인 필요'}</b>
          </span>
          {job?.url && /^https?:\/\//i.test(job.url) && (
            <a href={job.url} target="_blank" rel="noopener noreferrer">
              원문 공고 <ArrowUpRight size={14} />
            </a>
          )}
        </div>
        {loading ? (
          <p className="muted">
            <RefreshCw size={16} className="spin" /> 기록을 불러오고 있습니다.
          </p>
        ) : (
          <>
            <label htmlFor="job-note">내 메모</label>
            <Textarea
              id="job-note"
              value={note}
              onChange={(e) => {
                setNote(e.target.value);
                setSaved(false);
              }}
              placeholder="검토 이유나 다음에 확인할 내용을 남겨 보세요."
              maxLength={1000}
              disabled={saving}
            />
            <div className="history-note-actions">
              {saved && (
                <output>
                  <Check size={14} /> 메모를 저장했습니다
                </output>
              )}
              <Button
                variant="outline"
                onClick={() => void save()}
                disabled={saving || loading}
              >
                {saving ? '저장 중…' : '메모 저장'}
              </Button>
            </div>
            <h3>상태 변경과 메일 근거</h3>
            {events.length ? (
              <ol className="history-timeline">
                {events.map((event, i) => {
                  const link = event.url || event.mail_url;
                  const timestamp =
                    event.at || event.occurred_at || event.timestamp;
                  return (
                    <li key={event.id || `${timestamp}-${i}`}>
                      <span className="timeline-dot">
                        <Clock3 size={13} />
                      </span>
                      <div>
                        <time>{formatDate(timestamp)}</time>
                        <strong>
                          {event.needs_review
                            ? '메일 연결·판단 확인 필요'
                            : event.label ||
                              labels[event.kind || ''] ||
                              (event.source === 'mail'
                                ? '메일 확인'
                                : '상태 변경')}
                        </strong>
                        {event.subject && (
                          <span className="history-subject">
                            {event.subject}
                          </span>
                        )}
                        {event.note && <p>{event.note}</p>}
                        {event.evidence && (
                          <blockquote>{event.evidence}</blockquote>
                        )}
                        {event.identity_evidence && (
                          <p className="identity-evidence">
                            공고 연결 근거 · {event.identity_evidence}
                          </p>
                        )}
                        {link && /^https?:\/\//i.test(link) && (
                          <a
                            href={link}
                            target="_blank"
                            rel="noopener noreferrer"
                          >
                            원본 메일 보기 <ArrowUpRight size={13} />
                          </a>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ol>
            ) : (
              <p className="history-empty">
                저장된 변경 이력이 없습니다. 앞으로 분류한 내용과 확인된 메일이
                여기에 쌓입니다.
              </p>
            )}
          </>
        )}
        {error && (
          <p role="alert" className="error-text">
            {error}
          </p>
        )}
      </DialogContent>
    </Dialog>
  );
}
