// @ts-check
/**
 * liveness-api.mjs — zero-token liveness check for ATS-hosted job postings.
 *
 * Many postings live on ATS platforms (Greenhouse, Lever, Ashby, Workday, ...) that
 * expose a public JSON endpoint. We can confirm whether a posting is still live by
 * hitting that endpoint directly — no browser, no LLM tokens — and only fall back to
 * the Playwright check (liveness-browser.mjs) for non-ATS pages or when the API is
 * inconclusive. This is the cheap first rung of the liveness ladder.
 *
 * CONSERVATIVE BY DESIGN: a false "expired" is worse than the status quo (the user
 * misses a real job). So on a definitive 404/410 we return `expired`, and for
 * anything ambiguous (unknown ATS, redirect, 429/5xx, network/timeout) we return
 * `null` (→ caller falls back to Playwright).
 *
 * Two endpoint shapes:
 *   - Per-job (Greenhouse, Lever, Workday): the URL maps to a single-job endpoint,
 *     so a 200 is itself proof the posting is live.
 *   - Org-level (Ashby): the URL maps to the org's whole job board. A 200 only
 *     proves the board exists, so the provider's `interpret` step parses the board
 *     and confirms THIS posting is still listed before returning active/expired.
 *     (Ashby pages are JS-rendered, so the browser/static rung sees only nav/footer
 *     and false-reports live postings as expired — this API rung is authoritative.)
 *
 * SSRF-safe by construction: the request URL is built from a FIXED, hard-coded API
 * host plus path segments extracted from the posting URL with a strict charset
 * (no slashes / traversal), and server-side redirects are refused.
 */

import { DEFAULT_USER_AGENT } from './user-agent.mjs';

const TIMEOUT_MS = 8_000;
// Strict path-segment charset. Anything with a slash, dot-dot, or other char is
// rejected before it can reach the fixed-host API URL template.
const SAFE_SEGMENT = /^[A-Za-z0-9._-]+$/;
const ACTIVE_STRUCTURED_STATUSES = new Set(['active', 'open', 'published', 'recruiting']);
const CLOSED_STRUCTURED_STATUSES = new Set(['close', 'closed', 'expired', 'ended']);

function parseNextData(html) {
  const match = String(html).match(/<script\b[^>]*\bid=["']__NEXT_DATA__["'][^>]*>([\s\S]*?)<\/script>/i);
  if (!match) return null;
  try {
    return JSON.parse(match[1]);
  } catch {
    return null;
  }
}

function findObjectById(root, expectedId) {
  const queue = [root];
  const seen = new Set();
  while (queue.length) {
    const value = queue.shift();
    if (!value || typeof value !== 'object' || seen.has(value)) continue;
    seen.add(value);
    if (String(value.id ?? '') === String(expectedId)) return value;
    for (const child of Object.values(value)) {
      if (child && typeof child === 'object') queue.push(child);
    }
  }
  return null;
}

function saraminApplyIds(value) {
  return [...String(value).matchAll(
    /quickApplyForm\s*\(\s*(?:["']|&#0*39;|&apos;|&quot;)?\s*(\d+)/gi,
  )].map(match => match[1]);
}

/**
 * Classify a Saramin relay detail fragment conservatively.
 * @param {string} html
 * @param {string|number|null} [expectedId]
 * @returns {{ result: 'active' | 'expired', code: string, reason: string } | null}
 */
export function classifySaraminPostingHtml(html, expectedId = null) {
  if (typeof html !== 'string' || !html.trim()) return null;
  const expected = expectedId == null ? '' : String(expectedId);
  const applyIds = saraminApplyIds(html);
  if (expected && applyIds.length > 0 && !applyIds.includes(expected)) return null;

  const controls = html.match(/<(?:button|a)\b[^>]*>[\s\S]{0,400}?<\/(?:button|a)>/gi) || [];
  const scopedControls = expected
    ? controls.filter(control => saraminApplyIds(control).includes(expected))
    : controls;
  const hasClosedControl = scopedControls.some(control =>
    /title=["']\s*(?:접수마감|지원마감)\s*["']/i.test(control)
    || />\s*(?:접수마감|지원마감)\s*</i.test(control)
    || /(?:^|[\s<])disabled(?:\s|=|>|$)|\baria-disabled=["']true["']/i.test(control)
  );
  const hasMainClosedNotice = /본\s*채용정보는\s*마감되었습니다|마감된\s*채용정보(?:입니다)?|지원\s*기간이\s*종료|채용이\s*마감/.test(html);
  if (hasClosedControl || hasMainClosedNotice) {
    return {
      result: 'expired',
      code: 'saramin_posting_closed',
      reason: 'Saramin detail explicitly says the posting is closed',
    };
  }
  const hasApplyControl = scopedControls.some(control =>
    !/(?:^|[\s<])disabled(?:\s|=|>|$)|\baria-disabled=["']true["']/i.test(control)
    && (
      /title=["'][^"']*입사지원할\s*수\s*있는\s*창[^"']*["']/i.test(control)
      || />[\s\S]{0,200}?(?:입사지원|홈페이지\s*지원)[\s\S]{0,50}?</i.test(control)
    )
  );
  if (hasApplyControl) {
    return {
      result: 'active',
      code: 'saramin_apply_open',
      reason: 'Saramin detail exposes an active application control',
    };
  }
  return null;
}

/**
 * Classify a Remember posting from its server-rendered __NEXT_DATA__ payload.
 * @param {string} html
 * @param {string|number} expectedId
 * @returns {{ result: 'active' | 'expired', code: string, reason: string } | null}
 */
export function classifyRememberPostingHtml(html, expectedId) {
  const posting = findObjectById(parseNextData(html), expectedId);
  if (!posting || typeof posting.title !== 'string' || !posting.title.trim()) return null;
  const status = String(posting.status || '').toLowerCase();
  if (ACTIVE_STRUCTURED_STATUSES.has(status)) {
    return {
      result: 'active',
      code: 'remember_structured_active',
      reason: 'Remember structured posting status is active',
    };
  }
  if (CLOSED_STRUCTURED_STATUSES.has(status)) {
    return {
      result: 'expired',
      code: 'remember_structured_closed',
      reason: 'Remember structured posting status is closed',
    };
  }
  return null;
}

function dateKey(value) {
  const match = String(value).match(/^(\d{4})[.-](\d{1,2})[.-](\d{1,2})$/);
  if (!match) return null;
  return Number(match[1]) * 10_000 + Number(match[2]) * 100 + Number(match[3]);
}

function kstTodayKey(now = new Date()) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(now);
  const values = Object.fromEntries(parts.map(part => [part.type, part.value]));
  return Number(values.year) * 10_000 + Number(values.month) * 100 + Number(values.day);
}

/**
 * Classify JobKorea HTML. An explicit closed banner wins over a future or
 * ongoing deadline because employers may close a posting early.
 * @param {string} html
 * @param {Date} [now]
 * @returns {{ result: 'active' | 'expired', code: string, reason: string } | null}
 */
export function classifyJobKoreaPostingHtml(html, now = new Date()) {
  const text = String(html);
  if (/마감되었습니다\s*[.!]?/.test(text)) {
    return {
      result: 'expired',
      code: 'jobkorea_explicitly_closed',
      reason: 'JobKorea explicitly says the posting is closed',
    };
  }
  const deadlineMatch = text.match(/마감일\s*[:：]\s*(상시채용|\d{4}[.]\d{1,2}[.]\d{1,2})/);
  if (!deadlineMatch) return null;
  if (deadlineMatch[1] === '상시채용') {
    return {
      result: 'active',
      code: 'jobkorea_ongoing',
      reason: 'JobKorea marks the posting as ongoing',
    };
  }
  const deadline = dateKey(deadlineMatch[1]);
  if (!deadline) return null;
  if (deadline < kstTodayKey(now)) {
    return {
      result: 'expired',
      code: 'jobkorea_deadline_passed',
      reason: 'JobKorea deadline has passed',
    };
  }
  if (/남은기간/.test(text)) {
    return {
      result: 'active',
      code: 'jobkorea_future_deadline',
      reason: 'JobKorea shows remaining time before the deadline',
    };
  }
  return null;
}

/**
 * Classify a Wanted posting from its server-rendered __NEXT_DATA__ payload.
 * @param {string} html
 * @returns {{ result: 'active' | 'expired', code: string, reason: string } | null}
 */
export function classifyWantedPostingHtml(html) {
  if (typeof html !== 'string' || !html.trim()) return null;
  const match = html.match(/<script\b[^>]*\bid=["']__NEXT_DATA__["'][^>]*>([\s\S]*?)<\/script>/i);
  if (!match) return null;
  let job;
  try {
    job = JSON.parse(match[1])?.props?.pageProps?.initialData;
  } catch {
    return null;
  }
  return classifyWantedPostingPayload(job);
}

/**
 * Classify the response from Wanted's public per-posting detail API.
 * @param {unknown} payload
 * @param {string|number|null} [expectedId]
 * @returns {{ result: 'active' | 'expired', code: string, reason: string } | null}
 */
export function classifyWantedPostingPayload(payload, expectedId = null) {
  const root = payload && typeof payload === 'object' && !Array.isArray(payload) ? payload : null;
  const job = root?.job && typeof root.job === 'object' ? root.job : root;
  if (!job || typeof job.position !== 'string' || !job.position.trim()) return null;
  if (expectedId != null && String(job.id ?? '') !== String(expectedId)) return null;
  const status = String(job.status || '').toLowerCase();
  if (job.hidden === true || ['close', 'closed', 'expired'].includes(status)) {
    return {
      result: 'expired',
      code: job.hidden === true ? 'wanted_structured_hidden' : 'wanted_structured_closed',
      reason: job.hidden === true
        ? 'Wanted structured posting is hidden'
        : 'Wanted structured posting status is closed',
    };
  }
  if (['active', 'open', 'recruiting'].includes(status)) {
    return {
      result: 'active',
      code: 'wanted_structured_active',
      reason: 'Wanted structured posting status is active',
    };
  }
  return null;
}

// Most providers extract single path segments (SAFE_SEGMENT covers those directly).
// Workday's job path is genuinely multi-segment (a location slug + a title slug,
// e.g. "Toronto-ON-CAN/Agentic-AI-Engineer_R260010125"), so a `parts` value may
// itself contain slashes. This still validates every individual segment against
// the same strict charset (and rejects ".." in any of them) — it only relaxes
// "no slash at all" to "no *unsafe* content between slashes", so the traversal/
// injection guarantee is unchanged.
function isSafeValue(v) {
  if (typeof v !== 'string' || v.length === 0) return false;
  // SAFE_SEGMENT's charset includes "." (some real segments use dots), so ".."
  // alone passes that regex — same as the single-segment guard in
  // resolveAtsApi below, the explicit `!includes('..')` check per segment is
  // load-bearing, not redundant with the regex test.
  return v.split('/').every((seg) => seg.length > 0 && SAFE_SEGMENT.test(seg) && !seg.includes('..'));
}

// Each ATS: detect its posting URL, then map to a public JSON API URL.
// `match` returns the extracted path params (or null); `api` builds the FIXED-host URL.
// Optional per-provider fields:
//   `request`    — provider-specific RequestInit for non-GET/JSON sources.
//   `timeoutMs`  — override the default fetch timeout (slow/rate-limited APIs).
//   `interpret`  — read the 200 response body to decide liveness (org-level APIs
//                  where a 200 alone doesn't prove THIS posting is live).
const ATS_PROVIDERS = [
  {
    id: 'saramin',
    match(u) {
      if (u.hostname !== 'www.saramin.co.kr' || u.pathname !== '/zf_user/jobs/relay/view') return null;
      const id = u.searchParams.get('rec_idx');
      return id && /^\d+$/.test(id) ? { id } : null;
    },
    api: () => 'https://www.saramin.co.kr/zf_user/jobs/relay/view-ajax',
    request: ({ id }) => ({
      method: 'POST',
      redirect: 'error',
      headers: {
        'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'x-requested-with': 'XMLHttpRequest',
        'accept-language': 'ko-KR,ko;q=0.9',
        referer: `https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=${id}`,
      },
      body: new URLSearchParams({
        rec_idx: id,
        rec_seq: '0',
        view_type: 'mail_landing',
        t_ref: 'non-logged_relay_view',
        t_ref_content: 'category_new_rec',
      }).toString(),
    }),
    async interpret(res, { id }) {
      try {
        return classifySaraminPostingHtml(await res.text(), id);
      } catch {
        return null;
      }
    },
  },
  {
    id: 'wanted',
    match(u) {
      if (u.hostname !== 'www.wanted.co.kr') return null;
      const match = u.pathname.match(/^\/wd\/(\d+)\/?$/);
      return match ? { id: match[1] } : null;
    },
    api: ({ id }) => `https://www.wanted.co.kr/api/v4/jobs/${id}`,
    request: () => ({ method: 'GET', redirect: 'error' }),
    async interpret(res, { id }) {
      try {
        return classifyWantedPostingPayload(await res.json(), id);
      } catch {
        return null;
      }
    },
  },
  {
    id: 'remember',
    match(u) {
      if (u.hostname !== 'career.rememberapp.co.kr') return null;
      const match = u.pathname.match(/^\/job\/posting\/([A-Za-z0-9_-]+)\/?$/);
      return match ? { id: match[1] } : null;
    },
    api: ({ id }) => `https://career.rememberapp.co.kr/job/posting/${id}`,
    request: () => ({ method: 'GET', redirect: 'error' }),
    async interpret(res, { id }) {
      try {
        return classifyRememberPostingHtml(await res.text(), id);
      } catch {
        return null;
      }
    },
  },
  {
    id: 'jobkorea',
    match(u) {
      if (u.hostname !== 'www.jobkorea.co.kr') return null;
      const match = u.pathname.match(/^\/Recruit\/GI_Read\/(\d+)\/?$/i);
      return match ? { id: match[1] } : null;
    },
    api: ({ id }) => `https://www.jobkorea.co.kr/Recruit/GI_Read/${id}`,
    request: () => ({ method: 'GET', redirect: 'error' }),
    async interpret(res) {
      try {
        return classifyJobKoreaPostingHtml(await res.text());
      } catch {
        return null;
      }
    },
  },
  {
    id: 'greenhouse',
    // boards.greenhouse.io/{board}/jobs/{id} · job-boards[.eu].greenhouse.io/{board}/jobs/{id}
    match(u) {
      if (!/(^|\.)greenhouse\.io$/.test(u.hostname)) return null;
      const m = u.pathname.match(/^\/([^/]+)\/jobs\/(\d+)\/?$/);
      return m ? { board: m[1], id: m[2] } : null;
    },
    api: ({ board, id }) => `https://boards-api.greenhouse.io/v1/boards/${board}/jobs/${id}`,
  },
  {
    id: 'lever',
    // jobs.(eu.)?lever.co/{slug}/{id}
    match(u) {
      const host = u.hostname.match(/^jobs\.((?:eu\.)?lever\.co)$/);
      if (!host) return null;
      const m = u.pathname.match(/^\/([^/]+)\/([^/?#]+)\/?$/);
      return m ? { apiHost: `api.${host[1]}`, slug: m[1], id: m[2] } : null;
    },
    api: ({ apiHost, slug, id }) => `https://${apiHost}/v0/postings/${slug}/${id}`,
  },
  {
    id: 'ashby',
    // jobs.ashbyhq.com/{org}/{jobId}[/application]. Ashby's public posting API is
    // ORG-level (the whole job board), not per-job — so `api` maps to the board and
    // `interpret` confirms this {jobId} is still listed. Only {org} reaches the
    // fixed-host URL; {jobId} is used solely to filter the parsed board (SAFE_SEGMENT
    // still validates both).
    match(u) {
      if (u.hostname !== 'jobs.ashbyhq.com') return null;
      const m = u.pathname.match(/^\/([^/]+)\/([^/]+)(?:\/application)?\/?$/);
      return m ? { org: m[1], jobId: m[2] } : null;
    },
    api: ({ org }) => `https://api.ashbyhq.com/posting-api/job-board/${org}`,
    // Ashby's posting-api has a server-side latency floor and rate-limits repeated
    // unauthenticated hits (see providers/ashby.mjs). Give it more room than the ATS
    // default so a slow-but-live board doesn't time out into a Playwright fallback.
    timeoutMs: 20_000,
    async interpret(res, { jobId }) {
      let json;
      try {
        json = await res.json();
      } catch {
        return null; // unparseable body → inconclusive, let the browser decide
      }
      return classifyAshbyBoard(json, jobId);
    },
  },
  {
    id: 'workday',
    // {tenant}.{shard}.myworkdayjobs.com[/{xx-XX}]/{site}/job/{jobPath...}
    // Mirrors the tenant/shard/site detection in providers/workday.mjs, but for a
    // single posting rather than the board-wide CXS search endpoint. Workday's
    // per-job CXS endpoint (`/wday/cxs/{tenant}/{site}/job/{jobPath}`) is a
    // genuinely PER-JOB API like Greenhouse/Lever — a 200 is itself proof the
    // posting is live, confirmed against real tenants (BMO, TD, Manulife, CIBC):
    // an existing posting returns 200, a garbage job id returns 404.
    //
    // jobPath is intentionally multi-segment (Workday encodes a location slug and
    // a title slug as separate path parts, e.g.
    // "Toronto-ON-CAN/Agentic-AI-Engineer_R260010125") — isSafeValue (not the
    // single-segment SAFE_SEGMENT check other providers use directly) validates
    // it component-by-component.
    match(u) {
      const m = `${u.hostname}${u.pathname}`.match(
        /^([\w-]+)\.(wd[\w-]*)\.myworkdayjobs\.com\/(?:[a-z]{2}-[A-Z]{2}\/)?([^/?#]+)\/job\/(.+?)\/?$/
      );
      if (!m) return null;
      const [, tenant, shard, site, jobPath] = m;
      return { tenant, shard, site, jobPath };
    },
    api: ({ tenant, shard, site, jobPath }) =>
      `https://${tenant}.${shard}.myworkdayjobs.com/wday/cxs/${tenant}/${site}/job/${jobPath}`,
  },
];

/**
 * Decide liveness for one Ashby posting from its org's job-board API payload.
 * Pure + deterministic (no I/O), mirroring classifyLiveness in liveness-core.mjs.
 *
 * The public board lists only currently-published postings, so a posting that is
 * absent (or explicitly `isListed: false`) has been removed/unlisted → expired.
 * A present, listed posting → active. An unexpected shape → null (inconclusive),
 * so a future API change degrades to a Playwright fallback rather than a false
 * "expired".
 *
 * @param {any} json - parsed job-board response, expected shape `{ jobs: [...] }`
 * @param {string} jobId - the {jobId} from jobs.ashbyhq.com/{org}/{jobId}
 * @returns {{ result: 'active' | 'expired', code: string, reason: string } | null}
 */
export function classifyAshbyBoard(json, jobId) {
  if (!json || !Array.isArray(json.jobs)) return null; // unexpected shape → fall back
  const target = String(jobId).toLowerCase();
  const job = json.jobs.find((j) => typeof j?.id === 'string' && j.id.toLowerCase() === target);
  if (job && job.isListed !== false) {
    return { result: 'active', code: 'ashby_api_ok', reason: 'Ashby posting is listed on the board (live)' };
  }
  return { result: 'expired', code: 'ashby_api_unlisted', reason: 'Ashby posting not listed on the board — removed/unlisted' };
}

/**
 * Map a posting URL to its ATS API URL, or null if it isn't a known ATS posting
 * (or any extracted segment fails the strict charset). Pure + deterministic.
 * @param {string} rawUrl
 * @returns {{ ats: string, apiUrl: string, parts: Record<string, string>, requestInit?: RequestInit, timeoutMs?: number, interpret?: (res: Response, parts: Record<string, string>) => Promise<{ result: 'active' | 'expired', code: string, reason: string } | null> } | null}
 */
export function resolveAtsApi(rawUrl) {
  let u;
  try {
    u = new URL(rawUrl);
  } catch {
    return null;
  }
  if (u.protocol !== 'https:') return null;
  for (const provider of ATS_PROVIDERS) {
    const parts = provider.match(u);
    if (!parts) continue;
    // SSRF guard: every derived value must be safe — a single path segment for
    // most providers, or (Workday) a slash-separated sequence of safe segments.
    // isSafeValue enforces the same charset + no-".." rule either way.
    if (!Object.values(parts).every(isSafeValue)) return null;
    return {
      ats: provider.id,
      apiUrl: provider.api(parts),
      parts,
      requestInit: provider.request?.(parts),
      timeoutMs: provider.timeoutMs,
      interpret: provider.interpret,
    };
  }
  return null;
}

/** True if `url` is an ATS posting we can check via API (lets callers stay lazy about the browser). */
export function isAtsPosting(url) {
  return resolveAtsApi(url) !== null;
}

/**
 * Zero-token liveness check via the posting's ATS API.
 * @param {string} url
 * @returns {Promise<{ result: 'active' | 'expired', code: string, reason: string } | null>}
 *   null = not a known ATS posting, or inconclusive → caller should fall back to Playwright.
 */
export async function checkLivenessViaApi(url) {
  const resolved = resolveAtsApi(url);
  if (!resolved) return null;
  const { ats, apiUrl, parts, requestInit, interpret, timeoutMs } = resolved;

  // The timeout guards the whole classification (fetch + any `interpret` body read),
  // since aborting the shared signal also tears down an in-flight res.json().
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs || TIMEOUT_MS);
  try {
    let res;
    try {
      res = await fetch(apiUrl, {
        method: requestInit?.method || 'GET',
        ...requestInit,
        headers: {
          'user-agent': DEFAULT_USER_AGENT,
          accept: 'text/html,application/json;q=0.9,*/*;q=0.8',
          ...(requestInit?.headers || {}),
        },
        redirect: 'error', // refuse server-side redirects (SSRF + ambiguity guard)
        signal: controller.signal,
      });
    } catch {
      return null; // network / timeout / redirect → inconclusive, let Playwright decide
    }

    if (res.status === 404 || res.status === 410) {
      return { result: 'expired', code: `${ats}_api_gone`, reason: `ATS API ${res.status} — posting removed` };
    }
    if (res.status === 200) {
      // Org-level APIs (Ashby) inspect the body to confirm THIS posting; per-job
      // APIs (Greenhouse, Lever) treat a 200 as proof the posting is live.
      if (interpret) return await interpret(res, parts);
      return { result: 'active', code: `${ats}_api_ok`, reason: 'ATS API returns the posting (live)' };
    }
    return null; // 429/5xx/other → inconclusive, fall back to the browser check
  } catch {
    return null; // interpret abort / unexpected error → inconclusive
  } finally {
    clearTimeout(timer);
  }
}
