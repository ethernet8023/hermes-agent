#!/usr/bin/env node
// build-bundled-desktop.mjs — one local=CI driver for a bundled desktop
// installer. Every step always runs. A skipped step is a different artifact.
//
//   1. preflight: git + npm; a release tag is resolvable
//   2. npm ci at the repo root (stamp-gated)
//   3. build ui-tui and the dashboard SPA
//   4. pm bundle (repo snapshot + tool store + relocatable venv)
//   5. plant the just-built JS surfaces into the staged payload
//      (git archive only carries committed files; those dists are gitignored)
//   6. npm run build + builder in apps/desktop
//
// Usage:
//   node scripts/build-bundled-desktop.mjs --tag=v0.20.5
//   node scripts/build-bundled-desktop.mjs --tag=v0.20.5 --variant=bundled
//
// Signing is CI's job. Local builds are unsigned.

import { createHash } from 'node:crypto'
import { execSync, spawnSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { windowsFileVersion } from '../apps/desktop/scripts/windows-file-version.mjs'
import { materializePayloadLinks, stripFetchCache } from '../apps/desktop/scripts/materialize-payload-links.mjs'

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const PAYLOAD_DIR = path.join(REPO_ROOT, 'apps', 'desktop', 'build', 'agent-payload')

const args = process.argv.slice(2)
for (const flag of ['--no-install', '--no-package']) {
  if (args.includes(flag)) {
    fail(`${flag} is retired: every release build runs every step`)
  }
}
const tagArg = args.find(a => a.startsWith('--tag='))?.slice('--tag='.length)
const variant = args.find(a => a.startsWith('--variant='))?.slice('--variant='.length) || 'bundled'
const dashDash = process.argv.indexOf('--')
const extraBuilderArgs = dashDash === -1 ? [] : process.argv.slice(dashDash + 1)

if (!['bundled', 'light'].includes(variant)) {
  fail(`--variant must be 'bundled' or 'light', got '${variant}'`)
}

function fail(message) {
  console.error(`[build-bundled] ${message}`)
  process.exit(1)
}

function run(cmd, argv, opts = {}) {
  console.log(`[build-bundled] $ ${cmd} ${argv.join(' ')}`)
  const shell = process.platform === 'win32'
  if (shell) {
    const bad = argv.find(a => /\s/.test(a))
    if (bad) {
      fail(
        `argument with whitespace cannot cross the Windows shell: ${JSON.stringify(bad)} — pass it via environment instead`
      )
    }
  }
  const result = spawnSync(cmd, argv, { stdio: 'inherit', cwd: REPO_ROOT, shell, ...opts })
  if (result.status !== 0) {
    fail(`${cmd} exited ${result.status}`)
  }
}

function capture(cmd) {
  return execSync(cmd, { cwd: REPO_ROOT, encoding: 'utf8' }).trim()
}

function pythonMinorFromLock() {
  const lock = JSON.parse(fs.readFileSync(path.join(REPO_ROOT, 'pm', 'lock.json'), 'utf8'))
  const version = lock?.packages?.python?.version
  if (typeof version !== 'string') {
    fail('pm/lock.json has no python version')
  }
  return version.split('+')[0].split('.').slice(0, 2).join('.')
}

function requireOnPath(tool) {
  const probe = spawnSync(tool, ['--version'], { stdio: 'ignore', shell: process.platform === 'win32' })
  if (probe.status !== 0) {
    fail(`required tool missing: ${tool}`)
  }
}

// ── 1. preflight ────────────────────────────────────────────────────────────

for (const tool of ['git', 'npm', 'uv']) {
  requireOnPath(tool)
}

// Toolchain gates. The build's output depends on these tools, so a wrong
// version makes a silently different artifact (the first Windows build
// shipped a wrong-arch uv exactly this way). The rules come from ONE
// source — package.json "engines". The EMBEDDED runtimes are a separate
// concern: they come from pm/lock.json via `pm bundle` (the payload
// python/node/uv are the pinned artifacts in the pm store), never from
// the host toolchain — the gates below only approve the tools that BUILD
// the artifact (the JS surfaces are built and npm-installed by the host
// node; the payload interpreter is installed by the host uv). CI installs
// the pinned versions from pm/lock.json as the host toolchain, so
// gate == pin there by construction.
export function parseVersion(text) {
  const match = String(text).match(/(\d+)\.(\d+)\.(\d+)/)
  return match ? [Number(match[1]), Number(match[2]), Number(match[3])] : null
}

export function compareVersions(a, b) {
  for (let i = 0; i < 3; i += 1) {
    if (a[i] !== b[i]) return a[i] - b[i]
  }
  return 0
}

// The subset of semver ranges that package.json engines actually uses:
// space-separated comparators AND together, `||` separates alternatives.
// An unparseable comparator fails closed.
export function satisfiesRange(version, range) {
  return String(range).split('||').some(alternative => {
    const comparators = alternative.trim().split(/\s+/).filter(Boolean)
    if (comparators.length === 0) return false
    return comparators.every(comparator => {
      const m = comparator.match(/^(>=|<=|>|<|=)?v?(\d+)\.(\d+)\.(\d+)$/)
      if (!m) return false
      const cmp = compareVersions(version, [Number(m[2]), Number(m[3]), Number(m[4])])
      switch (m[1]) {
        case '>=': return cmp >= 0
        case '<=': return cmp <= 0
        case '>': return cmp > 0
        case '<': return cmp < 0
        default: return cmp === 0
      }
    })
  })
}

export function uvBannerProblem(banner) {
  // A build triple is three dash-joined words that end in letters
  // (aarch64-pc-windows-msvc). Its position varies: nix builds print it
  // first in the parens, official builds put a commit hash and a date
  // before it. Match it anywhere — the date (2026-07-31) cannot match
  // because its last segment is digits.
  return /[a-z0-9_]+-[a-z0-9]+-[a-z][a-z0-9-]*/.test(String(banner))
    ? null
    : 'its --version prints no build triple; the payload arch guard needs one (official uv 0.12+, or any nix/source build)'
}

const engines = JSON.parse(fs.readFileSync(path.join(REPO_ROOT, 'package.json'), 'utf8')).engines || {}

for (const tool of ['node', 'npm']) {
  const text = tool === 'node' ? process.version : capture('npm --version')
  const version = parseVersion(text)
  if (!version) {
    fail(`${tool}: cannot parse a version from ${JSON.stringify(text)}`)
  }
  const range = engines[tool]
  if (range && !satisfiesRange(version, range)) {
    fail(`${tool} ${version.join('.')} does not satisfy package.json engines ${JSON.stringify(range)} — the build would make a different artifact`)
  }
  console.log(`[build-bundled] ${tool} ${version.join('.')} (engines: ${range || 'unconstrained'})`)
}

{
  const uvBanner = capture('uv --version')
  const problem = uvBannerProblem(uvBanner)
  if (problem) {
    fail(`uv (${uvBanner}) would make a broken artifact: ${problem}`)
  }
  console.log(`[build-bundled] ${uvBanner} (build-host uv; the payload uv comes from pm/lock.json)`)
}

let tag = tagArg
if (!tag) {
  try {
    tag = capture('git describe --tags --exact-match')
  } catch {
    fail('no --tag=vX.Y.Z given and HEAD is not at an exact release tag')
  }
}
if (!/^v(?:0|[1-9]\d{0,2})\.\d+\.\d+(?:-nightly\.20\d{6}(?:\d{6})?)?$/.test(tag)) {
  fail(`'${tag}' is not a release tag (vX.Y.Z or vX.Y.0-nightly.YYYYMMDDHHMMSS)`)
}

const pyprojectVersion = fs
  .readFileSync(path.join(REPO_ROOT, 'pyproject.toml'), 'utf8')
  .match(/^version\s*=\s*"([^"]+)"/m)?.[1]
if (!pyprojectVersion) {
  fail('could not read version from pyproject.toml')
}
const isNightly = tag.includes('-nightly.')
if (!isNightly && tag !== `v${pyprojectVersion}`) {
  fail(`tag ${tag} does not match pyproject.toml version ${pyprojectVersion}`)
}
const artifactVersion = isNightly ? tag.slice(1) : pyprojectVersion
const fileVersion = windowsFileVersion(tag)

const passes = {
  linux: [{ targets: '--linux AppImage' }],
  darwin: [{ targets: '--mac dmg zip' }],
  win32: [{ targets: '--win msix' }]
}[process.platform]
if (!passes) {
  fail(`unsupported platform: ${process.platform}`)
}

console.log(`[build-bundled] tag=${tag} variant=${variant} platform=${process.platform}-${process.arch}`)

// ── 2. npm ci ───────────────────────────────────────────────────────────────

const installStamp = [
  `lock=${createHash('sha256').update(fs.readFileSync(path.join(REPO_ROOT, 'package-lock.json'))).digest('hex')}`,
  `node=${process.version}`,
  `npm=${execSync('npm --version', { encoding: 'utf8', shell: process.platform === 'win32' }).trim()}`,
  `target=${process.platform}-${process.arch}`
].join(' ')
const installStampPath = path.join(REPO_ROOT, 'node_modules', '.install-stamp')
if (fs.existsSync(installStampPath) && fs.readFileSync(installStampPath, 'utf8') === installStamp) {
  console.log('[build-bundled] node_modules matches its install stamp — npm ci output already present')
} else {
  fs.rmSync(installStampPath, { force: true })
  run('npm', ['ci', '--no-audit', '--no-fund', '--fetch-retries=5'], {
    env: { ...process.env, CI: 'true' }
  })
  fs.writeFileSync(installStampPath, installStamp)
}

// ── 3. JS surfaces ──────────────────────────────────────────────────────────

run('npm', ['run', 'build', '--workspace', 'ui-tui'])
run('npm', ['run', 'build', '--workspace', 'web'])

const tuiEntry = path.join(REPO_ROOT, 'ui-tui', 'dist', 'entry.js')
const webDist = path.join(REPO_ROOT, 'hermes_cli', 'web_dist')
if (!fs.existsSync(tuiEntry)) {
  fail(`ui-tui build did not write ${tuiEntry}`)
}
if (!fs.existsSync(path.join(webDist, 'index.html'))) {
  fail(`web build did not write ${webDist}/index.html`)
}

// ── 4. pm bundle ────────────────────────────────────────────────────────────

const pyMinor = pythonMinorFromLock()
run(
  'uv',
  ['run', '--no-project', '--python', pyMinor, 'python', '-m', 'pm.cli', 'bundle', '--out', PAYLOAD_DIR, '--ref', tag],
  { env: { ...process.env, PYTHONUTF8: '1' } }
)

// ── 5. plant JS into the staged snapshot ────────────────────────────────────
// git archive only carries committed files. The TUI and dashboard dists
// are gitignored, so they must be copied into the payload after staging.

const payloadRepo = path.join(PAYLOAD_DIR, 'hermes-agent')
if (!fs.existsSync(path.join(payloadRepo, 'pyproject.toml'))) {
  fail(`pm bundle did not write a repo snapshot at ${payloadRepo}`)
}

const plantedTui = path.join(payloadRepo, 'hermes_cli', 'tui_dist', 'entry.js')
fs.mkdirSync(path.dirname(plantedTui), { recursive: true })
fs.copyFileSync(tuiEntry, plantedTui)

const plantedWeb = path.join(payloadRepo, 'hermes_cli', 'web_dist')
fs.rmSync(plantedWeb, { recursive: true, force: true })
fs.cpSync(webDist, plantedWeb, { recursive: true, dereference: true })
if (!fs.existsSync(path.join(plantedWeb, 'index.html'))) {
  fail('planted web_dist is missing index.html')
}
console.log('[build-bundled] planted hermes_cli/tui_dist/entry.js and hermes_cli/web_dist into the payload')

const dropped = stripFetchCache(PAYLOAD_DIR)
console.log(`[build-bundled] dropped ${dropped} fetch- cache dirs from the payload`)
if (process.platform === 'darwin') {
  const n = materializePayloadLinks(PAYLOAD_DIR)
  console.log(`[build-bundled] materialized ${n} payload links so codesign can sign each path once`)
}

// ── 6. desktop build + package ──────────────────────────────────────────────

const env = {
  ...process.env,
  HERMES_DESKTOP_VARIANT: variant,
  HERMES_PAYLOAD_TAG: tag
}
const desktop = path.join(REPO_ROOT, 'apps', 'desktop')

for (const pass of passes) {
  console.log(`[build-bundled] pass: ${pass.targets}`)
  run('npm', ['run', 'build'], { cwd: desktop, env })
  run(
    'npm',
    [
      'run',
      'builder',
      '--',
      ...pass.targets.split(' '),
      `-c.extraMetadata.version=${artifactVersion}`,
      ...(fileVersion
        ? [`-c.extraMetadata.shortVersion=${fileVersion}`, `-c.extraMetadata.shortVersionWindows=${fileVersion}`]
        : []),
      ...extraBuilderArgs
    ],
    { cwd: desktop, env }
  )
}
console.log(`[build-bundled] artifacts: ${path.join(desktop, 'release')}`)
