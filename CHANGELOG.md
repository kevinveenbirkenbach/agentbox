# Changelog

## [1.2.0] - 2026-08-31

- Breaking: *agentbox mount* mounts writable; *--readonly* is the old shape
- Re-mounting a folder flips its access instead of adding a second entry
- *make test-e2e* installs docker and the compose plugin when missing
- Nested Docker: the daemon MTU follows the uplink's, not a fixed 1500
- Releases are published by hand; the release workflow is gone

## [1.1.0] - 2026-08-30

- *agentbox mount*: bind-mount a neighbour read-only, plus workspace entry
- *agentbox update*: re-provision a running box without a rebuild
- codex, gemini-cli, pi and oh-my-pi (on bun) beside claude-code
- Each agent's editor extension installed into the remote window
- *AGENTBOX_SKILLS*: clone skill collections and run their installers
- Seeded *~/.claude/settings.json*: *git commit* and *git push* ask
- Unsafe devcontainer config needs *--trust-config*, hashed on the host
- *make host-deps* installs sysbox: nested Docker without *--privileged*
- *agentbox up* writes the SSH *Include* for the remote window
- *agentbox code* prefers VSCodium and installs missing extensions
- *agentbox init* seeds workspace files, *code* opens them
- agentbox ships the devcontainer config it writes
- README states what the sandbox does and does not hold against
- Fix: an override's mounts no longer drop the home volume and */agentbox*
- Fix: local override and intermediate config are gitignored
- Fix: *make test-unit* runs inside the box
- Fix: *make install* from a checkout no longer trips over *pipx --force*

## [1.0.1] - 2026-08-24

Changed pypi.org name to agentboxer

## [1.0.0] - 2026-08-24

Official Release 🥳

