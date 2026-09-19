'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import {
  ArrowUpRight,
  ArrowRight,
  Bookmark,
  BriefcaseBusiness,
  Check,
  ChevronDown,
  Clock3,
  Inbox,
  LayoutGrid,
  LockKeyhole,
  LogOut,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
  X,
  Undo2,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
  DialogHeader,
  DialogFooter,
} from '@/components/ui/dialog';
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible';
import {
  NativeSelect,
  NativeSelectOption,
} from '@/components/ui/native-select';

import type { Job, Archive, Dashboard, MoveStatus } from './dashboard-types';
import { isMovable, canMove, moveNames } from './dashboard-types';
import { MovementProvider, MoveHandle, DraggableCard } from './job-movement';
import { MailSyncBar, JobHistory, HistoryButton } from './mail-panel';

type Session = { authenticated: boolean; csrf_token?: string };
type Decision = 'pending' | 'excluded' | 'applied' | 'new';
const siteStyle: Record<string, { mark: string; color: string }> = {
  wanted: { mark: 'W', color: '#146fff' },
  saramin: { mark: 'S', color: '#3568ff' },
  remember: { mark: 'R', color: '#152d2b' },
  linkedin: { mark: 'in', color: '#0a66c2' },
  jobkorea: { mark: 'J', color: '#0055ed' },
};
const stateName: Record<string, string> = {
  Evaluated: '검토 대기',
  Applied: '지원 완료',
  Responded: '회신 받음',
  Interview: '면접 진행',
  Offer: '오퍼 수신',
  Hired: '입사 확정',
  Rejected: '불합격',
  Discarded: '종료·철회',
  SKIP: '지원제외',
  pending: '지원보류',
  excluded: '지원제외',
  applied: '지원 완료',
  new: '추천으로 복원',
};
const decisionName: Record<Decision, string> = {
  pending: '지원보류',
  excluded: '지원제외',
  applied: '지원 기록',
  new: '추천으로 복원',
};
const date = (value?: string, withTime = false) => {
  if (!value) return '기록 없음';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return '날짜 미기록';
  return new Intl.DateTimeFormat('ko-KR', {
    month: '2-digit',
    day: '2-digit',
    ...(withTime ? { hour: '2-digit', minute: '2-digit', hour12: false } : {}),
    timeZone: 'Asia/Seoul',
  }).format(d);
};
const safeUrl = (url: string) => (/^https?:\/\//i.test(url) ? url : undefined);
const fitLabels: Record<string, string> = {
  verified_profile_fact_terms: '경력 근거와 관련된 표현',
  preferred_title: '선호 직급과 일치',
  secondary_title: '관련 직무와 일치',
  finance_leadership: '재무 리더십 관련',
  seniority_review: '직급 확인 필요',
  unsupported_seniority: '요구 경력 확인 필요',
  role_keyword_match: '관심 직무와 일치',
};
const fitReason = (reason: string) =>
  reason
    .split(/\s*·\s*/)
    .map((r) => fitLabels[r] || (/^[a-z_]+$/.test(r) ? '직무 관련성 검토' : r))
    .filter((v, i, a) => a.indexOf(v) === i)
    .join(' · ');

export default function Page() {
  const [session, setSession] = useState<Session | null>(null),
    [data, setData] = useState<Dashboard | null>(null),
    [tab, setTab] = useState('discover'),
    [query, setQuery] = useState(''),
    [error, setError] = useState(''),
    [busy, setBusy] = useState(false),
    [loading, setLoading] = useState(false),
    [password, setPassword] = useState(''),
    [notice, setNotice] = useState(''),
    [modal, setModal] = useState<{
      job: Job;
      status: string;
      application?: boolean;
    } | null>(null),
    [note, setNote] = useState(''),
    [appFilter, setAppFilter] = useState('active');
  const [dragging, setDragging] = useState(false),
    [historyJob, setHistoryJob] = useState<Job | null>(null),
    [undo, setUndo] = useState<{ token: string; expires: number } | null>(null);
  const movementActive = useRef(false),
    mutationActive = useRef(false),
    deferredRefresh = useRef(false);
  const latest = useRef({ data, session });
  useEffect(() => {
    latest.current = { data, session };
  }, [data, session]);
  const refreshEpoch = useRef(0);
  const api = useCallback(
    async (path: string, method = 'GET', body?: unknown) => {
      const r = await fetch(path, {
        method,
        credentials: 'same-origin',
        headers: {
          ...(body ? { 'Content-Type': 'application/json' } : {}),
          ...(latest.current.session?.csrf_token
            ? { 'X-CSRF-Token': latest.current.session.csrf_token }
            : {}),
        },
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
      const result = (await r
        .json()
        .catch(() => ({ error: '서버 응답을 읽을 수 없습니다.' }))) as Record<
        string,
        unknown
      >;
      if (!r.ok) {
        if (r.status === 401) {
          setSession({ authenticated: false });
          setData(null);
        }
        throw Object.assign(
          new Error(
            typeof result.error === 'string'
              ? result.error
              : typeof result.message === 'string'
                ? result.message
                : '요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.',
          ),
          { status: r.status, partial_commit: result.partial_commit === true },
        );
      }
      return result;
    },
    [],
  );
  const refresh = useCallback(
    async (force = false) => {
      if (!force && (movementActive.current || mutationActive.current)) {
        deferredRefresh.current = true;
        return;
      }
      const epoch = ++refreshEpoch.current;
      setLoading(true);
      try {
        const d = await api('/api/dashboard');
        if (
          epoch === refreshEpoch.current &&
          (force || !movementActive.current)
        ) {
          setData(d as unknown as Dashboard);
          setError('');
        }
        return d;
      } catch (e) {
        if (epoch === refreshEpoch.current) setError((e as Error).message);
        throw e;
      } finally {
        if (epoch === refreshEpoch.current) setLoading(false);
      }
    },
    [api],
  );
  const onMailChanged = useCallback(() => {
    void refresh().catch(() => {});
  }, [refresh]);
  const onMovementActive = useCallback(
    (value: boolean) => {
      setDragging(value);
      setLoading(false);
      movementActive.current = value;
      refreshEpoch.current += 1;
      if (!value && deferredRefresh.current && !mutationActive.current) {
        deferredRefresh.current = false;
        void refresh().catch(() => {});
      }
    },
    [refresh],
  );
  const moveJob = useCallback(
    async (job: Job, status: MoveStatus, noteText?: string) => {
      if (mutationActive.current) return;
      if (!isMovable(job))
        throw new Error('지원 이력은 드래그로 이동할 수 없습니다.');
      if (job.movement_status === status) return;
      mutationActive.current = true;
      refreshEpoch.current += 1;
      setLoading(false);
      setBusy(true);
      setError('');
      setUndo(null);
      try {
        const r = await api('/api/moves', 'POST', {
          id: job.id,
          status,
          expected_updated_at: job.decision_revision || job.updated_at || '',
          ...(noteText !== undefined ? { note: noteText } : {}),
        });
        refreshEpoch.current += 1;
        if (r.dashboard) setData(r.dashboard as Dashboard);
        setModal(null);
        setNote('');
        setNotice(
          status === 'new'
            ? '추천 후보로 복원했습니다. 모집 상태와 순위에 따라 표시됩니다.'
            : `${moveNames[status]}로 이동했습니다.`,
        );
        if (typeof r.undo_token === 'string') {
          const expires =
            typeof r.undo_expires_at === 'string'
              ? Date.parse(r.undo_expires_at)
              : Date.now() + 10000;
          setUndo({ token: r.undo_token, expires });
        }
        return r;
      } catch (e) {
        const message = (e as Error).message;
        if (
          (e as { status?: number }).status === 409 ||
          (e as { partial_commit?: boolean }).partial_commit
        ) {
          await refresh(true).catch(() => {});
        }
        setError(message);
        throw e;
      } finally {
        mutationActive.current = false;
        setBusy(false);
      }
    },
    [api, refresh],
  );
  const undoMove = useCallback(async () => {
    if (!undo || mutationActive.current) return;
    mutationActive.current = true;
    refreshEpoch.current += 1;
    setLoading(false);
    setBusy(true);
    setError('');
    try {
      const r = await api('/api/moves/undo', 'POST', { token: undo.token });
      if (r.dashboard) setData(r.dashboard as Dashboard);
      setUndo(null);
      setNotice('직전 이동을 되돌렸습니다.');
    } catch (e) {
      const message = (e as Error).message;
      setUndo(null);
      await refresh(true).catch(() => {});
      setError(message);
    } finally {
      mutationActive.current = false;
      setBusy(false);
    }
  }, [api, refresh, undo]);
  useEffect(() => {
    if (!undo) return;
    const timer = setTimeout(
      () => setUndo(null),
      Math.max(0, undo.expires - Date.now()),
    );
    return () => clearTimeout(timer);
  }, [undo]);
  useEffect(() => {
    api('/api/session')
      .then((s) => setSession(s as unknown as Session))
      .catch((e) => {
        setError(e.message);
        setSession({ authenticated: false });
      });
  }, [api]);
  useEffect(() => {
    if (!session?.authenticated) return;
    queueMicrotask(() => void refresh().catch(() => {}));
    const timer = setInterval(() => void refresh().catch(() => {}), 60000);
    const onFocus = () => void refresh().catch(() => {});
    window.addEventListener('focus', onFocus);
    return () => {
      clearInterval(timer);
      window.removeEventListener('focus', onFocus);
    };
  }, [session?.authenticated, refresh]);
  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(
      () => setNotice(''),
      undo ? Math.max(0, undo.expires - Date.now()) + 1500 : 5000,
    );
    return () => clearTimeout(timer);
  }, [notice, undo]);
  const changeDecision = useCallback(
    async (job: Job, status: string, noteText = '', application = false) => {
      if (!application && ['new', 'pending', 'excluded'].includes(status))
        return moveJob(job, status as MoveStatus, noteText || undefined);
      if (mutationActive.current) return;
      mutationActive.current = true;
      refreshEpoch.current += 1;
      setLoading(false);
      setBusy(true);
      setError('');
      try {
        const body = {
          status,
          note: noteText,
          expected_updated_at: job.updated_at || '',
        };
        const r = await api(
          application
            ? `/api/applications/${job.tracker_id}`
            : '/api/decisions',
          application ? 'PATCH' : 'POST',
          application ? body : { ...body, id: job.id },
        );
        refreshEpoch.current += 1;
        if (r.dashboard) setData(r.dashboard as Dashboard);
        else await refresh(true);
        setModal(null);
        setNote('');
        setNotice(
          status === 'new'
            ? '추천 후보로 복원했습니다. 모집 상태와 기존 지원 이력에 따라 표시됩니다.'
            : `${stateName[status] || status} 상태로 기록했습니다.`,
        );
        return r;
      } catch (e) {
        const message = (e as Error).message;
        if (
          (e as { status?: number }).status === 409 ||
          (e as { partial_commit?: boolean }).partial_commit
        )
          await refresh(true).catch(() => {});
        setError(message);
        throw e;
      } finally {
        mutationActive.current = false;
        setBusy(false);
      }
    },
    [api, refresh, moveJob],
  );
  useEffect(() => {
    const context = (
      document as unknown as {
        modelContext?: {
          registerTool: (
            tool: unknown,
            options: { signal: AbortSignal },
          ) => void | Promise<void>;
        };
      }
    ).modelContext;
    if (!context?.registerTool || !session?.authenticated) return;
    const life = new AbortController();
    const register = (tool: unknown) => {
      try {
        void Promise.resolve(
          context.registerTool(tool, { signal: life.signal }),
        ).catch(() => {});
      } catch {}
    };
    register({
      name: 'read_career_dashboard',
      description:
        'Read the currently displayed job recommendations and decision counts.',
      inputSchema: {
        type: 'object',
        properties: {},
        additionalProperties: false,
      },
      annotations: { readOnlyHint: true, untrustedContentHint: true },
      execute: () => latest.current.data,
    });
    register({
      name: 'record_job_decision',
      description:
        'Record the user decision for a displayed job as pending, excluded, applied, or restored. Applied records an already submitted application; it does not submit an application.',
      inputSchema: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          status: {
            type: 'string',
            enum: ['pending', 'excluded', 'applied', 'new'],
          },
          note: { type: 'string' },
        },
        required: ['id', 'status'],
        additionalProperties: false,
      },
      annotations: { readOnlyHint: false, untrustedContentHint: true },
      execute: async (input: unknown) => {
        const v = input as { id?: string; status?: string; note?: string };
        if (
          !v ||
          typeof v.id !== 'string' ||
          !['pending', 'excluded', 'applied', 'new'].includes(v.status || '') ||
          (v.note !== undefined && typeof v.note !== 'string')
        )
          throw new Error('Invalid decision input');
        const d = latest.current.data;
        const j = [
          ...(d?.sites.flatMap((s) => s.jobs) || []),
          ...(d?.pending.items || []),
          ...(d?.excluded.items || []),
        ].find((x) => x.id === v.id);
        if (!j) throw new Error('Job is not in the current dashboard');
        await changeDecision(j, v.status!, v.note || '');
        return { ok: true, id: v.id, status: v.status };
      },
    });
    return () => life.abort();
  }, [session?.authenticated, changeDecision]);
  const openDecision = (job: Job, status: string, application = false) => {
    setModal({ job, status, application });
    setNote('');
    setError('');
  };
  const login = async (e: { preventDefault(): void }) => {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      const s = await api('/api/login', 'POST', { password });
      setPassword('');
      setSession(s as unknown as Session);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const logout = async () => {
    try {
      await api('/api/logout', 'POST', {});
      setSession({ authenticated: false });
      setData(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const selectView = (value: string) => {
    setTab(value);
    setQuery('');
  };
  const matches = (j: Job) =>
    !query ||
    `${j.company} ${j.title}`
      .toLocaleLowerCase()
      .includes(query.toLocaleLowerCase());
  const active = (data?.applications || []).filter((j) =>
    ['Applied', 'Responded', 'Interview', 'Offer'].includes(
      j.canonical_status || '',
    ),
  );
  return (
    <MovementProvider
      busy={busy}
      onMove={moveJob}
      onActiveChange={onMovementActive}
    >
      <div className="desk">
        <header className="topbar">
          <Link href="/" className="brand" aria-label="Career Desk 홈">
            <span className="brand-mark">
              <ArrowUpRight size={24} />
            </span>
            career<span className="brand-light">desk</span>
          </Link>
          <div className="private">
            <ShieldCheck size={16} /> 나만의 커리어 공간
          </div>
          {session?.authenticated ? (
            <button
              className="logout"
              onClick={() => void logout()}
              aria-label="로그아웃"
              title="로그아웃"
            >
              <LogOut size={17} />
            </button>
          ) : (
            <span className="avatar">
              <LockKeyhole size={16} />
            </span>
          )}
        </header>
        {!session?.authenticated ? (
          <main className="login-main">
            <div className="login-card">
              <span className="login-symbol">
                <LockKeyhole size={26} />
              </span>
              <div className="eyebrow">YOUR PRIVATE WORKSPACE</div>
              <h1>
                나의 다음 커리어,
                <br />
                여기서 이어갑니다.
              </h1>
              <p>추천 공고와 지원 내역을 확인하려면 로그인하세요.</p>
              <form onSubmit={login}>
                <label htmlFor="password">접속 비밀번호</label>
                <Input
                  id="password"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete="current-password"
                  placeholder="비밀번호를 입력하세요"
                  required
                  disabled={busy}
                />
                <Button
                  type="submit"
                  className="login-button"
                  disabled={busy || !session}
                >
                  {busy ? '확인 중…' : '대시보드 열기'}
                  <ArrowRight size={17} />
                </Button>
              </form>
              {error && (
                <p role="alert" className="error-text">
                  {error}
                </p>
              )}
              <div className="login-footer">
                <ShieldCheck size={15} /> 지원 내역은 로그인 후에만 볼 수
                있습니다.
              </div>
              <nav className="login-info-links" aria-label="서비스 안내">
                {/* oxlint-disable-next-line nextjs/no-html-link-for-pages -- This link opens a static public HTML document. */}
                <a href="/about.html">서비스 안내</a>
                <span aria-hidden="true">·</span>
                {/* oxlint-disable-next-line nextjs/no-html-link-for-pages -- This link opens a static public HTML document. */}
                <a href="/privacy.html">개인정보처리방침</a>
                <span aria-hidden="true">·</span>
                {/* oxlint-disable-next-line nextjs/no-html-link-for-pages -- This link opens a static public HTML document. */}
                <a href="/terms.html">이용 안내</a>
              </nav>
            </div>
          </main>
        ) : (
          <main className="main">
            <div className="heading">
              <div>
                <div className="eyebrow">YOUR NEXT CHAPTER</div>
                <h1>
                  다음 기회를 만나보세요<span>.</span>
                </h1>
                <p>검토를 마치면, 다음 공고가 그 자리를 채웁니다.</p>
              </div>
              <Button
                className="refresh"
                variant="outline"
                disabled={loading || busy}
                onClick={() => void refresh().catch(() => {})}
              >
                <RefreshCw size={16} className={loading ? 'spin' : ''} />
                {loading ? '불러오는 중' : '새로고침'}
              </Button>
            </div>
            {error && (
              <div role="alert" className="alert error-alert">
                {error}
                <button
                  onClick={() => setError('')}
                  aria-label="오류 안내 닫기"
                >
                  <X size={17} />
                </button>
              </div>
            )}

            <MailSyncBar api={api} onChanged={onMailChanged} />
            {(data?.warnings || []).map((w, i) => (
              <div key={i} className="alert">
                {w}
              </div>
            ))}
            <div className="summary">
              <button
                className="metric"
                aria-pressed={tab === 'discover'}
                onClick={() => selectView('discover')}
              >
                <span>
                  <Sparkles size={17} />
                  지금 볼 추천
                </span>
                <strong>
                  {data
                    ? data.sites.reduce((n, s) => n + s.jobs.length, 0)
                    : '—'}
                  <small>사이트별 최대 5개</small>
                </strong>
              </button>
              <button
                className="metric"
                aria-pressed={tab === 'applications'}
                onClick={() => selectView('applications')}
              >
                <span>
                  <BriefcaseBusiness size={17} />
                  진행 중인 지원
                </span>
                <strong>
                  {data ? active.length : '—'}
                  <small>전체 {data?.applications.length ?? '—'}건</small>
                </strong>
              </button>
              <button
                className="metric"
                aria-pressed={tab === 'pending'}
                onClick={() => selectView('pending')}
              >
                <span>
                  <Bookmark size={17} />
                  지원보류
                </span>
                <strong>
                  {data?.pending.total ?? '—'}
                  <small>나중에 검토</small>
                </strong>
              </button>
              <button
                className="metric"
                aria-pressed={tab === 'excluded'}
                onClick={() => selectView('excluded')}
              >
                <span>
                  <Inbox size={17} />
                  지원제외
                </span>
                <strong>
                  {data?.excluded.total ?? '—'}
                  <small>검토 완료</small>
                </strong>
              </button>
            </div>
            <Tabs value={tab} onValueChange={(v) => selectView(String(v))}>
              <div className="toolbar">
                <TabsList className="main-tabs" variant="line">
                  <TabsTrigger value="discover">
                    <LayoutGrid />
                    추천 공고
                  </TabsTrigger>
                  <TabsTrigger value="applications">
                    <BriefcaseBusiness />
                    지원 현황
                  </TabsTrigger>
                  <TabsTrigger value="pending">
                    <Bookmark />
                    지원보류
                  </TabsTrigger>
                  <TabsTrigger value="excluded">
                    <Inbox />
                    지원제외
                  </TabsTrigger>
                </TabsList>
                <div className="search">
                  <Search size={18} />
                  <Input
                    placeholder="현재 목록에서 회사·직무 검색"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    aria-label="현재 목록에서 회사 또는 직무 검색"
                  />
                </div>
              </div>
              <TabsContent value="discover">
                <div className="section-heading">
                  <h2>
                    사이트별 추천 <span>TOP 5</span>
                  </h2>
                  <span className="muted">
                    {data?.source_run_at
                      ? `${date(data.source_run_at, true)} 수집 결과`
                      : '수집 결과를 연결하고 있습니다'}
                  </span>
                </div>
                <div className="site-grid">
                  {(data?.sites || [])
                    .filter((s) => siteStyle[s.id] || s.total > 0)
                    .map((s) => {
                      const style = siteStyle[s.id] || {
                        mark: s.name.charAt(0),
                        color: '#4b647f',
                      };
                      const jobs = s.jobs.filter(matches);
                      return (
                        <section className="site-panel" key={s.id}>
                          <header className="site-header">
                            <span
                              className="site-mark"
                              style={{ background: style.color }}
                            >
                              {style.mark}
                            </span>
                            <h3>{s.name}</h3>
                            <span className="site-count">
                              {s.jobs.length}
                              <b> / 5</b>
                            </span>
                          </header>
                          {jobs.length ? (
                            jobs.map((j, i) => (
                              <DraggableCard job={j} className="job" key={j.id}>
                                <div className="job-top">
                                  <span className="company">
                                    {j.company || '기업명 확인 필요'}
                                  </span>
                                  <span className="job-tools">
                                    <span className="rank">
                                      {String(i + 1).padStart(2, '0')}
                                    </span>
                                    <MoveHandle job={j} />
                                  </span>
                                </div>
                                <h4>{j.title}</h4>
                                <div className="job-meta">
                                  {j.location && <span>{j.location}</span>}
                                  {j.first_seen && (
                                    <span>첫 발견 {date(j.first_seen)}</span>
                                  )}
                                </div>
                                {j.reason && (
                                  <div className="fit">
                                    <Sparkles size={14} />
                                    <span>{fitReason(j.reason)}</span>
                                  </div>
                                )}
                                <div className="job-actions">
                                  <Button
                                    variant="outline"
                                    disabled={busy}
                                    onClick={() => openDecision(j, 'applied')}
                                  >
                                    지원 기록
                                  </Button>
                                  <Button
                                    variant="ghost"
                                    disabled={busy}
                                    onClick={() =>
                                      void changeDecision(j, 'pending').catch(
                                        () => {},
                                      )
                                    }
                                  >
                                    <Clock3 size={14} />
                                    지원보류
                                  </Button>
                                  <Button
                                    variant="ghost"
                                    disabled={busy}
                                    onClick={() =>
                                      void moveJob(j, 'excluded').catch(
                                        () => {},
                                      )
                                    }
                                  >
                                    <X size={14} />
                                    제외
                                  </Button>
                                  {safeUrl(j.url) && (
                                    <a
                                      href={j.url}
                                      target="_blank"
                                      rel="noopener noreferrer"
                                      aria-label={`${j.company} ${j.title} 공고 열기`}
                                      title="원문 공고 열기"
                                    >
                                      <ArrowUpRight size={18} />
                                    </a>
                                  )}
                                </div>
                                <HistoryButton job={j} onOpen={setHistoryJob} />
                              </DraggableCard>
                            ))
                          ) : (
                            <Empty
                              icon={<Inbox size={27} />}
                              title={
                                query
                                  ? '검색과 일치하는 추천이 없습니다'
                                  : '현재 추천할 공고가 없습니다'
                              }
                              detail={
                                query
                                  ? '검색어를 바꿔 확인해 보세요.'
                                  : '다음 수집에서 확인된 새 공고가 채워집니다.'
                              }
                            />
                          )}
                          <div className="site-bottom">
                            {s.total > s.jobs.length
                              ? `다음 후보 ${s.total - s.jobs.length}개 대기 중`
                              : s.jobs.length
                                ? '확인된 후보를 모두 표시하고 있습니다'
                                : '모집 여부가 확인된 공고만 표시합니다'}
                          </div>
                        </section>
                      );
                    })}
                </div>
                {!data && (
                  <Empty
                    icon={<RefreshCw className="spin" />}
                    title="최근 수집 결과를 불러오고 있습니다"
                    detail="잠시만 기다려 주세요."
                  />
                )}
                <div className="recommendation-note">
                  <Check size={15} />
                  지원·지원보류·지원제외한 공고는 추천에서 빠집니다. 다음 후보가
                  있으면 바로 채워집니다.
                </div>
              </TabsContent>
              <TabsContent value="applications">
                <div className="section-heading">
                  <h2>
                    지원 여정 <span>{data?.applications.length || 0}</span>
                  </h2>
                  <div className="filter-buttons">
                    <button
                      className={appFilter === 'active' ? 'selected' : ''}
                      onClick={() => setAppFilter('active')}
                    >
                      진행 중 {active.length}
                    </button>
                    <button
                      className={appFilter === 'all' ? 'selected' : ''}
                      onClick={() => setAppFilter('all')}
                    >
                      전체 내역
                    </button>
                  </div>
                </div>
                <div className="application-list">
                  {(appFilter === 'active' ? active : data?.applications || [])
                    .filter(matches)
                    .map((j) => (
                      <article className="application" key={j.id}>
                        <div
                          className={`state-dot ${j.canonical_status === 'Rejected' ? 'closed' : ''}`}
                        />
                        <div className="application-info">
                          <div className="company">{j.company}</div>
                          <h3>{j.title}</h3>
                          <div className="job-meta">
                            <span>
                              {typeof j.site_name === 'string'
                                ? j.site_name
                                : j.site || '지원 내역'}
                            </span>
                            {j.date && <span>기록 {date(j.date)}</span>}
                          </div>
                          {(j.note || j.notes) && (
                            <p className="record-note">{j.note || j.notes}</p>
                          )}
                        </div>
                        <div className="application-end">
                          <HistoryButton job={j} onOpen={setHistoryJob} />
                          <span
                            className={`state-pill ${j.canonical_status === 'Rejected' ? 'closed' : ''}`}
                          >
                            {j.application_stage_label ||
                              stateName[j.canonical_status || ''] ||
                              j.canonical_status}
                          </span>
                          <Button
                            variant="outline"
                            disabled={busy}
                            onClick={() =>
                              openDecision(
                                j,
                                j.canonical_status || 'Applied',
                                true,
                              )
                            }
                          >
                            상태 변경
                          </Button>
                          {safeUrl(j.url) && (
                            <a
                              href={j.url}
                              target="_blank"
                              rel="noopener noreferrer"
                              aria-label="원문 공고 열기"
                            >
                              <ArrowUpRight size={18} />
                            </a>
                          )}
                        </div>
                      </article>
                    ))}
                  {!(
                    appFilter === 'active' ? active : data?.applications || []
                  ).filter(matches).length && (
                    <Empty
                      icon={<BriefcaseBusiness size={28} />}
                      title="표시할 지원 내역이 없습니다"
                      detail={
                        appFilter === 'active'
                          ? '전체 내역에서 지난 지원 결과를 확인할 수 있습니다.'
                          : '추천 공고에서 ‘지원 기록’을 눌러 기록하세요.'
                      }
                    />
                  )}
                </div>
                <p className="list-caption">
                  지원 기록과 상태 변경은 현황 관리용입니다. 채용 사이트에 실제
                  지원서를 제출하지 않습니다.
                </p>
              </TabsContent>
              {(['pending', 'excluded'] as const).map((status) => (
                <TabsContent key={status} value={status}>
                  {tab === status && (
                    <>
                      <div className="section-heading">
                        <h2>
                          {status === 'pending'
                            ? '지원보류한 공고'
                            : '지원제외한 공고'}
                        </h2>
                        <span className="muted">사이트별 확인 · 최근 5개</span>
                      </div>
                      {data && (
                        <ArchivePanel
                          key={status}
                          status={status}
                          archive={data[status]}
                          api={api}
                          busy={busy}
                          onDecision={openDecision}
                          onMove={moveJob}
                          onHistory={setHistoryJob}
                          query={query}
                          revision={data.updated_at}
                        />
                      )}
                    </>
                  )}
                </TabsContent>
              ))}
            </Tabs>
            <footer>
              CAREER DESK{' '}
              <span>
                {data
                  ? `${date(data.served_at || data.generated_at, true)} 화면 갱신`
                  : '나의 판단이 쌓이는 커리어 공간'}
              </span>
            </footer>
          </main>
        )}
        {notice && !dragging && (
          <output className="notice" aria-live="polite">
            <Check size={18} />
            <span>{notice}</span>
            {undo && (
              <button
                className="undo-action"
                disabled={busy}
                onClick={() => void undoMove()}
              >
                <Undo2 size={15} />
                실행 취소
              </button>
            )}
          </output>
        )}
        <JobHistory
          job={historyJob}
          api={api}
          onClose={() => setHistoryJob(null)}
          onChanged={onMailChanged}
        />
        <Dialog
          open={Boolean(modal)}
          onOpenChange={(open) => {
            if (!open && !busy) setModal(null);
          }}
        >
          <DialogContent className="decision-dialog">
            <DialogHeader>
              <DialogTitle>
                {modal?.application
                  ? '지원 상태 변경'
                  : decisionName[modal?.status as Decision] || '상태 변경'}
              </DialogTitle>
              <DialogDescription>
                {modal?.job.company} · {modal?.job.title}
              </DialogDescription>
            </DialogHeader>
            {modal?.application ? (
              <>
                <label htmlFor="application-state">지원 상태</label>
                <NativeSelect
                  id="application-state"
                  value={modal.status}
                  onChange={(e) =>
                    setModal({ ...modal, status: e.target.value })
                  }
                >
                  {[
                    'Applied',
                    'Responded',
                    'Interview',
                    'Offer',
                    'Hired',
                    'Rejected',
                    'Discarded',
                  ].map((s) => (
                    <NativeSelectOption key={s} value={s}>
                      {stateName[s]}
                    </NativeSelectOption>
                  ))}
                </NativeSelect>
              </>
            ) : (
              <p className="dialog-hint">
                {modal?.status === 'applied'
                  ? '이미 제출한 지원 내역을 기록합니다. 이 버튼은 채용 사이트에 지원서를 제출하지 않습니다.'
                  : modal?.status === 'new'
                    ? '모집 여부와 지원 이력을 다시 반영해 추천 후보로 돌립니다. 기존 지원 기록은 유지됩니다.'
                    : modal?.status === 'pending'
                      ? '추천에서 잠시 내리고, 지원보류 목록에 보관합니다. 직접 복원하기 전까지 다시 추천하지 않습니다.'
                      : '추천 목록에서 제외합니다. 나중에 지원제외 화면에서 다시 복원할 수 있습니다.'}
              </p>
            )}
            <label htmlFor="decision-note">
              메모 <span className="muted">선택</span>
            </label>
            <Textarea
              id="decision-note"
              placeholder="검토 이유나 다음에 확인할 내용을 남겨 보세요."
              value={note}
              maxLength={1000}
              onChange={(e) => setNote(e.target.value)}
              disabled={busy}
            />
            {error && (
              <div role="alert" className="error-text">
                {error}
              </div>
            )}
            <DialogFooter>
              <Button
                variant="outline"
                disabled={busy}
                onClick={() => setModal(null)}
              >
                취소
              </Button>
              <Button
                disabled={busy}
                onClick={() =>
                  modal &&
                  void changeDecision(
                    modal.job,
                    modal.status,
                    note,
                    modal.application,
                  ).catch(() => {})
                }
              >
                {busy
                  ? '저장 중…'
                  : modal?.application
                    ? '상태 저장'
                    : modal?.status === 'applied'
                      ? '지원 내역 기록'
                      : modal?.status === 'new'
                        ? '복원하기'
                        : '저장하기'}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>
    </MovementProvider>
  );
}

function Empty({
  icon,
  title,
  detail,
}: {
  icon: React.ReactNode;
  title: string;
  detail: string;
}) {
  return (
    <div className="empty">
      {icon}
      <p>{title}</p>
      <span>{detail}</span>
    </div>
  );
}
function ArchivePanel({
  status,
  archive,
  api,
  busy,
  onDecision,
  onMove,
  onHistory,
  query,
  revision,
}: {
  status: 'pending' | 'excluded';
  archive: Archive;
  api: (path: string) => Promise<Record<string, unknown>>;
  busy: boolean;
  onDecision: (job: Job, status: string) => void;
  onMove: (job: Job, status: MoveStatus) => Promise<unknown>;
  onHistory: (job: Job) => void;
  query: string;
  revision: string;
}) {
  const [site, setSite] = useState('all'),
    [first, setFirst] = useState(archive.items),
    [total, setTotal] = useState(archive.total),
    [expanded, setExpanded] = useState(false),
    [more, setMore] = useState<Job[]>([]),
    [loading, setLoading] = useState(false),
    [error, setError] = useState('');
  const requestVersion = useRef(0),
    visibleCount = useRef(5);
  const siteChoices = archive.sources || [];
  useEffect(() => {
    const version = ++requestVersion.current;
    let active = true;
    const update = async () => {
      if (!active) return;
      setError('');
      if (site === 'all' && visibleCount.current === 5) {
        setFirst(archive.items);
        setTotal(archive.total);
        setMore([]);
        setLoading(false);
        return;
      }
      setLoading(true);
      try {
        const items: Job[] = [];
        let count = 0;
        const wanted = visibleCount.current;
        do {
          const limit = Math.min(100, wanted - items.length);
          const result = (await api(
            `/api/decisions?status=${status}&site=${encodeURIComponent(site)}&offset=${items.length}&limit=${limit}`,
          )) as unknown as Archive;
          items.push(...result.items);
          count = result.total;
          if (!result.items.length) break;
        } while (items.length < Math.min(wanted, count));
        if (active && version === requestVersion.current) {
          setFirst(items.slice(0, 5));
          setMore(items.slice(5));
          setTotal(count);
        }
      } catch (e) {
        if (active && version === requestVersion.current)
          setError((e as Error).message);
      } finally {
        if (active && version === requestVersion.current) setLoading(false);
      }
    };
    queueMicrotask(() => void update());
    return () => {
      active = false;
    };
  }, [archive, site, status, api, revision]);
  const selectSite = (selected: string) => {
    requestVersion.current += 1;
    visibleCount.current = 5;
    setSite(selected);
    setExpanded(false);
    setMore([]);
    setFirst([]);
    setTotal(
      siteChoices.find((s) => s.id === selected)?.total || archive.total,
    );
    setError('');
  };
  const loadMore = async () => {
    if (loading) return;
    const version = requestVersion.current;
    setLoading(true);
    setError('');
    try {
      const r = (await api(
        `/api/decisions?status=${status}&site=${encodeURIComponent(site)}&offset=${first.length + more.length}&limit=20`,
      )) as unknown as Archive;
      if (version === requestVersion.current) {
        setMore((old) => {
          const result = [
            ...old,
            ...r.items.filter(
              (item) => !old.some((previous) => previous.id === item.id),
            ),
          ];
          visibleCount.current = 5 + result.length;
          return result;
        });
        setTotal(r.total);
      }
    } catch (e) {
      if (version === requestVersion.current) setError((e as Error).message);
    } finally {
      if (version === requestVersion.current) setLoading(false);
    }
  };
  const row = (j: Job) => {
    const name = j.site_name || j.site || '출처 확인 필요';
    const style = siteStyle[j.site];
    const link = safeUrl(j.url);
    return (
      <DraggableCard
        job={j}
        className="archive-row"
        key={j.id}
        data-archive-status={status}
      >
        <div className="archive-info">
          <div className="job-top">
            <span className="company">{j.company}</span>
            <MoveHandle job={j} />
          </div>
          <h4>
            {link ? (
              <a href={link} target="_blank" rel="noopener noreferrer">
                {j.title}
                <ArrowUpRight size={13} />
              </a>
            ) : (
              j.title
            )}
          </h4>
          <div className="archive-source-line">
            <span className="source-label">공고 출처</span>
            <span className="source-badge">
              <i style={{ background: style?.color || '#8896a8' }} />
              {name}
            </span>
            {link && (
              <a href={link} target="_blank" rel="noopener noreferrer">
                공고 원문
                <ArrowUpRight size={14} />
              </a>
            )}
          </div>
          <div className="job-meta">
            <span>{date(j.decided_at || j.date || j.updated_at, true)}</span>
            {j.needs_review && <span className="review-badge">확인 필요</span>}
          </div>
          {j.canonical_status === 'Discarded' && (
            <span className="archive-state">
              {j.canonical_status_label || '종료·철회'} ·{' '}
              {j.ever_applied ? '지원 이력 보존' : '검토 이력 보존'}
            </span>
          )}
          {(j.note || j.notes) && (
            <p className="record-note">{j.note || j.notes}</p>
          )}
        </div>
        <div className="archive-actions">
          <HistoryButton job={j} onOpen={onHistory} />
          {isMovable(j) && (
            <>
              {status === 'pending' && (
                <Button
                  variant="ghost"
                  disabled={busy}
                  onClick={() => onDecision(j, 'applied')}
                >
                  지원 기록
                </Button>
              )}
              <Button
                variant="ghost"
                disabled={busy || !canMove(j, 'new')}
                onClick={() => void onMove(j, 'new').catch(() => {})}
                aria-label={`${j.company} ${j.title} 추천으로 복원`}
              >
                <Undo2 size={14} />
                <span>복원</span>
              </Button>
              <Button
                variant="ghost"
                disabled={busy}
                onClick={() =>
                  void onMove(
                    j,
                    status === 'pending' ? 'excluded' : 'pending',
                  ).catch(() => {})
                }
              >
                {status === 'pending' ? <X size={14} /> : <Clock3 size={14} />}
                <span>{status === 'pending' ? '제외' : '지원보류'}</span>
              </Button>
            </>
          )}
        </div>
      </DraggableCard>
    );
  };
  const filter = (j: Job) =>
    `${j.company} ${j.title}`.toLowerCase().includes(query.toLowerCase());
  return (
    <section
      className="archive-panel archive-single"
      aria-label={`${stateName[status]} 공고 목록`}
    >
      <header className="archive-header">
        <span className={`archive-icon ${status}`}>
          {status === 'pending' ? <Clock3 size={19} /> : <Inbox size={19} />}
        </span>
        <h3>
          {stateName[status]}
          <span>{archive.total}</span>
        </h3>
        <span className="muted">
          {site === 'all'
            ? '전체 출처'
            : siteChoices.find((s) => s.id === site)?.name ||
              '선택한 출처'}{' '}
          · {total}개
        </span>
      </header>
      <div className="archive-filter">
        <label htmlFor={`archive-site-${status}`}>공고 출처</label>
        <NativeSelect
          id={`archive-site-${status}`}
          value={site}
          onChange={(e) => selectSite(e.target.value)}
        >
          <NativeSelectOption value="all">
            전체 출처 ({archive.total})
          </NativeSelectOption>
          {site !== 'all' && !siteChoices.some((s) => s.id === site) && (
            <NativeSelectOption value={site}>
              선택한 출처 (0)
            </NativeSelectOption>
          )}
          {siteChoices.map((s) => (
            <NativeSelectOption key={s.id} value={s.id}>
              {s.name} ({s.total})
            </NativeSelectOption>
          ))}
        </NativeSelect>
        <span>선택한 출처의 최근 5개</span>
      </div>
      {error && (
        <p role="alert" className="error-text archive-error">
          {error}
        </p>
      )}
      {loading && !first.length ? (
        <Empty
          icon={<RefreshCw className="spin" size={23} />}
          title="선택한 출처의 공고를 불러오고 있습니다"
          detail="잠시만 기다려 주세요."
        />
      ) : (
        <>
          {first.filter(filter).map(row)}
          {!first.filter(filter).length && !error && (
            <Empty
              icon={
                status === 'pending' ? (
                  <Bookmark size={23} />
                ) : (
                  <Check size={23} />
                )
              }
              title={
                query
                  ? '일치하는 최근 기록이 없습니다'
                  : status === 'pending'
                    ? '지원보류한 공고가 없습니다'
                    : '지원제외한 공고가 없습니다'
              }
              detail={
                status === 'pending'
                  ? '답장하지 않은 제안과 나중에 검토할 공고가 여기에 모입니다.'
                  : '제외한 공고는 다시 추천하지 않습니다.'
              }
            />
          )}
        </>
      )}
      {total > first.length && first.length > 0 && (
        <Collapsible
          open={expanded}
          onOpenChange={(open) => {
            setExpanded(open);
            if (open && !more.length) void loadMore();
          }}
        >
          <CollapsibleTrigger className="archive-toggle">
            <span>
              {expanded
                ? '지난 기록 접기'
                : `지난 기록 ${total - first.length}개 더보기`}
            </span>
            <ChevronDown size={16} className={expanded ? 'rotated' : ''} />
          </CollapsibleTrigger>
          <CollapsibleContent>
            {more.filter(filter).map(row)}
            {first.length + more.length < total && (
              <Button
                className="more-button"
                variant="ghost"
                disabled={loading}
                onClick={() => void loadMore()}
              >
                {loading ? '불러오는 중…' : '더보기'}
                <ChevronDown size={15} />
              </Button>
            )}
          </CollapsibleContent>
        </Collapsible>
      )}
    </section>
  );
}
