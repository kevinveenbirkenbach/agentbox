from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

SHARE_DIR = Path(__file__).resolve().parent / "share"
BASE_CONFIG = SHARE_DIR / "devcontainer.base.json"
POST_CREATE = "bash /agentbox/post-create.sh"
POST_CREATE_SCRIPT = SHARE_DIR / "post-create.sh"
SHARE_MOUNT_TARGET = "/agentbox"
PROJECT_CONFIG = ".devcontainer/devcontainer.json"
PROJECT_POST_CREATE = ".devcontainer/post-create.sh"
PROJECT_POST_CREATE_COMMAND = "bash .devcontainer/post-create.sh"
LOCAL_OVERRIDE = ".devcontainer/agentbox.local.json"
MERGED_IN_PROJECT = ".devcontainer/.agentbox.json"
BASENAME_PLACEHOLDER = "${localWorkspaceFolderBasename}"
DIGEST_LENGTH = 6


def alias_registry(registry_dir: Path | None) -> dict[str, Path]:
    if registry_dir is None or not registry_dir.is_dir():
        return {}
    return {
        entry.name: Path(entry.read_text(encoding="utf-8").strip())
        for entry in sorted(registry_dir.iterdir())
        if entry.is_file()
    }


def claim_alias(registry_dir: Path, alias: str, workspace: Path) -> None:
    registry_dir.mkdir(parents=True, exist_ok=True)
    (registry_dir / alias).write_text(f"{workspace.resolve()}\n", encoding="utf-8")


def alias_for(workspace: Path, registry_dir: Path | None = None) -> str:
    workspace = workspace.resolve()
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", workspace.name).strip("-")
    if not slug:
        raise ValueError(f"cannot derive an alias from {workspace}")

    registry = alias_registry(registry_dir)
    for alias, owner in registry.items():
        if owner == workspace:
            return alias

    if slug not in registry:
        return slug

    digest = hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()[:DIGEST_LENGTH]
    return f"{slug}-{digest}"


def apply_alias(config: dict, alias: str) -> dict:
    config["name"] = alias
    mounts = config.get("mounts")
    if mounts:
        config["mounts"] = [
            mount.replace(BASENAME_PLACEHOLDER, alias) for mount in mounts
        ]
    return config


def workspace_target(workspace: Path) -> str:
    return f"/workspaces/{workspace.resolve().name}"


PROVISIONING_ENV = ("AGENTBOX_AGENTS", "AGENTBOX_BUN_AGENTS", "AGENTBOX_SKILLS")


def provisioning_env(config: dict) -> dict[str, str]:
    declared = config.get("containerEnv", {})
    return {name: declared[name] for name in PROVISIONING_ENV if name in declared}


def post_create_command(workspace: Path) -> str | None:
    workspace = workspace.resolve()
    if not (workspace / PROJECT_CONFIG).exists():
        return POST_CREATE
    if (workspace / PROJECT_POST_CREATE).exists():
        return PROJECT_POST_CREATE_COMMAND
    return None


def deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def apply_override(config: dict, override: dict) -> dict:
    own = list(config.get("mounts", []))
    merged = deep_merge(config, override)
    mounts = own + [mount for mount in merged.get("mounts", []) if mount not in own]
    if mounts:
        merged["mounts"] = mounts
    return merged


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def base_config(share_dir: Path, alias: str) -> dict:
    config = apply_alias(_read_json(BASE_CONFIG), alias)
    mounts = list(config.get("mounts", []))
    mounts.append(
        f"source={share_dir},target={SHARE_MOUNT_TARGET},type=bind,readonly"
    )
    config["mounts"] = mounts
    config["postCreateCommand"] = POST_CREATE
    return config


def resolve_config(workspace: Path, state_dir: Path, share_dir: Path, alias: str) -> Path | None:
    workspace = workspace.resolve()
    project = workspace / PROJECT_CONFIG
    override = workspace / LOCAL_OVERRIDE

    if project.exists():
        if not override.exists():
            return None
        merged = apply_override(_read_json(project), _read_json(override))
        target = workspace / MERGED_IN_PROJECT
        target.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
        return target

    config = base_config(share_dir, alias)
    if override.exists():
        config = apply_override(config, _read_json(override))
    target = state_dir / alias / "devcontainer.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return target


def ssh_config_block(alias: str, port: int, key_file: Path, user: str) -> str:
    return "\n".join(
        [
            f"Host {alias}",
            "  HostName 127.0.0.1",
            f"  Port {port}",
            f"  User {user}",
            f"  IdentityFile {key_file}",
            "  IdentitiesOnly yes",
            "  StrictHostKeyChecking no",
            "  UserKnownHostsFile /dev/null",
            "",
        ]
    )


def declared_extensions(config: dict) -> list[str]:
    vscode = config.get("customizations", {}).get("vscode", {})
    return list(vscode.get("extensions", []))


AGENT_EXTENSIONS = {
    "@anthropic-ai/claude-code": "Anthropic.claude-code",
    "@openai/codex": "openai.chatgpt",
    "@google/gemini-cli": "Google.gemini-cli-vscode-ide-companion",
}


def agent_extensions(config: dict) -> list[str]:
    requested = config.get("containerEnv", {}).get("AGENTBOX_AGENTS", "").split()
    return [AGENT_EXTENSIONS[name] for name in requested if name in AGENT_EXTENSIONS]


SAFE_KEYS = frozenset(
    {
        "name",
        "image",
        "features",
        "overrideFeatureInstallOrder",
        "forwardPorts",
        "portsAttributes",
        "otherPortsAttributes",
        "containerEnv",
        "remoteEnv",
        "customizations",
        "settings",
        "postCreateCommand",
        "postStartCommand",
        "postAttachCommand",
        "updateContentCommand",
        "waitFor",
        "shutdownAction",
        "workspaceFolder",
        "userEnvProbe",
        "hostRequirements",
    }
)
PRIVILEGE_FEATURES = ("docker-in-docker", "docker-outside-of-docker")
HARDENED_CONFIG = ".agentbox.json"
SYSBOX_RUNTIME = "sysbox-runc"


def project_layers(workspace: Path) -> dict:
    workspace = workspace.resolve()
    merged: dict = {}
    for relative in (PROJECT_CONFIG, LOCAL_OVERRIDE):
        candidate = workspace / relative
        if candidate.exists():
            merged = deep_merge(merged, _read_json(candidate))
    return merged


def risky_settings(config: dict) -> list[str]:
    found = [key for key in sorted(config) if key not in SAFE_KEYS]
    found += [f"features:{name}" for name in sorted(privilege_features(config))]
    return found


def privilege_features(config: dict) -> list[str]:
    return [
        name
        for name in config.get("features", {})
        if any(marker in name for marker in PRIVILEGE_FEATURES)
    ]


def harden(config: dict) -> dict:
    hardened = dict(config)
    run_args = list(hardened.get("runArgs", []))
    if f"--runtime={SYSBOX_RUNTIME}" not in run_args:
        run_args.append(f"--runtime={SYSBOX_RUNTIME}")
    hardened["runArgs"] = run_args
    return hardened


def write_hardened(source: Path, config: dict) -> Path:
    target = source.parent / HARDENED_CONFIG
    target.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return target


def config_digest(config: dict) -> str:
    payload = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


BOX_SETTINGS = {
    "permissions": {
        "defaultMode": "acceptEdits",
        "allow": ["Bash(*)", "Edit", "Read", "Write", "WebFetch", "WebSearch"],
        "ask": ["Bash(git commit*)", "Bash(git push*)"],
    }
}


def box_settings_json() -> str:
    return json.dumps(BOX_SETTINGS, indent=2) + "\n"


WORKSPACE_SUFFIX = ".code-workspace"
DEFAULT_WORKSPACE = f"default{WORKSPACE_SUFFIX}"
LOCAL_WORKSPACE = f"local{WORKSPACE_SUFFIX}"
WORKSPACE_PREFERENCE = (LOCAL_WORKSPACE, DEFAULT_WORKSPACE)
WORKSPACE_TEMPLATE = {"folders": [{"path": "."}], "settings": {}}


def workspace_files(workspace: Path) -> list[Path]:
    return sorted(workspace.glob(f"*{WORKSPACE_SUFFIX}"))


def resolve_workspace_file(workspace: Path, name: str | None = None) -> Path | None:
    found = workspace_files(workspace)
    if name is not None:
        if not name.endswith(WORKSPACE_SUFFIX):
            name += WORKSPACE_SUFFIX
        target = workspace / name
        if target.is_file():
            return target
        listing = ", ".join(path.name for path in found) or "none"
        raise LookupError(f"no {name} in {workspace} — found: {listing}")
    for preferred in WORKSPACE_PREFERENCE:
        target = workspace / preferred
        if target.is_file():
            return target
    if len(found) == 1:
        return found[0]
    return None


def seed_workspace_files(workspace: Path) -> list[Path]:
    written = []
    for name in WORKSPACE_PREFERENCE:
        target = workspace / name
        if target.exists():
            continue
        target.write_text(json.dumps(WORKSPACE_TEMPLATE, indent=2) + "\n", encoding="utf-8")
        written.append(target)
    return written
