"""This Hermes installation: who owns it, and the tools it needs.

Two halves of one question, kept together because they answer each other:

* :mod:`installation.tree` derives WHO OWNS the running tree — a git
  checkout that ``hermes update`` may replace, or a sealed tree whose
  steward (Nix, Docker, the desktop bundle) owns it wholesale.
* :mod:`installation.registry`, :mod:`installation.provisioner` and
  :mod:`installation.env` own the native tools that install needs: the
  pin table, the download-verify-stage engine, and the PATH assembly.

Ownership decides what provisioning is allowed to do. A git checkout
heals its own drift; a sealed tree cannot, so drift there means the
artifact was built against a different pin table than the code it ships
and the steward has to rebuild.

``runtime-pins.json`` lives here rather than at the repo root because the
pins ship WITH the code — same review, same version, same release.

Everything here is pure stdlib. the provisioner installs the tools the
rest of Hermes needs, so it has to import before anything is installed.
"""
