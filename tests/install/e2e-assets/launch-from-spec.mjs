// @ts-check
/**
 * Launch the Hermes desktop app from a captured launch spec and click the
 * real update flow: Settings -> About -> "Update now".
 *
 * The spec is written by launch-capture/sitecustomize.py at `hermes
 * desktop`'s own spawn site, so argv, cwd, and the fully-constructed env
 * are the product's own -- this launcher only translates the npm-exec
 * source shape into a direct electron binary path (Playwright needs a
 * real executable, and the electron npm shim would re-spawn out of our
 * control).
 *
 * Usage (from the scratch dir where the driver installed @playwright/test):
 *   node launch-from-spec.mjs --spec /path/launch-spec.json \
 *     [--result $HERMES_HOME/.hermes-update-result.json] \
 *     [--expect-sha <sha> --repo-dir <install dir>] [--no-update]
 *
 * --no-update: launch + wait for the window + close. The smoke arm.
 * Otherwise: click Update now, then poll for completion. Two signals,
 * either satisfies (poll whichever are given, first hit wins):
 *   --result      the windows hand-off's result file
 *                 (HERMES_HOME/.hermes-update-result.json)
 *   --expect-sha  the installed checkout reaching the expected commit -
 *                 the source-install signal, where the About pane's update
 *                 runs `hermes update` and no result file exists.
 * The Playwright close event is unreliable across the update handoff, so
 * neither signal is an app event.
 */

import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { parseArgs } from 'node:util';
import { _electron } from '@playwright/test';

/**
 * @typedef {{argv: string[], cwd: string, env: Record<string, string>,
 *            matchedShape: 'source' | 'packaged'}} LaunchSpec
 */

/**
 * Resolve what _electron.launch needs from a captured spec.
 * @param {LaunchSpec} spec
 * @returns {{executablePath: string, args: string[], cwd: string,
 *            env: Record<string, string>}}
 */
export function resolveLaunch(spec) {
  if (spec.matchedShape === 'packaged') {
    return {
      executablePath: spec.argv[0],
      args: spec.argv.slice(1),
      cwd: spec.cwd,
      env: spec.env,
    };
  }
  // Source shape: ["npm", "exec", "--", "electron", ".", ...extra] running
  // in apps/desktop. Electron's real binary lives in the workspace-hoisted
  // node_modules; `electron/index.js` exports its path but requires the
  // module -- cheaper here to read the path file it derives from.
  const desktopDir = spec.cwd;
  const idx = spec.argv.findIndex((t) => t === 'electron');
  const extra = idx >= 0 ? spec.argv.slice(idx + 1).filter((t) => t !== '.') : [];
  const candidates = [
    path.join(desktopDir, 'node_modules', 'electron'),
    path.join(desktopDir, '..', '..', 'node_modules', 'electron'),
  ];
  for (const moduleDir of candidates) {
    const pathTxt = path.join(moduleDir, 'path.txt');
    if (!fs.existsSync(pathTxt)) continue;
    const rel = fs.readFileSync(pathTxt, 'utf8').trim();
    const exe = path.join(moduleDir, 'dist', rel);
    if (fs.existsSync(exe)) {
      return { executablePath: exe, args: ['.', ...extra], cwd: desktopDir, env: spec.env };
    }
  }
  throw new Error(`no electron binary found under ${candidates.join(' or ')}`);
}

/** @param {string} msg */
function log(msg) {
  console.log(`[launch-from-spec] ${msg}`);
}

async function main() {
  const { values } = parseArgs({
    options: {
      spec: { type: 'string' },
      result: { type: 'string' },
      'expect-sha': { type: 'string' },
      'repo-dir': { type: 'string' },
      'no-update': { type: 'boolean', default: false },
      'timeout-ms': { type: 'string', default: '600000' },
    },
  });
  if (!values.spec) throw new Error('--spec is required');
  /** @type {LaunchSpec} */
  const spec = JSON.parse(fs.readFileSync(values.spec, 'utf8'));
  const launch = resolveLaunch(spec);
  log(`launching ${launch.executablePath} (shape: ${spec.matchedShape})`);

  const app = await _electron.launch({
    executablePath: launch.executablePath,
    args: launch.args,
    cwd: launch.cwd,
    env: launch.env,
  });
  const window = await app.firstWindow({ timeout: 120_000 });
  await window.waitForLoadState('domcontentloaded');
  log(`window up: ${await window.title()}`);
  await window.screenshot({ path: `${values.spec}.window.png` }).catch(() => {});

  if (values['no-update']) {
    log('smoke mode: window proven, closing');
    await app.close().catch(() => {});
    return;
  }

  if (!values.result && !(values['expect-sha'] && values['repo-dir'])) {
    throw new Error('need --result and/or --expect-sha + --repo-dir unless --no-update');
  }
  const deadline = Date.now() + Number(values['timeout-ms']);


  // Dismiss the onboarding overlay when present. The drivers seed a
  // provider so the overlay SHOULD never mount, but it has a real boot
  // window: the renderer inits `configured` from a localStorage cache
  // (null on a fresh install) and only flips after gateway probes, so the
  // overlay can mount late - first as a buttonless boot-progress card,
  // then as the provider picker with the real escape hatch, "I'll choose
  // a provider later" (i18n en: chooseLater). Two traps this loop avoids:
  // a one-shot dismiss probe loses to the late mount, and visibility is
  // the wrong readiness signal - the settings gear is "visible" UNDER the
  // fullscreen overlay while the overlay intercepts every click. So:
  // alternate short-timeout dismiss clicks with short-timeout settings
  // clicks until a settings click actually LANDS (Playwright's hit-target
  // check makes a landed click proof the overlay is gone).
  const later = window.getByRole('button', { name: /choose a provider later|skip/i }).first()
  const settingsButton = window.getByRole('button', { name: /open settings|settings/i }).first()

  const overlayDeadline = Date.now() + 180_000
  let settingsOpened = false
  const brief = (e) => String(e && e.message || e).split('\n').slice(0, 3).join(' | ')
  for (let iter = 1; ; iter++) {
    await later
      .click({ timeout: 2_000 })
      .then(async () => {
        log('dismissed onboarding overlay')
        await later.waitFor({ state: 'hidden', timeout: 15_000 }).catch(() => {})
      })
      .catch((e) => log(`[overlay] iter ${iter} chooseLater click failed: ${brief(e)}`))
    try {
      await settingsButton.click({ timeout: 4_000 })
      settingsOpened = true
      break
    } catch (e) {
      log(`[overlay] iter ${iter} settings click failed: ${brief(e)}`)
    }
    if (Date.now() > overlayDeadline) break
  }
  if (!settingsOpened) {
    await window.screenshot({ path: `${values.spec}.overlay-stuck.png` }).catch(() => {})
    throw new Error('onboarding overlay never cleared: Settings not clickable within 180s')
  }

  // Settings is open: About -> Update now.
  await window.getByRole('tab', { name: /about/i }).or(
    window.getByRole('button', { name: /about/i })).first().click();
  const updateNow = window.getByRole('button', { name: /update now/i }).first();
  await updateNow.waitFor({ state: 'visible', timeout: 60_000 });
  await updateNow.click();
  log('clicked Update now; polling for result file');

  // The app may relaunch/exit during the update; completion signals are
  // product state, not Playwright events.
  const resultPath = values.result;
  const expectSha = values['expect-sha'];
  const repoDir = values['repo-dir'];
  /** @returns {string} */
  const headSha = () => {
    try {
      return execFileSync('git', ['-C', /** @type {string} */ (repoDir), 'rev-parse', 'HEAD'], {
        encoding: 'utf8',
      }).trim();
    } catch {
      return '';
    }
  };
  for (;;) {
    if (resultPath && fs.existsSync(resultPath)) {
      log(`update result present: ${fs.readFileSync(resultPath, 'utf8').slice(0, 200)}`);
      break;
    }
    if (expectSha && repoDir && headSha() === expectSha) {
      log(`checkout reached expected sha ${expectSha}`);
      break;
    }
    if (Date.now() > deadline) {
      await window.screenshot({ path: `${values.spec}.timeout.png` }).catch(() => {});
      throw new Error('update completion signal never appeared (result file / expected sha)');
    }
    await new Promise((r) => setTimeout(r, 2_000));
  }
  await app.close().catch(() => {});
}

const invoked = process.argv[1] && path.resolve(process.argv[1]) === (await import('node:url')).fileURLToPath(import.meta.url);
if (invoked) {
  await main();
}
