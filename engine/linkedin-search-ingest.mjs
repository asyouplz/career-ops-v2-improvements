#!/usr/bin/env node

/**
 * Normalize public search-index results that point to LinkedIn job pages and
 * append accepted candidates to the shared career-ops pipeline.
 *
 * This module never requests LinkedIn. A separate bounded search step provides
 * at most two queries / twenty result cards. Only company, role, and location
 * explicitly encoded in a result title are accepted; missing fields are never
 * inferred from snippets.
 */
import { existsSync, readFileSync } from 'fs';
import { resolve } from 'path';
import { fileURLToPath } from 'url';
import yaml from 'js-yaml';
import {
  appendToPipeline,
  appendToScanHistory,
  buildCompanyCanonicalizer,
  buildLocationFilter,
  buildTitleFilter,
  companyRoleDedupKey,
  loadSeenCompanyRoles,
  loadSeenUrls,
  normalizeUrlForDedup,
} from './scan.mjs';

const DEFAULT_PORTALS_PATH = process.env.CAREER_OPS_PORTALS || 'portals.yml';
const SOURCE = 'linkedin-site-search';
const MAX_QUERIES = 2;
const MAX_RESULTS = 20;

function cleanString(value, maxLength = 500) {
  if (typeof value !== 'string') return '';
  return value
    .replace(/[\r\n\t]+/g, ' ')
    .replace(/\s{2,}/g, ' ')
    .trim()
    .slice(0, maxLength);
}

/** Accept only an official HTTPS LinkedIn jobs/view identity URL. */
export function canonicalizeLinkedInJobUrl(value) {
  if (typeof value !== 'string' || value.trim() === '' || value.length > 2000) return null;
  let parsed;
  try {
    parsed = new URL(value.trim());
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
  return `https://www.linkedin.com/jobs/view/${idMatch[1]}`;
}

/** Parse structured search output while tolerating one Markdown JSON fence. */
export function parseLinkedInSearchPayload(text) {
  if (typeof text !== 'string' || text.trim() === '') {
    throw new Error('LinkedIn search output is empty');
  }
  const raw = text.trim();
  const candidates = [raw];
  const fenced = raw.match(/```(?:json)?\s*([\s\S]*?)```/i);
  if (fenced) candidates.push(fenced[1].trim());
  const firstBrace = raw.indexOf('{');
  const lastBrace = raw.lastIndexOf('}');
  if (firstBrace !== -1 && lastBrace > firstBrace) {
    candidates.push(raw.slice(firstBrace, lastBrace + 1));
  }

  let lastError = null;
  for (const candidate of candidates) {
    try {
      const parsed = JSON.parse(candidate);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error('top-level JSON must be an object');
      }
      if (parsed.status !== 'ok') {
        throw new Error(`search status is ${JSON.stringify(parsed.status)}`);
      }
      if (!Number.isInteger(parsed.query_count) || parsed.query_count < 0 || parsed.query_count > MAX_QUERIES) {
        throw new Error(`query_count must be an integer from 0 to ${MAX_QUERIES}`);
      }
      if (!Array.isArray(parsed.results)) throw new Error('results must be an array');
      if (parsed.results.length > MAX_RESULTS) {
        throw new Error(`results exceeds the ${MAX_RESULTS}-item safety limit`);
      }
      return parsed;
    } catch (err) {
      lastError = err;
    }
  }
  throw new Error(`Invalid LinkedIn search JSON: ${lastError?.message || 'parse failed'}`);
}

function linkedInCompanySlug(value) {
  if (typeof value !== 'string' || value.length > 2000) return null;
  let parsed;
  try {
    parsed = new URL(value.trim());
  } catch {
    return null;
  }
  let segment;
  try {
    const match = parsed.pathname.match(/^\/jobs\/view\/([^/]+)\/?$/i);
    if (!match) return null;
    segment = decodeURIComponent(match[1]);
  } catch {
    return null;
  }
  const stem = segment.replace(/-\d{6,}$/, '');
  const atIndex = stem.lastIndexOf('-at-');
  if (atIndex <= 0 || atIndex + 4 >= stem.length) return null;
  return stem.slice(atIndex + 4);
}

function slugComparable(value) {
  return cleanString(value, 500)
    .normalize('NFKC')
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, '');
}

function splitCompanyPrefix(prefix, companySlug) {
  const target = slugComparable(companySlug);
  if (!target) return null;
  let accumulated = '';
  for (let index = 0; index < prefix.length; index++) {
    accumulated += slugComparable(prefix[index]);
    if (accumulated === target) {
      const company = prefix.slice(0, index + 1).trim();
      const title = prefix.slice(index + 1).trim();
      return company && title ? { company, title } : null;
    }
    if (!target.startsWith(accumulated)) return null;
  }
  return null;
}

/**
 * Parse only title shapes whose company, job title, and location boundaries are
 * explicit. The description/snippet is intentionally ignored for field
 * extraction because it is often stale, truncated, or unrelated.
 */
export function parseLinkedInSearchResult(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const url = canonicalizeLinkedInJobUrl(raw.url);
  const resultTitle = cleanString(raw.title, 500);
  if (!url || !resultTitle) return null;

  let company = '';
  let title = '';
  let location = '';

  const english = resultTitle.match(
    /^(.+?)\s+(?:is\s+)?(?:hiring|seeking|recruiting)\s+(.+?)\s+in\s+(.+?)\s*\|\s*LinkedIn$/i,
  );
  if (english) {
    [, company, title, location] = english;
  } else {
    const korean = resultTitle.match(
      /^(.+)\(([^()]+)\)\s+채용\s*중\s*\|\s*LinkedIn$/i,
    );
    if (korean) {
      const split = splitCompanyPrefix(korean[1], linkedInCompanySlug(raw.url));
      if (split) {
        ({ company, title } = split);
        location = korean[2];
      }
    }
  }

  company = cleanString(company, 200);
  title = cleanString(title, 300);
  location = cleanString(location, 200);
  if (!company || !title || !location) return null;
  return { url, company, title, location, source: SOURCE };
}

export function normalizeLinkedInSearchResults(
  payload,
  config = {},
  {
    seenUrls = new Set(),
    seenCompanyRoles = new Set(),
    canonicalizeCompany = buildCompanyCanonicalizer(config.company_aliases),
  } = {},
) {
  if (
    !payload || payload.status !== 'ok' || !Number.isInteger(payload.query_count) ||
    payload.query_count < 0 || payload.query_count > MAX_QUERIES ||
    !Array.isArray(payload.results) || payload.results.length > MAX_RESULTS
  ) {
    throw new Error('LinkedIn search payload violates the bounded search contract');
  }

  const titleFilter = buildTitleFilter(config.title_filter);
  const locationFilter = buildLocationFilter(config.location_filter);
  const offers = [];
  const stats = {
    queryCount: payload.query_count,
    received: payload.results.length,
    accepted: 0,
    invalid: 0,
    filteredTitle: 0,
    filteredLocation: 0,
    duplicateUrl: 0,
    duplicateRole: 0,
  };

  for (const raw of payload.results) {
    const offer = parseLinkedInSearchResult(raw);
    if (!offer) {
      stats.invalid++;
      continue;
    }
    if (!titleFilter(offer.title)) {
      stats.filteredTitle++;
      continue;
    }
    if (!locationFilter(offer.location, offer.url, offer.title)) {
      stats.filteredLocation++;
      continue;
    }

    const urlKey = normalizeUrlForDedup(offer.url);
    if (seenUrls.has(urlKey)) {
      stats.duplicateUrl++;
      continue;
    }
    const roleKey = companyRoleDedupKey(offer.company, offer.title, canonicalizeCompany);
    if (seenCompanyRoles.has(roleKey)) {
      stats.duplicateRole++;
      continue;
    }

    offers.push(offer);
    seenUrls.add(urlKey);
    seenCompanyRoles.add(roleKey);
  }

  stats.accepted = offers.length;
  return { offers, stats };
}

function valueOf(args, flag) {
  const exact = args.indexOf(flag);
  if (exact !== -1) return args[exact + 1];
  const prefix = `${flag}=`;
  const inline = args.find((arg) => arg.startsWith(prefix));
  return inline ? inline.slice(prefix.length) : null;
}

function scanHistoryPolicy(config = {}) {
  const parsed = Number.parseInt(config.scan_history?.recheck_after_days, 10);
  return { recheckAfterDays: Number.isFinite(parsed) && parsed >= 0 ? parsed : null };
}

async function main() {
  const args = process.argv.slice(2);
  const inputPath = valueOf(args, '--input');
  const dryRun = args.includes('--dry-run');
  if (!inputPath) {
    console.error('Usage: node linkedin-search-ingest.mjs --input <search-output.json> [--dry-run]');
    process.exitCode = 2;
    return;
  }

  const portalsPath = resolve(DEFAULT_PORTALS_PATH);
  if (!existsSync(portalsPath)) throw new Error(`portals config not found: ${portalsPath}`);
  const config = yaml.load(readFileSync(portalsPath, 'utf-8')) || {};
  const payload = parseLinkedInSearchPayload(readFileSync(resolve(inputPath), 'utf-8'));
  const policy = scanHistoryPolicy(config);
  const canonicalizeCompany = buildCompanyCanonicalizer(config.company_aliases);
  const { seen } = loadSeenUrls(policy);
  const seenCompanyRoles = loadSeenCompanyRoles(
    'data/applications.md',
    canonicalizeCompany,
    { policy },
  );
  const result = normalizeLinkedInSearchResults(payload, config, {
    seenUrls: seen,
    seenCompanyRoles,
    canonicalizeCompany,
  });

  if (!dryRun && result.offers.length > 0) {
    await appendToPipeline(result.offers);
    appendToScanHistory(result.offers, new Date().toISOString().slice(0, 10));
  }
  console.log(JSON.stringify({
    status: 'ok',
    source: SOURCE,
    dryRun,
    ...result.stats,
    added: dryRun ? 0 : result.offers.length,
  }));
}

const isMain = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  main().catch((err) => {
    console.error(`LinkedIn site-search ingest failed: ${err.message}`);
    process.exitCode = 1;
  });
}
