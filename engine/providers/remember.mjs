// @ts-check
/** @typedef {import('./_types.js').Provider} Provider */

import { BROWSER_LIKE_USER_AGENT } from './_http.mjs';

const SEARCH_API = 'https://career-api.rememberapp.co.kr/job_postings/search';
const JOB_ORIGIN = 'https://career.rememberapp.co.kr';
const DEFAULT_PER_PAGE = 50;
const DEFAULT_MAX_PAGES = 3;
const INTER_REQUEST_DELAY_MS = 200;
const MAX_RETRIES = 2;
const RETRY_BASE_DELAY_MS = 500;
const RETRY_MAX_DELAY_MS = 4_000;
const TRANSIENT_NETWORK_CODES = new Set([
  'ECONNRESET', 'ETIMEDOUT', 'EAI_AGAIN', 'ECONNREFUSED',
  'ENETUNREACH', 'EHOSTUNREACH', 'EPIPE',
]);

function sleep(ms, ctx) {
  if (typeof ctx?.sleep === 'function') return ctx.sleep(ms);
  return new Promise(resolve => setTimeout(resolve, ms));
}

function parseRetryAfterMs(value) {
  if (!value) return null;
  const seconds = Number(value);
  if (Number.isFinite(seconds) && seconds >= 0) return seconds * 1000;
  const dateMs = Date.parse(value);
  return Number.isFinite(dateMs) ? Math.max(0, dateMs - Date.now()) : null;
}

function isRetryableError(err) {
  const status = err?.status;
  if (typeof status === 'number') return status === 429 || (status >= 500 && status <= 599);
  const code = err?.code || err?.cause?.code;
  return err instanceof TypeError
    || err?.name === 'TypeError'
    || err?.name === 'AbortError'
    || TRANSIENT_NETWORK_CODES.has(code);
}

async function fetchJsonWithRetry(ctx, url, opts) {
  let lastError;
  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    try {
      return await ctx.fetchJson(url, opts);
    } catch (err) {
      lastError = err;
      if (attempt === MAX_RETRIES || !isRetryableError(err)) throw err;
      const retryAfterMs = parseRetryAfterMs(err?.retryAfter);
      const backoffMs = Math.min(RETRY_BASE_DELAY_MS * 2 ** attempt, RETRY_MAX_DELAY_MS);
      await sleep(retryAfterMs == null ? backoffMs : Math.min(retryAfterMs, RETRY_MAX_DELAY_MS * 4), ctx);
    }
  }
  throw lastError;
}

function boundedInt(value, fallback, min, max) {
  const n = Number(value);
  return Number.isInteger(n) && n >= min ? Math.min(n, max) : fallback;
}

function keywordsFrom(entry) {
  const raw = Array.isArray(entry.searchKeywords) ? entry.searchKeywords : [entry.searchKeywords || ''];
  const keywords = [...new Set(raw.map(v => String(v).trim()).filter(Boolean))];
  if (!keywords.length) throw new Error('remember: configure searchKeywords in your local portals.yml');
  return keywords;
}

function rememberLocation(item) {
  if (typeof item?.normalized_address === 'string' && item.normalized_address.trim()) return item.normalized_address.trim();
  if (typeof item?.address === 'string' && item.address.trim()) return item.address.trim();
  const addresses = Array.isArray(item?.addresses) ? item.addresses : [];
  return addresses
    .map(address => [address?.address_level1, address?.address_level2, address?.address_level3]
      .map(v => typeof v === 'string' ? v.trim() : '')
      .filter(Boolean)
      .join(' '))
    .filter(Boolean)
    .join(', ');
}

function toEpochMs(value) {
  if (!value) return undefined;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? undefined : parsed;
}

/**
 * Normalize one Remember Career API row.
 * @param {any} item
 * @param {string} fallbackCompany
 * @returns {{title:string,url:string,company:string,location:string,description?:string,postedAt?:number}|null}
 */
export function normalizeRememberJob(item, fallbackCompany = '') {
  if (!item || typeof item !== 'object') return null;
  const title = typeof item.title === 'string' ? item.title.trim() : '';
  const id = String(item.id ?? '').trim();
  if (!title || !/^\d+$/.test(id)) return null;

  const company = String(item.organization?.name || item.company?.name || fallbackCompany || '').trim();
  const location = rememberLocation(item);
  const description = String(item.job_description || item.description || '').trim();
  const postedAt = toEpochMs(item.starts_at || item.created_at || item.published_at);

  return {
    title,
    url: `${JOB_ORIGIN}/job/posting/${id}`,
    company,
    location,
    ...(description ? { description } : {}),
    ...(postedAt != null ? { postedAt } : {}),
  };
}

function buildPayload(keyword, page, per) {
  return {
    search: {
      organization_include: [],
      job_group_ids: [],
      job_ids: [],
      experience_level: 0,
      experience_level_filter: [],
      company_tag_ids: [],
      keywords: [keyword],
      exclude_keywords: [],
      locations: [],
      theme_ids: [],
      type: 'search',
      includeAppliedJobPosting: false,
    },
    page,
    per,
    sort: 'starts_at_desc',
    meta: { device_uid: 'career-ops-scan', device_os: 'web' },
  };
}

/** @type {Provider} */
export default {
  id: 'remember',

  detect() { return null; },

  async fetch(entry, ctx) {
    const keywords = keywordsFrom(entry);
    const per = boundedInt(entry.perPage, DEFAULT_PER_PAGE, 1, 50);
    const maxPages = boundedInt(entry.maxPages, DEFAULT_MAX_PAGES, 1, 10);
    const byUrl = new Map();
    let firstRequest = true;
    let succeededOnce = false;

    for (const keyword of keywords) {
      for (let page = 1; page <= maxPages; page++) {
        if (firstRequest) firstRequest = false;
        else await sleep(INTER_REQUEST_DELAY_MS, ctx);
        let json;
        try {
          json = /** @type {any} */ (await fetchJsonWithRetry(ctx, SEARCH_API, {
            method: 'POST',
            redirect: 'error',
            headers: {
              accept: 'application/json',
              'accept-language': 'ko-KR,ko;q=0.9,en;q=0.8',
              'content-type': 'application/json',
              origin: JOB_ORIGIN,
              referer: `${JOB_ORIGIN}/job/postings`,
              'user-agent': BROWSER_LIKE_USER_AGENT,
            },
            body: JSON.stringify(buildPayload(keyword, page, per)),
          }));
        } catch (err) {
          if (!succeededOnce) throw err;
          console.error(`  ⚠ remember: keyword "${keyword}" page ${page} failed (${err.message}) — keeping the ${byUrl.size} jobs collected so far`);
          return [...byUrl.values()];
        }

        if (!Array.isArray(json?.data)) {
          const err = new Error('remember: unexpected API response (expected data[])');
          if (!succeededOnce) throw err;
          console.error(`  ⚠ remember: keyword "${keyword}" page ${page} malformed (${err.message}) — keeping the ${byUrl.size} jobs collected so far`);
          return [...byUrl.values()];
        }
        succeededOnce = true;
        for (const item of json.data) {
          const job = normalizeRememberJob(item, entry.name || 'Remember');
          if (job) byUrl.set(job.url, job);
        }

        const total = Number(json?.meta?.total_count);
        if (json.data.length < per || (Number.isFinite(total) && page * per >= total)) break;
      }
    }

    return [...byUrl.values()];
  },
};
