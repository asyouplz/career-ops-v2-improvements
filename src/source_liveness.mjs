#!/usr/bin/env node

import { pathToFileURL } from 'node:url';

const MAX_URLS = 100;
const CONCURRENCY = 4;
const MAX_BODY_CHARS = 2_000_000;
const REQUEST_TIMEOUT_MS = 12_000;
const ACTIVE_STATUSES = new Set(['active', 'open', 'published', 'recruiting']);
const CLOSED_STATUSES = new Set(['close', 'closed', 'expired', 'ended']);

function safeUrl(raw) {
  try {
    const url = new URL(String(raw));
    return url.protocol === 'https:' ? url : null;
  } catch {
    return null;
  }
}

function parseNextData(html) {
  const match = String(html).match(
    /<script\b[^>]*\bid=["']__NEXT_DATA__["'][^>]*>([\s\S]*?)<\/script>/i,
  );
  if (!match) return null;
  try {
    return JSON.parse(match[1]);
  } catch {
    return null;
  }
}

function findObjectByNumericId(root, expectedId) {
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

export function classifyRememberPostingHtml(html, expectedId) {
  const posting = findObjectByNumericId(parseNextData(html), expectedId);
  if (!posting || typeof posting.title !== 'string' || !posting.title.trim()) return null;
  const status = String(posting.status || '').toLowerCase();
  if (ACTIVE_STATUSES.has(status)) {
    return { result: 'active', code: 'remember_structured_active' };
  }
  if (CLOSED_STATUSES.has(status)) {
    return { result: 'expired', code: 'remember_structured_closed' };
  }
  return null;
}

export function classifyWantedPostingHtml(html, expectedId) {
  const root = parseNextData(html);
  const initial = root?.props?.pageProps?.initialData;
  const posting = String(initial?.id ?? '') === String(expectedId)
    ? initial
    : findObjectByNumericId(root, expectedId);
  return classifyWantedPostingPayload(posting, expectedId);
}

export function classifyWantedPostingPayload(payload, expectedId) {
  const root = payload && typeof payload === 'object' && !Array.isArray(payload) ? payload : null;
  const posting = root?.job && typeof root.job === 'object' ? root.job : root;
  const title = posting?.position || posting?.title;
  if (!posting || typeof title !== 'string' || !title.trim()) return null;
  if (String(posting.id ?? '') !== String(expectedId)) return null;
  const status = String(posting.status || '').toLowerCase();
  if (posting.hidden === true || CLOSED_STATUSES.has(status)) {
    return {
      result: 'expired',
      code: posting.hidden === true ? 'wanted_structured_hidden' : 'wanted_structured_closed',
    };
  }
  if (ACTIVE_STATUSES.has(status)) {
    return { result: 'active', code: 'wanted_structured_active' };
  }
  return null;
}

function classifyWantedPostingJson(text, expectedId) {
  try {
    return classifyWantedPostingPayload(JSON.parse(text), expectedId);
  } catch {
    return null;
  }
}

function quickApplyIds(value) {
  return [...String(value).matchAll(
    /quickApplyForm\s*\(\s*(?:(?:["']|&(?:quot|apos|#0*34|#0*39|#x0*22|#x0*27);)\s*)?(\d+)/gi,
  )].map(match => match[1]);
}

export function classifySaraminPostingHtml(html, expectedId) {
  if (typeof html !== 'string' || !html.trim()) return null;
  const expected = String(expectedId || '');
  const applyIds = quickApplyIds(html);
  if (expected && applyIds.length > 0 && !applyIds.includes(expected)) return null;

  const controls = html.match(/<(?:button|a)\b[^>]*>[\s\S]{0,400}?<\/(?:button|a)>/gi) || [];
  const scoped = controls.filter(control => {
    if (!expected) return true;
    return quickApplyIds(control).includes(expected);
  });
  const closed = scoped.some(control =>
    /title=["']\s*(?:접수마감|지원마감)\s*["']/i.test(control)
    || />\s*(?:접수마감|지원마감)\s*</i.test(control)
    || /(?:^|[\s<])disabled(?:\s|=|>|$)|\baria-disabled=["']true["']/i.test(control)
  ) || /본\s*채용정보는\s*마감되었습니다|마감된\s*채용정보(?:입니다)?|지원\s*기간이\s*종료|채용이\s*마감/.test(html);
  if (closed) return { result: 'expired', code: 'saramin_structured_closed' };

  const active = scoped.some(control =>
    !/(?:^|[\s<])disabled(?:\s|=|>|$)|\baria-disabled=["']true["']/i.test(control)
    && (
      /title=["'][^"']*입사지원할\s*수\s*있는\s*창[^"']*["']/i.test(control)
      || />[\s\S]{0,200}?(?:입사지원|홈페이지\s*지원)[\s\S]{0,50}?</i.test(control)
    )
  );
  return active ? { result: 'active', code: 'saramin_structured_active' } : null;
}

function dateKey(value) {
  const match = String(value).match(/^(\d{4})[.-](\d{1,2})[.-](\d{1,2})$/);
  if (!match) return null;
  return Number(match[1]) * 10_000 + Number(match[2]) * 100 + Number(match[3]);
}

function kstTodayKey(now = new Date()) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Seoul',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(now);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return Number(values.year) * 10_000 + Number(values.month) * 100 + Number(values.day);
}

export function classifyJobKoreaPostingHtml(html, now = new Date()) {
  const text = String(html);
  if (/마감되었습니다\s*[.!]?/.test(text)) {
    return { result: 'expired', code: 'jobkorea_explicitly_closed' };
  }
  const deadlineMatch = text.match(/마감일\s*[:：]\s*(상시채용|\d{4}[.]\d{1,2}[.]\d{1,2})/);
  if (!deadlineMatch) return null;
  if (deadlineMatch[1] === '상시채용') {
    return { result: 'active', code: 'jobkorea_ongoing' };
  }
  const deadline = dateKey(deadlineMatch[1]);
  if (!deadline) return null;
  if (deadline < kstTodayKey(now)) {
    return { result: 'expired', code: 'jobkorea_deadline_passed' };
  }
  if (/남은기간/.test(text)) {
    return { result: 'active', code: 'jobkorea_future_deadline' };
  }
  return null;
}

function route(rawUrl) {
  const url = safeUrl(rawUrl);
  if (!url) return null;
  let match;
  if (url.hostname === 'www.saramin.co.kr'
      && url.pathname === '/zf_user/jobs/relay/view'
      && /^\d+$/.test(url.searchParams.get('rec_idx') || '')) {
    const id = url.searchParams.get('rec_idx');
    return {
      source: 'saramin',
      url: new URL('https://www.saramin.co.kr/zf_user/jobs/relay/view-ajax'),
      id,
      classify: classifySaraminPostingHtml,
      requestInit: {
        method: 'POST',
        headers: {
          'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
          'x-requested-with': 'XMLHttpRequest',
          referer: `https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=${id}`,
        },
        body: new URLSearchParams({
          rec_idx: id,
          rec_seq: '0',
          view_type: 'mail_landing',
          t_ref: 'non-logged_relay_view',
          t_ref_content: 'category_new_rec',
        }).toString(),
      },
    };
  }
  if (url.hostname === 'career.rememberapp.co.kr'
      && (match = url.pathname.match(/^\/job\/posting\/([A-Za-z0-9_-]+)\/?$/))) {
    return { source: 'remember', url, id: match[1], classify: classifyRememberPostingHtml };
  }
  if (url.hostname === 'www.wanted.co.kr'
      && (match = url.pathname.match(/^\/wd\/(\d+)\/?$/))) {
    return {
      source: 'wanted',
      url: new URL(`https://www.wanted.co.kr/api/v4/jobs/${match[1]}`),
      id: match[1],
      classify: classifyWantedPostingJson,
      requestInit: { headers: { accept: 'application/json' } },
    };
  }
  if (url.hostname === 'www.jobkorea.co.kr'
      && (match = url.pathname.match(/^\/Recruit\/GI_Read\/(\d+)\/?$/i))) {
    return { source: 'jobkorea', url, id: match[1], classify: classifyJobKoreaPostingHtml };
  }
  return null;
}

async function inspect(rawUrl) {
  const routed = route(rawUrl);
  if (!routed) return null;
  let response;
  try {
    response = await fetch(routed.url, {
      ...(routed.requestInit || {}),
      redirect: 'error',
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      headers: {
        accept: 'text/html,application/xhtml+xml',
        'accept-language': 'ko-KR,ko;q=0.9,en;q=0.8',
        'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/136 Safari/537.36',
        ...(routed.requestInit?.headers || {}),
      },
    });
  } catch {
    return null;
  }
  if (response.status === 404 || response.status === 410) {
    return { source: routed.source, result: 'expired', code: 'http_gone' };
  }
  if (response.status !== 200) return null;
  const body = await response.text();
  if (!body || body.length > MAX_BODY_CHARS) return null;
  const classified = routed.source === 'jobkorea'
    ? routed.classify(body)
    : routed.classify(body, routed.id);
  return classified ? { source: routed.source, ...classified } : null;
}

async function inspectConcurrently(urls) {
  const inspected = new Array(urls.length);
  let next = 0;
  async function worker() {
    while (next < urls.length) {
      const index = next++;
      inspected[index] = await inspect(urls[index]);
    }
  }
  await Promise.all(
    Array.from({ length: Math.min(CONCURRENCY, urls.length) }, () => worker()),
  );
  return inspected;
}

async function main() {
  const urls = process.argv.slice(2, 2 + MAX_URLS);
  const results = {};
  const details = {};
  const inspectedItems = await inspectConcurrently(urls);
  for (let index = 0; index < urls.length; index++) {
    const url = urls[index];
    const inspected = inspectedItems[index];
    if (!inspected) continue;
    results[url] = inspected.result;
    details[url] = {
      source: inspected.source,
      code: inspected.code,
      checker: 'career-ops-v2-structured-source',
    };
  }
  console.log(JSON.stringify({
    status: Object.keys(results).length === urls.length ? 'ok' : 'partial',
    requested: urls.length,
    checked: Object.keys(results).length,
    results,
    details,
  }));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(String(error?.message || error));
    process.exit(1);
  });
}
