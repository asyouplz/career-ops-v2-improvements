#!/usr/bin/env node

/**
 * Convert directly verified LinkedIn candidates into Career-Ops offers.
 *
 * Dry-run is the default. A real write requires both --apply and the explicit
 * CAREER_OPS_V2_ENABLE_APPLY=1 guard, which will remain disabled during staging.
 */

import { existsSync, readFileSync } from 'node:fs';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const MODULE_DIR = dirname(fileURLToPath(import.meta.url));
const STAGING_ROOT = resolve(MODULE_DIR, '..');
const DEFAULT_PROJECT_ROOT = process.env.CAREER_OPS_PROJECT_ROOT || '';

function clean(value, maxLength = 1000) {
  if (typeof value !== 'string') return '';
  return value
    .normalize('NFKC')
    .replace(/[\r\n\t]+/g, ' ')
    .replace(/\s{2,}/g, ' ')
    .trim()
    .slice(0, maxLength);
}

export function directCandidateToOffer(candidate) {
  if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) return null;
  if (candidate.direct_verified !== true || candidate.liveness !== 'active') return null;
  const url = clean(candidate.url, 2000);
  const title = clean(candidate.title, 300);
  const company = clean(candidate.company, 200);
  const location = clean(candidate.location, 200);
  const postedAt = Number(candidate.freshness_epoch);
  if (!/^https:\/\/www\.linkedin\.com\/jobs\/view\/\d{6,}$/.test(url)) return null;
  if (!title || !company || !location) return null;
  return {
    url,
    title,
    company,
    location,
    source: 'linkedin-direct-public',
    note: 'LinkedIn public page directly verified',
    ...(Number.isFinite(postedAt) && postedAt > 0 ? { postedAt } : {}),
  };
}

export function eligibleDirectOffers(payload) {
  if (!payload || !Array.isArray(payload.candidates)) return [];
  const offers = [];
  const seen = new Set();
  for (const candidate of payload.candidates) {
    const offer = directCandidateToOffer(candidate);
    if (!offer || seen.has(offer.url)) continue;
    seen.add(offer.url);
    offers.push(offer);
  }
  return offers;
}

async function loadProductionModules(projectRoot) {
  const scan = await import(pathToFileURL(join(projectRoot, 'scan.mjs')).href);
  const yamlModule = await import(pathToFileURL(join(projectRoot, 'node_modules', 'js-yaml', 'index.js')).href);
  return { scan, yaml: yamlModule.default || yamlModule };
}

function parseArgs(argv) {
  const options = {
    input: null,
    projectRoot: DEFAULT_PROJECT_ROOT,
    apply: false,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '--input') options.input = argv[++index];
    else if (arg.startsWith('--input=')) options.input = arg.slice('--input='.length);
    else if (arg === '--project-root') options.projectRoot = argv[++index];
    else if (arg.startsWith('--project-root=')) options.projectRoot = arg.slice('--project-root='.length);
    else if (arg === '--apply') options.apply = true;
    else if (arg === '--dry-run') options.apply = false;
    else if (arg === '--help' || arg === '-h') options.help = true;
    else throw new Error(`Unknown argument: ${arg}`);
  }
  return options;
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.help) {
    console.log('Usage: node linkedin_direct_ingest.mjs --project-root <path> --input result.json [--dry-run|--apply]');
    return;
  }
  if (!options.projectRoot) {
    throw new Error('--project-root or CAREER_OPS_PROJECT_ROOT must point to a separately installed Career-Ops directory');
  }
  if (!options.input) throw new Error('--input is required');
  const inputPath = isAbsolute(options.input) ? options.input : resolve(options.input);
  if (!existsSync(inputPath)) throw new Error(`Input not found: ${inputPath}`);
  if (options.apply && process.env.CAREER_OPS_V2_ENABLE_APPLY !== '1') {
    throw new Error('Apply mode is locked; CAREER_OPS_V2_ENABLE_APPLY=1 is required');
  }

  const projectRoot = resolve(options.projectRoot);
  const payload = JSON.parse(readFileSync(inputPath, 'utf8'));
  const inputOffers = eligibleDirectOffers(payload);
  const { scan, yaml } = await loadProductionModules(projectRoot);
  const portalsPath = join(projectRoot, 'portals.yml');
  const config = yaml.load(readFileSync(portalsPath, 'utf8')) || {};
  const titleFilter = scan.buildTitleFilter(config.title_filter);
  const locationFilter = scan.buildLocationFilter(config.location_filter);
  const canonicalizeCompany = scan.buildCompanyIdentityCanonicalizer(config.company_aliases);
  const historyDays = Number.parseInt(config.scan_history?.recheck_after_days, 10);
  const policy = { recheckAfterDays: Number.isFinite(historyDays) && historyDays >= 0 ? historyDays : null };

  const previousCwd = process.cwd();
  process.chdir(projectRoot);
  try {
    const { seen } = scan.loadSeenUrls(policy);
    const seenCompanyRoles = scan.loadSeenCompanyRoles(
      'data/applications.md',
      canonicalizeCompany,
      { policy, roleNormalizer: scan.normalizeRoleForIdentity },
    );
    const stats = {
      received: inputOffers.length,
      filtered_title: 0,
      filtered_location: 0,
      duplicate_url: 0,
      duplicate_role: 0,
      accepted: 0,
      added: 0,
    };
    const accepted = [];
    for (const offer of inputOffers) {
      if (!titleFilter(offer.title)) {
        stats.filtered_title += 1;
        continue;
      }
      if (!locationFilter(offer.location, offer.url, offer.title)) {
        stats.filtered_location += 1;
        continue;
      }
      const urlKey = scan.normalizeUrlForDedup(offer.url);
      if (seen.has(urlKey)) {
        stats.duplicate_url += 1;
        continue;
      }
      const roleKey = scan.companyRoleIdentityKey(offer.company, offer.title, canonicalizeCompany);
      if (seenCompanyRoles.has(roleKey)) {
        stats.duplicate_role += 1;
        continue;
      }
      seen.add(urlKey);
      seenCompanyRoles.add(roleKey);
      offer.companyKey = canonicalizeCompany(offer.company);
      offer.roleKey = scan.normalizeRoleForIdentity(offer.title);
      accepted.push(offer);
    }
    stats.accepted = accepted.length;
    if (options.apply && accepted.length > 0) {
      await scan.appendToPipeline(accepted);
      scan.appendToScanHistory(accepted, new Date().toISOString().slice(0, 10));
      stats.added = accepted.length;
    }
    console.log(JSON.stringify({
      schema_version: 1,
      status: 'ok',
      source: 'linkedin-direct-public',
      dry_run: !options.apply,
      ...stats,
      accepted_preview: accepted.slice(0, 5).map(({ url, title, company, location }) => ({
        url, title, company, location,
      })),
    }));
  } finally {
    process.chdir(previousCwd);
  }
}

const isMain = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  main().catch((error) => {
    console.error(JSON.stringify({
      schema_version: 1,
      status: 'error',
      source: 'linkedin-direct-public',
      error: clean(error?.message || String(error), 500),
    }));
    process.exitCode = 1;
  });
}
