import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'vitest'

import { prunePayload, resolveTargets } from '../scripts/stage-agent-payloads.mjs'

// ─── prunePayload ──────────────────────────────────────────────────
//
// Real temp trees, no fs mocks: the function's contract is filesystem
// behavior (delete these members, keep those), so the test builds a
// miniature payload and asserts survivor/victim sets.

function mkFile(root, rel, bytes = 8) {
  const full = path.join(root, rel)
  fs.mkdirSync(path.dirname(full), { recursive: true })
  fs.writeFileSync(full, Buffer.alloc(bytes, 0x61))
  return full
}

function mkPayload({ platform, arch }) {
  const out = fs.mkdtempSync(path.join(os.tmpdir(), 'prune-payload-'))
  const gitEntry = `git-2.53.0-${platform}-${arch}`

  // repo/: victims + survivors
  mkFile(out, 'repo/tests/hermes_cli/test_x.py')
  mkFile(out, 'repo/website/docs/index.md')
  mkFile(out, 'repo/hermes_cli/main.py')
  mkFile(out, 'repo/apps/desktop/package.json')
  mkFile(out, 'repo/skills/coding/SKILL.md')

  // python/: Windows layout (Lib/) with victims + load-bearing survivors
  const py = 'python/cpython-3.11.15-windows-aarch64-none'
  mkFile(out, `${py}/Lib/tkinter/__init__.py`)
  mkFile(out, `${py}/Lib/idlelib/idle.py`)
  mkFile(out, `${py}/Lib/pydoc_data/topics.py`)
  mkFile(out, `${py}/Lib/turtledemo/clock.py`)
  mkFile(out, `${py}/Lib/turtle.py`)
  mkFile(out, `${py}/Lib/test/test_os.py`)
  mkFile(out, `${py}/tcl/tcl8.6/init.tcl`)
  mkFile(out, `${py}/DLLs/tcl86t.dll`)
  mkFile(out, `${py}/DLLs/_tkinter.pyd`)
  mkFile(out, `${py}/Lib/ensurepip/__init__.py`)
  mkFile(out, `${py}/Lib/venv/__init__.py`)
  mkFile(out, `${py}/Lib/site-packages/pip/__init__.py`)
  mkFile(out, `${py}/Lib/asyncio/__init__.py`)
  mkFile(out, `${py}/DLLs/_ssl.pyd`)

  // git/: MSYS fat + the binaries that must survive
  mkFile(out, `${gitEntry}/cmd/git.exe`)
  mkFile(out, `${gitEntry}/bin/bash.exe`)
  mkFile(out, `${gitEntry}/usr/bin/sh.exe`)
  mkFile(out, `${gitEntry}/usr/share/perl5/Git.pm`)
  mkFile(out, `${gitEntry}/usr/lib/perl5/core_perl/Config.pm`)
  mkFile(out, `${gitEntry}/clangarm64/share/doc/git-doc/git.html`)
  mkFile(out, `${gitEntry}/usr/share/locale/de/LC_MESSAGES/git.mo`)
  mkFile(out, `${gitEntry}/clangarm64/share/gitk/lib/gitk.tcl`)
  mkFile(out, `${gitEntry}/clangarm64/lib/tcl8.6/init.tcl`)
  mkFile(out, `${gitEntry}/clangarm64/bin/git.exe`)
  mkFile(out, `${gitEntry}/usr/bin/perl.exe`)

  // site-packages survivor
  mkFile(out, 'site-packages/pydantic/__init__.py')

  // facts: the layout authority prunePayload reads the git root from
  fs.writeFileSync(
    path.join(out, 'runtimes.json'),
    JSON.stringify({
      schemaVersion: 2,
      tools: { git: { version: '2.53.0', path: `${gitEntry}/cmd/git.exe` } }
    })
  )
  return { out, gitEntry }
}

test('prunePayload removes the named dead weight and nothing else (win32)', () => {
  const target = resolveTargets('win32', 'arm64')
  const { out, gitEntry } = mkPayload({ platform: 'win32', arch: 'arm64' })

  const reclaimed = prunePayload(out, target)
  assert.ok(reclaimed > 0, 'reports reclaimed bytes')

  const gone = [
    'repo/tests',
    'repo/website',
    'python/cpython-3.11.15-windows-aarch64-none/Lib/tkinter',
    'python/cpython-3.11.15-windows-aarch64-none/Lib/idlelib',
    'python/cpython-3.11.15-windows-aarch64-none/Lib/pydoc_data',
    'python/cpython-3.11.15-windows-aarch64-none/Lib/turtledemo',
    'python/cpython-3.11.15-windows-aarch64-none/Lib/turtle.py',
    'python/cpython-3.11.15-windows-aarch64-none/Lib/test',
    'python/cpython-3.11.15-windows-aarch64-none/tcl',
    'python/cpython-3.11.15-windows-aarch64-none/DLLs/tcl86t.dll',
    'python/cpython-3.11.15-windows-aarch64-none/DLLs/_tkinter.pyd',
    `${gitEntry}/usr/share/perl5`,
    `${gitEntry}/usr/lib/perl5`,
    `${gitEntry}/clangarm64/share/doc`,
    `${gitEntry}/usr/share/locale`,
    `${gitEntry}/clangarm64/share/gitk`,
    `${gitEntry}/clangarm64/lib/tcl8.6`
  ]
  for (const rel of gone) {
    assert.ok(!fs.existsSync(path.join(out, rel)), `expected pruned: ${rel}`)
  }

  const kept = [
    'repo/hermes_cli/main.py',
    'repo/apps/desktop/package.json',
    'repo/skills/coding/SKILL.md',
    // pip-ladder tiers and eject flows stay intact
    'python/cpython-3.11.15-windows-aarch64-none/Lib/ensurepip/__init__.py',
    'python/cpython-3.11.15-windows-aarch64-none/Lib/venv/__init__.py',
    'python/cpython-3.11.15-windows-aarch64-none/Lib/site-packages/pip/__init__.py',
    'python/cpython-3.11.15-windows-aarch64-none/Lib/asyncio/__init__.py',
    'python/cpython-3.11.15-windows-aarch64-none/DLLs/_ssl.pyd',
    // git binaries and the bash the terminal tool needs
    `${gitEntry}/cmd/git.exe`,
    `${gitEntry}/bin/bash.exe`,
    `${gitEntry}/usr/bin/sh.exe`,
    `${gitEntry}/clangarm64/bin/git.exe`,
    // perl.exe itself survives (only the module trees go)
    `${gitEntry}/usr/bin/perl.exe`,
    'site-packages/pydantic/__init__.py'
  ]
  for (const rel of kept) {
    assert.ok(fs.existsSync(path.join(out, rel)), `expected kept: ${rel}`)
  }

  fs.rmSync(out, { recursive: true, force: true })
})

test('prunePayload is idempotent (second run reclaims zero)', () => {
  const target = resolveTargets('win32', 'arm64')
  const { out } = mkPayload({ platform: 'win32', arch: 'arm64' })
  prunePayload(out, target)
  const second = prunePayload(out, target)
  assert.equal(second, 0)
  fs.rmSync(out, { recursive: true, force: true })
})

test('prunePayload leaves the git store alone on POSIX targets', () => {
  const target = resolveTargets('darwin', 'arm64')
  const out = fs.mkdtempSync(path.join(os.tmpdir(), 'prune-posix-'))
  mkFile(out, 'repo/tests/test_x.py')
  mkFile(out, 'git-2.53.0-darwin-arm64/share/doc/git.html')
  fs.writeFileSync(
    path.join(out, 'runtimes.json'),
    JSON.stringify({ tools: { git: { path: 'git-2.53.0-darwin-arm64/bin/git' } } })
  )
  prunePayload(out, target)
  // repo prune is cross-platform…
  assert.ok(!fs.existsSync(path.join(out, 'repo/tests')))
  // …but the dugite-native tree is already lean: untouched.
  assert.ok(fs.existsSync(path.join(out, 'git-2.53.0-darwin-arm64/share/doc/git.html')))
  fs.rmSync(out, { recursive: true, force: true })
})

test('prunePayload survives a missing runtimes.json (repo/python prune still runs)', () => {
  const target = resolveTargets('win32', 'x64')
  const out = fs.mkdtempSync(path.join(os.tmpdir(), 'prune-nofacts-'))
  mkFile(out, 'repo/website/index.md')
  assert.doesNotThrow(() => prunePayload(out, target))
  assert.ok(!fs.existsSync(path.join(out, 'repo/website')))
  fs.rmSync(out, { recursive: true, force: true })
})
