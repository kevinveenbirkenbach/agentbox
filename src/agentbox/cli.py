from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import config as cfg

DEVCONTAINER_CLI = ["npx", "--yes", "@devcontainers/cli"]
CONTAINER_USER = "dev"
CONTAINER_SSH_PORT = 2222
EDITORS = ("code-oss", "codium", "code")
GITIGNORE_ENTRY = cfg.MERGED_IN_PROJECT
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


def write_ssh_config(state: Path, alias: str, port: int, key_file: Path) -> bool:
    target = ssh_dir(state) / f"{alias}.conf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        cfg.ssh_config_block(alias, port, key_file, CONTAINER_USER),
        encoding="utf-8",
    )
    user_config = Path.home() / ".ssh" / "config"
    try:
        return include_line(state) in user_config.read_text(encoding="utf-8")
    except OSError:
        return False


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
    key_file = ensure_key(state, alias)

    override = cfg.resolve_config(workspace, state / "run", cfg.SHARE_DIR, alias)
    subprocess.run(up_command(workspace, override, args.rebuild), check=True)

    cid = container_id(workspace)
    if cid is None:
        print("✖ container not found after up", file=sys.stderr)
        return 1

    inject_key(workspace, override, key_file)
    port = ssh_port(cid)
    included = write_ssh_config(state, alias, port, key_file)

    print(f"\nagentbox '{alias}' is up on 127.0.0.1:{port}\n")
    print("  agentbox run claude       agent inside the container")
    print("  agentbox shell            shell inside the container")
    print("  agentbox code             open the editor on the container\n")
    if not included:
        print(f"Add this line once to ~/.ssh/config, then 'ssh {alias}' works:")
        print(f"  {include_line(state)}\n")
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


def cmd_code(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve()
    alias = alias_for(workspace, state_dir())
    editor = next((shutil.which(name) for name in EDITORS if shutil.which(name)), None)
    if editor is None:
        print(f"✖ no editor found, looked for: {', '.join(EDITORS)}", file=sys.stderr)
        return 1
    command = [editor, "--remote", f"ssh-remote+{alias}", cfg.workspace_target(workspace)]
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
    target = workspace / cfg.PROJECT_CONFIG
    if target.exists() and not args.force:
        print(f"✖ {target} exists, use --force to overwrite", file=sys.stderr)
        return 1
    config = cfg.apply_alias(
        json.loads(cfg.BASE_CONFIG.read_text(encoding="utf-8")),
        claim_alias(workspace, state_dir()),
    )
    config["postCreateCommand"] = "npm install -g $AGENTBOX_AGENTS"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"→ wrote {target}")

    gitignore = workspace / ".gitignore"
    if gitignore.exists() and GITIGNORE_ENTRY not in gitignore.read_text(encoding="utf-8"):
        with gitignore.open("a", encoding="utf-8") as handle:
            handle.write(f"{GITIGNORE_ENTRY}\n")
        print(f"→ added {GITIGNORE_ENTRY} to .gitignore")
    return 0


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
    up.set_defaults(func=cmd_up)

    run = subparsers.add_parser("run", help="run a command inside the sandbox")
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(func=cmd_run)

    subparsers.add_parser("shell", help="open a shell inside the sandbox").set_defaults(
        func=cmd_shell
    )
    subparsers.add_parser("code", help="open the editor on the sandbox").set_defaults(
        func=cmd_code
    )
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
