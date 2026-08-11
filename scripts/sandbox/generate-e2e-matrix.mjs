#!/usr/bin/env node
/**
 * Expand the install/update support matrix into concrete E2E combinations.
 *
 * One source of truth for every {os, install-method, update-method} pair a
 * user could be on. This file only DECLARES and EXPANDS: it knows nothing
 * about which combinations CI can drive. Every combination is dispatched to
 * its OS's run workflow, and THAT workflow natively skips the method pairs
 * its driver cannot run yet -- capability knowledge lives next to each
 * driver (install-e2e-run.yml, install-e2e-windows-run.yml,
 * install-e2e-macos-run.yml).
 *
 * Used by .github/workflows/install-e2e.yml, which runs it with the picked
 * release tags (annotated at pick time with what each tag's tree ships):
 *
 *   node scripts/sandbox/generate-e2e-matrix.mjs \
 *     --tags '[{"ref":"v2026.8.3","desktop":true}]' --route all
 *
 * Prints JSON: { linux: {include:[...]}, windows: {include:[...]},
 * macos: {include:[...]} } -- every entry is {name, install_method,
 * update_method, install_ref}, and windows entries add tag_has_desktop
 * (from the tag annotation) so the run workflow can natively skip
 * desktop-surface legs from releases that predate the desktop app.
 */

import path from 'node:path';
import { parseArgs } from 'node:util';
import { fileURLToPath } from 'node:url';

/**
 * Method ids are strict machine strings -- workflows and the IMPLEMENTED
 * table key off them, so an unknown id must fail loudly (see KNOWN_METHODS).
 *
 * `versions` on an entry expands it into one combination per version. Only
 * "latest" is allowed today: it means "the artifact published on the website
 * right now" (Hermes-Setup.exe has no versioned archive yet). When archived
 * installer versions exist, widen ALLOWED_VERSIONS.
 */
export const SPEC = {
  windows: {
    install: [
      // irm https://hermes.nousresearch.com/install.ps1 | iex
      { method: 'irm-iex' },
      // Website Hermes-Setup.exe, clicked through the GUI.
      { method: 'desktop-installer', versions: ['latest'] },
    ],
    update: [
      { method: 'irm-iex' },
      // Run the bootstrap exe again over an existing install (--update flow).
      { method: 'desktop-installer-rerun', versions: ['latest'] },
      { method: 'hermes-update' },
      // Settings -> About -> "Update now" inside the running desktop app.
      { method: 'desktop-app' },
    ],
  },
  macos: {
    install: [
      { method: 'curl-bash' },
      { method: 'packaged-app' },
    ],
    update: [
      { method: 'curl-bash' },
      { method: 'hermes-update' },
      { method: 'app-update' },
    ],
  },
  linux: {
    install: [
      { method: 'curl-bash' },
    ],
    update: [
      { method: 'curl-bash' },
      { method: 'hermes-update' },
    ],
  },
};

const KNOWN_METHODS = new Set([
  'irm-iex',
  'desktop-installer',
  'desktop-installer-rerun',
  'hermes-update',
  'desktop-app',
  'curl-bash',
  'packaged-app',
  'app-update',
]);

const ALLOWED_VERSIONS = new Set(['latest']);

function validateEntry(os, kind, entry) {
  if (typeof entry.method !== 'string' || !KNOWN_METHODS.has(entry.method)) {
    throw new Error(`${os}.${kind}: unknown method id ${JSON.stringify(entry.method)} -- add it to KNOWN_METHODS if intentional`);
  }
  if ('versions' in entry) {
    if (!Array.isArray(entry.versions) || entry.versions.length === 0) {
      throw new Error(`${os}.${kind}.${entry.method}: versions must be a non-empty array`);
    }
    for (const v of entry.versions) {
      if (!ALLOWED_VERSIONS.has(v)) {
        throw new Error(`${os}.${kind}.${entry.method}: version ${JSON.stringify(v)} not allowed -- only ${[...ALLOWED_VERSIONS].join(', ')} until versioned installer archives exist`);
      }
    }
  }
  const unknown = Object.keys(entry).filter((k) => k !== 'method' && k !== 'versions');
  if (unknown.length) {
    throw new Error(`${os}.${kind}.${entry.method}: unknown keys ${unknown.join(', ')}`);
  }
}

/** Expand one method entry into concrete ids ("desktop-installer@latest"). */
export function expandMethod(os, kind, entry) {
  validateEntry(os, kind, entry);
  if (!entry.versions) return [entry.method];
  return entry.versions.map((v) => `${entry.method}@${v}`);
}

/** Every {os, install, update, secondUpdate} combination in SPEC. */
export function generateEnvironments(spec) {
  const envs = [];
  for (const [os, osSpec] of Object.entries(spec)) {
    const { install, update, secondUpdate = [], ...unknown } = osSpec;
    if (Object.keys(unknown).length) {
      throw new Error(`${os}: unknown spec keys ${Object.keys(unknown).join(', ')}`);
    }
    // Chained second updates (install -> update -> update again) are a real
    // axis -- the updater that RESULTS from an update must itself update --
    // but nothing implements them yet. Refuse a spec that declares them so
    // the first implementation is forced to come through here.
    if (!Array.isArray(secondUpdate) || secondUpdate.length !== 0) {
      throw new Error(`${os}: secondUpdate must be empty until a second-update leg is implemented`);
    }
    const installs = install.flatMap((e) => expandMethod(os, 'install', e));
    const updates = update.flatMap((e) => expandMethod(os, 'update', e));
    for (const i of installs) {
      for (const u of updates) {
        envs.push({ os, install: i, update: u, secondUpdate: '' });
      }
    }
  }
  return envs;
}

/** Mirror of install-e2e.yml's dispatch `route` choice. */
function routeWants(route, env) {
  switch (route) {
    case 'all':
      return true;
    case 'both':
      return env.os === 'linux';
    case 'update':
      return env.os === 'linux' && env.update === 'hermes-update';
    case 'installer':
      return env.os === 'linux' && env.update === 'curl-bash';
    case 'windows-desktop':
      return env.os === 'windows';
    default:
      throw new Error(`unknown route filter: ${JSON.stringify(route)}`);
  }
}

/**
 * Split the combinations into one matrix per OS.
 *
 * `tags` (the released versions we test updating FROM, as {ref, desktop}
 * annotation objects) is the OUTER axis: for each tag, for each
 * combination, one dispatch that installs the tag and updates to HEAD.
 * No capability filtering happens here -- every declared combination is
 * dispatched, and the OS's run workflow natively skips what its driver
 * cannot run yet. Entry names carry everything (os, method pair, tag
 * transition) because slash-joined leg names are all the graph renders.
 */
export function buildMatrices(envs, { tags = [], route = 'all' } = {}) {
  for (const t of tags) {
    if (typeof t?.ref !== 'string' || typeof t?.desktop !== 'boolean') {
      throw new Error(`tags must be {ref, desktop} annotation objects, got ${JSON.stringify(t)}`);
    }
  }
  const byOs = { linux: [], windows: [], macos: [] };
  for (const env of envs) {
    if (!routeWants(route, env)) continue;
    const bucket = byOs[env.os];
    if (!bucket) {
      throw new Error(`no matrix bucket for os ${JSON.stringify(env.os)}`);
    }
    if (tags.length === 0) {
      throw new Error('combinations were selected but no --tags given');
    }
    for (const tag of tags) {
      const entry = {
        name: `${env.os}: ${env.install} -> ${env.update} (${tag.ref} -> HEAD)`,
        install_method: env.install,
        update_method: env.update,
        install_ref: tag.ref,
      };
      if (env.os === 'windows') entry.tag_has_desktop = tag.desktop;
      bucket.push(entry);
    }
  }
  return Object.fromEntries(
    Object.entries(byOs).map(([os, include]) => [os, { include }]),
  );
}

function main() {
  const { values } = parseArgs({
    options: {
      tags: { type: 'string', default: '[]' },
      route: { type: 'string', default: 'all' },
    },
  });
  const tags = JSON.parse(values.tags);
  if (!Array.isArray(tags)) {
    throw new Error('--tags must be a JSON array of {ref, desktop} annotation objects');
  }
  const envs = generateEnvironments(SPEC);
  const matrices = buildMatrices(envs, { tags, route: values.route });
  process.stdout.write(`${JSON.stringify(matrices, null, 2)}\n`);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  main();
}
