// @ts-check
/** @typedef {import('./_types.js').Provider} Provider */

import { BROWSER_LIKE_USER_AGENT } from './_http.mjs';
import { decodeEntities } from './_html-entities.mjs';

const ORIGIN = 'https://www.saramin.co.kr';
const SEARCH_PATH = '/zf_user/search';
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

async function fetchTextWithRetry(ctx, url, opts) {
  let lastError;
  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    try {
      return await ctx.fetchText(url, opts);
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

function cleanHtml(value = '') {
  return decodeEntities(String(value)).replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
}

function attr(attrs, name) {
  const match = String(attrs).match(new RegExp(`\\b${name}\\s*=\\s*(["'])([\\s\\S]*?)\\1`, 'i'));
  return match ? decodeEntities(match[2]) : '';
}

function canonicalPostingUrl(href) {
  if (!href) return null;
  let url;
  try { url = new URL(href, ORIGIN); } catch { return null; }
  if (url.protocol !== 'https:' || url.hostname !== 'www.saramin.co.kr' || url.pathname !== '/zf_user/jobs/relay/view') return null;
  const id = url.searchParams.get('rec_idx') || '';
  if (!/^\d+$/.test(id)) return null;
  return `${ORIGIN}/zf_user/jobs/relay/view?rec_idx=${id}`;
}

function dateFromCard(card) {
  const match = card.match(/(?:등록일|수정일)\s*(\d{2})[./-](\d{2})[./-](\d{2})/);
  if (!match) return undefined;
  return Date.UTC(2000 + Number(match[1]), Number(match[2]) - 1, Number(match[3]));
}

function cardChunks(html) {
  const starts = [...String(html).matchAll(/<div\b[^>]*class\s*=\s*(["'])[^"']*\bitem_recruit\b[^"']*\1[^>]*>/gi)].map(m => m.index);
  return starts.map((start, index) => String(html).slice(start, starts[index + 1] ?? String(html).length));
}

/**
 * Parse Saramin search-result HTML.
 * @param {string} html
 * @param {string} fallbackCompany
 */
export function parseSaraminHtml(html, fallbackCompany = 'Saramin') {
  const jobs = [];
  const seen = new Set();

  for (const card of cardChunks(html)) {
    const titleMatch = card.match(/<h2\b[^>]*class\s*=\s*(["'])[^"']*\bjob_tit\b[^"']*\1[^>]*>[\s\S]*?<a\b([^>]*)>([\s\S]*?)<\/a>/i);
    if (!titleMatch) continue;
    const href = attr(titleMatch[2], 'href');
    const url = canonicalPostingUrl(href);
    const title = cleanHtml(attr(titleMatch[2], 'title') || titleMatch[3]);
    if (!url || !title || seen.has(url)) continue;

    const companyMatch = card.match(/<strong\b[^>]*class\s*=\s*(["'])[^"']*\bcorp_name\b[^"']*\1[^>]*>[\s\S]*?<a\b[^>]*>([\s\S]*?)<\/a>/i);
    const conditionMatch = card.match(/<div\b[^>]*class\s*=\s*(["'])[^"']*\bjob_condition\b[^"']*\1[^>]*>[\s\S]*?<span\b[^>]*>([\s\S]*?)<\/span>/i);
    const postedAt = dateFromCard(card);
    seen.add(url);
    jobs.push({
      title,
      url,
      company: cleanHtml(companyMatch?.[2] || fallbackCompany),
      location: cleanHtml(conditionMatch?.[2] || ''),
      ...(postedAt != null ? { postedAt } : {}),
    });
  }
  return jobs;
}

function keywordsFrom(entry) {
  const raw = Array.isArray(entry.searchKeywords) ? entry.searchKeywords : [entry.searchKeywords || ''];
  const keywords = [...new Set(raw.map(v => String(v).trim()).filter(Boolean))];
  if (!keywords.length) throw new Error('saramin: configure searchKeywords in your local portals.yml');
  return keywords;
}

function boundedPages(value) {
  const n = Number(value);
  return Number.isInteger(n) && n >= 1 ? Math.min(n, 5) : 2;
}

/** @type {Provider} */
export default {
  id: 'saramin',

  detect() { return null; },

  async fetch(entry, ctx) {
    const byUrl = new Map();
    const maxPages = boundedPages(entry.maxPages);
    let firstRequest = true;
    let succeededOnce = false;
    for (const keyword of keywordsFrom(entry)) {
      for (let page = 1; page <= maxPages; page++) {
        if (firstRequest) firstRequest = false;
        else await sleep(INTER_REQUEST_DELAY_MS, ctx);
        const url = new URL(SEARCH_PATH, ORIGIN);
        url.searchParams.set('searchType', 'search');
        url.searchParams.set('searchword', keyword);
        url.searchParams.set('recruitPage', String(page));
        let html;
        try {
          html = await fetchTextWithRetry(ctx, url.href, {
            redirect: 'error',
            headers: {
              accept: 'text/html,application/xhtml+xml',
              'accept-language': 'ko-KR,ko;q=0.9,en;q=0.8',
              'user-agent': BROWSER_LIKE_USER_AGENT,
            },
          });
        } catch (err) {
          if (!succeededOnce) throw err;
          console.error(`  ⚠ saramin: keyword "${keyword}" page ${page} failed (${err.message}) — keeping the ${byUrl.size} jobs collected so far`);
          return [...byUrl.values()];
        }
        succeededOnce = true;
        const jobs = parseSaraminHtml(html, entry.name || 'Saramin');
        for (const job of jobs) byUrl.set(job.url, job);
        if (jobs.length === 0) break;
      }
    }
    return [...byUrl.values()];
  },
};
