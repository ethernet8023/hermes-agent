// Wraps the electron-builder CLI so the arguments compose in one place, in
// the first spawn with no shell in between.
//
// electron-builder downloads and extracts Electron itself (via electronVersion
// + ELECTRON_MIRROR). Earlier revisions passed -c.electronDist to reuse the
// installed node_modules/electron/dist as a dodge for a 26.8.x bug that could
// re-unpack a broken Electron.app (#38673, #47917) — but 27's copyDir of that
// dist mangles the framework symlinks and codesign rejects the bundle, while
// its archive extraction preserves them. The dodge now causes the class of
// bug it prevented, so it is gone.

import fs from "node:fs"
import path from "node:path"
import { spawnSync } from "node:child_process"
import { createRequire } from "node:module"

const require = createRequire(import.meta.url)

function electronBuilderCli() {
  // 27 no longer exports ./package.json; resolve the entry module and walk
  // up to the package root instead.
  const entry = require.resolve("electron-builder")
  let dir = path.dirname(entry)
  while (!fs.existsSync(path.join(dir, "package.json"))) {
    const parent = path.dirname(dir)
    if (parent === dir) throw new Error("electron-builder package root not found")
    dir = parent
  }
  const bin = require(path.join(dir, "package.json")).bin
  const rel = typeof bin === "string" ? bin : bin["electron-builder"]
  return path.join(dir, rel)
}

const args = []
args.push(...process.argv.slice(2))

// The config file wraps package.json's "build" field to add the one option
// JSON cannot express: mac.sign.ignore as a function (Mach-O-only signing).
// package.json's "build" wins over config files unless --config is explicit,
// so name it here.
if (!args.some((a) => a === "--config" || a.startsWith("--config="))) {
  args.push("--config", "electron-builder.config.cjs")
}

// Never let electron-builder publish. On a CI tag build it auto-detects
// GitHub and demands GH_TOKEN after the artifacts are already built.
// The release workflow uploads artifacts in its own step.
if (!args.includes("--publish") && !args.some((a) => a.startsWith("-p"))) {
  args.push("--publish", "never")
}

// Windows signing config lives in electron-builder.config.cjs — the single
// source of truth — composed there from the AZURE_SIGN_* variables. It
// cannot ride through -c arguments: the publisherName contains spaces and
// commas that die in cmd.exe hops, and -c.win.sign.* args would override
// the config file's cached-sign hook wiring. This block only announces the
// decision in the log.
if (args.includes("--win") && process.env.AZURE_SIGN_ENDPOINT && process.env.AZURE_CLIENT_ID) {
  console.log(`[run-electron-builder] Windows signing: Azure Trusted Signing at ${process.env.AZURE_SIGN_ENDPOINT}`)
}

// Cap concurrent fs.open calls in the electron-builder process.
//
// @electron/osx-sign's walk recurses with Promise.all and no concurrency
// bound, opening every file it meets through isbinaryfile, so peak
// descriptors track the size of the .app. The bundled payload
// (site-packages/lark_oapi alone is 11,112 files) blew past the macOS
// limit with EMFILE during signing.
//
// --require, not an import inside this file: isbinaryfile captures
// `promisify(fs.open)` at ITS module load, so the patch has to be
// installed before electron-builder's require graph is walked. Preloading
// in the child is the only point that is reliably early enough.
const preload = path.join(import.meta.dirname, "fs-open-limit.cjs")

const result = spawnSync(process.execPath, ["--require", preload, electronBuilderCli(), ...args], {
  stdio: "inherit",
})
if (result.error) {
  console.error(`[run-electron-builder] spawn failed: ${result.error.message}`)
  process.exit(1)
}
process.exit(result.status == null ? 1 : result.status)
