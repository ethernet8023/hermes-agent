// hermes-shim: the CLI trampoline for BUNDLED desktop installs.
//
// A bundled artifact is sealed and signed at build time, so the distlib
// console-script launchers (assembled on the user machine, absolute
// interpreter path appended to a stub) can never exist there. This binary
// is the replacement: finished in CI, signed like every other payload PE/
// Mach-O, and fully self-relative so it works wherever the install lands.
//
// One binary, three names. Staging copies it to hermes, hermes-agent and
// hermes-acp inside agent-payload/bin/; the entry module is chosen from
// the invoked name (argv[0] first — an MSIX ExecutionAlias materializes
// all aliases against ONE packaged exe and the alias name only survives
// in argv[0] — then the real exe basename as fallback).
//
// Target resolution: a one-line sidecar `shim-target.txt` beside the
// shims holds the bin-relative path of the payload CPython. Staging
// writes it from the same probed value the payload manifest records.
// The shim never parses JSON and never embeds a path: byte-identical
// binaries across targets whose python layout differs, and the sidecar
// is data, not code — writing it breaks no signature.
//
// Import wiring: the bundled payload is sealed, so the python's own
// site-packages has no working editable install of hermes-agent (the
// `uv sync` editable pointer names the BUILD machine's payload path).
// The shim therefore sets PYTHONPATH itself from the sidecar — the
// payload repo + the venv site-packages — replacing any inherited
// PYTHONPATH. Layout facts live in the sidecar (3 lines: interpreter,
// site-packages, repo), so the shim stays byte-identical across targets.
//
// Zero crate dependencies on purpose (supply-chain surface of the ONE
// binary every bundled install puts on PATH). The only unsafe is the
// kernel32 SetConsoleCtrlHandler FFI on Windows.

use std::env;
use std::ffi::OsString;
use std::io::{Error, ErrorKind};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

const TARGET_FILE: &str = "shim-target.txt";

/// The `python -m` entry module for an invoked program name, or None for
/// a name we do not own. Mirrors [project.scripts] in pyproject.toml —
/// hermes runs hermes_cli.main, hermes-agent runs run_agent, hermes-acp
/// runs acp_adapter.entry.
fn entry_module(invoked: &str) -> Option<&'static str> {
    // Case-insensitive on the whole name: Windows resolution preserves
    // whatever casing the user typed, and NTFS matched it regardless.
    let lower = invoked.to_ascii_lowercase();
    let stem = lower.strip_suffix(".exe").unwrap_or(&lower);
    match stem {
        "hermes" => Some("hermes_cli.main"),
        "hermes-agent" => Some("run_agent"),
        "hermes-acp" => Some("acp_adapter.entry"),
        _ => None,
    }
}

/// The basename of a path-shaped OsString, as UTF-8 (lossy). argv[0] may
/// be a bare name, a relative path, or an absolute path depending on how
/// the caller spawned us; only the final component names the program.
fn program_basename(raw: &OsString) -> String {
    Path::new(raw)
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// The payload layout facts the shim needs, all bin-relative (forward
/// slashes in the sidecar on every platform, same convention as the
/// payload manifest). Line order in shim-target.txt is fixed:
///   1. interpreter — the payload CPython to exec
///   2. site-packages — the venv's dependency tree (sealed payload has no
///      working editable install; the shim points PYTHONPATH at it)
///   3. repo — the payload source tree that owns hermes_cli
struct Sidecar {
    interpreter: PathBuf,
    site_packages: PathBuf,
    repo: PathBuf,
}

fn parse_relative(line: &str) -> Result<PathBuf, Error> {
    // Forward slashes in the sidecar on every platform (same convention
    // as the payload manifest); split and rejoin through the host paths.
    let rel = PathBuf::from_iter(line.split('/'));
    if rel.is_absolute() || line.starts_with('/') || line.chars().nth(1) == Some(':') {
        return Err(Error::new(
            ErrorKind::InvalidData,
            format!("{TARGET_FILE} must hold a relative path, got: {line}"),
        ));
    }
    Ok(rel)
}

/// Read the sidecar and resolve the three payload layout facts against
/// the shim's own directory. Rejects absolute sidecar content: the whole
/// point is relocatability, and an absolute path in the sidecar means a
/// staging bug — failing loudly beats silently launching a foreign python.
fn read_sidecar(shim_dir: &Path) -> Result<Sidecar, Error> {
    let text = std::fs::read_to_string(shim_dir.join(TARGET_FILE)).map_err(|e| {
        Error::new(
            e.kind(),
            format!(
                "cannot read {} (is this shim inside its bundle's bin directory?): {e}",
                shim_dir.join(TARGET_FILE).display()
            ),
        )
    })?;
    let lines: Vec<&str> = text
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty() && !l.starts_with('#'))
        .collect();
    if lines.len() != 3 {
        return Err(Error::new(
            ErrorKind::InvalidData,
            format!(
                "{TARGET_FILE} must hold exactly 3 lines (interpreter, site-packages, repo), got {}: {}",
                lines.len(),
                shim_dir.join(TARGET_FILE).display()
            ),
        ));
    }
    Ok(Sidecar {
        interpreter: shim_dir.join(parse_relative(lines[0])?),
        site_packages: shim_dir.join(parse_relative(lines[1])?),
        repo: shim_dir.join(parse_relative(lines[2])?),
    })
}

/// The PYTHONPATH the shim hands the payload python: repo first (its
/// packages win over anything in site-packages), then the venv deps.
fn compose_pythonpath(repo: &Path, site_packages: &Path, sep: &str) -> String {
    format!("{}{sep}{}", repo.display(), site_packages.display())
}

/// The user-level pycache prefix: the sealed payload must never see
/// __pycache__ writes (signature-breaking on mac, read-only mount on
/// AppImage). Only consulted when the user has not set their own.
fn default_pycache_dir() -> Option<PathBuf> {
    #[cfg(windows)]
    {
        env::var_os("LOCALAPPDATA").map(|base| PathBuf::from(base).join("hermes").join("pycache"))
    }
    #[cfg(not(windows))]
    {
        env::var_os("HOME").map(|base| PathBuf::from(base).join(".cache").join("hermes-pycache"))
    }
}

fn real_main() -> Result<ExitCode, Error> {
    let mut args = env::args_os();
    let argv0 = args.next().unwrap_or_default();
    let rest: Vec<OsString> = args.collect();

    // The invoked name decides the entry module. argv[0] first (MSIX
    // aliases, symlinks, hardlinks all preserve the invoked name there),
    // then the real exe basename (some launchers blank argv[0]).
    let exe = env::current_exe()?;
    // canonicalize: the mac exposure is a symlink in ~/.local/bin and the
    // sidecar lives beside the REAL file inside the .app, not the link.
    let exe_real = exe.canonicalize().unwrap_or(exe);
    let invoked = program_basename(&argv0);
    let module = entry_module(&invoked)
        .or_else(|| {
            entry_module(
                &exe_real
                    .file_name()
                    .map(|n| n.to_string_lossy().into_owned())
                    .unwrap_or_default(),
            )
        })
        .ok_or_else(|| {
            Error::new(
                ErrorKind::InvalidInput,
                format!("hermes-shim invoked under unknown name {invoked:?}; expected hermes, hermes-agent or hermes-acp"),
            )
        })?;

    let shim_dir = exe_real
        .parent()
        .ok_or_else(|| Error::new(ErrorKind::NotFound, "shim has no parent directory"))?;
    let sidecar = read_sidecar(shim_dir)?;
    let interpreter = &sidecar.interpreter;
    if !interpreter.is_file() {
        return Err(Error::new(
            ErrorKind::NotFound,
            format!(
                "bundled interpreter missing at {} — the Hermes install is damaged; reinstall from the website",
                interpreter.display()
            ),
        ));
    }

    let mut cmd = std::process::Command::new(interpreter);
    cmd.arg("-m").arg(module).args(&rest);
    // Same hygiene as the POSIX wrappers: an inherited PYTHONPATH could
    // shadow bundled modules with foreign ones. PYTHONHOME would repoint
    // the stdlib entirely. The payload python has no working editable
    // install (the venv's editable pointer names the build machine), so
    // the shim supplies the payload's own import roots instead.
    cmd.env_remove("PYTHONPATH");
    cmd.env_remove("PYTHONHOME");
    cmd.env(
        "PYTHONPATH",
        compose_pythonpath(
            &sidecar.repo,
            &sidecar.site_packages,
            if cfg!(windows) { ";" } else { ":" },
        ),
    );
    if env::var_os("PYTHONPYCACHEPREFIX").is_none() {
        if let Some(dir) = default_pycache_dir() {
            cmd.env("PYTHONPYCACHEPREFIX", dir);
        }
    }

    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        // exec never returns on success; the child IS the process, so
        // signals, exit codes and the controlling tty all just work.
        Err(cmd.exec())
    }
    #[cfg(windows)]
    {
        // No exec on Windows: spawn, ignore console ctrl events in the
        // shim (the console delivers CTRL_C to every process attached to
        // it — python must own the interrupt, the shim must not die first
        // and orphan it), wait, forward the code. Same arrangement the
        // distlib launchers use.
        unsafe {
            SetConsoleCtrlHandler(std::ptr::null_mut(), 1);
        }
        let status = cmd.status()?;
        Ok(ExitCode::from(status.code().unwrap_or(1).clamp(0, 255) as u8))
    }
}

#[cfg(windows)]
#[link(name = "kernel32")]
extern "system" {
    fn SetConsoleCtrlHandler(handler: *mut core::ffi::c_void, add: u32) -> i32;
}

fn main() -> ExitCode {
    match real_main() {
        Ok(code) => code,
        Err(err) => {
            eprintln!("hermes: {err}");
            ExitCode::from(2)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn entry_module_maps_all_three_names_and_exe_variants() {
        assert_eq!(entry_module("hermes"), Some("hermes_cli.main"));
        assert_eq!(entry_module("hermes-agent"), Some("run_agent"));
        assert_eq!(entry_module("hermes-acp"), Some("acp_adapter.entry"));
        assert_eq!(entry_module("hermes.exe"), Some("hermes_cli.main"));
        assert_eq!(entry_module("HERMES.EXE"), Some("hermes_cli.main"));
        assert_eq!(entry_module("Hermes-Agent.exe"), Some("run_agent"));
    }

    #[test]
    fn entry_module_rejects_foreign_names() {
        assert_eq!(entry_module("python"), None);
        assert_eq!(entry_module("hermes-shim"), None);
        assert_eq!(entry_module(""), None);
        // The GUI binary shares the stem 'Hermes' — but as a shim name it
        // maps to the CLI on purpose; what must NOT match is anything else.
        assert_eq!(entry_module("hermesd"), None);
        assert_eq!(entry_module("hermes2"), None);
    }

    #[test]
    fn program_basename_handles_paths_and_bare_names() {
        assert_eq!(program_basename(&OsString::from("hermes")), "hermes");
        assert_eq!(program_basename(&OsString::from("/usr/local/bin/hermes")), "hermes");
        assert_eq!(program_basename(&OsString::from(r"C:\x\hermes.exe")), if cfg!(windows) { "hermes.exe" } else { r"C:\x\hermes.exe" });
        assert_eq!(program_basename(&OsString::from("")), "");
    }

    #[test]
    fn read_sidecar_parses_three_relative_lines_and_joins_them() {
        let dir = unique_test_dir();
        std::fs::write(
            dir.join(TARGET_FILE),
            "../tools/cpython-3.11.15/python.exe\n../venv/Lib/site-packages\n../hermes-agent\n",
        )
        .unwrap();
        let sidecar = read_sidecar(&dir).unwrap();
        assert_eq!(
            sidecar.interpreter,
            dir.join("..").join("tools").join("cpython-3.11.15").join("python.exe")
        );
        assert_eq!(
            sidecar.site_packages,
            dir.join("..").join("venv").join("Lib").join("site-packages")
        );
        assert_eq!(sidecar.repo, dir.join("..").join("hermes-agent"));
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn read_sidecar_skips_comments_and_blank_lines() {
        let dir = unique_test_dir();
        std::fs::write(
            dir.join(TARGET_FILE),
            "# comment\n\n../tools/cpython-3.11.15/python.exe\n../venv/Lib/site-packages\n../hermes-agent\n",
        )
        .unwrap();
        let sidecar = read_sidecar(&dir).unwrap();
        assert_eq!(
            sidecar.interpreter,
            dir.join("..").join("tools").join("cpython-3.11.15").join("python.exe")
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn read_sidecar_rejects_wrong_line_counts() {
        let dir = unique_test_dir();
        std::fs::write(dir.join(TARGET_FILE), "../tools/python.exe\n").unwrap();
        assert!(read_sidecar(&dir).is_err());
        std::fs::remove_dir_all(&dir).unwrap();

        let dir = unique_test_dir();
        std::fs::write(
            dir.join(TARGET_FILE),
            "../tools/python.exe\n../venv/Lib/site-packages\n../hermes-agent\nextra\n",
        )
        .unwrap();
        assert!(read_sidecar(&dir).is_err());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn read_sidecar_rejects_empty_and_absolute_lines() {
        let dir = unique_test_dir();
        std::fs::write(dir.join(TARGET_FILE), "# only comments\n").unwrap();
        assert!(read_sidecar(&dir).is_err());
        std::fs::remove_dir_all(&dir).unwrap();

        let dir = unique_test_dir();
        std::fs::write(
            dir.join(TARGET_FILE),
            "/abs/python\n../venv/Lib/site-packages\n../hermes-agent\n",
        )
        .unwrap();
        assert!(read_sidecar(&dir).is_err());
        std::fs::remove_dir_all(&dir).unwrap();

        let dir = unique_test_dir();
        std::fs::write(
            dir.join(TARGET_FILE),
            "../tools/python.exe\nC:/venv/Lib/site-packages\n../hermes-agent\n",
        )
        .unwrap();
        assert!(read_sidecar(&dir).is_err());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn compose_pythonpath_orders_repo_before_site_packages() {
        let repo = Path::new("/payload/hermes-agent");
        let sp = Path::new("/payload/venv/Lib/site-packages");
        assert_eq!(
            compose_pythonpath(repo, sp, ";"),
            "/payload/hermes-agent;/payload/venv/Lib/site-packages"
        );
        assert_eq!(
            compose_pythonpath(repo, sp, ":"),
            "/payload/hermes-agent:/payload/venv/Lib/site-packages"
        );
    }

    fn unique_test_dir() -> std::path::PathBuf {
        use std::sync::atomic::{AtomicU32, Ordering};
        static COUNTER: AtomicU32 = AtomicU32::new(0);
        let dir = std::env::temp_dir().join(format!(
            "hermes-shim-test-{}-{}",
            std::process::id(),
            COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }
}
