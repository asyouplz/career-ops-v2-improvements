#!/usr/bin/env node

/**
 * Batch identity and eligibility resolver used by both the scanner and V2.
 *
 * Input is one JSON object on stdin. Output is one JSON object on stdout. The
 * resolver performs no writes and makes no network calls.
 */

import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import process from 'node:process';
import yaml from 'js-yaml';

import {
  buildCompanyIdentityCanonicalizer,
  buildCompanyIdentityResolver,
  buildLocationFilter,
  companyRoleIdentityKey,
  normalizeRoleForIdentity,
  normalizeUrlForDedup,
} from './scan.mjs';
import { roleFuzzyMatch } from './role-matcher.mjs';
import { normalizeTextKey } from './tracker-parse.mjs';

const HARD_BLOCK_STATUSES = new Set([
  'applied', 'responded', 'interview', 'offer', 'rejected', 'discarded',
]);
const COMPANY_BLOCK_STATUSES = new Set([
  'applied', 'responded', 'interview', 'offer', 'rejected',
]);

function digest(prefix, value) {
  return `${prefix}_${createHash('sha256').update(value).digest('hex').slice(0, 20)}`;
}

function clean(value, limit = 1000) {
  return String(value ?? '').replace(/\s+/g, ' ').trim().slice(0, limit);
}

function readStdin() {
  return new Promise((resolve, reject) => {
    let value = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', chunk => { value += chunk; });
    process.stdin.on('end', () => {
      try {
        resolve(JSON.parse(value || '{}'));
      } catch (error) {
        reject(error);
      }
    });
    process.stdin.on('error', reject);
  });
}

export function resolveIdentityBatch(payload, portalsConfig = {}) {
  const resolveCompanyIdentity = buildCompanyIdentityResolver(portalsConfig.company_aliases);
  const canonicalizeCompany = name => resolveCompanyIdentity(name).company_key;
  const locationFilter = buildLocationFilter(portalsConfig.location_filter);
  const tracker = Array.isArray(payload?.tracker) ? payload.tracker : [];
  const records = Array.isArray(payload?.records) ? payload.records : [];

  const trackerIdentity = tracker
    .filter(item => item && typeof item === 'object')
    .map(item => ({
      id: clean(item.id, 20),
      company: clean(item.company, 200),
      role: clean(item.role, 300),
      status: clean(item.status, 40),
      company_key: canonicalizeCompany(item.company),
      role_key: normalizeRoleForIdentity(item.role),
    }));

  const results = records
    .filter(item => item && typeof item === 'object')
    .map(record => {
      const id = clean(record.id, 120);
      const company = clean(record.company, 200);
      const role = clean(record.role ?? record.title, 300);
      const location = clean(record.location, 300);
      const url = clean(record.url, 1600);
      const companyIdentity = resolveCompanyIdentity(company);
      const companyKey = companyIdentity.company_key;
      const roleKey = normalizeRoleForIdentity(role);
      const identityTextKey = normalizeTextKey(record.identity_text);
      const postingKey = companyRoleIdentityKey(company, role, canonicalizeCompany);
      const normalizedUrl = normalizeUrlForDedup(url) || url;
      const companyMatches = trackerIdentity.filter(item => (
        (companyKey && item.company_key === companyKey)
        || (!companyKey && item.company_key && identityTextKey.includes(item.company_key))
      ));
      const rankedMatches = companyMatches
        .map(item => ({
          ...item,
          role_match: Boolean(
            roleKey && item.role_key
            && (roleKey === item.role_key || roleFuzzyMatch(role, item.role))
          ),
        }))
        .sort((left, right) => (
          Number(right.role_match) - Number(left.role_match)
          || Number(right.id || 0) - Number(left.id || 0)
        ));

      let historyGate = 'eligible';
      let historyReason = 'no_blocking_history';
      let historyMatches = [];
      if (companyIdentity.status === 'ambiguous') {
        historyGate = 'excluded';
        historyReason = 'ambiguous_company_identity';
      } else if (record.pipeline_bucket === 'conditional_reapply') {
        historyGate = 'excluded';
        historyReason = 'pipeline_conditional_reapply';
      } else {
        const sameRole = rankedMatches.filter(item => item.role_match);
        const sameRoleBlocked = sameRole.find(item => HARD_BLOCK_STATUSES.has(item.status.toLowerCase()));
        if (sameRoleBlocked) {
          historyGate = 'excluded';
          historyReason = `same_role_${sameRoleBlocked.status.toLowerCase()}`;
          historyMatches = [sameRoleBlocked];
        } else {
          const priorCompany = rankedMatches.filter(item => COMPANY_BLOCK_STATUSES.has(item.status.toLowerCase()));
          if (priorCompany.length > 0) {
            historyGate = 'excluded';
            historyReason = 'same_company_prior_application';
            historyMatches = priorCompany.slice(0, 3);
          }
        }
      }

      const listingSeed = normalizedUrl || `${postingKey}::${clean(record.source_id, 80)}`;
      return {
        id,
        company_key: companyKey,
        company_identity_status: companyIdentity.status,
        company_identity_candidates: companyIdentity.candidates,
        role_key: roleKey,
        posting_key: postingKey,
        posting_cluster_id: digest('pc', postingKey),
        listing_instance_id: digest('li', listingSeed),
        normalized_url: normalizedUrl || null,
        location_eligible: locationFilter(location, url, role),
        tracker_matches: rankedMatches.slice(0, 3).map(item => ({
          id: item.id,
          company: item.company,
          role: item.role,
          status: item.status,
          role_match: item.role_match,
        })),
        history_gate: historyGate,
        history_reason: historyReason,
        history_tracker_matches: historyMatches.map(item => ({
          id: item.id,
          company: item.company,
          role: item.role,
          status: item.status,
        })),
      };
    });

  return {
    schema_version: 'career-ops.identity.v1',
    record_count: results.length,
    results,
  };
}

if (import.meta.url === new URL(process.argv[1] || '', 'file:').href) {
  try {
    const payload = await readStdin();
    const portalsPath = clean(payload.portals_path, 2000) || 'portals.yml';
    const config = yaml.load(readFileSync(portalsPath, 'utf8')) || {};
    process.stdout.write(`${JSON.stringify(resolveIdentityBatch(payload, config))}\n`);
  } catch (error) {
    process.stderr.write(`identity-resolver: ${error.message}\n`);
    process.exitCode = 1;
  }
}
