{
  lib,
  stdenv,
  fetchurl,
  unzip,
}:
# The thin translation layer: pm/lock.json -> one fetched, unpacked
# derivation per package for this nix system. Nothing here knows versions,
# urls, or hashes — the lockfile is the complete machine interface, the
# same one `pm install` and `pm bundle` consume.
#
# Consumers pick what they need:   (callPackage ./pm-packages.nix { }).ripgrep
# Packages with no artifact for this system are simply absent.
let
  lock = builtins.fromJSON (builtins.readFile ../pm/lock.json);

  target =
    let
      arch = if stdenv.hostPlatform.isAarch64 then "arm64" else "x64";
      os =
        if stdenv.hostPlatform.isDarwin then "darwin"
        else if stdenv.hostPlatform.isLinux then "linux"
        else "win32";
    in
    "${os}-${arch}";

  artifactFor = pin: pin.artifacts.${target} or pin.artifacts.any or null;

  derive = name: pin: artifact:
    stdenv.mkDerivation {
      pname = name;
      version = pin.version;

      src = fetchurl {
        url = artifact.url;
        sha256 = artifact.sha256;
      };

      # pm's store publishes the unpacked tree; mirror that shape.
      sourceRoot = ".";
      nativeBuildInputs = lib.optional (lib.hasSuffix ".zip" artifact.url) unzip;
      dontBuild = true;
      dontConfigure = true;

      installPhase = ''
        mkdir -p $out
        cp -r . $out/
      '';
    };
in
lib.filterAttrs (_: v: v != null) (
  lib.mapAttrs (
    name: pin:
    let
      artifact = artifactFor pin;
    in
    if artifact == null then null else derive name pin artifact
  ) lock.packages
)
