from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import config as cfg

DEVCONTAINER_CLI = ["npx", "--yes", "@devcontainers/cli"]
CONTAINER_USER = "dev"
CONTAINER_SSH_PORT = 2222
EDITORS = ("codium", "code", "code-oss")
RESOLVERS = ("jeanp413.open-remote-ssh", "ms-vscode-remote.remote-ssh")
EDITORS_WITHOUT_REMOTE_SERVER = ("code-oss",)
VSCODIUM_PACKAGE = "vscodium-bin"
SETTINGS_DIRS = (("VSCodium", "Code - OSS"),)
GITIGNORE_ENTRIES = (
    cfg.MERGED_IN_PROJECT,
    f"*{cfg.WORKSPACE_SUFFIX}",
    f"!{cfg.DEFAULT_WORKSPACE}",
)
UP_BINARIES = ("docker", "npx", "ssh-keygen")
EXEC_BINARIES = ("docker", "npx")


def missing_binaries(names: tuple[str, ...]) -> list[str]:
    return [name for name in names if shutil.which(name) is None]


def require_binaries(names: tuple[str, ...]) -> int:
    missing = missing_binaries(names)
    if not missing:
        return 0
    print(f"✖ missing on this host: {', '.join(missing)}", file=sys.stderr)
    print(f"  agentbox needs: {', '.join(names)}", file=sys.stderr)
    return 3


def state_dir() -> Path:
    override = os.environ.get("AGENTBOX_HOME")
    if override:
        return Path(override)
    return Path.home() / ".config" / "agentbox"


def ssh_dir(state: Path) -> Path:
    return state / "ssh.d"


def include_line(state: Path) -> str:
    return f"Include {ssh_dir(state)}/*.conf"


def _capture(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def container_id(workspace: Path) -> str | None:
    label = f"label=devcontainer.local_folder={workspace.resolve()}"
    out = _capture(["docker", "ps", "-aq", "--filter", label])
    ids = out.splitlines()
    if not ids:
        return None
    return ids[0]


def ssh_port(cid: str) -> int:
    out = _capture(["docker", "port", cid, f"{CONTAINER_SSH_PORT}/tcp"])
    first = out.splitlines()[0]
    return int(first.rsplit(":", 1)[1])


def ensure_key(state: Path, alias: str) -> Path:
    key_file = state / "keys" / alias / "id_ed25519"
    if key_file.exists():
        return key_file
    key_file.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            f"agentbox-{alias}",
            "-f",
            str(key_file),
        ],
        check=True,
    )
    return key_file


def exec_command(workspace: Path, override: Path | None, command: list[str]) -> list[str]:
    prefix = [*DEVCONTAINER_CLI, "exec", "--workspace-folder", str(workspace)]
    if override is not None:
        prefix += ["--override-config", str(override)]
    return [*prefix, *command]


def inject_key(workspace: Path, override: Path | None, key_file: Path) -> None:
    remote = (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        "cat >~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
    )
    subprocess.run(
        exec_command(workspace, override, ["bash", "-c", remote]),
        input=key_file.with_suffix(".pub").read_text(encoding="utf-8"),
        text=True,
        check=True,
    )


def alias_dir(state: Path) -> Path:
    return state / "aliases"


def claim_alias(workspace: Path, state: Path) -> str:
    alias = cfg.alias_for(workspace, alias_dir(state))
    cfg.claim_alias(alias_dir(state), alias, workspace)
    return alias


def alias_for(workspace: Path, state: Path) -> str:
    return cfg.alias_for(workspace, alias_dir(state))


def write_ssh_config(state: Path, alias: str, port: int, key_file: Path) -> None:
    target = ssh_dir(state) / f"{alias}.conf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        cfg.ssh_config_block(alias, port, key_file, CONTAINER_USER),
        encoding="utf-8",
    )


def ensure_ssh_include(state: Path, home: Path) -> str:
    line = include_line(state)
    user_config = home / ".ssh" / "config"
    try:
        existing = user_config.read_text(encoding="utf-8") if user_config.exists() else ""
        if line in existing.splitlines():
            return "present"
        user_config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        user_config.write_text(f"{line}\n\n{existing}", encoding="utf-8")
        user_config.chmod(0o600)
        return "added"
    except OSError:
        return "failed"


def sysbox_available() -> bool:
    result = subprocess.run(
        ["docker", "info", "--format", "{{json .Runtimes}}"], capture_output=True, text=True
    )
    return cfg.SYSBOX_RUNTIME in result.stdout


def harden_nested_docker(source: Path, config: dict) -> Path | None:
    features = cfg.privilege_features(config)
    if features:
        print(f"⚠ {', '.join(features)} forces --privileged on this box:", file=sys.stderr)
        print("  every capability, no AppArmor, the host's block devices in /dev.", file=sys.stderr)
        print("  Escaping is a mount command, not an exploit.", file=sys.stderr)
        if sysbox_available():
            print(f"  {cfg.SYSBOX_RUNTIME} is installed but cannot be combined with it — drop the", file=sys.stderr)
            print("  feature and install docker inside the image instead; sysbox provides the", file=sys.stderr)
            print("  nested daemon without privileges.", file=sys.stderr)
        else:
            print("  Install sysbox, drop the feature, and install docker inside the image:", file=sys.stderr)
            print("    yay -S sysbox-ce-bin", file=sys.stderr)
        return None

    if not sysbox_available():
        return None

    hardened = cfg.write_hardened(source, cfg.harden(config))
    print(f"→ running under {cfg.SYSBOX_RUNTIME}: container root is an unprivileged host user")
    return hardened


SEED_FILE = "/tmp/agentbox-seed.json"
SEED_SETTINGS_SCRIPT = (
    f"mkdir -p ~/.claude && cat >{SEED_FILE} && "
    "node -e '"
    'const fs = require("fs");'
    'const target = process.env.HOME + "/.claude/settings.json";'
    f'const seed = JSON.parse(fs.readFileSync("{SEED_FILE}", "utf8"));'
    "let current = {};"
    "if (fs.existsSync(target)) {"
    'const raw = fs.readFileSync(target, "utf8").trim();'
    "if (raw) { try { current = JSON.parse(raw); } catch (error) { process.exit(0); } }"
    "}"
    "if (current.permissions) { process.exit(0); }"
    'fs.writeFileSync(target, JSON.stringify({ ...current, ...seed }, null, 2) + "\\n");'
    'console.log("seeded");'
    f"'; status=$?; rm -f {SEED_FILE}; exit $status"
)


def seed_agent_settings(workspace: Path, override: Path | None) -> str:
    result = subprocess.run(
        exec_command(workspace, override, ["bash", "-c", SEED_SETTINGS_SCRIPT]),
        input=cfg.box_settings_json(),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return "failed"
    return "seeded" if "seeded" in result.stdout else "kept"


def trusted_file(state: Path, alias: str) -> Path:
    return state / "trusted" / alias


def is_trusted(state: Path, alias: str, digest: str) -> bool:
    record = trusted_file(state, alias)
    try:
        return record.read_text(encoding="utf-8").strip() == digest
    except OSError:
        return False


def record_trust(state: Path, alias: str, digest: str) -> None:
    record = trusted_file(state, alias)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(f"{digest}\n", encoding="utf-8")


def guard_project_config(workspace: Path, state: Path, alias: str, trust_now: bool) -> int:
    layers = cfg.project_layers(workspace)
    risky = cfg.risky_settings(layers)
    if not risky:
        return 0

    digest = cfg.config_digest(layers)
    if trust_now:
        record_trust(state, alias, digest)
        print(f"→ trusted this project's {', '.join(risky)}")
        return 0
    if is_trusted(state, alias, digest):
        return 0

    print(f"✖ {cfg.PROJECT_CONFIG} sets {', '.join(risky)}", file=sys.stderr)
    print("  These decide what the container may reach on this host, and an agent with", file=sys.stderr)
    print("  write access to the repository can change them. Read the diff, then allow", file=sys.stderr)
    print("  this exact configuration:", file=sys.stderr)
    print("    agentbox up --trust-config", file=sys.stderr)
    return 6


def up_command(workspace: Path, override: Path | None, rebuild: bool) -> list[str]:
    command = [*DEVCONTAINER_CLI, "up", "--workspace-folder", str(workspace)]
    if override is not None:
        command += ["--override-config", str(override)]
        if not override.is_relative_to(workspace):
            command += ["--no-lockfile"]
    if rebuild:
        command += ["--remove-existing-container"]
    return command


def cmd_up(args: argparse.Namespace) -> int:
    blocked = require_binaries(UP_BINARIES)
    if blocked:
        return blocked

    workspace = args.workspace.resolve()
    state = state_dir()
    alias = claim_alias(workspace, state)

    blocked = guard_project_config(workspace, state, alias, args.trust_config)
    if blocked:
        return blocked

    key_file = ensure_key(state, alias)

    override = cfg.resolve_config(workspace, state / "run", cfg.SHARE_DIR, alias)
    source = override if override is not None else workspace / cfg.PROJECT_CONFIG
    hardened = harden_nested_docker(source, json.loads(source.read_text(encoding="utf-8")))
    subprocess.run(up_command(workspace, hardened or override, args.rebuild), check=True)

    cid = container_id(workspace)
    if cid is None:
        print("✖ container not found after up", file=sys.stderr)
        return 1

    inject_key(workspace, override, key_file)

    if cfg.privilege_features(json.loads(source.read_text(encoding="utf-8"))):
        print("→ agent permissions left untouched: a privileged box is no boundary", file=sys.stderr)
    else:
        seeded = seed_agent_settings(workspace, hardened or override)
        if seeded == "seeded":
            print("→ agents run unrestricted inside the box, commit and push ask first")
        if seeded == "failed":
            print("✖ could not write ~/.claude/settings.json in the box", file=sys.stderr)
            print("  the agent runs on its own defaults there, commit and push do not ask", file=sys.stderr)

    port = ssh_port(cid)
    write_ssh_config(state, alias, port, key_file)
    include = ensure_ssh_include(state, Path.home())

    print(f"\nagentbox '{alias}' is up on 127.0.0.1:{port}\n")
    print("  agentbox run claude       agent inside the container")
    print("  agentbox shell            shell inside the container")
    print("  agentbox code             open the editor on the container\n")
    if include == "added":
        print(f"Added to ~/.ssh/config so 'ssh {alias}' works:")
        print(f"  {include_line(state)}\n")
    if include == "failed":
        print(f"✖ could not update ~/.ssh/config — add this line yourself, at the top:", file=sys.stderr)
        print(f"  {include_line(state)}\n", file=sys.stderr)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    blocked = require_binaries(EXEC_BINARIES)
    if blocked:
        return blocked

    workspace = args.workspace.resolve()
    state = state_dir()
    alias = alias_for(workspace, state)
    override = cfg.resolve_config(workspace, state / "run", cfg.SHARE_DIR, alias)
    return subprocess.run(exec_command(workspace, override, args.command)).returncode


def cmd_shell(args: argparse.Namespace) -> int:
    args.command = ["zsh"]
    return cmd_run(args)


def with_env(command: str, env: dict[str, str]) -> str:
    if not env:
        return command
    prefix = " ".join(f"{name}={shlex.quote(value)}" for name, value in env.items())
    return f"{prefix} {command}"


def cmd_update(args: argparse.Namespace) -> int:
    blocked = require_binaries(EXEC_BINARIES)
    if blocked:
        return blocked

    workspace = args.workspace.resolve()
    command = cfg.post_create_command(workspace)
    if command is None:
        print(f"✖ {cfg.PROJECT_CONFIG} provisions the box itself", file=sys.stderr)
        print(f"  There is no {cfg.PROJECT_POST_CREATE} to re-run. Run whatever its", file=sys.stderr)
        print("  postCreateCommand does, or rebuild: agentbox up --rebuild", file=sys.stderr)
        return 8

    state = state_dir()
    alias = alias_for(workspace, state)
    override = cfg.resolve_config(workspace, state / "run", cfg.SHARE_DIR, alias)
    source = override if override is not None else workspace / cfg.PROJECT_CONFIG
    env = cfg.provisioning_env(json.loads(source.read_text(encoding="utf-8")))

    print("→ re-running the provisioning inside the running box")
    args.command = ["bash", "-c", with_env(command, env)]
    return cmd_run(args)


def find_editor() -> str | None:
    for name in EDITORS:
        path = shutil.which(name)
        if path:
            return path
    return None


def installed_resolver(editor: str) -> str | None:
    result = subprocess.run([editor, "--list-extensions"], capture_output=True, text=True)
    for resolver in RESOLVERS:
        if resolver in result.stdout:
            return resolver
    return None


def ensure_resolver(editor: str) -> int:
    if installed_resolver(editor) is not None:
        return 0
    for resolver in RESOLVERS:
        print(f"→ installing {resolver} into {editor}")
        if subprocess.run([editor, "--install-extension", resolver]).returncode == 0:
            print(f"→ quit every running {Path(editor).name} window before connecting: a resolver")
            print("  installed after startup is not picked up by a running instance")
            return 0
    print(f"✖ {editor} cannot resolve ssh-remote — no marketplace served either resolver", file=sys.stderr)
    for resolver in RESOLVERS:
        print(f"  tried: {editor} --install-extension {resolver}", file=sys.stderr)
    return 4


def editor_name(editor: str) -> str:
    return Path(editor).resolve().name


def require_capable_editor(editor: str) -> int:
    if editor_name(editor) not in EDITORS_WITHOUT_REMOTE_SERVER:
        return 0
    print(f"✖ {editor_name(editor)} cannot open a remote window: it publishes no remote", file=sys.stderr)
    print("  server build of its own, and the client installs the server under its own", file=sys.stderr)
    print("  commit hash, which no other build matches.", file=sys.stderr)
    print(f"  Install VSCodium first, then run this again — agentbox prefers it:", file=sys.stderr)
    print(f"    yay -S {VSCODIUM_PACKAGE}", file=sys.stderr)
    return 5


def link_editor_settings(home: Path) -> str:
    for first, second in SETTINGS_DIRS:
        primary = home / ".config" / first / "User"
        secondary = home / ".config" / second / "User"
        if primary.is_symlink() or secondary.is_symlink():
            return "present"
        if primary.exists() and secondary.exists():
            return "conflict"
        if secondary.exists():
            source, link = secondary, primary
        elif primary.exists():
            source, link = primary, secondary
        else:
            return "nothing"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(source)
    return "linked"


def host_extensions(editor: str) -> list[str]:
    result = subprocess.run([editor, "--list-extensions"], capture_output=True, text=True)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def remote_extensions(editor: str, alias: str) -> list[str]:
    result = subprocess.run(
        [editor, "--remote", f"ssh-remote+{alias}", "--list-extensions"],
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def missing_extensions(wanted: list[str], installed: list[str]) -> list[str]:
    present = {name.lower() for name in installed}
    return [name for name in wanted if name.lower() not in present]


def install_arguments(editor: str, alias: str, missing: list[str]) -> list[str]:
    command = [editor, "--remote", f"ssh-remote+{alias}"]
    for name in missing:
        command += ["--install-extension", name]
    return command


def sync_extensions(editor: str, alias: str, wanted: list[str]) -> int:
    if not wanted:
        return 0
    missing = missing_extensions(wanted, remote_extensions(editor, alias))
    if not missing:
        return 0
    print(f"→ installing {len(missing)} extensions into the box: {', '.join(missing)}")
    subprocess.run(install_arguments(editor, alias, missing))
    return len(missing)


def cmd_code(args: argparse.Namespace) -> int:
    editor = find_editor()
    if editor is None:
        print(f"✖ no editor found, looked for: {', '.join(EDITORS)}", file=sys.stderr)
        print(f"  install VSCodium: yay -S {VSCODIUM_PACKAGE}", file=sys.stderr)
        return 1

    blocked = require_capable_editor(editor)
    if blocked:
        return blocked

    linked = link_editor_settings(Path.home())
    if linked == "linked":
        print("→ linked the VSCodium and Code - OSS settings directories, configure either one")
    if linked == "conflict":
        print("⚠ VSCodium and Code - OSS both carry their own settings — not linking them", file=sys.stderr)
    blocked = ensure_resolver(editor)
    if blocked:
        return blocked

    workspace = args.workspace.resolve()
    state = state_dir()
    alias = alias_for(workspace, state)

    override = cfg.resolve_config(workspace, state / "run", cfg.SHARE_DIR, alias)
    source = override if override is not None else workspace / cfg.PROJECT_CONFIG
    declared = cfg.declared_extensions(json.loads(source.read_text(encoding="utf-8")))
    sync_extensions(editor, alias, declared or host_extensions(editor))

    try:
        target = cfg.resolve_workspace_file(workspace, args.name)
    except LookupError as error:
        print(f"✖ {error}", file=sys.stderr)
        return 7

    remote_path = cfg.workspace_target(workspace)
    if target is None and cfg.workspace_files(workspace):
        wanted = " or ".join(cfg.WORKSPACE_PREFERENCE)
        print(f"⚠ several *{cfg.WORKSPACE_SUFFIX} files and no {wanted} — opening the folder", file=sys.stderr)
    if target is not None:
        remote_path = f"{remote_path}/{target.name}"

    command = [editor, "--remote", f"ssh-remote+{alias}", remote_path]
    return subprocess.run(command).returncode


def cmd_down(args: argparse.Namespace) -> int:
    blocked = require_binaries(("docker",))
    if blocked:
        return blocked

    workspace = args.workspace.resolve()
    cid = container_id(workspace)
    if cid is None:
        print(f"→ no agentbox container for {workspace}")
        return 0
    subprocess.run(["docker", "rm", "-f", cid], check=True)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve()
    for seeded in cfg.seed_workspace_files(workspace):
        print(f"→ wrote {seeded}")

    target = workspace / cfg.PROJECT_CONFIG
    script = workspace / cfg.PROJECT_POST_CREATE
    existing = [path for path in (target, script) if path.exists()]
    if existing and not args.force:
        for path in existing:
            print(f"✖ {path} exists, use --force to overwrite", file=sys.stderr)
        return 1
    config = cfg.apply_alias(
        json.loads(cfg.BASE_CONFIG.read_text(encoding="utf-8")),
        claim_alias(workspace, state_dir()),
    )
    config["postCreateCommand"] = cfg.PROJECT_POST_CREATE_COMMAND
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"→ wrote {target}")

    shutil.copyfile(cfg.POST_CREATE_SCRIPT, script)
    print(f"→ wrote {script}")

    gitignore = workspace / ".gitignore"
    if gitignore.exists():
        missing = missing_gitignore_entries(gitignore.read_text(encoding="utf-8"))
        if missing:
            with gitignore.open("a", encoding="utf-8") as handle:
                handle.write("".join(f"{entry}\n" for entry in missing))
            print(f"→ added {', '.join(missing)} to .gitignore")
    return 0


def missing_gitignore_entries(existing: str) -> list[str]:
    lines = {line.strip() for line in existing.splitlines()}
    return [entry for entry in GITIGNORE_ENTRIES if entry not in lines]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentbox",
        description="Run coding agents in a per-project dev container sandbox.",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="project directory (default: current directory)",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    up = subparsers.add_parser("up", help="build and start the sandbox")
    up.add_argument("--rebuild", action="store_true", help="recreate an existing container")
    up.add_argument(
        "--trust-config",
        action="store_true",
        help="allow this project's container settings after reading them",
    )
    up.set_defaults(func=cmd_up)

    run = subparsers.add_parser("run", help="run a command inside the sandbox")
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(func=cmd_run)

    subparsers.add_parser("shell", help="open a shell inside the sandbox").set_defaults(
        func=cmd_shell
    )
    subparsers.add_parser(
        "update", help="reinstall the agents and skills in the running sandbox"
    ).set_defaults(func=cmd_update)
    code = subparsers.add_parser("code", help="open the editor on the sandbox")
    code.add_argument(
        "name",
        nargs="?",
        help=f"workspace file to open, with or without {cfg.WORKSPACE_SUFFIX}",
    )
    code.set_defaults(func=cmd_code)
    subparsers.add_parser("down", help="remove the sandbox container").set_defaults(
        func=cmd_down
    )

    init = subparsers.add_parser("init", help="write a project devcontainer.json")
    init.add_argument("--force", action="store_true", help="overwrite an existing config")
    init.set_defaults(func=cmd_init)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.subcommand == "run" and not args.command:
        print("✖ agentbox run needs a command", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
