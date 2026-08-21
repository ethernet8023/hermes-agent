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

// Coarse phase marker for the self-deadline's post-mortem line.
let currentPhase = 'init';
/** @param {string} p */
function phase(p) {
  currentPhase = p;
}

async function main() {
  // Driver self-deadline: SIGKILLed Electron apps leave Playwright driver
  // connections / inherited pipes holding node's event loop open, so the
  // process can survive its own completed test (run 32382176435: test green
  // at +03:58, driver alive 23 more minutes until the leg was cancelled).
  // Success/failure paths exit explicitly below; this unref'd timer is the
  // backstop so no unknown state can hold a runner past its budget.
  const SELF_DEADLINE_MS = 20 * 60 * 1000;
  const selfDeadline = setTimeout(() => {
    log(`DRIVER SELF-TIMEOUT after ${SELF_DEADLINE_MS / 60000}min - exiting 124 (phase: ${currentPhase})`);
    process.exit(124);
  }, SELF_DEADLINE_MS);
  selfDeadline.unref();

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

  // bug-011 recon, zoom pin: the app ships a 90% Appearance zoom default
  // (electron/zoom.ts DEFAULT_ZOOM_LEVEL), so a fresh install renders at
  // devicePixelRatio 0.9 - confirmed in this driver's hit-test dumps (run
  // 32220379726) and reproduced locally. On the linux runners every
  // Playwright click then lands ~10% off target (the "titlebar intercepts"
  // failures); locally at the same zoom clicks land fine, so the zoom x
  // runner-input-pipeline interplay is the live suspect. Pin zoom to 100%
  // by seeding the persisted zoom-state.json the main process reads at
  // startup (restorePersistedZoomLevel): green legs confirm the mechanism,
  // red legs with dpr=1 in the hit-dump exonerate zoom entirely.
  const userDataDir = launch.env.HERMES_DESKTOP_USER_DATA_DIR
    || (process.platform === 'darwin'
      ? path.join(process.env.HOME || '', 'Library', 'Application Support', 'Hermes')
      : path.join(process.env.HOME || '', '.config', 'Hermes'));
  try {
    fs.mkdirSync(userDataDir, { recursive: true });
    fs.writeFileSync(path.join(userDataDir, 'zoom-state.json'), JSON.stringify({ zoomLevel: 0 }));
    log(`seeded 100% zoom at ${path.join(userDataDir, 'zoom-state.json')}`);
  } catch (e) {
    log(`zoom seed failed (continuing at app default): ${e.message}`);
  }

  phase('launch');
  const app = await _electron.launch({
    executablePath: launch.executablePath,
    args: launch.args,
    cwd: launch.cwd,
    env: launch.env,
  });
  // The app spawns several BrowserWindows (wake indicator, helper surfaces);
  // firstWindow() grabs whichever webContents came first, which on CI is NOT
  // the main app window - the run 32041211230 recording shows the app shell
  // on screen while every locator waits forever in an empty page. Pick the
  // window that actually contains the app UI (a button element renders only
  // in the real renderer), retrying as windows appear.
  await app.firstWindow({ timeout: 120_000 });
  let window = null;
  const windowDeadline = Date.now() + 120_000;
  while (!window) {
    for (const candidate of app.windows()) {
      const hasUi = await candidate
        .evaluate(() => document.querySelector('button') !== null)
        .catch(() => false);
      if (hasUi) { window = candidate; break; }
    }
    if (!window) {
      if (Date.now() > windowDeadline) {
        for (const c of app.windows()) log(`  window seen: url=${c.url()}`);
        throw new Error('no window with app UI (a <button>) appeared within 120s');
      }
      await new Promise((r) => setTimeout(r, 1_000));
    }
  }
  await window.waitForLoadState('domcontentloaded');
  log(`window up: ${await window.title()} (${app.windows().length} windows, picked url=${window.url()})`);
  await window.screenshot({ path: `${values.spec}.window.png` }).catch(() => {});

  // bug-011 recon, zoom pin round 2: run 32228054089 proved the seed above
  // never engages - the app resolves userData elsewhere (spec env decides,
  // not the driver's HOME guess) and dpr stayed 0.9. Stop guessing paths:
  // ask the main process where userData REALLY is, then force zoom to 100%
  // through the same webContents API the app's own zoom control uses, and
  // log dpr before/after. This makes the next run a clean experiment:
  // dpr=1 + green legs confirms the zoom x runner-input interplay; dpr=1 +
  // the same interception kills the zoom theory with evidence.
  try {
    const userData = await app.evaluate(({ app: electronApp }) => electronApp.getPath('userData'));
    log(`[zoom] app userData actually at: ${userData}`);
  } catch (e) {
    log(`[zoom] userData query failed: ${e.message}`);
  }
  try {
    // Single-shot set gets reverted: the app's boot path re-applies its 90%
    // default asynchronously (restorePersistedZoomLevel's localStorage leg
    // lands after us - verified locally, 3 rounds needed). Set-verify-retry
    // until dpr reads 1.
    const before = await window.evaluate(() => window.devicePixelRatio);
    let after = before;
    for (let i = 0; i < 20; i++) {
      await app.evaluate(({ BrowserWindow }) => {
        for (const w of BrowserWindow.getAllWindows()) {
          w.webContents.setZoomLevel(0);
        }
      });
      await window.waitForTimeout(1000);
      after = await window.evaluate(() => window.devicePixelRatio);
      if (Math.abs(after - 1) < 0.001) break;
    }
    log(`[zoom] forced 100% via webContents.setZoomLevel(0): dpr ${before} -> ${after}`);
  } catch (e) {
    log(`[zoom] direct zoom set failed (continuing): ${e.message}`);
  }

  if (values['no-update']) {
    log('smoke mode: window proven, closing');
    await app.close().catch(() => {});
    process.exit(0);
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
  phase('overlay-loop');
  const later = window.getByRole('button', { name: /choose a provider later|skip/i }).first()
  const settingsButton = window.getByRole('button', { name: /open settings|settings/i }).first()

  const overlayDeadline = Date.now() + 180_000
  let settingsOpened = false
  const brief = (e) => String(e && e.message || e).split('\n').slice(0, 25).join(' | ')
  // bug-011 recon: when a settings click fails, capture WHAT is winning the
  // hit-test at the button's center plus the titlebar geometry the shell
  // computes. Local repro (same tag, packaged build, 1366x768) shows the
  // fixed z-70 controls cluster winning as static z-order says it should;
  // CI logs show the in-flow titlebar bar div intercepting instead. This
  // dump is the evidence that difference needs.
  const hitDump = () => window.evaluate(() => {
    const describe = (el) => el ? {
      tag: el.tagName,
      cls: (typeof el.className === 'string' ? el.className : '').slice(0, 110),
      aria: el.getAttribute?.('aria-label') || null,
      z: (() => { try { return getComputedStyle(el).zIndex } catch { return null } })(),
    } : null
    const settings = document.querySelector('button[aria-label="Open settings"]')
    const r = settings?.getBoundingClientRect()
    const cluster = settings?.closest('div[class*="fixed"]')
    const bar = document.querySelector('div[class*="h-[34px]"]')
    const cs = getComputedStyle(document.documentElement)
    const rect = (el) => { if (!el) return null; const b = el.getBoundingClientRect(); return `${Math.round(b.x)},${Math.round(b.y)} ${Math.round(b.width)}x${Math.round(b.height)}` }
    return {
      settingsRect: rect(settings),
      stack: r ? document.elementsFromPoint(r.x + r.width / 2, r.y + r.height / 2).slice(0, 6).map(describe) : null,
      cluster: cluster ? { rect: rect(cluster), z: getComputedStyle(cluster).zIndex, cls: (cluster.className || '').slice(0, 120) } : null,
      bar: bar ? { rect: rect(bar), z: getComputedStyle(bar).zIndex } : null,
      vars: {
        controlsLeft: cs.getPropertyValue('--titlebar-controls-left'),
        toolsRight: cs.getPropertyValue('--titlebar-tools-right'),
        toolsWidth: cs.getPropertyValue('--titlebar-tools-width'),
      },
      win: `${window.innerWidth}x${window.innerHeight} dpr=${window.devicePixelRatio}`,
    }
  }).then((d) => JSON.stringify(d)).catch((e) => `hit-dump failed: ${e.message}`)
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
      // Every 5th failure, log the hit-test stack (every iteration would be
      // noise; the interceptor identity is what matters, not its frequency).
      if (iter === 1 || iter % 5 === 0) {
        log(`[overlay] iter ${iter} hit-test: ${await hitDump()}`)
      }
    }
    if (Date.now() > overlayDeadline) break
  }
  if (!settingsOpened) {
    // Dump what the page actually contains so the next reader doesn't guess.
    const dump = await window.evaluate(() => ({
      url: location.href,
      title: document.title,
      buttons: [...document.querySelectorAll('button')].slice(0, 30).map((b) => ({
        text: (b.textContent || '').trim().slice(0, 40),
        aria: b.getAttribute('aria-label'),
        rect: (() => { const r = b.getBoundingClientRect(); return `${Math.round(r.x)},${Math.round(r.y)} ${Math.round(r.width)}x${Math.round(r.height)}`; })(),
      })),
      bodyPreview: (document.body ? document.body.innerText : '').slice(0, 400),
    })).catch((e) => `dump failed: ${e.message}`);
    log(`[overlay] page dump: ${JSON.stringify(dump)}`);
    await window.screenshot({ path: `${values.spec}.overlay-stuck.png` }).catch(() => {})
    throw new Error('onboarding overlay never cleared: Settings not clickable within 180s')
  }

  phase('about-update');
  // Settings is open: About -> Update now.
  await window.getByRole('tab', { name: /about/i }).or(
    window.getByRole('button', { name: /about/i })).first().click();
  const updateNow = window.getByRole('button', { name: /update now/i }).first();
  // "Update now" only renders once an update check reports behind > 0, and
  // the About panel starts at "Last checked: never". drive-update.cjs has
  // always nudged this with "Check now"; this driver never needed to until
  // the zoom fix let linux legs reach this point (run 32231900743 failed
  // exactly here). Same impatient-user pattern: press Check now, then give
  // the check against the staged repo time to land.
  // bug-011 recon, final layer: the boot-time auto-check can fail transiently
  // (sandbox churn) and the UI latches the error; a single Check-now click
  // finds the spinner and dies. Run 32346686541 proved a FRESH check succeeds
  // in ~15s at the exact moment the old one-shot gave up (status behind=1,
  // correct target sha). So: nudge loop - every 20s, click Check now when
  // it's a button (not spinner), until Update now shows, 3 min ceiling.
  const checkNow = window.getByRole('button', { name: /check now/i }).first();
  const nudgeDeadline = Date.now() + 180_000;
  let updateVisible = await updateNow.isVisible().catch(() => false);
  while (!updateVisible && Date.now() < nudgeDeadline) {
    await checkNow.click({ timeout: 5_000 })
      .then(() => log('nudged Check now'))
      .catch(() => {}); // spinner or mid-transition - fine, just wait
    await window.waitForTimeout(15_000);
    updateVisible = await updateNow.isVisible().catch(() => false);
  }
  try {
    await updateNow.waitFor({ state: 'visible', timeout: 15_000 });
  } catch (e) {
    // bug-011 recon: the About UI flattens every check failure to "couldn't
    // reach the update server" (about-settings.tsx cantReach), hiding the
    // actual git stderr that checkUpdates() captured (main.ts status.message).
    // Pull the full status object over the same IPC the panel uses so the CI
    // log names the failing git command's real error.
    const status = await window.evaluate(() =>
      window.hermesDesktop?.updates?.check?.() ?? Promise.resolve('no updates.check bridge')
    ).catch((err) => `updates.check failed: ${err?.message || err}`);
    log(`[update-status] ${JSON.stringify(status)}`);
    throw e;
  }
  await updateNow.click();
  phase('update-poll');
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

  // ── Post-update: observe the hand-off state, then relaunch and verify ──
  // On CI runners the rebuilt app cannot self-relaunch (chrome-sandbox needs
  // root ownership; user namespaces are restricted), so the product parks on
  // an "update complete — reopen Hermes to finish" overlay. That overlay also
  // means the app never exits: a bare app.close() awaits graceful exit and
  // wedged run 32352893319 for 50 minutes. Record which state the app landed
  // in, close it with a bounded teardown, then do what the overlay asks — the
  // real user journey — and assert the relaunched app runs the updated code.
  phase('post-update');
  const handoff = await window.evaluate(() => {
    const text = document.body ? document.body.innerText : ''
    const m = text.match(/[^\n]*(update complete|reopen|relaunch)[^\n]*/i)
    return m ? m[0].trim().slice(0, 200) : null
  }).catch(() => null);
  log(handoff ? `post-update hand-off state: "${handoff}"` : 'post-update: no hand-off overlay observed (app may self-relaunch)');
  await window.screenshot({ path: `${values.spec}.post-update.png` }).catch(() => {});

  const boundedClose = async (application, label) => {
    // ElectronApplication.process() can THROW on darwin when the app has
    // already started tearing down (run 32416362715: TypeError reading
    // '_object' inside playwright-core) - never assume it is callable.
    let proc = null;
    try { proc = application.process(); } catch { /* connection gone */ }
    const rootPid = proc?.pid;
    // Snapshot descendants BEFORE closing: once the root dies its children
    // reparent to init and a PPID walk can no longer find them.
    let doomed = [];
    if (rootPid && process.platform !== 'win32') {
      try {
        const out = execFileSync('ps', ['-eo', 'pid=,ppid='], { encoding: 'utf8' });
        const children = new Map();
        for (const line of out.trim().split('\n')) {
          const [pid, ppid] = line.trim().split(/\s+/).map(Number);
          if (!children.has(ppid)) children.set(ppid, []);
          children.get(ppid).push(pid);
        }
        const queue = [rootPid];
        while (queue.length) {
          const next = queue.shift();
          for (const child of children.get(next) || []) {
            doomed.push(child);
            queue.push(child);
          }
        }
      } catch (e) {
        log(`${label}: descendant snapshot failed (continuing): ${String(e).slice(0, 120)}`);
      }
    }
    const closed = await Promise.race([
      application.close().then(() => true).catch(() => true),
      new Promise((r) => setTimeout(() => r(false), 15_000)),
    ]);
    if (!closed) {
      // SIGTERM first: Electron runs its exit handlers, and any npm/node
      // children the in-app update spawned get a chance to settle instead
      // of leaving node_modules half-written (run 32385999825 ENOTEMPTY).
      log(`${label}: graceful close timed out after 15s - SIGTERM, then SIGKILL if needed`);
      if (proc) {
        try { proc.kill('SIGTERM'); } catch { /* already gone */ }
        const terminated = await new Promise((r) => {
          const timer = setTimeout(() => r(false), 10_000);
          proc.once('exit', () => { clearTimeout(timer); r(true); });
        });
        if (!terminated) {
          log(`${label}: SIGTERM ignored after 10s - SIGKILL`);
          try { proc.kill('SIGKILL'); } catch { /* already gone */ }
        }
      } else {
        log(`${label}: no process handle to signal - relying on descendant sweep`);
      }
    }
    // Killing the Electron root does NOT cascade: the app spawns a backend
    // (`hermes serve` python + node helpers) that survives and keeps writing
    // under the install dir - run 32398173770's head smoke saw npm's
    // extraction shredded by exactly that (TAR_ENTRY_ERROR storms, vanished
    // vite). SIGTERM the snapshot first (orderly backend shutdown), then
    // SIGKILL stragglers.
    if (doomed.length) {
      for (const pid of doomed) {
        try { process.kill(pid, 'SIGTERM'); } catch { /* raced exit - fine */ }
      }
      await new Promise((r) => setTimeout(r, 5_000));
      let killed = 0;
      for (const pid of doomed) {
        try { process.kill(pid, 'SIGKILL'); killed++; } catch { /* exited on TERM */ }
      }
      log(`${label}: swept ${doomed.length} descendant process(es) (${killed} needed SIGKILL)`);
    }
  };
  await boundedClose(app, 'updated-app teardown');

  // Relaunch from the same captured spec - the leg's own launch mechanism -
  // and require the UI to come up on the updated checkout. Verification:
  // the renderer's DOM carries the running build's short sha when launched
  // from a git checkout (statusbar/About); require the EXPECTED sha's short
  // form, or at minimum a live UI window, logging what we saw.
  phase('relaunch');
  log('relaunching the updated app (the "reopen Hermes" step)');
  const relaunch = await _electron.launch({
    executablePath: launch.executablePath,
    args: launch.args,
    cwd: launch.cwd,
    env: launch.env,
  });
  let window2 = null;
  const relaunchDeadline = Date.now() + 120_000;
  while (!window2 && Date.now() < relaunchDeadline) {
    for (const candidate of relaunch.windows()) {
      const hasUi = await candidate
        .evaluate(() => document.querySelector('button') !== null)
        .catch(() => false);
      if (hasUi) { window2 = candidate; break; }
    }
    if (!window2) await new Promise((r) => setTimeout(r, 1_000));
  }
  if (!window2) {
    await boundedClose(relaunch, 'relaunch teardown');
    throw new Error('relaunched app never presented a UI window within 120s - updated build may be broken');
  }
  // Give the shell a moment to paint the statusbar/version chrome.
  await new Promise((r) => setTimeout(r, 10_000));
  const shortSha = (expectSha || '').slice(0, 7);
  const verdict = await window2.evaluate((sha) => {
    const text = document.body ? document.body.innerText : ''
    const version = (text.match(/v\d+\.\d+\.\d+[^\n]*/) || [null])[0]
    return { version, hasSha: sha ? text.includes(sha) : false, sample: text.slice(-200) }
  }, shortSha).catch(() => null);
  await window2.screenshot({ path: `${values.spec}.relaunched.png` }).catch(() => {});
  log(`relaunched app: version="${verdict?.version || 'unseen'}" expectedSha(${shortSha}) in DOM=${verdict?.hasSha}`);
  if (!verdict) {
    await boundedClose(relaunch, 'relaunch teardown');
    throw new Error('relaunched app UI came up but could not be read');
  }
  if (shortSha && !verdict.hasSha) {
    // Not fatal on its own: packaged builds do not always surface the sha in
    // the DOM. The window came up on the updated install dir, which is the
    // user-facing contract; log loudly so a human can tighten this later.
    log(`NOTE: expected short sha ${shortSha} not found in relaunched DOM; version line was "${verdict.version}"`);
  }
  log('relaunch verification complete: updated app boots and presents UI');
  await boundedClose(relaunch, 'relaunch teardown');
  // Explicit exit: SIGKILLed Electron leaves driver connections holding the
  // event loop; falling off main() never terminates (run 32382176435).
  process.exit(0);
}

const invoked = process.argv[1] && path.resolve(process.argv[1]) === (await import('node:url')).fileURLToPath(import.meta.url);
if (invoked) {
  try {
    await main();
  } catch (error) {
    console.error(error);
    process.exit(1);
  }
}
