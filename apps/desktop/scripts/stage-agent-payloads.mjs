/**
 * stage-agent-payloads.mjs: assemble the resources-resident agent runtime
 * that ships inside the bundled desktop artifact. Design:
 * .hermes/plans/2026-08-07_resources-resident-bundled-runtime.md.
 *
 * Output: apps/desktop/build/agent-payload/
 *   manifest.json          schemaVersion, tag, commit, platform, arch
 *   repo/                  plain source tree at the release tag (no .git),
 *                          plus the PREBUILT JS surfaces (ui-tui dist +
 *                          node_modules, web_dist) and the build stamp
 *   uv/                    static uv binary for this platform/arch
 *   python/                uv-managed CPython (python-build-standalone).
 *                          Its own site-packages carries hermes-bundle.pth
 *                          with RELATIVE paths to repo/ and site-packages/,
 *                          so the interpreter resolves the runtime wherever
 *                          the app bundle sits — no venv, no PYTHONPATH.
 *   site-packages/         the full dependency tree from uv.lock, installed
 *                          at build time with `pip install --target` on the
 *                          payload interpreter. The backend runs directly
 *                          from here; nothing materializes at first launch.
 *   node/                  official node dist for this platform/arch
 *
 * Gating: the script does nothing unless HERMES_DESKTOP_VARIANT=bundled.
 * That variable is an internal build-time env for CI wiring, not user
 * config. Thus dev builds and current CI keep producing external builds.
 * There is no per-item skip: an embedded payload is complete, or this
 * script throws and the build fails.
 *
 * The heavy work shells out to git, uv, and tar. The decision logic
 * (target resolution, pip arg construction, manifest shape) is exported as
 * pure functions. Thus vitest covers it without network or toolchains.
 */

import { execSync, spawnSync } from "node:child_process"
import { createHash } from "node:crypto"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"

import { isMain } from "./utils.mjs"

export const PAYLOAD_SCHEMA_VERSION = 3

const DESKTOP_ROOT = path.resolve(import.meta.dirname, "..")
const REPO_ROOT = path.resolve(DESKTOP_ROOT, "..", "..")
const OUT_DIR = path.join(DESKTOP_ROOT, "build", "agent-payload")

/**
 * Map (process.platform, process.arch) to the uv, python-build-standalone,
 * and node target descriptors. There is one artifact per (os, arch) pair.
 * Mac universal2 is deliberately NOT a target. We ship two artifacts
 * (plan §6).
 *
 * There are no cross-platform wheel tags here, on purpose. A CI runner per
 * (os, arch) pair assembles the payloads. electron-builder needs per-OS
 * runners for signing anyway. Thus the script fetches wheels NATIVELY with
 * `uvx pip wheel --only-binary=:all:`. The platform of the runner is the
 * target platform.
 */
export function resolveTargets(platform = process.platform, arch = process.arch) {
  const table = {
    "linux-x64": {
      uvTarget: "x86_64-unknown-linux-gnu",
      pythonPlatform: "x86_64-unknown-linux-gnu",
      nodeDist: "linux-x64",
      uvPython: "linux-x86_64-gnu",
    },
    "linux-arm64": {
      uvTarget: "aarch64-unknown-linux-gnu",
      pythonPlatform: "aarch64-unknown-linux-gnu",
      nodeDist: "linux-arm64",
      uvPython: "linux-aarch64-gnu",
    },
    "darwin-x64": {
      uvTarget: "x86_64-apple-darwin",
      pythonPlatform: "x86_64-apple-darwin",
      nodeDist: "darwin-x64",
      uvPython: "macos-x86_64-none",
      // cryptography 49+ publishes macOS wheels for arm64 only (48.0.1
      // was the last universal2). The pin is a security floor, so the
      // Intel artifact builds the EXACT pinned version from sdist on
      // the runner (needs Rust — preinstalled on the macos GitHub
      // runners), same arrangement as win32-arm64 below.
      sourceBuild: ["cryptography"],
    },
    "darwin-arm64": {
      uvTarget: "aarch64-apple-darwin",
      pythonPlatform: "aarch64-apple-darwin",
      nodeDist: "darwin-arm64",
      uvPython: "macos-aarch64-none",
    },
    "win32-x64": {
      uvTarget: "x86_64-pc-windows-msvc",
      pythonPlatform: "x86_64-pc-windows-msvc",
      nodeDist: "win-x64",
      uvPython: "windows-x86_64-none",
    },
    "win32-arm64": {
      uvTarget: "aarch64-pc-windows-msvc",
      pythonPlatform: "aarch64-pc-windows-msvc",
      nodeDist: "win-arm64",
      uvPython: "windows-aarch64-none",
      // Pinned packages with no published win_arm64 wheel. pip builds
      // these from sdist on the runner (needs MSVC arm64 + Rust).
      // pyyaml publishes win_arm64 wheels for cp312+ only — the payload
      // python is 3.11, so it builds here too (pure fallback when the
      // libyaml accelerator is unavailable).
      sourceBuild: ["cryptography", "httptools", "ruamel-yaml-clib", "pywinpty", "pyyaml"],
    },
  }
  const key = `${platform}-${arch}`
  const target = table[key]
  if (!target) {
    throw new Error(`unsupported payload target: ${key}`)
  }
  return { key, platform, arch, ...target }
}

/**
 * Build the `pip install --target` argument list that fills the payload's
 * site-packages. The caller invokes it through `uvx pip …` ON the staged
 * payload interpreter, natively on the target runner, so wheels resolve
 * for the target platform/arch. With --only-binary=:all: it never
 * compiles on the user machine — there IS no install step on the user
 * machine; the backend imports straight from this directory.
 *
 * Exception: the target's sourceBuild list. Some pinned packages publish
 * no wheel for a target (win32-arm64: cryptography dropped win_arm64
 * after 46.0.3; httptools and ruamel-yaml-clib never shipped one;
 * pywinpty 2.x has none). For those named packages pip builds the
 * EXACT pinned version from its sdist ON the build runner, which yields
 * real target-arch code in site-packages — the user machine still
 * never compiles. The build runner needs the toolchains (MSVC arm64 +
 * Rust on windows-11-arm). A later --no-binary overrides --only-binary
 * per package; the list stays empty for every target whose pins are
 * fully covered by published wheels.
 */
export function pipTargetArgs({ sitePackagesDir, sourceBuild = [] }) {
  return [
    "install",
    "--only-binary", ":all:",
    ...(sourceBuild.length > 0 ? ["--no-binary", sourceBuild.join(",")] : []),
    "-r", "requirements-payload.txt",
    "--target", sitePackagesDir,
    // pip warns without this when --target sees an existing dir; staging
    // wipes first, so upgrade semantics never actually apply.
    "--upgrade",
    // No console-script shims: the bundle always launches `python -m`,
    // and --target's scripts would carry the BUILD host's shebang paths.
    "--no-compile",
  ]
}

/**
 * The full uv python-install request for a target: version AND platform.
 * A bare version request ("3.11") lets uv fall back to another
 * architecture when the native build is unavailable — the arm64 Windows
 * test box got a silent x86_64 CPython that way. The full request either
 * installs the right build or fails loudly.
 */
export function pythonRequest(target, version = process.env.HERMES_PAYLOAD_PYTHON || "3.11") {
  return `cpython-${version}-${target.uvPython}`
}

/**
 * Assert that a staged tool's own version banner names the target triple.
 * `uv --version` and `python -VV` both print their build triple/platform.
 * A mismatch means the payload carries the WRONG architecture (for
 * example, an x64 uv copied from PATH into an arm64 artifact — it runs
 * on the build host through emulation and ships broken). The manifest
 * would then lie about the payload's contents. Fail the build instead.
 */
export function assertBanner(item, banner, mustContain) {
  if (!banner.includes(mustContain)) {
    throw new Error(
      `${item}: staged binary reports "${banner.trim()}" which does not ` +
        `contain the build target "${mustContain}" — wrong-architecture ` +
        `payload. Provide a matching binary (HERMES_PAYLOAD_UV for uv) or ` +
        `build on a native runner.`
    )
  }
}

/**
 * The substring that each staged tool's banner must contain for a target.
 * uv prints a full triple (x86_64-pc-windows-msvc). CPython's `python -VV`
 * prints a compiler/platform line that differs per OS, so the check keys
 * on the architecture words for it. Node prints nothing useful in
 * --version, so its check uses `node -p process.arch` = target arch.
 */
export function bannerExpectations(target) {
  const archWords = {
    x64: ["x86_64", "AMD64", "x64"],
    arm64: ["aarch64", "ARM64", "arm64"],
  }[target.arch]

  return {
    uv: target.uvTarget,
    pythonAny: archWords,
    node: target.arch,
  }
}


/**
 * Resolve the release tag to stage. CI passes --tag=vX.Y.Z. Local runs can
 * fall back to `git describe` for smoke tests. When bundling was requested
 * and no tag exists, payload staging is a hard error. A bundled artifact
 * without a pinned tag produces un-updatable installs.
 */
export function resolveTag(argv, describeFn) {
  const explicit = argv.find((a) => a.startsWith("--tag="))
  if (explicit) {
    const tag = explicit.slice("--tag=".length).trim()
    if (!/^v(?:0|[1-9]\d{0,2})\.\d+\.\d+$/.test(tag)) {
      throw new Error(`--tag must be a final release tag (vX.Y.Z), got: ${tag}`)
    }
    return tag
  }
  const described = describeFn()
  if (described && /^v(?:0|[1-9]\d{0,2})\.\d+\.\d+$/.test(described)) {
    return described
  }
  throw new Error(
    "no release tag: pass --tag=vX.Y.Z (CI) or run from a checkout at an exact release tag"
  )
}

/**
 * Build the manifest that marks a complete embedded payload. The Electron
 * main process treats its presence (schemaVersion match, external: absent)
 * as the payload-present sentinel. Completeness is a build-time invariant:
 * main() throws before this manifest is written when any stage fails.
 */
export function buildManifest({ tag, commit, target }) {
  return {
    schemaVersion: PAYLOAD_SCHEMA_VERSION,
    tag,
    commit,
    platform: target.platform,
    arch: target.arch,
    builtAt: new Date().toISOString(),
  }
}

/**
 * The cache identity of the python/ + site-packages/ pair. These two
 * stages dominate staging time (win32-arm64 compiles cryptography and
 * friends from sdist with MSVC + Rust for 15+ minutes), and their content
 * is a pure function of exactly these inputs — the release tag is NOT one
 * of them. When the key matches a previous run's, the trees are reusable
 * as-is; everything tag-dependent (repo/, dist-info, manifest) is staged
 * fresh every run. The key says "reuse is allowed"; the arch probes and
 * the import backstop still decide "reuse is correct".
 */
export function stageCacheKey({ target, pythonVersion, requirementsText }) {
  return createHash("sha256")
    .update(
      JSON.stringify({
        schemaVersion: PAYLOAD_SCHEMA_VERSION,
        target: target.key,
        uvPython: target.uvPython,
        pythonVersion,
        sourceBuild: target.sourceBuild || [],
        requirements: createHash("sha256").update(requirementsText).digest("hex"),
      })
    )
    .digest("hex")
}

// ─── impure staging steps (they shell out, have no unit tests, and run in CI) ──────
function loadPins() {
  const pins = JSON.parse(fs.readFileSync(path.join(REPO_ROOT, "runtime-pins.json"), "utf8"))

  return pins.tools
}

/**
 * Managed runtime tools (node, npm, uv, git, gh, ripgrep) for the payload.
 *
 * The payload IS a runtime dir, so this shells out to the SAME Python
 * provisioner a source install and `hermes update` use. Everything about
 * a tool — its exact version, its per-target download URL and sha256, how
 * its archive unpacks — lives in runtime-pins.json and
 * installation/provisioner.py. A second implementation here would
 * be a second thing to keep correct, and the digest verification is not
 * something to reimplement twice.
 *
 * The staging runner IS the target machine (see `resolveTargets`: one CI
 * runner per (os, arch) pair, because electron-builder needs per-OS
 * runners for signing anyway). `--target` is still passed explicitly so
 * the payload's target is stated rather than inferred from whatever
 * interpreter happens to run this, but it names the host, so the
 * provisioner's run-the-binary check does execute here.
 * `assertPayloadArch` below independently re-checks the arch from the
 * file headers.
 */
function stageManagedRuntimes(target, outDir, pythonExe) {
  const targetKey = `${target.platform}-${target.arch}`

  run(pythonExe, [
    "-m",
    "installation.provisioner",
    "--runtime-dir",
    outDir,
    "--target",
    targetKey,
  ], { cwd: REPO_ROOT })

  assertPayloadArch(target, outDir)
}

/**
 * Confirm every staged tool binary is built for the target.
 *
 * Paths come from the runtimes.json the provisioner just wrote — the
 * payload dir is its own store (installation.paths.resolve_bases), so
 * each fact's relative path names a store entry like
 * `node-22.19.0-win32-x64/node.exe`. The facts are the layout authority;
 * a hardcoded `node/node.exe` map here silently diverged when the
 * store-entry naming landed and every bundled build died on it.
 *
 * Header inspection, not execution: the build host usually cannot run
 * what it just staged, and emulation would make a wrong-arch binary look
 * fine anyway.
 */
export function assertPayloadArch(target, outDir) {
  const required = ["node", "uv", "git", "gh", "ripgrep"]

  let facts
  try {
    facts = JSON.parse(fs.readFileSync(path.join(outDir, "runtimes.json"), "utf8"))
  } catch (err) {
    throw new Error(`payload arch audit: cannot read runtimes.json in ${outDir}: ${err.message}`)
  }

  for (const tool of required) {
    const fact = facts.tools?.[tool]
    if (!fact || !fact.path) {
      throw new Error(`${tool}: no fact in the staged payload's runtimes.json`)
    }
    if (path.isAbsolute(fact.path)) {
      // A "system" fact records a machine binary outside the payload —
      // never acceptable in a sealed artifact that must carry its own.
      throw new Error(`${tool}: fact records an absolute path (${fact.path}) — a sealed payload must carry the managed tool`)
    }
    const binary = path.join(outDir, fact.path)
    if (!fs.existsSync(binary)) {
      throw new Error(`${tool}: ${fact.path} missing from the staged payload`)
    }

    const arch = target.platform === "win32"
      ? probePeArch(binary)
      : (probeMachOArch(binary) ?? probeElfArch(binary))

    if (arch !== "unknown" && arch !== target.arch) {
      throw new Error(`${tool}: staged binary is ${arch}, expected ${target.arch}`)
    }
  }
}

function probePeArch(exePath) {
  const fd = fs.openSync(exePath, "r")
  try {
    const head = Buffer.alloc(64)
    fs.readSync(fd, head, 0, 64, 0)
    if (head[0] !== 0x4d || head[1] !== 0x5a) return "unknown"
    const peOffset = head.readUInt32LE(0x3c)
    const peHead = Buffer.alloc(6)
    const n = fs.readSync(fd, peHead, 0, 6, peOffset)
    if (n < 6 || peHead.readUInt32LE(0) !== 0x00004550) return "unknown"
    const machine = peHead.readUInt16LE(4)
    return PE_MACHINES[machine] || "unknown"
  } finally {
    fs.closeSync(fd)
  }
}

const PE_MACHINES = {
  0x014c: "ia32",
  0x01c0: "arm",
  0x01c4: "arm",
  0x8664: "x64",
  0xaa64: "arm64",
}

// Mach-O cputype values (mach/machine.h). CPU_ARCH_ABI64 (0x01000000) is
// OR'd into the 64-bit variants.
const MACHO_CPU_TYPES = {
  0x01000007: "x64", // CPU_TYPE_X86_64
  0x0100000c: "arm64", // CPU_TYPE_ARM64
  0x00000007: "ia32", // CPU_TYPE_X86
  0x0000000c: "arm", // CPU_TYPE_ARM
}

/**
 * Architecture of a Mach-O binary, or null when it is not Mach-O.
 *
 * Handles thin binaries (both endiannesses) and universal/fat archives.
 * A fat binary reports "universal" rather than a single arch: shipping
 * one is not wrong, it just is not a single-arch answer, and the caller
 * decides whether that is acceptable.
 */
export function probeMachOArch(binaryPath) {
  const fd = fs.openSync(binaryPath, "r")
  try {
    const head = Buffer.alloc(8)
    if (fs.readSync(fd, head, 0, 8, 0) < 8) return null
    const magic = head.readUInt32BE(0)

    // Universal binary: 0xcafebabe (fat) / 0xcafebabf (fat64), big-endian.
    if (magic === 0xcafebabe || magic === 0xcafebabf) return "universal"

    // Thin: 0xfeedface/0xfeedfacf, either byte order.
    const le = head.readUInt32LE(0)
    if (le === 0xfeedface || le === 0xfeedfacf) {
      return MACHO_CPU_TYPES[head.readUInt32LE(4) >>> 0] || "unknown"
    }
    if (magic === 0xfeedface || magic === 0xfeedfacf) {
      return MACHO_CPU_TYPES[head.readUInt32BE(4) >>> 0] || "unknown"
    }
    return null
  } finally {
    fs.closeSync(fd)
  }
}

// ELF e_machine values (elf.h).
const ELF_MACHINES = {
  0x03: "ia32", // EM_386
  0x28: "arm", // EM_ARM
  0x3e: "x64", // EM_X86_64
  0xb7: "arm64", // EM_AARCH64
}

/** Architecture of an ELF binary, or null when it is not ELF. */
export function probeElfArch(binaryPath) {
  const fd = fs.openSync(binaryPath, "r")
  try {
    const head = Buffer.alloc(20)
    if (fs.readSync(fd, head, 0, 20, 0) < 20) return null
    if (head[0] !== 0x7f || head[1] !== 0x45 || head[2] !== 0x4c || head[3] !== 0x46) {
      return null
    }
    // e_ident[EI_DATA]: 1 = little-endian, 2 = big-endian.
    const machine = head[5] === 2 ? head.readUInt16BE(18) : head.readUInt16LE(18)
    return ELF_MACHINES[machine] || "unknown"
  } finally {
    fs.closeSync(fd)
  }
}

function run(cmd, args, opts = {}) {
  // stdio: inherit — subprocess output (pip's resolution errors, uv's
  // install messages) streams to the build log in real time. The throw
  // below only names the command; the CAUSE is in the streamed output
  // directly above it.
  const result = spawnSync(cmd, args, { stdio: "inherit", ...opts })
  if (result.error) {
    throw new Error(`${cmd} did not start: ${result.error.message}`)
  }
  if (result.status !== 0) {
    throw new Error(`${cmd} ${args.join(" ")} exited ${result.status} — its error output is printed above`)
  }
}

/**
 * Capture a probe command's stdout for inspection (banner checks). On
 * failure the captured stderr goes into the thrown error, so probe
 * failures are never silent.
 */
function probe(cmd, args) {
  const result = spawnSync(cmd, args, { encoding: "utf8" })
  if (result.error) {
    throw new Error(`${cmd} did not start: ${result.error.message}`)
  }
  if (result.status !== 0) {
    throw new Error(`${cmd} ${args.join(" ")} exited ${result.status}: ${(result.stderr || "").trim()}`)
  }
  return result.stdout
}

function stageRepo(tag, outDir) {
  const repoDir = path.join(outDir, "repo")
  fs.rmSync(repoDir, { recursive: true, force: true })
  fs.mkdirSync(repoDir, { recursive: true })
  // rev-list, not `rev-parse <tag>^{commit}`: execSync on Windows runs
  // through cmd.exe, where ^ is the escape character and eats the brace.
  const commit = execSync(`git rev-list -n 1 ${tag}`, { cwd: REPO_ROOT, encoding: "utf8" }).trim()
  const commitDate = execSync(`git log -1 --format=%ct ${tag}`, { cwd: REPO_ROOT, encoding: "utf8" }).trim()
  // The payload repo is a PLAIN SOURCE TREE, deliberately without .git.
  // Bundled installs never run git against the checkout: updates replace
  // the whole tree (electron-updater), and `hermes update --eject` makes
  // its own fresh clone. A shipped .git also broke in transit: `git gc`
  // packs all refs, which leaves .git/refs/ empty, and electron-builder's
  // resource copy drops empty directories — git then refuses to recognize
  // the repository at all. git archive gives a clean tree of exactly the
  // tag's tracked files.
  const archive = path.join(outDir, ".repo-archive.tar")
  run("git", ["archive", "--format=tar", "-o", archive, tag], { cwd: REPO_ROOT })
  run(hostTarBin(), ["-xf", archive, "-C", repoDir])
  fs.rmSync(archive, { force: true })
  // The PREBUILT JS surfaces live inside the repo tree, exactly where a
  // source checkout builds them. CI builds ui-tui (with hermes-ink) and
  // the dashboard SPA BEFORE this script runs; here they are copied in
  // as plain directories. The SPA's real outDir is hermes_cli/web_dist
  // (web/vite.config.ts) — the old js-prebuilt list named a root-level
  // web_dist that never existed, and its existsSync filter silently
  // dropped it from every artifact. dereference: ui-tui/node_modules
  // carries the hermes-ink workspace symlink, and symlinks do not
  // reliably survive the electron-builder resource copy.
  const jsSurfaces = ["ui-tui/dist", "ui-tui/node_modules", "hermes_cli/web_dist"].filter((p) =>
    fs.existsSync(path.join(REPO_ROOT, p))
  )
  if (jsSurfaces.length < 3) {
    throw new Error(`repo: prebuilt JS surfaces missing — run the ui-tui/web builds first (found: ${jsSurfaces.join(", ") || "none"})`)
  }
  for (const surface of jsSurfaces) {
    fs.cpSync(path.join(REPO_ROOT, surface), path.join(repoDir, surface), {
      recursive: true,
      dereference: true,
    })
  }
  // Version provenance without git: the schema-v2 build stamp. The
  // version_info ladder prefers this stamp over git probing, so bundled
  // installs report exact-release provenance (distance 0, the tag's
  // commit) with no .git present.
  // uv run, not bare python3: on Windows `python3` resolves to the
  // Microsoft Store alias (exit 9009). uv is a hard prerequisite of this
  // script anyway, and the desktop `build` npm script already runs this
  // same stamp writer through it.
  run("uv", [
    "run", "--no-project", "--python", "3",
    path.join(repoDir, "scripts", "write_install_stamp.py"),
    "--output", path.join(repoDir, "install-stamp.json"),
    "--commit", commit,
    "--commit-date", commitDate,
    "--base-version", tag.slice(1),
    "--distance", "0",
    "--source", "ci",
    "--distribution", "desktop-app",
  ])
  return commit
}

/**
 * The payload must ship NO symlink that is absolute, escapes the payload
 * root, or dangles. macOS codesign --strict rejects the whole .app for
 * any of them ("invalid destination for symbolic link in bundle"), and
 * they are dead weight on every platform. Individual stages try to avoid
 * creating them, but the sources vary (uv's install alias, node's npm/npx
 * bin links copied by cpSync, npm's .bin links), so this final pass owns
 * the invariant for the whole tree:
 *  - absolute link with a live target inside the root → rewritten relative
 *  - link resolving outside the root (or dangling) with a live target →
 *    replaced by a real copy of the target
 *  - dangling link → removed
 */
export function sanitizeSymlinks(rootDir, fsImpl = fs) {
  const root = path.resolve(rootDir)
  const contains = (p) => p === root || p.startsWith(root + path.sep)

  const walk = (dir) => {
    for (const entry of fsImpl.readdirSync(dir, { withFileTypes: true })) {
      const entryPath = path.join(dir, entry.name)
      if (entry.isSymbolicLink()) {
        const target = fsImpl.readlinkSync(entryPath)
        const resolved = path.resolve(path.dirname(entryPath), target)
        const targetExists = fsImpl.existsSync(resolved)
        if (!targetExists) {
          fsImpl.rmSync(entryPath, { force: true })
        } else if (contains(resolved)) {
          if (path.isAbsolute(target)) {
            fsImpl.rmSync(entryPath, { force: true })
            fsImpl.symlinkSync(path.relative(path.dirname(entryPath), resolved), entryPath)
          }
        } else {
          fsImpl.rmSync(entryPath, { recursive: true, force: true })
          fsImpl.cpSync(resolved, entryPath, { recursive: true, dereference: true })
        }
      } else if (entry.isDirectory()) {
        walk(entryPath)
      }
    }
  }
  walk(root)
}

// Windows: name System32's bsdtar by full path. A GNU tar earlier on
// PATH (Git bash on the GitHub runners) reads "C:" in a path as a
// remote host name. bsdtar also reads .zip, so one extraction call
// covers every archive format the payload pipeline downloads.
export function hostTarBin() {
  return process.platform === "win32"
    ? path.join(process.env.SystemRoot || "C:\\Windows", "System32", "tar.exe")
    : "tar"
}

function stageUvAndPython(target, outDir, { reusePython = false } = {}) {
  const pythonDir = path.join(outDir, "python")
  // Wipe before staging (stageRepo does the same). A rerun after a failed
  // or wrong-arch attempt must not leave a stale interpreter beside the
  // new one — the banner probe would find the old build first. The
  // python install is the expensive half, and a cache-key match (main)
  // skips its reinstall.
  if (!reusePython) {
    fs.rmSync(pythonDir, { recursive: true, force: true })
    fs.mkdirSync(pythonDir, { recursive: true })
  }

  // The uv that INSTALLS the payload interpreter is a BUILD tool: it runs
  // here, on the build host, so it comes from PATH (HERMES_PAYLOAD_UV
  // overrides). The uv that SHIPS in the payload is a managed runtime
  // built for the target — the provisioner stages that one from the pin
  // table, and on a cross-build the two are not the same architecture.
  const buildUv = process.env.HERMES_PAYLOAD_UV || "uv"

  const expect = bannerExpectations(target)

  // --no-bin: staging must not write launcher shims into the build
  // host's ~/.local/bin (it collided with a preexisting python3.11.exe
  // on the Windows test box). On reuse the install is already on disk;
  // the probes below still run against it.
  if (!reusePython) {
    run(buildUv, ["python", "install", "--no-bin", "--install-dir", pythonDir, pythonRequest(target)])
  }

  // uv leaves two things beside the versioned install that must not ship:
  // a minor-version alias that is an ABSOLUTE symlink to this build host's
  // path (codesign --strict rejects the .app: "invalid destination for
  // symbolic link in bundle" — the June darwin lane failures), and its
  // bookkeeping files (.lock, .temp, .gitignore). findEmbeddedPython
  // prefers the real patch-versioned directory, so nothing reads the alias.
  for (const entry of fs.readdirSync(pythonDir)) {
    const entryPath = path.join(pythonDir, entry)
    const isRealInstall = pythonDirPattern(target).test(entry) && !fs.lstatSync(entryPath).isSymbolicLink()
    if (!isRealInstall) {
      fs.rmSync(entryPath, { recursive: true, force: true })
    }
  }

  // python-build-standalone's windows-aarch64 dist ships an X64
  // vcruntime140_1.dll beside an otherwise all-arm64 install (verified
  // by PE header). The DLL exists solely for x64 __CxxFrameHandler4
  // exception unwinding; arm64 binaries never link it and an x64 DLL
  // cannot load into an arm64 process, so it is inert dead weight —
  // delete it rather than teach the arch audit to tolerate it.
  if (target.key === "win32-arm64") {
    for (const entry of fs.readdirSync(pythonDir, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue
      fs.rmSync(path.join(pythonDir, entry.name, "vcruntime140_1.dll"), { force: true })
    }
  }

  // The installed CPython proves its architecture at runtime.
  // `python -VV` names the arch on Windows ("[MSC v.1944 64 bit (ARM64)]")
  // but not on Linux/macOS ("[Clang 22.1.3 ]"), so the check asks
  // platform.machine() — the value the binary itself reports. The
  // install-directory pattern above already pins the requested build;
  // this is the runtime backstop.
  const pythonBinary = findPythonBinary(pythonDir, target)
  const pythonMachine = probe(pythonBinary, ["-c", "import platform; print(platform.machine())"])
  if (!expect.pythonAny.some((word) => pythonMachine.includes(word))) {
    assertBanner("python", pythonMachine, expect.pythonAny.join("|"))
  }
  return pythonBinary
}

/**
 * Match the directory `uv python install` creates for a request. The
 * request names a minor version (cpython-3.11-windows-aarch64-none), and
 * uv installs into a PATCH-versioned directory
 * (cpython-3.11.15-windows-aarch64-none) plus a minor-version alias that
 * is a junction on Windows. The matcher accepts both shapes and nothing
 * of any other version or triple.
 */
export function pythonDirPattern(target, version = process.env.HERMES_PAYLOAD_PYTHON || "3.11") {
  const escape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
  return new RegExp(`^cpython-${escape(version)}(\\.\\d+)?(rc\\d+)?-${escape(target.uvPython)}$`)
}

function findPythonBinary(pythonDir, target) {
  // Search only directories that match the REQUESTED build, so a stray
  // install of another architecture can never satisfy the probe. The
  // wipe above prevents strays; this is the backstop. The alias
  // entry is a junction/symlink — do not require isDirectory().
  const name = target.platform === "win32" ? "python.exe" : "python3"
  const pattern = pythonDirPattern(target)
  const roots = fs
    .readdirSync(pythonDir, { withFileTypes: true })
    .filter((e) => (e.isDirectory() || e.isSymbolicLink()) && pattern.test(e.name))
    .map((e) => path.join(pythonDir, e.name))
  if (roots.length === 0) {
    throw new Error(`python: nothing matching ${pattern} under ${pythonDir} after uv python install`)
  }
  const stack = [...roots]
  while (stack.length) {
    const dir = stack.pop()
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name)
      if (entry.isDirectory() && !entry.isSymbolicLink()) {
        stack.push(full)
      } else if (entry.name === name) {
        return full
      }
    }
  }
  throw new Error(`python: no ${name} found under ${roots.join(", ")}`)
}

function stageSitePackages(target, outDir, pythonBinary, { reuse = false } = {}) {
  const sitePackagesDir = path.join(outDir, "site-packages")
  // Export the lock to a requirements file, then install the whole tree
  // with pip running ON THE STAGED PAYLOAD INTERPRETER: pip resolves
  // platform tags for the interpreter that executes it, so this is what
  // pins site-packages to the target architecture. (uvx pip runs under
  // uvx's own python — on the arm64 test box that pulled win_amd64
  // wheels.) No venv anywhere: a venv's bin/python is a symlink to an
  // ABSOLUTE build-host path, and the .app runs from unpredictable
  // locations (renames, Gatekeeper translocation, AppImage mounts).
  // main() already exported requirements-payload.txt (the cache key
  // hashes it); on reuse the installed tree is already on disk and only
  // the pip install is skipped — the dist-info rewrite and the import
  // backstop below run every time.
  if (!pythonBinary) {
    throw new Error("site-packages: the uv/python stage must run first (it provides the payload interpreter)")
  }
  if (!reuse) {
    fs.rmSync(sitePackagesDir, { recursive: true, force: true })
    fs.mkdirSync(sitePackagesDir, { recursive: true })
    run(
      "uvx",
      ["--python", pythonBinary, "pip", ...pipTargetArgs({ sitePackagesDir, sourceBuild: target.sourceBuild || [] })],
      { cwd: REPO_ROOT }
    )
  }

  // hermes-agent's own code imports from repo/ (the .pth puts it first on
  // sys.path — PROJECT_ROOT derivations need the real tree around the
  // packages). But importlib.metadata.version("hermes-agent") needs a
  // dist-info. pip cannot produce one here: setup.py deliberately blocks
  // wheel builds outside Nix (and pip install --target builds a wheel
  // internally). importlib.metadata only reads METADATA, so write the
  // minimal dist-info directly — same trick as flat layouts everywhere.
  // The version comes from repo/, which is staged fresh every run: on a
  // cache reuse the previous release's dist-info is on disk and MUST be
  // replaced, or the payload would report the old version.
  for (const entry of fs.readdirSync(sitePackagesDir)) {
    if (/^hermes_agent-.*\.dist-info$/.test(entry)) {
      fs.rmSync(path.join(sitePackagesDir, entry), { recursive: true, force: true })
    }
  }
  const version = probe(pythonBinary, [
    "-c",
    `import pathlib, re; print(re.search(r'__version__ = \"([^\"]+)\"', pathlib.Path(${JSON.stringify(
      path.join(outDir, "repo", "hermes_cli", "__init__.py")
    )}).read_text(encoding="utf-8")).group(1))`,
  ]).trim()
  const distInfo = path.join(sitePackagesDir, `hermes_agent-${version}.dist-info`)
  fs.mkdirSync(distInfo, { recursive: true })
  fs.writeFileSync(
    path.join(distInfo, "METADATA"),
    `Metadata-Version: 2.1\nName: hermes-agent\nVersion: ${version}\n`
  )
  fs.writeFileSync(path.join(distInfo, "INSTALLER"), "hermes-desktop-bundle\n")

  // Architecture backstop: import the heaviest native extensions with
  // site-packages on the path. On the native CI runner a wrong-arch
  // tree fails here instead of on the user machine. (The old wheelhouse
  // filename check has no equivalent — pip already unpacked the wheels —
  // and actually importing is the stronger proof.)
  probe(pythonBinary, [
    "-c",
    `import sys; sys.path.insert(0, ${JSON.stringify(sitePackagesDir)}); import pydantic_core, cryptography, charset_normalizer`,
  ])
}

/**
 * The relative sys.path entries for the bundle glue. A .pth file's
 * non-import lines are resolved against the DIRECTORY CONTAINING THE
 * .PTH FILE, so relative entries make the payload fully relocatable:
 * no absolute paths exist anywhere in the artifact. repo/ comes first
 * so its packages win over anything in site-packages.
 */
export function bundlePthLines(purelibDir, payloadRoot, pathModule = path) {
  return ["repo", "site-packages"].map((entry) =>
    pathModule.relative(purelibDir, pathModule.join(payloadRoot, entry))
  )
}

function writeBundlePth(outDir, pythonBinary) {
  // Ask the interpreter where its own site-packages lives instead of
  // hardcoding the layout (POSIX: lib/python3.11/site-packages,
  // Windows: Lib/site-packages).
  const purelib = probe(pythonBinary, ["-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"]).trim()
  if (!purelib || !fs.existsSync(purelib)) {
    throw new Error(`bundle pth: interpreter reports nonexistent purelib: ${purelib}`)
  }
  fs.writeFileSync(
    path.join(purelib, "hermes-bundle.pth"),
    bundlePthLines(purelib, outDir).join("\n") + "\n"
  )
}

/**
 * Drop payload members that no runtime path can reach. Every entry is a
 * deliberate, named decision — this is an allowlist of deletions, not a
 * heuristic sweep, and it runs LAST so the staging probes above always
 * see the full trees they verified.
 *
 * What is NOT pruned, and why (verified against the runtime code):
 *  - python's pip/setuptools/ensurepip and the payload uv: sealed
 *    installs lazy-install optional extras at runtime through the
 *    installation.pip_ladder tiers (uv → `python -m pip` → ensurepip),
 *    so all three are load-bearing.
 *  - repo/apps: `hermes gui` dev/unpacked flows read apps/desktop.
 *  - site-packages: pip already installs without tests for most wheels;
 *    the few residual test dirs are single-digit MB and package-owned.
 *
 * Measured on the v0.27.0 win32-arm64 payload (1,268 MB installed):
 *  - PortableGit ships a full MSYS2 userland; git never calls the perl
 *    scripts Hermes can't reach (git-svn, send-email, legacy add -i),
 *    the HTML docs, gitk/git-gui, or the message catalogs. ~85 MB.
 *  - repo/ ships tests/ (34 MB) and website/ (27 MB) from git archive;
 *    nothing imports either at runtime.
 *  - the interpreter ships tcl/tkinter, idlelib, turtledemo and
 *    pydoc_data; no dependency imports tkinter (repo-wide grep). ~8 MB.
 */
export function prunePayload(outDir, target, fsImpl = fs) {
  const rmrf = (rel) => {
    const full = path.join(outDir, rel)
    if (!fsImpl.existsSync(full)) return 0
    const size = duBytes(full, fsImpl)
    fsImpl.rmSync(full, { recursive: true, force: true })
    return size
  }

  let reclaimed = 0

  // repo/: build- and docs-only trees. git archive exports every tracked
  // file; these two are the largest with zero runtime consumers.
  for (const rel of ["repo/tests", "repo/website"]) {
    reclaimed += rmrf(rel)
  }

  // python/: GUI/teaching stdlib nobody imports. KEEP ensurepip (pip
  // ladder tier 3), venv (eject flows), distutils (setuptools compat).
  const pythonRoot = path.join(outDir, "python")
  if (fsImpl.existsSync(pythonRoot)) {
    for (const entry of fsImpl.readdirSync(pythonRoot, { withFileTypes: true })) {
      if (!entry.isDirectory() || entry.isSymbolicLink?.()) continue
      const installRel = path.join("python", entry.name)
      // POSIX python-build-standalone nests lib/python3.11/; Windows uses Lib/.
      const libCandidates = [
        path.join(installRel, "Lib"),
        ...(fsImpl.existsSync(path.join(outDir, installRel, "lib"))
          ? fsImpl
              .readdirSync(path.join(outDir, installRel, "lib"), { withFileTypes: true })
              .filter((e) => e.isDirectory() && /^python\d/.test(e.name))
              .map((e) => path.join(installRel, "lib", e.name))
          : []),
      ]
      for (const lib of libCandidates) {
        for (const mod of ["tkinter", "turtledemo", "idlelib", "pydoc_data", "test"]) {
          reclaimed += rmrf(path.join(lib, mod))
        }
        reclaimed += rmrf(path.join(lib, "turtle.py"))
      }
      // Tcl/Tk data + DLLs exist only for the tkinter just removed.
      reclaimed += rmrf(path.join(installRel, "tcl"))
      for (const dll of ["tcl86t.dll", "tk86t.dll", "_tkinter.pyd", "zlib1.dll__tk"]) {
        reclaimed += rmrf(path.join(installRel, "DLLs", dll))
      }
    }
  }

  // git/: Windows-only (POSIX ships lean dugite-native already). The
  // store entry name comes from the facts — the layout authority.
  if (target.platform === "win32") {
    let facts
    try {
      facts = JSON.parse(fsImpl.readFileSync(path.join(outDir, "runtimes.json"), "utf8"))
    } catch {
      facts = null
    }
    const gitPath = facts?.tools?.git?.path
    if (gitPath && !path.isAbsolute(gitPath)) {
      // fact path is like git-2.53.0-win32-arm64/cmd/git.exe → store root.
      const gitRoot = gitPath.split(/[\\/]/)[0]
      const gitPrunes = [
        // Perl: only reachable via git-svn / send-email / legacy add -i,
        // none of which Hermes invokes. Biggest single win (~32 MB).
        "usr/share/perl5",
        "usr/lib/perl5",
        // HTML documentation (~21 MB) and localized messages.
        "clangarm64/share/doc",
        "mingw64/share/doc",
        "usr/share/doc",
        "clangarm64/share/locale",
        "mingw64/share/locale",
        "usr/share/locale",
        "clangarm64/share/man",
        "mingw64/share/man",
        "usr/share/man",
        // Tk GUIs (gitk / git-gui) and the Tcl runtime that exists for them.
        "clangarm64/share/gitk",
        "mingw64/share/gitk",
        "clangarm64/share/git-gui",
        "mingw64/share/git-gui",
        "clangarm64/lib/tcl8.6",
        "mingw64/lib/tcl8.6",
        "clangarm64/lib/tk8.6",
        "mingw64/lib/tk8.6",
      ]
      for (const rel of gitPrunes) {
        reclaimed += rmrf(path.join(gitRoot, rel))
      }
    }
  }

  console.log(`[stage-agent-payloads] pruned ${(reclaimed / (1024 * 1024)).toFixed(1)} MB of unreachable payload members`)
  return reclaimed
}

function duBytes(root, fsImpl = fs) {
  let total = 0
  const stack = [root]
  while (stack.length) {
    const dir = stack.pop()
    let entries
    try {
      const stat = fsImpl.lstatSync(dir)
      if (!stat.isDirectory()) return total + stat.size
      entries = fsImpl.readdirSync(dir, { withFileTypes: true })
    } catch {
      continue
    }
    for (const entry of entries) {
      const full = path.join(dir, entry.name)
      if (entry.isDirectory() && !entry.isSymbolicLink()) {
        stack.push(full)
      } else {
        try {
          total += fsImpl.lstatSync(full).size
        } catch {
          /* vanished */
        }
      }
    }
  }
  return total
}

function main() {
  if (process.env.HERMES_DESKTOP_VARIANT !== "bundled") {
    // bootstrap and light artifacts carry no payload: write a stub
    // manifest anyway. Then the extraResources entry always has a real
    // directory to copy. The behavior of electron-builder for a missing
    // `from` changes between versions. The stub also lets runtime code
    // read manifest.json uniformly and learn that there are no payloads.
    fs.mkdirSync(OUT_DIR, { recursive: true })
    fs.writeFileSync(
      path.join(OUT_DIR, "manifest.json"),
      JSON.stringify({ schemaVersion: PAYLOAD_SCHEMA_VERSION, external: true }, null, 2) + "\n"
    )
    console.log("[stage-agent-payloads] HERMES_DESKTOP_VARIANT != bundled — wrote external stub manifest")
    return
  }
  const target = resolveTargets()
  const tag = resolveTag(process.argv.slice(2), () => {
    try {
      return execSync("git describe --tags --exact-match", { cwd: REPO_ROOT, encoding: "utf8" }).trim()
    } catch {
      return null
    }
  })

  fs.mkdirSync(OUT_DIR, { recursive: true })

  // The expensive stages (python install + site-packages) are reused
  // when their cache identity matches the previous run's — CI restores
  // them via actions/cache keyed on uv.lock. Export the requirements
  // FIRST: the key hashes the exported file, which is what pip actually
  // installs from. Reuse skips only the installs; every probe, the
  // dist-info rewrite, the .pth, and the manifest run identically on
  // both paths, so a wrong or stale cache fails the same checks a bad
  // fresh staging would.
  run("uv", ["export", "--frozen", "--no-emit-project", "-o", "requirements-payload.txt"], { cwd: REPO_ROOT })
  const cacheKey = stageCacheKey({
    target,
    pythonVersion: process.env.HERMES_PAYLOAD_PYTHON || "3.11",
    requirementsText: fs.readFileSync(path.join(REPO_ROOT, "requirements-payload.txt"), "utf8"),
  })
  const cacheKeyFile = path.join(OUT_DIR, ".stage-cache-key")
  let reuse = false
  try {
    reuse = fs.readFileSync(cacheKeyFile, "utf8").trim() === cacheKey
  } catch {
    // No key file: first run or restored nothing — stage from scratch.
  }
  // A stale or foreign key means the trees on disk are for other inputs.
  // Drop the key BEFORE restaging: an interrupted run must never leave a
  // matching key beside half-staged trees.
  fs.rmSync(cacheKeyFile, { force: true })
  if (reuse) {
    console.log(`[stage-agent-payloads] python + site-packages reused (cache key ${cacheKey.slice(0, 12)}…)`)
  }

  // Every stage runs, in order. A failure throws and the build fails:
  // an embedded payload is complete, or it does not exist.
  console.log(`[stage-agent-payloads] staging: repo (${target.key}, ${tag})`)
  const commit = stageRepo(tag, OUT_DIR)
  console.log(`[stage-agent-payloads] staging: uv + python (${target.key}, ${tag})`)
  const payloadPython = stageUvAndPython(target, OUT_DIR, { reusePython: reuse })
  console.log(`[stage-agent-payloads] staging: site-packages (${target.key}, ${tag})`)
  stageSitePackages(target, OUT_DIR, payloadPython, { reuse })
  // The glue that makes the payload interpreter resolve repo/ and
  // site-packages/ wherever the bundle sits. Written after both stages
  // exist so a failed staging run never leaves a .pth that points at
  // nothing.
  writeBundlePth(OUT_DIR, payloadPython)
  // node, uv, git, gh, ripgrep in one call, from the pinned URLs and
  // digests, writing the runtimes.json the desktop reads at launch.
  console.log(`[stage-agent-payloads] staging: managed runtimes (${target.key}, ${tag})`)
  stageManagedRuntimes(target, OUT_DIR, payloadPython)
  // Prune AFTER every stage and probe has seen its full tree, and on the
  // cache-reuse path too (deletions are idempotent — a pruned tree just
  // yields zero). The import backstop already proved the heavy natives
  // load; nothing pruned below is on any import path.
  console.log(`[stage-agent-payloads] pruning unreachable payload members`)
  prunePayload(OUT_DIR, target)
  console.log(`[stage-agent-payloads] sanitizing symlinks`)
  sanitizeSymlinks(OUT_DIR)

  const manifest = buildManifest({ tag, commit, target })
  fs.writeFileSync(path.join(OUT_DIR, "manifest.json"), JSON.stringify(manifest, null, 2) + "\n")
  // The key is written LAST: it asserts that the python/site-packages
  // trees on disk are complete for these inputs, which is only true once
  // every stage and probe above has passed.
  fs.writeFileSync(cacheKeyFile, cacheKey + "\n")
  console.log(`[stage-agent-payloads] wrote ${path.join(OUT_DIR, "manifest.json")}`)
}

if (isMain(import.meta.url)) {
  main()
}
