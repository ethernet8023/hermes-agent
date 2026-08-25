# nix/runtime-pins.nix — managed runtime tools, built from runtime-pins.json
#
# runtime-pins.json is the ONE table of managed tool versions and digests.
# It already feeds the Python provisioner (source installs, `hermes
# update`, desktop payload staging). This file makes Nix a fourth consumer
# of that table rather than a second table: every version, URL and digest
# here is read from the JSON, so a pin bump stays one edit.
#
# Shape:
#
#   * one derivation per pinned tool, each holding that tool's own tree
#     exactly as upstream ships it;
#   * `extends` in the table becomes a real Nix dependency — npm's
#     derivation takes node's, so Nix orders the builds and neither this
#     file nor a reader restates "npm needs node";
#   * `bundle` symlinks those derivations into the directory layout
#     `installation/registry.py` describes, and writes `runtimes.json`
#     with the registry's own code.
#
# Nothing here wraps a program or exports an environment variable. The
# bundle is a runtime dir, and `installation/env.py` already knows
# how to turn one of those into PATH, GIT_EXEC_PATH, npm_config_cache and
# the rest — on every install kind. A Nix-specific version of any of that
# would be a second implementation of tested behaviour.
{
  lib,
  stdenv,
  fetchurl,
  autoPatchelfHook,
  unzip,
  python3,
  runCommand,
  curl,
  expat,
  fontconfig,
  zlib,
}:
let
  pins = (builtins.fromJSON (builtins.readFile ../installation/runtime-pins.json)).tools;

  # Pin-table target keys use Node/Python spellings so one string works on
  # both sides of the JS/Python boundary; Nix systems spell it the other
  # way round. This is the only place the two vocabularies meet.
  targetBySystem = {
    "x86_64-linux" = "linux-x64";
    "aarch64-linux" = "linux-arm64";
    "x86_64-darwin" = "darwin-x64";
    "aarch64-darwin" = "darwin-arm64";
  };

  target =
    targetBySystem.${stdenv.hostPlatform.system}
      or (throw "runtime-pins: no pin target for ${stdenv.hostPlatform.system}");

  # A tool either pins one target-independent artifact ('any', a registry
  # tarball whose bytes do not vary) or one entry per target. That entry is
  # an artifact to download, or a declared gap saying why none exists. Same
  # resolution the Python registry does — see `pinned_file`.
  artifactOf =
    name: entry:
    let
      spec =
        entry.files.any or entry.files.${target}
          or (throw "runtime-pins: ${name} names no entry for ${target}");
    in
    if spec ? missing then
      throw "runtime-pins: ${name} has no build for ${target}: ${spec.missing}"
    else
      spec;

  # fetchurl's `sha256` takes the bare lowercase hex the table already
  # stores — the same string the Python provisioner verifies, so there is
  # no second encoding to keep in sync. Nix enforces it as a fixed-output
  # derivation: a tampered pin fails the build, as it fails provisioning.
  fetchPinned = name: entry: fetchurl { inherit (artifactOf name entry) url sha256; };

  extendsOf = entry: entry.extends or [ ];

  # Prebuilt upstream binaries link against a normal FHS glibc, which does
  # not exist here. autoPatchelfHook rewrites the interpreter and RPATH
  # onto the nixpkgs runtime; macOS binaries are already relocatable.
  #
  # One library set covers every tool: these are the shared objects the
  # pinned artifacts actually ask for (zlib broadly; curl/expat for
  # dugite's http helpers; fontconfig for the Skia lib dugite ships).
  patchelfInputs = lib.optionals stdenv.hostPlatform.isLinux [
    stdenv.cc.cc.lib
    zlib
    curl
    expat
    fontconfig
  ];

  mkToolBase =
    name: entry: extra:
    stdenv.mkDerivation (
      {
        pname = "hermes-runtime-${name}";
        version = entry.version;
        src = fetchPinned name entry;

        nativeBuildInputs = [ unzip ] ++ lib.optionals stdenv.hostPlatform.isLinux [ autoPatchelfHook ];
        buildInputs = patchelfInputs;

        dontUnpack = true;
        dontBuild = true;
        dontConfigure = true;

        passthru = {
          pinnedVersion = entry.version;
          pinnedUrl = (artifactOf name entry).url;
          extends = map (dep: tools.${dep}) (extendsOf entry);
        };

        meta = {
          description = "Hermes managed runtime ${name} ${entry.version} (pinned in runtime-pins.json)";
          platforms = lib.platforms.unix;
        };
      }
      // extra
    );

  # The common case: unpack the artifact and keep upstream's own layout.
  #
  # Un-nesting a lone versioned wrapper directory is decided by what the
  # archive CONTAINS, not by a per-tool list — the same rule the Python
  # provisioner uses, and for the same reason (uv nests on POSIX and not
  # on Windows, so a hardcoded list gets it wrong).
  mkUnpackedTool =
    name: entry:
    mkToolBase name entry {
      installPhase = ''
        runHook preInstall
        mkdir -p unpacked
        tar -xf "$src" -C unpacked 2>/dev/null || unzip -q "$src" -d unpacked

        inner=unpacked
        entries=("$inner"/*)
        if [ ''${#entries[@]} -eq 1 ] && [ -d "''${entries[0]}" ]; then
          inner="''${entries[0]}"
        fi

        mkdir -p "$out"
        cp -R "$inner"/. "$out/"
        runHook postInstall
      '';
    };

  # npm is the one tool that cannot simply be unpacked. Its own bin/npm
  # resolves npm-cli.js from dirname(process.execPath), so unpacked onto a
  # PATH it finds the npm BUNDLED inside node — the copy this pin exists
  # to supersede — and dies with MODULE_NOT_FOUND when that copy is gone.
  # Letting npm install itself produces the launchers the platform
  # actually needs instead of hand-written shims, and is the same install
  # the Python provisioner performs (`_stage_npm`) from the same
  # digest-verified tarball, offline.
  #
  # `extends` is what makes this work without a special case: node is a
  # real build input, so Nix has already built and patched it.
  mkNpmTool =
    name: entry:
    let
      node = tools.node;
    in
    mkToolBase name entry {
      installPhase = ''
        runHook preInstall
        mkdir -p "$out"
        # npm insists on a writable HOME and cache; the sandbox's HOME is
        # deliberately unwritable. Neither belongs in $out — a real
        # install keeps its cache in the runtime dir, which
        # managed_tool_env points npm_config_cache at.
        HOME="$TMPDIR" npm_config_cache="$TMPDIR/npm-cache" \
          ${node}/bin/node ${node}/lib/node_modules/npm/bin/npm-cli.js \
          install --global --prefix "$out" --offline --no-audit --no-fund \
          "$src"

        # npm writes its launchers with `#!/usr/bin/env node`, which does
        # not exist inside a Nix build sandbox — any derivation using this
        # npm as a BUILD tool dies with "bad interpreter". Point them at
        # the node this tool extends, which is also more correct: the
        # pinned npm should run on the pinned node, not on whatever node
        # a PATH lookup finds first.
        #
        # patchShebangs is not enough on its own here: it resolves `env
        # node` against the build PATH, which is not necessarily the node
        # in the pin table.
        for launcher in "$out"/bin/*; do
          [ -f "$launcher" ] || continue
          case "$(head -c 2 "$launcher")" in
            '#!') substituteInPlace "$launcher" \
                    --replace-quiet '#!/usr/bin/env node' '#!${node}/bin/node' ;;
          esac
        done
        runHook postInstall
      '';
    };

  # An `extends` edge means "staged by what it extends", which today is
  # npm-shaped: run the extended tool's installer. A second extender with
  # different mechanics would add a branch here; one edge, one meaning
  # until then.
  mkTool =
    name: entry: if extendsOf entry == [ ] then mkUnpackedTool name entry else mkNpmTool name entry;

  # OPTIONAL tools are provisioned on demand into a writable runtime dir,
  # and a Nix install has no such thing: its runtime dir is a sealed store
  # path. Building them into the bundle would ship an on-demand capability
  # to every user, so the bundle carries the required tools only. A Nix
  # user who wants an optional capability gets it the way they get every
  # other package: by adding it to their configuration.
  requiredPins = lib.filterAttrs (_: entry: !(entry.optional or false)) pins;

  tools = lib.mapAttrs mkTool requiredPins;

  # The installation package, filtered to source. An allowlist of .py and
  # .json rather than a copy of the directory: a stray __pycache__ or an
  # editor swapfile would otherwise change this derivation's hash, so the
  # bundle would rebuild depending on whether someone had run Python in
  # the checkout.
  installationSrc = lib.cleanSourceWith {
    src = ../installation;
    name = "hermes-installation-src";
    filter =
      path: type:
      let
        base = baseNameOf (toString path);
      in
      if type == "directory" then
        base != "__pycache__"
      else
        lib.hasSuffix ".py" base || lib.hasSuffix ".json" base;
  };

  # The bundle IS a tool store: one entry per pinned tuple, named
  # `<tool>-<version>-<target>` exactly as `registry.store_entry_name`
  # spells it, plus the `runtimes.json` facts manifest beside them. It is
  # therefore its own facts dir AND its own store — a sealed store path
  # cannot write into the machine-wide `~/.hermes/tools`, and does not
  # need to: everything it will ever hold is built here. Symlinks, so the
  # tools stay separately built and separately cached.
  #
  # Facts are written by installation/registry.py itself, and the PATH
  # dirs are then read back out with installation.env.managed_path_dirs —
  # the same call every Hermes subprocess makes. Nix consumers take that
  # list instead of guessing, which matters because the layout is
  # per-tool: node/git/gh/npm expose `<entry>/bin`, uv and ripgrep put
  # the binary at `<entry>/` directly, and lib.makeBinPath (which only
  # ever appends /bin) silently drops the second kind.
  bundle =
    runCommand "hermes-runtime-dir"
      {
        passthru = tools // {
          inherit target;
          pinnedVersions = lib.mapAttrs (_: entry: entry.version) pins;
        };
      }
      ''
        mkdir -p "$out"
        ${lib.concatStringsSep "\n" (
          lib.mapAttrsToList (
            name: drv: ''ln -s ${drv} "$out/${name}-${requiredPins.${name}.version}-${target}"''
          ) tools
        )}

        mkdir -p "$TMPDIR/pypath"
        ln -s ${installationSrc} "$TMPDIR/pypath/installation"
        export PYTHONPATH="$TMPDIR/pypath"
        ${python3}/bin/python3 - "$out" "${target}" <<'PY'
        import sys
        from pathlib import Path

        from installation.registry import (
            RuntimeFact, install_order, is_optional, load_pins, path_order, save_facts,
        )
        from installation.provisioner import _fact_path_dirs, _fact_rel

        runtime_dir, target = Path(sys.argv[1]), sys.argv[2]
        pins = load_pins()

        facts = {}
        for tool in install_order(pins):
            # Optional tools are provisioned on demand into a writable
            # runtime dir; this bundle is a sealed store path and carries
            # the required tools only (see requiredPins above). Recording
            # one here would promise a binary that was never built.
            if is_optional(tool, pins):
                continue
            version = pins[tool]["version"]
            rel = _fact_rel(tool, version, target)
            if not (runtime_dir / rel).exists():
                raise SystemExit(f"runtime-pins: {tool} is missing {rel}")
            facts[tool] = RuntimeFact(
                version=version,
                path=rel,
                path_dirs=_fact_path_dirs(tool, version, target),
            )

        save_facts(facts, runtime_dir, path_order=path_order(pins))

        # Emit the assembled PATH dirs and tool env for Nix consumers,
        # straight out of the assembler every Hermes subprocess uses. Written
        # as files rather than recomputed in Nix because both are genuinely
        # per-tool and already encoded here: uv and ripgrep keep their binary
        # at the tree root while the rest use bin/ (`_dirs_for`), and dugite's
        # git needs GIT_EXEC_PATH or it cannot find its own remote helpers
        # (`managed_tool_env`).
        from installation.env import managed_path_dirs, managed_tool_env  # noqa: E402

        dirs = managed_path_dirs(runtime_dir)
        assert len(dirs) >= len(facts), (
            f"assembled {len(dirs)} PATH dirs for {len(facts)} tools — "
            "a provisioned tool contributed nothing"
        )
        (runtime_dir / "path-dirs").write_text(
            "".join(f"{d}\n" for d in dirs), encoding="utf-8"
        )

        # Shell-sourceable, one `export K=V` per line. shlex.quote because a
        # store path is well-behaved but this is going through a shell.
        import shlex  # noqa: E402

        (runtime_dir / "tool-env").write_text(
            "".join(
                f"export {key}={shlex.quote(value)}\n"
                for key, value in sorted(managed_tool_env(runtime_dir).items())
            ),
            encoding="utf-8",
        )
        PY
      '';
in
bundle
