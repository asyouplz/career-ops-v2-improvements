#!/usr/bin/env node

/** Build data/posting-index.tsv from append-only scan-history without rewriting it. */

import { createHash } from 'node:crypto';
import { readFileSync, renameSync, writeFileSync } from 'node:fs';
import process from 'node:process';
import yaml from 'js-yaml';

import {
  buildCompanyIdentityCanonicalizer,
  normalizeRoleForIdentity,
  normalizeUrlForDedup,
} from './scan.mjs';

const historyPath = process.env.CAREER_OPS_SCAN_HISTORY || 'data/scan-history.tsv';
const indexPath = process.env.CAREER_OPS_POSTING_INDEX || 'data/posting-index.tsv';
const portalsPath = process.env.CAREER_OPS_PORTALS || 'portals.yml';

function digest(prefix, value) {
  return `${prefix}_${createHash('sha256').update(value).digest('hex').slice(0, 20)}`;
}

function safe(value) {
  return String(value ?? '').replace(/[\t\r\n]+/g, ' ').trim();
}

export function buildPostingIndex(historyText, config = {}) {
  const canonicalizeCompany = buildCompanyIdentityCanonicalizer(config.company_aliases);
  const seenListings = new Set();
  const rows = [];
  for (const line of historyText.split('\n')) {
    const [url, firstSeen, portal, title, company, status = 'added'] = line.split('\t');
    if (!url || url === 'url' || !title || !company) continue;
    const normalizedUrl = normalizeUrlForDedup(url);
    const listingId = digest('li', normalizedUrl);
    if (seenListings.has(listingId)) continue;
    seenListings.add(listingId);
    const companyKey = canonicalizeCompany(company);
    const roleKey = normalizeRoleForIdentity(title);
    const clusterId = digest('pc', `${companyKey}::${roleKey}`);
    rows.push([
      listingId,
      clusterId,
      url,
      firstSeen,
      portal,
      companyKey,
      roleKey,
      status,
    ].map(safe).join('\t'));
  }
  return [
    'listing_instance_id\tposting_cluster_id\turl\tfirst_seen\tportal\tcompany_key\trole_key\tstatus',
    ...rows,
    '',
  ].join('\n');
}

if (import.meta.url === new URL(process.argv[1] || '', 'file:').href) {
  const config = yaml.load(readFileSync(portalsPath, 'utf8')) || {};
  const output = buildPostingIndex(readFileSync(historyPath, 'utf8'), config);
  const tempPath = `${indexPath}.tmp-${process.pid}`;
  writeFileSync(tempPath, output, 'utf8');
  renameSync(tempPath, indexPath);
  process.stdout.write(JSON.stringify({
    status: 'ok',
    rows: Math.max(0, output.trim().split('\n').length - 1),
    path: indexPath,
  }) + '\n');
}
