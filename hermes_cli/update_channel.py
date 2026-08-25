"""Per-install update-channel records — the manifest's replacement.

The old ``.hermes-install.json`` manifest carried TWO facts and both moved
elsewhere: ``installMode`` was a second source of truth for the stamp's
question (the exact cause of the sealed-refusal bug — the stamp's required
``updateMechanism`` answers it now), and ``channel`` was config. Neither
``read_install_manifest`` nor ``is_bundled_install`` nor eject exist
anymore; this module keeps only the channel vocabulary and the per-install
channel records.

Channel storage — per install, never home-global::

    update:
      installs:
        a4f3b2c1d0e9f8a7:                      # install id (sha16 of the
          path: /home/u/.hermes/hermes-agent   #   canonical install root)
          channel: nightly

One config.yaml serves many installs (host + docker gateway + desktop all
bind-mount one ``~/.hermes``), so a home-global ``update.channel`` key is
UNSAFE and does not exist: setting nightly for a dev checkout must not
flip the desktop app's feed. The id is ``boot_bootstrap._install_key`` —
sha16 of the canonical install-root PATH, the same key that names the
``installs/<sha16>/`` state folder. Path-derived on purpose: an
electron-updater update replaces the artifact (new stamp bytes) at the
same path, and the channel opt-in must survive that.

* Written by ``hermes update --set-channel <x>`` from inside an install
  (it knows its own id — the user never types a sha).
* Shown by ``hermes update --install-id`` and the desktop About page.
* Channels are meaningful ONLY where the mechanism is ``self`` (which git
  ref: main / stable / nightly→main) or ``electron-updater`` (which feed:
  latest.yml / nightly.yml). ``external`` installs have no channel; the
  steward owns updates.

Pure-stdlib leaf module (plus hermes-internal imports done lazily): the
installers and boot paths read it before the full config machinery loads.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

CHANNEL_MAIN = "main"
CHANNEL_STABLE = "stable"
CHANNEL_NIGHTLY = "nightly"
VALID_CHANNELS = (CHANNEL_MAIN, CHANNEL_STABLE, CHANNEL_NIGHTLY)

# A nightly release tag: v<major>.<minor>.0-nightly.<YYYYMMDDHHMMSS>, or the
# legacy date-only shape. Mirrors _release_tag in
# scripts/write_install_stamp.py, which writes the ``tag`` field this reads.
_NIGHTLY_TAG_RE = re.compile(r"^v(?:0|[1-9]\d{0,2})\.\d+\.\d+-nightly\.20\d{6}(?:\d{6})?$")


def is_nightly_tag(tag: Any) -> bool:
    """True when ``tag`` is a nightly release tag."""
    return isinstance(tag, str) and bool(_NIGHTLY_TAG_RE.match(tag.strip()))


def install_id(project_root: Optional[Path] = None) -> str:
    """The sha16 id of the install at ``project_root`` (default: this one).

    Delegates to ``boot_bootstrap._install_key`` so the channel record and
    the ``installs/<sha16>/`` state folder share one identity.
    """
    from hermes_cli.boot_bootstrap import _install_key

    if project_root is None:
        from installation.paths import get_install_root

        project_root = get_install_root()
    return _install_key(Path(project_root))


def _install_records(config: Optional[dict]) -> dict:
    if not isinstance(config, dict):
        return {}
    update_cfg = config.get("update")
    if not isinstance(update_cfg, dict):
        return {}
    installs = update_cfg.get("installs")
    return installs if isinstance(installs, dict) else {}


def channel_record(config: Optional[dict], project_root: Optional[Path] = None) -> dict:
    """This install's ``{path, channel}`` record from config, or ``{}``."""
    record = _install_records(config).get(install_id(project_root))
    return record if isinstance(record, dict) else {}


def default_channel(project_root: Optional[Path] = None) -> str:
    """The channel an unconfigured install tracks.

    ``self`` source installs follow main (historical behavior).
    ``electron-updater`` bundles follow the channel their own artifact was
    published to: a nightly artifact tracks nightly, every other bundle
    tracks stable. The stamp's ``tag`` is the authority, the same fact
    apps/desktop/product-identity.cjs keys the published feed name on — so
    the feed a nightly artifact asks for and the feed it was published to
    can never disagree. Deriving stable here instead would send a fresh
    nightly install to look for its ``nightly.yml`` feed file under the
    newest STABLE release, where that file does not exist (404), leaving
    the install unable to update at all.
    """
    from installation.paths import get_install_root
    from installation.tree import read_build_info

    root = Path(project_root) if project_root is not None else get_install_root()
    try:
        build_info = read_build_info(root)
    except RuntimeError:
        return CHANNEL_MAIN
    if build_info.get("updateMechanism") != "electron-updater":
        return CHANNEL_MAIN
    return CHANNEL_NIGHTLY if is_nightly_tag(build_info.get("tag")) else CHANNEL_STABLE


def resolve_update_channel(
    config: Optional[dict] = None,
    project_root: Optional[Path] = None,
) -> str:
    """The effective update channel for this install.

    Resolution: the per-install record (``update.installs.<sha16>.channel``)
    when valid; otherwise the mechanism default (main for self-source,
    stable for electron-updater bundles). Source installs asking for
    nightly normalize to main — nightly builds are release artifacts, and
    a git checkout tracks branches; callers print the note.
    """
    configured: Any = channel_record(config, project_root).get("channel")
    if isinstance(configured, str) and configured.strip().lower() in VALID_CHANNELS:
        channel = configured.strip().lower()
    else:
        channel = default_channel(project_root)

    if channel == CHANNEL_NIGHTLY:
        from installation.paths import get_install_root
        from installation.tree import read_build_info

        root = Path(project_root) if project_root is not None else get_install_root()
        try:
            mechanism = read_build_info(root).get("updateMechanism")
        except RuntimeError:
            mechanism = None
        if mechanism != "electron-updater":
            # nightly→main normalization for source installs.
            return CHANNEL_MAIN
    return channel


def nightly_normalized_note() -> str:
    """The one-line note callers print when nightly normalizes to main."""
    return (
        "→ Channel 'nightly' on a source install tracks main "
        "(nightly builds are desktop release artifacts)."
    )


def set_install_channel(
    channel: str,
    project_root: Optional[Path] = None,
) -> str:
    """Persist ``channel`` for THIS install in config.yaml. Returns the id.

    Refuses on ``external`` mechanism — those installs have no channel;
    the steward owns updates. Raises ``ValueError`` for both bad channel
    values and external installs; the CLI surfaces the message.
    """
    channel = (channel or "").strip().lower()
    if channel not in VALID_CHANNELS:
        raise ValueError(
            f"unknown channel {channel!r} (one of {', '.join(VALID_CHANNELS)})"
        )

    from installation.paths import get_install_root
    from installation.tree import read_build_info

    root = Path(project_root) if project_root is not None else get_install_root()
    try:
        mechanism = read_build_info(root).get("updateMechanism")
    except RuntimeError:
        mechanism = None
    if mechanism == "external":
        distribution = read_build_info(root).get("distribution") or "an external steward"
        raise ValueError(
            f"channels don't apply here; updates are owned by {distribution}"
        )

    sha16 = install_id(root)
    _write_channel_record(sha16, str(root), channel, artifact_channel=default_channel(root))
    return sha16


def seed_install_channel(project_root: Optional[Path] = None) -> Optional[str]:
    """Seed this install's channel record from the artifact, at boot.

    The record is keyed by PATH and lives in config.yaml, so it outlives the
    artifact that wrote it: install stable, uninstall, install nightly to
    the same location, and the stale ``stable`` record would pin the new
    nightly build to the stable feed forever. Installing a build of a given
    flavor IS the choice of that flavor, so the artifact wins that argument.

    ``artifactChannel`` records which artifact default the stored channel
    was chosen against, which is what makes the two cases separable:

    * it matches the current artifact — the stored channel is a deliberate
      choice made on THIS flavor of build (``--set-channel``), so keep it.
    * it differs, or is absent — the artifact changed under the record;
      reseed to the new artifact's default.

    Returns the channel written, or None when nothing needed writing.
    Never raises: a config problem must not stop boot.
    """
    from installation.paths import get_install_root

    root = Path(project_root) if project_root is not None else get_install_root()
    try:
        artifact = default_channel(root)
        # Only artifacts with a release feed have a flavor to seed from; a
        # source checkout's channel is not an artifact property.
        if artifact not in (CHANNEL_STABLE, CHANNEL_NIGHTLY):
            return None

        from hermes_cli.config import get_config_path

        import yaml

        try:
            raw = yaml.safe_load(get_config_path().read_text(encoding="utf-8-sig")) or {}
        except (FileNotFoundError, OSError, ValueError):
            raw = {}
        record = channel_record(raw if isinstance(raw, dict) else {}, root)

        # A deliberate choice made on THIS flavor of build stands.
        if record.get("artifactChannel") == artifact:
            return None

        _write_channel_record(install_id(root), str(root), artifact, artifact_channel=artifact)
        return artifact
    except Exception as exc:  # noqa: BLE001 — seeding must never break boot
        logger.debug("channel seeding skipped: %s", exc)
        return None


def _write_channel_record(
    sha16: str, path: str, channel: str, artifact_channel: Optional[str] = None
) -> None:
    """Write ``update.installs.<sha16>`` into config.yaml, preserving the rest."""
    import yaml

    from hermes_cli.config import get_config_path

    config_path = get_config_path()
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
    except FileNotFoundError:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"config at {config_path} is not a mapping")

    update_cfg = raw.setdefault("update", {})
    if not isinstance(update_cfg, dict):
        raise ValueError("config key 'update' is not a mapping")
    installs = update_cfg.setdefault("installs", {})
    if not isinstance(installs, dict):
        raise ValueError("config key 'update.installs' is not a mapping")
    record = installs.setdefault(sha16, {})
    if not isinstance(record, dict):
        record = {}
        installs[sha16] = record
    record["path"] = path  # DATA, for humans + doctor GC
    record["channel"] = channel
    # Which artifact default this channel was chosen against. seed_install_channel
    # compares it to the installed artifact to tell a deliberate choice from a
    # record the artifact outlived.
    if artifact_channel is not None:
        record["artifactChannel"] = artifact_channel

    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    tmp.replace(config_path)


def stale_channel_records(config: Optional[dict]) -> list[tuple[str, dict, str]]:
    """Doctor's staleness triad over ``update.installs``.

    Returns ``(sha16, record, reason)`` where reason is one of:

    * ``"replaced"`` — the recorded path exists but the install there keys
      to a DIFFERENT sha16 (the tree moved / was recreated elsewhere and a
      new record claimed it; this one is a leftover).
    * ``"missing"``  — nothing at the recorded path: offer GC (keep-on-doubt).
    * ``"unclaimed"`` — the sha16 matches no live install record
      (``installs/<sha16>/install.json``): offer GC.
    """
    from hermes_cli.boot_bootstrap import _install_key

    stale: list[tuple[str, dict, str]] = []
    for sha16, record in _install_records(config).items():
        if not isinstance(record, dict):
            continue
        recorded_path = record.get("path")
        if not isinstance(recorded_path, str) or not recorded_path:
            # No path fact — fall through to the live-record check only.
            recorded_path = None

        if recorded_path is not None:
            path = Path(recorded_path)
            if not path.exists():
                stale.append((sha16, record, "missing"))
                continue
            if _install_key(path) != sha16:
                stale.append((sha16, record, "replaced"))
                continue

        # Cross-check against the live install-state records: a channel
        # record whose sha16 has no installs/<sha16>/install.json was
        # either hand-written or its install never booted post-record.
        try:
            from hermes_cli.boot_bootstrap import installs_root

            if not (installs_root() / sha16 / "install.json").is_file():
                stale.append((sha16, record, "unclaimed"))
        except Exception as exc:  # noqa: BLE001 — doctor sweep must not raise
            logger.debug("installs_root unavailable: %s", exc)
    return stale
