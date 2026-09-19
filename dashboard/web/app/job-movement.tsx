'use client';
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from 'react';
import type {
  ComponentProps,
  ReactNode,
  PointerEvent as ReactPointerEvent,
} from 'react';
import { GripVertical, LayoutGrid, Bookmark, Inbox, X } from 'lucide-react';
import { isMovable, canMove, moveNames } from './dashboard-types';
import type { Job, MoveStatus } from './dashboard-types';

type Selection = {
  job: Job;
  mode: 'pointer' | 'menu';
  x: number;
  y: number;
  target: MoveStatus | null;
};
type Movement = {
  begin: (job: Job, mode: Selection['mode'], x?: number, y?: number) => void;
  point: (x: number, y: number) => void;
  finish: () => void;
  cancel: (ownerId?: string) => void;
  activeId: string | null;
  busy: boolean;
};
const Context = createContext<Movement | null>(null);
const targets: MoveStatus[] = ['new', 'pending', 'excluded'];
const icons = { new: LayoutGrid, pending: Bookmark, excluded: Inbox };

export function MovementProvider({
  children,
  busy,
  onMove,
  onActiveChange,
}: {
  children: ReactNode;
  busy: boolean;
  onMove: (job: Job, target: MoveStatus) => Promise<unknown>;
  onActiveChange: (active: boolean) => void;
}) {
  const [selection, setSelection] = useState<Selection | null>(null);
  const [announcement, setAnnouncement] = useState('');
  const current = useRef<Selection | null>(null);
  const origin = useRef<HTMLElement | null>(null);
  const originY = useRef(0);
  const dock = useRef<HTMLFieldSetElement>(null);
  const restoreFocus = useCallback(
    () =>
      requestAnimationFrame(() => {
        const visible = Array.from(
          document.querySelectorAll<HTMLElement>('.move-handle:not(:disabled)'),
        )
          .filter((el) => {
            const box = el.getBoundingClientRect();
            return box.bottom > 0 && box.top < window.innerHeight;
          })
          .sort(
            (a, b) =>
              Math.abs(a.getBoundingClientRect().top - originY.current) -
              Math.abs(b.getBoundingClientRect().top - originY.current),
          );
        const button = origin.current?.isConnected
          ? origin.current
          : visible[0];
        button?.focus({ preventScroll: true });
      }),
    [],
  );
  const cancel = useCallback(
    (ownerId?: string) => {
      if (ownerId && current.current?.job.id !== ownerId) return;
      current.current = null;
      setSelection(null);
      onActiveChange(false);
      restoreFocus();
    },
    [onActiveChange, restoreFocus],
  );
  const begin = useCallback(
    (job: Job, mode: Selection['mode'], x = 0, y = 0) => {
      if (busy || !isMovable(job)) return;
      origin.current = document.activeElement as HTMLElement;
      originY.current = origin.current?.getBoundingClientRect().top || 0;
      const next: Selection = { job, mode, x, y, target: null };
      current.current = next;
      setSelection(next);
      onActiveChange(true);
      setAnnouncement(
        `${job.company} ${job.title}. 이동할 목록을 선택하세요. Escape 키로 취소합니다.`,
      );
      if (mode === 'menu')
        requestAnimationFrame(() =>
          dock.current
            ?.querySelector<HTMLButtonElement>(
              '[data-drop-status]:not(:disabled)',
            )
            ?.focus(),
        );
    },
    [busy, onActiveChange],
  );
  const choose = async (target: MoveStatus) => {
    const active = current.current;
    if (!active || busy || !canMove(active.job, target)) return;
    current.current = null;
    setSelection(null);
    try {
      await onMove(active.job, target);
      setAnnouncement(
        `${active.job.company} 공고를 ${moveNames[target]}로 이동했습니다.`,
      );
    } catch {
      setAnnouncement('이동을 완료하지 못했습니다. 안내 내용을 확인해 주세요.');
    } finally {
      onActiveChange(false);
      restoreFocus();
    }
  };
  const point = useCallback((x: number, y: number) => {
    const active = current.current;
    if (!active) return;
    const el = document
      .elementFromPoint(x, y)
      ?.closest<HTMLButtonElement>('[data-drop-status]');
    const target =
      el && !el.disabled ? (el.dataset.dropStatus as MoveStatus) : null;
    const next = { ...active, x, y, target };
    current.current = next;
    setSelection(next);
  }, []);
  const finish = () => {
    const active = current.current;
    if (active?.target) void choose(active.target);
    else cancel();
  };
  const selectionOpen = selection !== null;
  useEffect(() => {
    if (!selectionOpen) return;
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        cancel();
      }
      if (
        ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)
      ) {
        event.preventDefault();
        const buttons = Array.from(
          dock.current?.querySelectorAll<HTMLButtonElement>(
            '[data-drop-status]:not(:disabled)',
          ) || [],
        );
        const index = buttons.indexOf(
          document.activeElement as HTMLButtonElement,
        );
        const step = ['ArrowLeft', 'ArrowUp'].includes(event.key) ? -1 : 1;
        buttons[(index + step + buttons.length) % buttons.length]?.focus();
      }
    };
    const blur = () => cancel();
    window.addEventListener('keydown', key);
    window.addEventListener('blur', blur);
    return () => {
      window.removeEventListener('keydown', key);
      window.removeEventListener('blur', blur);
    };
  }, [selectionOpen, cancel]);
  return (
    <Context.Provider
      value={{
        begin,
        point,
        finish,
        cancel,
        activeId: selection?.job.id || null,
        busy,
      }}
    >
      {children}
      <output className="sr-only" aria-live="polite">
        {announcement}
      </output>
      {selection && (
        <>
          {selection.mode === 'pointer' && (
            <div
              className="drag-preview"
              style={{ left: selection.x, top: selection.y }}
              aria-hidden="true"
            >
              <GripVertical size={18} />
              <span>
                {selection.job.company}
                <b>{selection.job.title}</b>
              </span>
            </div>
          )}
          <fieldset className="move-dock" ref={dock} aria-label="공고 이동">
            <div className="move-dock-heading">
              <span>
                <b>{selection.job.company}</b> · 이동할 곳에 놓으세요
              </span>
              <button onClick={() => cancel()} aria-label="이동 취소">
                <X size={18} />
              </button>
            </div>
            <div className="move-targets">
              {targets.map((target) => {
                const Icon = icons[target];
                const same = selection.job.movement_status === target;
                const allowed = canMove(selection.job, target);
                return (
                  <button
                    key={target}
                    data-drop-status={target}
                    className={`move-target ${target} ${selection.target === target ? 'over' : ''}`}
                    disabled={busy || !allowed}
                    onClick={() => void choose(target)}
                  >
                    <Icon size={23} />
                    <span>{moveNames[target]}</span>
                    <small>
                      {same
                        ? '현재 위치'
                        : !allowed
                          ? '모집 확인 필요'
                          : target === 'new'
                            ? '추천 후보로 복원'
                            : '여기에 놓기'}
                    </small>
                  </button>
                );
              })}
            </div>
            <p>지원현황은 이동 대상에서 제외됩니다.</p>
          </fieldset>
        </>
      )}
    </Context.Provider>
  );
}

const interactiveSurface = (target: EventTarget | null) =>
  target instanceof Element &&
  Boolean(
    target.closest(
      'a,button,input,textarea,select,label,summary,[role="button"],[role="link"],[contenteditable]:not([contenteditable="false"]),[data-no-card-drag],.record-note',
    ),
  );
type CardGesture = {
  kind: 'pointer' | 'touch';
  id: number;
  x: number;
  y: number;
  active: boolean;
  timer?: ReturnType<typeof setTimeout>;
};

export function DraggableCard({
  job,
  children,
  className = '',
  ...props
}: Omit<ComponentProps<'article'>, 'ref'> & { job: Job }) {
  const context = useContext(Context);
  const element = useRef<HTMLElement>(null);
  const gesture = useRef<CardGesture | null>(null);
  const suppressClickUntil = useRef(0);
  const latest = useRef({ context, job });
  useLayoutEffect(() => {
    latest.current = { context, job };
  }, [context, job]);
  const movable = isMovable(job);
  const reset = useCallback(() => {
    const state = gesture.current;
    gesture.current = null;
    if (state?.timer) clearTimeout(state.timer);
    if (state?.active) {
      suppressClickUntil.current = Date.now() + 500;
      latest.current.context?.cancel(latest.current.job.id);
    }
    if (
      state?.kind === 'pointer' &&
      element.current?.hasPointerCapture(state.id)
    )
      element.current.releasePointerCapture(state.id);
  }, []);
  const activate = useCallback(
    (state: CardGesture) => {
      const { context: current, job: currentJob } = latest.current;
      if (
        !current ||
        current.busy ||
        !isMovable(currentJob) ||
        !element.current?.isConnected
      ) {
        reset();
        return;
      }
      state.active = true;
      element.current
        .querySelector<HTMLButtonElement>('.move-handle')
        ?.focus({ preventScroll: true });
      current.begin(currentJob, 'pointer', state.x, state.y);
    },
    [reset],
  );
  const finish = useCallback((x: number, y: number) => {
    const state = gesture.current;
    gesture.current = null;
    if (state?.timer) clearTimeout(state.timer);
    if (state?.active) {
      suppressClickUntil.current = Date.now() + 500;
      latest.current.context?.point(x, y);
      latest.current.context?.finish();
    }
    if (
      state?.kind === 'pointer' &&
      element.current?.hasPointerCapture(state.id)
    )
      element.current.releasePointerCapture(state.id);
  }, []);
  useEffect(() => {
    const card = element.current;
    if (!card || !movable) return;
    const start = (event: TouchEvent) => {
      suppressClickUntil.current = 0;
      if (event.touches.length !== 1) {
        reset();
        return;
      }
      if (latest.current.context?.busy || interactiveSurface(event.target))
        return;
      const touch = event.touches[0];
      const state: CardGesture = {
        kind: 'touch',
        id: touch.identifier,
        x: touch.clientX,
        y: touch.clientY,
        active: false,
      };
      gesture.current = state;
      state.timer = setTimeout(() => {
        if (gesture.current === state) activate(state);
      }, 320);
    };
    const move = (event: TouchEvent) => {
      const state = gesture.current;
      if (state?.kind !== 'touch') return;
      // Once a move has been allowed to scroll, never take that gesture back.
      if (!state.active || event.touches.length !== 1 || !event.cancelable) {
        reset();
        return;
      }
      const touch = Array.from(event.touches).find(
        (item) => item.identifier === state.id,
      );
      if (!touch) {
        reset();
        return;
      }
      event.preventDefault();
      latest.current.context?.point(touch.clientX, touch.clientY);
    };
    const end = (event: TouchEvent) => {
      const state = gesture.current;
      if (state?.kind !== 'touch') return;
      const touch = Array.from(event.changedTouches).find(
        (item) => item.identifier === state.id,
      );
      if (!touch) return;
      if (state.active && event.cancelable) event.preventDefault();
      finish(touch.clientX, touch.clientY);
    };
    const multitouch = (event: TouchEvent) => {
      if (event.touches.length > 1) reset();
    };
    const scroll = () => {
      if (!gesture.current?.active || gesture.current?.kind === 'touch')
        reset();
    };
    const visibility = () => {
      if (document.hidden) reset();
    };
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') reset();
    };
    card.addEventListener('touchstart', start, { passive: true });
    card.addEventListener('touchmove', move, { passive: false });
    card.addEventListener('touchend', end, { passive: false });
    card.addEventListener('touchcancel', reset);
    window.addEventListener('touchstart', multitouch, { passive: true });
    window.addEventListener('scroll', scroll, true);
    window.addEventListener('blur', reset);
    window.addEventListener('keydown', key);
    document.addEventListener('visibilitychange', visibility);
    return () => {
      card.removeEventListener('touchstart', start);
      card.removeEventListener('touchmove', move);
      card.removeEventListener('touchend', end);
      card.removeEventListener('touchcancel', reset);
      window.removeEventListener('touchstart', multitouch);
      window.removeEventListener('scroll', scroll, true);
      window.removeEventListener('blur', reset);
      window.removeEventListener('keydown', key);
      document.removeEventListener('visibilitychange', visibility);
      reset();
    };
  }, [movable, job.id, activate, finish, reset]);
  // Keep article semantics; the enclosed MoveHandle provides the equivalent keyboard action.
  return (
    // oxlint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <article
      {...props}
      ref={element}
      className={`${className}${movable ? ' draggable-card' : ''}${context?.activeId === job.id ? ' card-moving' : ''}`}
      onPointerDown={(event) => {
        suppressClickUntil.current = 0;
        if (
          event.pointerType === 'touch' ||
          !event.isPrimary ||
          event.button !== 0 ||
          !movable ||
          !context ||
          context.busy ||
          interactiveSurface(event.target)
        )
          return;
        gesture.current = {
          kind: 'pointer',
          id: event.pointerId,
          x: event.clientX,
          y: event.clientY,
          active: false,
        };
        event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerMove={(event) => {
        const state = gesture.current;
        if (state?.kind !== 'pointer' || state.id !== event.pointerId) return;
        if (
          !state.active &&
          Math.hypot(event.clientX - state.x, event.clientY - state.y) > 7
        )
          activate(state);
        if (state.active) {
          event.preventDefault();
          context?.point(event.clientX, event.clientY);
        }
      }}
      onPointerUp={(event) => {
        if (
          gesture.current?.kind === 'pointer' &&
          gesture.current.id === event.pointerId
        )
          finish(event.clientX, event.clientY);
      }}
      onPointerCancel={(event) => {
        if (event.pointerType !== 'touch') reset();
      }}
      onClickCapture={(event) => {
        if (event.detail > 0 && Date.now() < suppressClickUntil.current) {
          suppressClickUntil.current = 0;
          event.preventDefault();
          event.stopPropagation();
        }
      }}
      onContextMenu={(event) => {
        if (!interactiveSurface(event.target) && gesture.current?.active)
          event.preventDefault();
      }}
    >
      {children}
    </article>
  );
}

export function MoveHandle({ job }: { job: Job }) {
  const context = useContext(Context);
  const pointer = useRef<{
    id: number;
    x: number;
    y: number;
    active: boolean;
    timer?: ReturnType<typeof setTimeout>;
  } | null>(null);
  const suppressClick = useRef(false);
  useEffect(
    () => () => {
      if (pointer.current?.timer) clearTimeout(pointer.current.timer);
    },
    [],
  );
  if (!context || !isMovable(job)) return null;
  const down = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (context.busy || event.button !== 0) return;
    event.currentTarget.focus({ preventScroll: true });
    event.currentTarget.setPointerCapture(event.pointerId);
    suppressClick.current = false;
    const state = {
      id: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      active: false,
      timer: undefined as ReturnType<typeof setTimeout> | undefined,
    };
    pointer.current = state;
    if (event.pointerType !== 'mouse')
      state.timer = setTimeout(() => {
        state.active = true;
        suppressClick.current = true;
        context.begin(job, 'pointer', state.x, state.y);
      }, 320);
  };
  return (
    <button
      type="button"
      className={`move-handle ${context.activeId === job.id ? 'moving' : ''}`}
      disabled={context.busy}
      aria-label={`${job.company} ${job.title} 이동`}
      title="끌어서 분류 · 누르면 이동 메뉴"
      onContextMenu={(event) => event.preventDefault()}
      onPointerDown={down}
      onPointerMove={(event) => {
        const state = pointer.current;
        if (!state || state.id !== event.pointerId) return;
        if (
          !state.active &&
          Math.hypot(event.clientX - state.x, event.clientY - state.y) > 7
        ) {
          if (state.timer) {
            clearTimeout(state.timer);
            state.timer = undefined;
            suppressClick.current = true;
          }
          if (event.pointerType === 'mouse') {
            state.active = true;
            suppressClick.current = true;
            context.begin(job, 'pointer', event.clientX, event.clientY);
          }
        }
        if (state.active) context.point(event.clientX, event.clientY);
      }}
      onPointerUp={(event) => {
        const state = pointer.current;
        if (!state) return;
        if (state.timer) clearTimeout(state.timer);
        if (state.active) {
          context.point(event.clientX, event.clientY);
          context.finish();
        }
        pointer.current = null;
      }}
      onPointerCancel={() => {
        if (pointer.current?.timer) clearTimeout(pointer.current.timer);
        pointer.current = null;
        suppressClick.current = true;
        context.cancel();
      }}
      onClick={() => {
        if (suppressClick.current) {
          suppressClick.current = false;
          return;
        }
        context.begin(job, 'menu');
      }}
    >
      <GripVertical size={18} />
      <span className="sr-only">이동</span>
    </button>
  );
}
