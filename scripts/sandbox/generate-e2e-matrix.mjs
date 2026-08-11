#!/usr/bin/env node
/**
 * Expand the install/update support matrix into concrete E2E combinations.
 *
 * One source of truth for every {os, install-method, update-method} pair a
 * user could be on. Implemented combos fan out into real CI jobs -- one job
 * per combination -- and everything else lands in a "skipped" matrix so the
 * TODO surface stays visible in every run instead of buried in comments.
 *
 * Used by .github/workflows/install-e2e-tag.yml, which runs it once per
 * starting release tag:
 *
 *   node scripts/sandbox/generate-e2e-matrix.mjs \
 *     --tags '["v2026.8.3"]' --route all
 *
 * Prints JSON: { linux: {include:[...]}, windows: {include:[...]},
 * skipped: {include:[...]} }. linux entries carry {name, route,
 * install_ref} for install-e2e-run.yml; windows entries carry {name,
 * install_method, update_method, install_ref} for
 * install-e2e-windows-run.yml (which itself natively skips method pairs it
 * cannot drive yet); skipped entries are the macOS combos (no workflow
 * exists at all).
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

/**
 * How each implemented combination maps onto a reusable workflow.
 *
 * linux: every combo is implemented; `route` is install-e2e-run.yml's input.
 * windows: ALL combos dispatch to install-e2e-windows-run.yml -- the run
 *   workflow itself natively skips (grey) any {install, update} method pair
 *   it cannot drive yet, so "which windows methods work" lives THERE, next
 *   to the driver, not here.
 * macos: nothing is implemented; combos surface as native skips in the
 *   caller via install-e2e-skip.yml without burning a tag fanout.
 */
export const LINUX_ROUTES = {
  'hermes-update': 'update',
  'curl-bash': 'installer',
};

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
 * Split the combinations into per-workflow matrices.
 *
 * `tags` (the released versions we test updating FROM) is the OUTER axis,
 * applied to every OS with a driving workflow: for each tag, for each
 * combo, one job that installs the tag and updates to HEAD. Linux legs
 * pass the tag as install-ref for the sandbox to install; Windows legs
 * pass it as the ref the staged serve.git parks `main` at -- the published
 * installer has no commit pin, so that IS the installed version.
 *
 * Skip placement follows where the knowledge lives: macOS combos are known
 * unimplementable HERE (no workflow exists), so they go to the `skipped`
 * matrix -- one native grey check per combo, not multiplied by tags, since
 * no tag would change the outcome. Windows combos ALL dispatch to the run
 * workflow, which natively skips the method pairs it cannot drive.
 */
export function buildMatrices(envs, { tags = [], route = 'all' } = {}) {
  const linux = [];
  const windows = [];
  const skipped = [];
  const needTags = () => {
    if (tags.length === 0) {
      throw new Error('a combo with a driving workflow was selected but no --tags given');
    }
  };
  for (const env of envs) {
    if (!routeWants(route, env)) continue;
    if (env.os === 'macos') {
      skipped.push({ ...env, reason: 'no macos workflow yet' });
      continue;
    }
    if (env.os === 'linux') {
      const linuxRoute = LINUX_ROUTES[env.update];
      if (!linuxRoute) {
        throw new Error(`linux update method ${JSON.stringify(env.update)} has no route mapping`);
      }
      needTags();
      for (const tag of tags) {
        linux.push({
          name: `${env.install} -> ${env.update}`,
          route: linuxRoute,
          install_ref: tag,
        });
      }
      continue;
    }
    if (env.os === 'windows') {
      needTags();
      for (const tag of tags) {
        windows.push({
          name: `${env.install} -> ${env.update}`,
          install_method: env.install,
          update_method: env.update,
          install_ref: tag,
        });
      }
      continue;
    }
    throw new Error(`no workflow routing for os ${JSON.stringify(env.os)}`);
  }
  return {
    linux: { include: linux },
    windows: { include: windows },
    skipped: { include: skipped },
  };
}

function main() {
  const { values } = parseArgs({
    options: {
      tags: { type: 'string', default: '[]' },
      route: { type: 'string', default: 'all' },
    },
  });
  const tags = JSON.parse(values.tags);
  if (!Array.isArray(tags) || !tags.every((t) => typeof t === 'string')) {
    throw new Error('--tags must be a JSON array of strings');
  }
  const envs = generateEnvironments(SPEC);
  const matrices = buildMatrices(envs, { tags, route: values.route });
  process.stdout.write(`${JSON.stringify(matrices, null, 2)}\n`);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  main();
}
