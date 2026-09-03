#!/usr/bin/env node

/**
 * Bounded, anonymous LinkedIn public-jobs collector.
 *
 * The collector opens only configured public search pages and a small number of
 * official job detail pages. It never logs in, stores cookies, submits forms, or
 * follows application links. When public pages are unavailable, the caller can
 * fall back to the existing DDGS collector.
 */

import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const MODULE_DIR = dirname(fileURLToPath(import.meta.url));
const STAGING_ROOT = resolve(MODULE_DIR, '..');
const DEFAULT_CONFIG = join(STAGING_ROOT, 'config', 'linkedin_queries.json');
const DEFAULT_PROJECT_ROOT = process.env.CAREER_OPS_PROJECT_ROOT || join(STAGING_ROOT, 'engine');
const PUBLIC_SEARCH_BASE = 'https://www.linkedin.com/jobs/search/';
const PUBLIC_JOB_BASE = 'https://www.linkedin.com/jobs/view/';

function clean(value, maxLength = 1000) {
  if (typeof value !== 'string') return '';
  return value
    .normalize('NFKC')
    .replace(/[\r\n\t]+/g, ' ')
    .replace(/\s{2,}/g, ' ')
    .trim()
    .slice(0, maxLength);
}

function comparable(value) {
  return clean(value, 2000).toLocaleLowerCase('en-US');
}

function compactComparable(value) {
  return [...comparable(value)].filter(character => /[\p{L}\p{N}]/u.test(character)).join('');
}

function hasAny(value, terms = []) {
  const haystack = comparable(value);
  const compactHaystack = compactComparable(value);
  return terms.some((term) => {
    const needle = comparable(term);
    const compactNeedle = compactComparable(term);
    return haystack.includes(needle)
      || (compactNeedle.length >= 4 && compactHaystack.includes(compactNeedle));
  });
}

function boundedInteger(value, fallback, min, max) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(min, Math.min(max, parsed));
}

export function canonicalizeLinkedInJobUrl(value) {
  if (typeof value !== 'string' || value.trim() === '' || value.length > 3000) return null;
  let parsed;
  try {
    parsed = new URL(value.trim(), 'https://www.linkedin.com');
  } catch {
    return null;
  }
  if (parsed.protocol !== 'https:') return null;
  const host = parsed.hostname.toLowerCase().replace(/\.$/, '');
  if (host !== 'linkedin.com' && !host.endsWith('.linkedin.com')) return null;
  const match = parsed.pathname.match(/^\/jobs\/view\/([^/]+)\/?$/i);
  if (!match) return null;
  let segment;
  try {
    segment = decodeURIComponent(match[1]);
  } catch {
    return null;
  }
  const idMatch = segment.match(/(?:^|-)(\d{6,})$/);
  if (!idMatch) return null;
  return `${PUBLIC_JOB_BASE}${idMatch[1]}`;
}

export function linkedInJobId(value) {
  const canonical = canonicalizeLinkedInJobUrl(value);
  return canonical ? canonical.slice(PUBLIC_JOB_BASE.length) : null;
}

export function buildSearchUrl(query) {
  const keywords = clean(query?.keywords, 500);
  const location = clean(query?.location, 300);
  if (!keywords || !location) throw new Error('Each LinkedIn query requires keywords and location');
  const url = new URL(PUBLIC_SEARCH_BASE);
  url.searchParams.set('keywords', keywords);
  url.searchParams.set('location', location);
  url.searchParams.set('position', '1');
  url.searchParams.set('pageNum', '0');
  return url.toString();
}

export function normalizeSearchRecord(raw, queryName = '') {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const url = canonicalizeLinkedInJobUrl(raw.url || raw.href);
  const title = clean(raw.title, 300);
  const company = clean(raw.company, 200);
  const location = clean(raw.location, 200);
  if (!url || !title || !company || !location) return null;
  return {
    job_id: linkedInJobId(url),
    url,
    title,
    company,
    location,
    listed_at: clean(raw.listed_at || raw.posted_at, 100) || null,
    query_name: clean(queryName || raw.query_name, 120) || null,
    source: 'linkedin-direct-public',
  };
}

export function listedAtEpoch(value, now = new Date()) {
  const text = clean(value, 100).toLocaleLowerCase('en-US');
  if (!text) return 0;
  const reference = now instanceof Date && Number.isFinite(now.getTime()) ? now : new Date();
  if (/^20\d{2}-\d{2}-\d{2}(?:[t\s].*)?$/.test(text)) {
    const parsed = Date.parse(text);
    return Number.isFinite(parsed) ? parsed : 0;
  }
  if (/^(?:오늘|방금(?:\s*전)?|today|just now)$/.test(text)) return reference.getTime();
  const relative = text.match(/^(\d{1,3})\s*(분|시간|일|주|minute|minutes|hour|hours|day|days|week|weeks)\s*(?:전|ago)$/);
  if (!relative) return 0;
  const units = {
    '분': 60_000,
    minute: 60_000,
    minutes: 60_000,
    '시간': 3_600_000,
    hour: 3_600_000,
    hours: 3_600_000,
    '일': 86_400_000,
    day: 86_400_000,
    days: 86_400_000,
    '주': 604_800_000,
    week: 604_800_000,
    weeks: 604_800_000,
  };
  return reference.getTime() - Number(relative[1]) * units[relative[2]];
}

export function scoreCandidate(record, config) {
  const title = comparable(record?.title);
  let score = 0;
  for (const [index, term] of (config.priority_terms || []).entries()) {
    if (title.includes(comparable(term))) score += Math.max(1, 30 - index);
  }
  if (title.includes('manager') || title.includes('팀장')) score += 4;
  if (record?.listed_at) score += 1;
  return score;
}

export function filterAndRankSearchRecords(records, config, options = {}) {
  const maxCards = boundedInteger(
    options.maxCards,
    config?.limits?.max_cards_per_query || 25,
    1,
    100,
  );
  const accepted = [];
  const externalSeenUrls = new Set(
    [...(options.seenUrls || [])]
      .map((value) => canonicalizeLinkedInJobUrl(value))
      .filter(Boolean),
  );
  const seenUrls = new Set();
  const seenRoles = new Set();
  const overrideableExclusions = new Set(
    (config.scope_override_excluded_terms || []).map(comparable),
  );
  const hardExclusions = (config.excluded_title_terms || [])
    .filter(term => !overrideableExclusions.has(comparable(term)));
  const stats = {
    received: 0,
    invalid: 0,
    filtered_title: 0,
    filtered_location: 0,
    duplicate_url: 0,
    duplicate_role: 0,
    previously_seen: 0,
    accepted: 0,
  };

  for (const raw of records || []) {
    stats.received += 1;
    const record = normalizeSearchRecord(raw, raw?.query_name);
    if (!record) {
      stats.invalid += 1;
      continue;
    }
    const title = record.title;
    if (!hasAny(title, config.positive_title_terms || [])) {
      stats.filtered_title += 1;
      continue;
    }
    if (hasAny(title, hardExclusions)) {
      stats.filtered_title += 1;
      continue;
    }
    if (
      hasAny(title, config.scope_override_excluded_terms || [])
      && !hasAny(title, config.scope_override_title_terms || [])
    ) {
      stats.filtered_title += 1;
      continue;
    }
    if (!hasAny(record.location, config.allowed_locations || [])) {
      stats.filtered_location += 1;
      continue;
    }
    if (externalSeenUrls.has(record.url)) {
      stats.previously_seen += 1;
      continue;
    }
    if (seenUrls.has(record.url)) {
      stats.duplicate_url += 1;
      continue;
    }
    const roleKey = `${comparable(record.company)}\u0000${comparable(record.title)}`;
    if (seenRoles.has(roleKey)) {
      stats.duplicate_role += 1;
      continue;
    }
    seenUrls.add(record.url);
    seenRoles.add(roleKey);
    accepted.push({
      ...record,
      freshness_epoch: listedAtEpoch(record.listed_at, options.now),
      priority_score: scoreCandidate(record, config),
    });
  }

  accepted.sort((left, right) => (
    right.freshness_epoch - left.freshness_epoch
    || right.priority_score - left.priority_score
    || String(left.company).localeCompare(String(right.company), 'ko')
    || String(left.title).localeCompare(String(right.title), 'ko')
  ));
  const bounded = accepted.slice(0, maxCards);
  stats.accepted = bounded.length;
  return { candidates: bounded, stats };
}

function challengeDetected(status, title, bodyText) {
  if ([429, 999].includes(status)) return true;
  const sample = `${title || ''}\n${bodyText || ''}`.slice(0, 8000);
  return /captcha|security verification|보안 확인|challenge page|unusual activity/i.test(sample);
}

function expiredDetected(bodyText) {
  const sample = String(bodyText || '').slice(0, 20000);
  return /no longer accepting applications|job is no longer available|채용이 마감|지원 접수.*마감|공고가 종료/i.test(sample);
}

async function loadPlaywright(projectRoot) {
  const candidates = [
    process.env.PLAYWRIGHT_MODULE,
    pathToFileURL(join(projectRoot, 'node_modules', 'playwright', 'index.mjs')).href,
    'playwright',
  ].filter(Boolean);
  let lastError = null;
  for (const candidate of candidates) {
    try {
      return await import(candidate);
    } catch (error) {
      lastError = error;
    }
  }
  throw new Error(`Unable to import Playwright: ${lastError?.message || 'module not found'}`);
}

async function extractSearchPage(page, query, limits) {
  const started = Date.now();
  const url = buildSearchUrl(query);
  let response;
  try {
    response = await page.goto(url, {
      waitUntil: 'domcontentloaded',
      timeout: limits.navigation_timeout_ms,
    });
    await page.waitForTimeout(limits.settle_ms);
  } catch (error) {
    return {
      ok: false,
      query_name: clean(query.name, 120),
      url,
      elapsed_ms: Date.now() - started,
      error: clean(String(error), 500),
      records: [],
    };
  }

  const pageData = await page.evaluate((maxCards) => {
    const bodyText = document.body?.innerText || '';
    const anchors = [...document.querySelectorAll(
      'a.base-card__full-link[href*="/jobs/view/"], a[href*="/jobs/view/"]',
    )];
    const records = [];
    const seen = new Set();
    for (const anchor of anchors) {
      const href = anchor.href || anchor.getAttribute('href') || '';
      const hrefKey = href.split('?')[0];
      if (!hrefKey || seen.has(hrefKey)) continue;
      const card = anchor.closest(
        '.base-search-card, .job-search-card, .base-card, li',
      ) || anchor.parentElement;
      if (!card) continue;
      const pick = (selectors) => {
        for (const selector of selectors) {
          const value = card.querySelector(selector)?.textContent?.trim();
          if (value) return value;
        }
        return '';
      };
      const title = pick([
        'h3.base-search-card__title',
        '.base-search-card__title',
        '.job-search-card__title',
        'h3',
      ]) || anchor.textContent?.trim() || '';
      const company = pick([
        'h4.base-search-card__subtitle',
        '.base-search-card__subtitle',
        '.job-search-card__subtitle',
        'h4',
      ]);
      const location = pick([
        '.job-search-card__location',
        '.base-search-card__metadata',
      ]);
      const listedAt = card.querySelector('time')?.getAttribute('datetime')
        || card.querySelector('time')?.textContent?.trim()
        || '';
      if (!title || !company || !location) continue;
      seen.add(hrefKey);
      records.push({ href, title, company, location, listed_at: listedAt });
      if (records.length >= maxCards) break;
    }
    return {
      title: document.title,
      body_sample: bodyText.slice(0, 8000),
      records,
      login_prompt: /sign in|로그인|join now|지금 가입/i.test(bodyText.slice(0, 8000)),
      explicit_no_results: /no matching jobs|검색 결과가 없습니다|0\s+jobs?/i.test(bodyText.slice(0, 8000)),
    };
  }, limits.max_cards_per_query);

  const status = response?.status() ?? null;
  const challenge = challengeDetected(status, pageData.title, pageData.body_sample);
  const ok = !challenge && (pageData.records.length > 0 || pageData.explicit_no_results);
  return {
    ok,
    query_name: clean(query.name, 120),
    url,
    final_url: page.url(),
    http_status: status,
    elapsed_ms: Date.now() - started,
    record_count: pageData.records.length,
    login_prompt: pageData.login_prompt,
    challenge,
    explicit_no_results: pageData.explicit_no_results,
    error: ok ? null : challenge ? 'LinkedIn challenge or rate limit detected' : 'No structured public job cards found',
    records: pageData.records.map((record) => ({ ...record, query_name: clean(query.name, 120) })),
  };
}

async function extractDetailPage(page, candidate, limits) {
  const started = Date.now();
  let response;
  try {
    response = await page.goto(candidate.url, {
      waitUntil: 'domcontentloaded',
      timeout: limits.navigation_timeout_ms,
    });
    await page.waitForTimeout(limits.settle_ms);
  } catch (error) {
    return {
      ...candidate,
      direct_verified: false,
      liveness: 'uncertain',
      verification_error: clean(String(error), 500),
      detail_elapsed_ms: Date.now() - started,
    };
  }

  const pageData = await page.evaluate((descriptionChars) => {
    const text = document.body?.innerText || '';
    const pick = (selectors) => {
      for (const selector of selectors) {
        const value = document.querySelector(selector)?.textContent?.trim();
        if (value) return value;
      }
      return '';
    };
    const description = pick([
      '.show-more-less-html__markup',
      '.description__text',
      '.jobs-description__content',
      '[class*="description"]',
    ]);
    return {
      document_title: document.title,
      body_sample: text.slice(0, 20000),
      title: pick(['h1', '.top-card-layout__title', '.topcard__title']),
      company: pick([
        '.topcard__org-name-link',
        '.top-card-layout__card a',
        '.sub-nav-cta__optional-url',
      ]),
      location: pick([
        '.topcard__flavor--bullet',
        '.top-card-layout__second-subline span',
        '.sub-nav-cta__meta-text',
      ]),
      description: description.slice(0, descriptionChars),
      description_length: description.length,
      login_prompt: /sign in|로그인|join now|지금 가입/i.test(text.slice(0, 8000)),
    };
  }, limits.description_chars);

  const status = response?.status() ?? null;
  const challenge = challengeDetected(status, pageData.document_title, pageData.body_sample);
  const expired = [404, 410].includes(status) || expiredDetected(pageData.body_sample);
  const active = !challenge && !expired && Boolean(pageData.title) && pageData.description_length >= 80;
  const liveness = expired ? 'expired' : active ? 'active' : 'uncertain';
  return {
    ...candidate,
    title: clean(pageData.title, 300) || candidate.title,
    company: clean(pageData.company, 200) || candidate.company,
    location: clean(pageData.location, 200) || candidate.location,
    description: clean(pageData.description, limits.description_chars),
    description_original_chars: pageData.description_length,
    http_status: status,
    final_url: page.url(),
    direct_verified: active,
    liveness,
    login_prompt: pageData.login_prompt,
    challenge,
    verification_error: challenge ? 'LinkedIn challenge or rate limit detected' : null,
    detail_elapsed_ms: Date.now() - started,
    verified_at: new Date().toISOString(),
  };
}

function limitsFromConfig(config, overrides = {}) {
  const raw = config.limits || {};
  return {
    max_queries: boundedInteger(overrides.maxQueries, raw.max_queries || 2, 1, 4),
    max_cards_per_query: boundedInteger(
      overrides.maxCards,
      raw.max_cards_per_query || 25,
      1,
      50,
    ),
    max_detail_pages: boundedInteger(
      overrides.maxDetails,
      raw.max_detail_pages || 5,
      0,
      5,
    ),
    description_chars: boundedInteger(raw.description_chars, 1800, 200, 4000),
    navigation_timeout_ms: boundedInteger(raw.navigation_timeout_ms, 30000, 5000, 60000),
    settle_ms: boundedInteger(raw.settle_ms, 1200, 0, 5000),
  };
}

export function validateConfig(config) {
  if (!config || typeof config !== 'object' || Array.isArray(config)) {
    throw new Error('LinkedIn config must be an object');
  }
  if (!Array.isArray(config.queries) || config.queries.length === 0 || config.queries.length > 4) {
    throw new Error('LinkedIn config requires between one and four queries');
  }
  for (const query of config.queries) buildSearchUrl(query);
  for (const key of [
    'allowed_locations',
    'positive_title_terms',
    'priority_terms',
    'excluded_title_terms',
  ]) {
    if (!Array.isArray(config[key])) throw new Error(`${key} must be an array`);
  }
  for (const key of ['scope_override_title_terms', 'scope_override_excluded_terms']) {
    if (config[key] != null && !Array.isArray(config[key])) {
      throw new Error(`${key} must be an array when provided`);
    }
  }
  return config;
}

export async function fetchDirectLinkedIn(config, options = {}) {
  if (!options.projectRoot && !DEFAULT_PROJECT_ROOT) {
    throw new Error('--project-root or CAREER_OPS_PROJECT_ROOT must point to a separately installed Career-Ops directory');
  }
  validateConfig(config);
  const limits = limitsFromConfig(config, options);
  const queries = config.queries.slice(0, limits.max_queries);
  const projectRoot = resolve(options.projectRoot || DEFAULT_PROJECT_ROOT);
  const { chromium } = await loadPlaywright(projectRoot);
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    locale: 'ko-KR',
    userAgent: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/136.0.0.0 Safari/537.36',
  });
  const page = await context.newPage();
  const queryResults = [];
  const rawRecords = [];
  const started = Date.now();
  try {
    for (const query of queries) {
      const result = await extractSearchPage(page, query, limits);
      queryResults.push({ ...result, records: undefined });
      rawRecords.push(...result.records);
    }
    const successfulQueries = queryResults.filter((result) => result.ok).length;
    if (successfulQueries === 0) {
      return {
        schema_version: 1,
        status: 'unavailable',
        source: 'linkedin-direct-public',
        fallback_required: true,
        elapsed_ms: Date.now() - started,
        query_results: queryResults,
        filter_stats: null,
        candidates: [],
      };
    }

    const filtered = filterAndRankSearchRecords(rawRecords, config, {
      maxCards: limits.max_cards_per_query * limits.max_queries,
      seenUrls: options.seenUrls,
      now: options.now,
    });
    const details = [];
    let detailStopReason = null;
    for (const candidate of filtered.candidates.slice(0, limits.max_detail_pages)) {
      const detail = await extractDetailPage(page, candidate, limits);
      details.push(detail);
      if (detail.challenge) {
        detailStopReason = 'challenge_or_rate_limit';
        break;
      }
    }
    const detailByUrl = new Map(details.map((item) => [item.url, item]));
    const candidates = filtered.candidates.map((item) => detailByUrl.get(item.url) || {
      ...item,
      direct_verified: false,
      liveness: 'not_checked',
      description: '',
    });
    return {
      schema_version: 1,
      status: successfulQueries === queryResults.length ? 'ok' : 'partial',
      source: 'linkedin-direct-public',
      fallback_required: false,
      elapsed_ms: Date.now() - started,
      limits,
      query_results: queryResults,
      filter_stats: filtered.stats,
      detail_stop_reason: detailStopReason,
      candidates,
    };
  } finally {
    await context.close();
    await browser.close();
  }
}


function parseArgs(argv) {
  const options = {
    config: DEFAULT_CONFIG,
    projectRoot: DEFAULT_PROJECT_ROOT,
    output: null,
    pretty: false,
    maxDetails: undefined,
    seenUrls: null,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '--config') options.config = argv[++index];
    else if (arg.startsWith('--config=')) options.config = arg.slice('--config='.length);
    else if (arg === '--project-root') options.projectRoot = argv[++index];
    else if (arg.startsWith('--project-root=')) options.projectRoot = arg.slice('--project-root='.length);
    else if (arg === '--output') options.output = argv[++index];
    else if (arg.startsWith('--output=')) options.output = arg.slice('--output='.length);
    else if (arg === '--max-details') options.maxDetails = argv[++index];
    else if (arg.startsWith('--max-details=')) options.maxDetails = arg.slice('--max-details='.length);
    else if (arg === '--seen-urls') options.seenUrls = argv[++index];
    else if (arg.startsWith('--seen-urls=')) options.seenUrls = arg.slice('--seen-urls='.length);
    else if (arg === '--pretty') options.pretty = true;
    else if (arg === '--help' || arg === '-h') options.help = true;
    else throw new Error(`Unknown argument: ${arg}`);
  }
  return options;
}

function usage() {
  return [
    'Usage: node linkedin_direct_fetch.mjs [options]',
    '  --config <path>         query/filter JSON',
    '  --project-root <path>   Career-Ops project containing Playwright',
    '  --max-details <0..5>    detail-page bound',
    '  --seen-urls <path>       JSON array or newline list excluded before detail checks',
    '  --output <path>         also write result JSON',
    '  --pretty                pretty-print JSON',
  ].join('\n');
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.help) {
    console.log(usage());
    return;
  }
  const configPath = isAbsolute(options.config) ? options.config : resolve(options.config);
  if (!existsSync(configPath)) throw new Error(`Config not found: ${configPath}`);
  const config = JSON.parse(readFileSync(configPath, 'utf8'));
  if (options.seenUrls) {
    const rawSeen = readFileSync(resolve(options.seenUrls), 'utf8');
    try {
      const parsedSeen = JSON.parse(rawSeen);
      options.seenUrls = Array.isArray(parsedSeen) ? parsedSeen : [];
    } catch {
      options.seenUrls = rawSeen.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    }
  } else {
    options.seenUrls = [];
  }
  const result = await fetchDirectLinkedIn(config, options);
  const json = JSON.stringify(result, null, options.pretty ? 2 : 0);
  if (options.output) writeFileSync(resolve(options.output), `${json}\n`, 'utf8');
  console.log(json);
}

const isMain = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  main().catch((error) => {
    console.error(JSON.stringify({
      schema_version: 1,
      status: 'error',
      source: 'linkedin-direct-public',
      fallback_required: true,
      error: clean(error?.message || String(error), 500),
      candidates: [],
    }));
    process.exitCode = 1;
  });
}
