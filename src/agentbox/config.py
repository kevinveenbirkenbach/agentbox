from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

SHARE_DIR = Path(__file__).resolve().parent / "share"
BASE_CONFIG = SHARE_DIR / "devcontainer.base.json"
POST_CREATE = "bash /agentbox/post-create.sh"
SHARE_MOUNT_TARGET = "/agentbox"
PROJECT_CONFIG = ".devcontainer/devcontainer.json"
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


def deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
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
        merged = deep_merge(_read_json(project), _read_json(override))
        target = workspace / MERGED_IN_PROJECT
        target.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
        return target

    config = base_config(share_dir, alias)
    if override.exists():
        config = deep_merge(config, _read_json(override))
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
