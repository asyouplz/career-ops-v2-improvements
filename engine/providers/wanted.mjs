// @ts-check
/** @typedef {import('./_types.js').Provider} Provider */

import { BROWSER_LIKE_USER_AGENT, fetchJsonWithRetry } from './_http.mjs';

const SEARCH_API = 'https://www.wanted.co.kr/api/chaos/search/v1/position';
const DETAIL_API = 'https://www.wanted.co.kr/api/v4/jobs';
const JOB_ORIGIN = 'https://www.wanted.co.kr';
const DEFAULT_PER_PAGE = 24;
const DEFAULT_MAX_PAGES = 1;
const DEFAULT_MAX_CANDIDATES = 50;
const SEARCH_DELAY_MS = 150;
const DETAIL_DELAY_MS = 100;
const ALLOWED_SORTS = new Set(['job.latest_order', 'job.recommend_order']);

function boundedInt(value, fallback, min, max) {
  const n = Number(value);
  return Number.isInteger(n) && n >= min ? Math.min(n, max) : fallback;
}

function sleep(ms, ctx) {
  if (typeof ctx?.sleep === 'function') return ctx.sleep(ms);
  return new Promise(resolve => setTimeout(resolve, ms));
}

function keywordsFrom(entry) {
  const raw = Array.isArray(entry.searchKeywords) ? entry.searchKeywords : [entry.searchKeywords || ''];
  const values = [...new Set(raw.map(value => String(value).trim()).filter(Boolean))];
  if (!values.length) throw new Error('wanted: configure searchKeywords in your local portals.yml');
  return values;
}

function locationsFrom(entry) {
  const raw = Array.isArray(entry.locationKeys) ? entry.locationKeys : [entry.locationKeys || ''];
  const values = [...new Set(raw.map(value => String(value).trim()).filter(Boolean))];
  return values;
}

function sortsFrom(entry) {
  const raw = Array.isArray(entry.sortModes) ? entry.sortModes : [entry.sortModes || 'job.latest_order'];
  const values = [...new Set(raw.map(value => String(value).trim()).filter(value => ALLOWED_SORTS.has(value)))];
  return values.length ? values : ['job.latest_order'];
}

export function buildWantedSearchUrl({ keyword, sort, locations, limit, offset }) {
  const url = new URL(SEARCH_API);
  url.searchParams.set('query', keyword);
  url.searchParams.set('sort', sort);
  url.searchParams.set('country', 'kr');
  for (const location of locations) url.searchParams.append('locations', location);
  url.searchParams.set('limit', String(limit));
  url.searchParams.set('offset', String(offset));
  return url.toString();
}

function detailText(detail) {
  return [detail?.intro, detail?.main_tasks, detail?.requirements, detail?.preferred_points]
    .filter(value => typeof value === 'string' && value.trim())
    .join('\n\n')
    .trim();
}

/** Normalize one public Wanted detail API response. */
export function normalizeWantedJob(payload, fallbackCompany = 'Wanted') {
  const item = payload?.job;
  if (!item || typeof item !== 'object') return null;
  const id = String(item.id ?? '').trim();
  const title = String(item.position || '').trim();
  const status = String(item.status || '').toLowerCase();
  if (!/^\d+$/.test(id) || !title || item.hidden === true || status !== 'active') return null;
  const company = String(item.company?.name || fallbackCompany).trim();
  const location = [...new Set([
    item.address?.location,
    item.address?.district,
    item.address?.full_location,
  ]
    .map(value => typeof value === 'string' ? value.trim() : '')
    .filter(Boolean))]
    .join(' ');
  const description = detailText(item.detail);
  return {
    title,
    url: `${JOB_ORIGIN}/wd/${id}`,
    company,
    location,
    ...(description ? { description } : {}),
  };
}

function requestOptions(referer) {
  return {
    redirect: 'error',
    headers: {
      accept: 'application/json',
      'accept-language': 'ko-KR,ko;q=0.9,en;q=0.8',
      referer,
      'user-agent': BROWSER_LIKE_USER_AGENT,
    },
  };
}

/** @type {Provider} */
export default {
  id: 'wanted',

  detect() { return null; },

  async fetch(entry, ctx) {
    const keywords = keywordsFrom(entry);
    const locations = locationsFrom(entry);
    const sorts = sortsFrom(entry);
    const perPage = boundedInt(entry.perPage, DEFAULT_PER_PAGE, 1, 50);
    const maxPages = boundedInt(entry.maxPages, DEFAULT_MAX_PAGES, 1, 3);
    const maxCandidates = boundedInt(entry.maxCandidates, DEFAULT_MAX_CANDIDATES, 1, 100);
    const listed = new Map();
    let searchRequests = 0;
    let succeededOnce = false;

    outer:
    for (const keyword of keywords) {
      for (const sort of sorts) {
        for (let page = 0; page < maxPages; page++) {
          if (searchRequests++) await sleep(SEARCH_DELAY_MS, ctx);
          const url = buildWantedSearchUrl({
            keyword,
            sort,
            locations,
            limit: perPage,
            offset: page * perPage,
          });
          let json;
          try {
            json = await fetchJsonWithRetry(ctx, url, requestOptions(`${JOB_ORIGIN}/search?query=${encodeURIComponent(keyword)}&tab=position`));
          } catch (err) {
            if (!succeededOnce) throw err;
            console.error(`  ⚠ wanted: keyword "${keyword}" page ${page + 1} failed (${err.message}) — keeping partial results`);
            break outer;
          }
          if (!Array.isArray(json?.data)) {
            if (!succeededOnce) throw new Error('wanted: unexpected search response (expected data[])');
            break outer;
          }
          succeededOnce = true;
          for (const item of json.data) {
            const id = String(item?.id ?? '').trim();
            const title = String(item?.position || '').trim();
            if (!/^\d+$/.test(id) || !title) continue;
            if (typeof ctx?.acceptTitle === 'function' && !ctx.acceptTitle(title)) continue;
            if (!listed.has(id)) listed.set(id, { id, title });
            if (listed.size >= maxCandidates) break outer;
          }
          if (json.data.length < perPage || !json?.links?.next) break;
        }
      }
    }

    const jobs = [];
    let detailIndex = 0;
    for (const { id } of listed.values()) {
      if (detailIndex++) await sleep(DETAIL_DELAY_MS, ctx);
      try {
        const payload = await fetchJsonWithRetry(
          ctx,
          `${DETAIL_API}/${id}`,
          requestOptions(`${JOB_ORIGIN}/wd/${id}`),
        );
        const job = normalizeWantedJob(payload, entry.name || 'Wanted');
        if (job) jobs.push(job);
      } catch (err) {
        console.error(`  ⚠ wanted: detail ${id} failed (${err.message}) — skipping this posting`);
      }
    }
    return jobs;
  },
};
