#!/usr/bin/env node
// @ts-check
/**
 * Expand the install/update support matrix into concrete E2E combinations.
 *
 * One source of truth for every {os, install-method, update-method} pair a
 * user could be on. This file only DECLARES and EXPANDS: it knows nothing
 * about which combinations CI can drive. Every combination is dispatched to
 * its OS's run workflow, and THAT workflow natively skips the method pairs
 * its driver cannot run yet -- capability knowledge lives next to each
 * driver (install-e2e-run.yml for linux AND macos,
 * install-e2e-windows-run.yml). Correctness here is enforced by the type
 * unions below (checked via `tsc --checkJs`), not by runtime validation --
 * anything the types can't catch is self-evident on the next CI run.
 *
 * Used by .github/workflows/install-e2e.yml, which runs it with the picked
 * release tags (annotated at pick time with what each tag's tree ships):
 *
 *   node scripts/sandbox/generate-e2e-matrix.mjs \
 *     --tags '[{"ref":"v2026.8.3","desktop":true}]'
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
 * The closed method/version vocabulary. Workflows key off these exact
 * strings, so they are types, not conventions.
 *
 * @typedef {'latest'} InstallerVersion
 *   The artifact published on the website right now -- Hermes-Setup.exe has
 *   no versioned archive yet. Widen this union when one exists.
 * @typedef {'installer-script' | 'desktop-installer' | 'packaged-app'} InstallMethod
 *   installer-script is the platform's one-liner (curl | bash on
 *   linux/macos, irm | iex on windows); packaged-app is declared but not
 *   used by any OS spec yet.
 * @typedef {InstallMethod | 'hermes-update' | 'app-update'} UpdateMethod
 *   Every install method doubles as an update method (re-run it over the
 *   existing install), plus the updater CLI and the running app's own
 *   Update button.
 * @typedef {'linux' | 'windows' | 'macos'} Os
 *
 * @typedef {{method: InstallMethod, versions?: InstallerVersion[]}} InstallEntry
 * @typedef {{method: UpdateMethod, versions?: InstallerVersion[]}} UpdateEntry
 *   `versions` expands the entry into one combination per version
 *   ("desktop-installer@latest").
 * @typedef {{install: InstallEntry[], update: UpdateEntry[], secondUpdate?: never[]}} OsSpec
 *   secondUpdate (install -> update -> update again) is a real future axis
 *   -- the updater that RESULTS from an update must itself update -- typed
 *   `never[]` so declaring one is a type error until a leg implements it.
 *
 * @typedef {{ref: string, desktop: boolean}} TagAnnotation
 *   A picked release tag plus what its own tree ships (annotated by
 *   pick-releases in install-e2e.yml).
 *
 * @typedef {{name: string, install_method: string, update_method: string,
 *            install_ref: string, tag_has_desktop?: boolean}} MatrixEntry
 */

/** @type {Record<Os, OsSpec>} */
export const SPEC = {
  windows: {
    install: [
      // irm https://hermes.nousresearch.com/install.ps1 | iex
      { method: 'installer-script' },
      // Website Hermes-Setup.exe, clicked through the GUI.
      { method: 'desktop-installer', versions: ['latest'] },
    ],
    update: [
      { method: 'installer-script' },
      // Run the bootstrap exe again over an existing install (--update flow).
      { method: 'desktop-installer', versions: ['latest'] },
      { method: 'hermes-update' },
      // Settings -> About -> "Update now" inside the running desktop app.
      { method: 'app-update' },
    ],
  },
  macos: {
    install: [
      { method: 'installer-script' },
    ],
    update: [
      { method: 'installer-script' },
      { method: 'hermes-update' },
      { method: 'app-update' },
    ],
  },
  linux: {
    install: [
      { method: 'installer-script' },
    ],
    update: [
      { method: 'installer-script' },
      { method: 'hermes-update' },
    ],
  },
};

/**
 * Expand one method entry into concrete ids ("desktop-installer@latest").
 * @param {InstallEntry | UpdateEntry} entry
 * @returns {string[]}
 */
export function expandMethod(entry) {
  if (!entry.versions) return [entry.method];
  return entry.versions.map((v) => `${entry.method}@${v}`);
}

/**
 * Every {os, install, update} combination in SPEC.
 * @param {Record<Os, OsSpec>} spec
 * @returns {{os: Os, install: string, update: string}[]}
 */
export function generateEnvironments(spec) {
  /** @type {{os: Os, install: string, update: string}[]} */
  const envs = [];
  for (const [os, osSpec] of /** @type {[Os, OsSpec][]} */ (Object.entries(spec))) {
    for (const install of osSpec.install.flatMap(expandMethod)) {
      for (const update of osSpec.update.flatMap(expandMethod)) {
        envs.push({ os, install, update });
      }
    }
  }
  return envs;
}

/**
 * Split the combinations into one matrix per OS.
 *
 * `tags` (the released versions we test updating FROM) is the OUTER axis:
 * for each tag, for each combination, one dispatch that installs the tag
 * and updates to HEAD. No capability filtering happens here -- every
 * declared combination is dispatched, and the OS's run workflow natively
 * skips what its driver cannot run yet. Entry names carry everything (os,
 * method pair, tag transition) because slash-joined leg names are all the
 * graph renders.
 *
 * @param {{os: Os, install: string, update: string}[]} envs
 * @param {TagAnnotation[]} tags
 * @returns {Record<Os, {include: MatrixEntry[]}>}
 */
export function buildMatrices(envs, tags) {
  /** @type {Record<Os, {include: MatrixEntry[]}>} */
  const byOs = { linux: { include: [] }, windows: { include: [] }, macos: { include: [] } };
  for (const env of envs) {
    for (const tag of tags) {
      /** @type {MatrixEntry} */
      const entry = {
        name: `${env.os}: ${env.install} -> ${env.update} (${tag.ref} -> HEAD)`,
        install_method: env.install,
        update_method: env.update,
        install_ref: tag.ref,
      };
      if (env.os === 'windows') entry.tag_has_desktop = tag.desktop;
      byOs[env.os].include.push(entry);
    }
  }
  return byOs;
}

/**
 * Render the plan as a markdown cross-table for $GITHUB_STEP_SUMMARY:
 * one row per {os, install -> update} combination, one column per
 * starting tag. Every cell is dispatched; whether it RUNS or greys out
 * is the run workflow's call (capability lives there, not here), so the
 * chart only distinguishes the one thing the plan itself knows: windows
 * desktop-surface legs from tags that predate the desktop app.
 *
 * @param {{os: Os, install: string, update: string}[]} envs
 * @param {TagAnnotation[]} tags
 * @returns {string}
 */
export function renderMarkdownPlan(envs, tags) {
  const needsDesktop = (/** @type {string} */ m) =>
    m.startsWith('desktop-installer') || m === 'app-update';
  const lines = [
    '### Install & Update E2E plan',
    '',
    `${envs.length} combinations x ${tags.length} starting tags = ${envs.length * tags.length} legs`,
    '',
    `| combination | ${tags.map((t) => t.ref).join(' | ')} |`,
    `|---|${tags.map(() => '---').join('|')}|`,
  ];
  for (const env of envs) {
    const cells = tags.map((tag) => {
      if (
        env.os === 'windows' && !tag.desktop &&
        (needsDesktop(env.install) || needsDesktop(env.update))
      ) {
        return 'pre-desktop';
      }
      return '&#x2705;';
    });
    lines.push(`| \`${env.os}: ${env.install} -> ${env.update}\` | ${cells.join(' | ')} |`);
  }
  lines.push(
    '',
    '&#x2705; dispatched -- the OS run workflow decides run vs native skip',
    '(unimplemented method pairs grey out there). `pre-desktop`: the tag',
    "ships no desktop app, so desktop-surface legs grey out regardless.",
    '',
  );
  return lines.join('\n');
}

function main() {
  const { values } = parseArgs({
    options: {
      tags: { type: 'string', default: '[]' },
      format: { type: 'string', default: 'json' },
    },
  });
  const tags = /** @type {TagAnnotation[]} */ (JSON.parse(values.tags));
  const envs = generateEnvironments(SPEC);
  if (values.format === 'markdown') {
    process.stdout.write(renderMarkdownPlan(envs, tags));
    return;
  }
  const matrices = buildMatrices(envs, tags);
  process.stdout.write(`${JSON.stringify(matrices, null, 2)}\n`);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  main();
}
