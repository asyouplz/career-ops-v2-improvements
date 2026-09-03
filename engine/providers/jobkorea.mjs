// @ts-check
/** @typedef {import('./_types.js').Provider} Provider */

import { BROWSER_LIKE_USER_AGENT } from './_http.mjs';
import { decodeEntities } from './_html-entities.mjs';

const ORIGIN = 'https://www.jobkorea.co.kr';
const SEARCH_PATH = '/Search/';
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
  if (url.protocol !== 'https:' || url.hostname !== 'www.jobkorea.co.kr') return null;
  const match = url.pathname.match(/^\/Recruit\/GI_Read\/(\d+)\/?$/i);
  return match ? `${ORIGIN}/Recruit/GI_Read/${match[1]}` : null;
}

function cardChunks(html) {
  const source = String(html);
  const starts = [...source.matchAll(/<div\b[^>]*data-sentry-component\s*=\s*(["'])CardJob\1[^>]*>/gi)].map(m => m.index);
  return starts.map((start, index) => source.slice(start, starts[index + 1] ?? source.length));
}

function titleAnchor(card) {
  for (const match of card.matchAll(/<a\b([^>]*)>([\s\S]*?)<\/a>/gi)) {
    if (/\bdata-sentry-component\s*=\s*(["'])Title\1/i.test(match[1])) return { attrs: match[1], html: match[2] };
  }
  return null;
}

/**
 * Parse JobKorea's server-rendered search cards.
 * @param {string} html
 * @param {string} fallbackCompany
 */
export function parseJobKoreaHtml(html, fallbackCompany = 'JobKorea') {
  const jobs = [];
  const seen = new Set();
  for (const card of cardChunks(html)) {
    const anchor = titleAnchor(card);
    if (!anchor) continue;
    const url = canonicalPostingUrl(attr(anchor.attrs, 'href'));
    const title = cleanHtml(anchor.html);
    if (!url || !title || seen.has(url)) continue;

    const companyMatch = card.match(/<span\b[^>]*class\s*=\s*(["'])[^"']*\btext-gray700\b[^"']*\btext-typo-b2-16\b[^"']*\1[^>]*>([\s\S]*?)<\/span>/i);
    const locationMatch = card.match(/emoji--basicemoji-place2[\s\S]{0,1200}?<span\b[^>]*class\s*=\s*(["'])[^"']*\btext-typo-b4-14\b[^"']*\1[^>]*>([\s\S]*?)<\/span>/i);
    seen.add(url);
    jobs.push({
      title,
      url,
      company: cleanHtml(companyMatch?.[2] || fallbackCompany),
      location: cleanHtml(locationMatch?.[2] || ''),
    });
  }
  return jobs;
}

function keywordsFrom(entry) {
  const raw = Array.isArray(entry.searchKeywords) ? entry.searchKeywords : [entry.searchKeywords || ''];
  const keywords = [...new Set(raw.map(v => String(v).trim()).filter(Boolean))];
  if (!keywords.length) throw new Error('jobkorea: configure searchKeywords in your local portals.yml');
  return keywords;
}

function boundedPages(value) {
  const n = Number(value);
  return Number.isInteger(n) && n >= 1 ? Math.min(n, 5) : 2;
}

/** @type {Provider} */
export default {
  id: 'jobkorea',

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
        url.searchParams.set('stext', keyword);
        url.searchParams.set('Page_No', String(page));
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
          console.error(`  ⚠ jobkorea: keyword "${keyword}" page ${page} failed (${err.message}) — keeping the ${byUrl.size} jobs collected so far`);
          return [...byUrl.values()];
        }
        succeededOnce = true;
        const jobs = parseJobKoreaHtml(html, entry.name || 'JobKorea');
        for (const job of jobs) byUrl.set(job.url, job);
        if (jobs.length === 0) break;
      }
    }
    return [...byUrl.values()];
  },
};
