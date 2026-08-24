from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentbox import cli
from agentbox import config as cfg


class TestDeepMerge(unittest.TestCase):
    def test_scalar_override_wins(self) -> None:
        self.assertEqual(cfg.deep_merge({"a": 1}, {"a": 2}), {"a": 2})

    def test_nested_dicts_merge(self) -> None:
        base = {"features": {"node": {}, "sshd": {}}}
        override = {"features": {"node": {"version": "22"}}}
        self.assertEqual(
            cfg.deep_merge(base, override),
            {"features": {"node": {"version": "22"}, "sshd": {}}},
        )

    def test_lists_are_replaced_not_appended(self) -> None:
        self.assertEqual(cfg.deep_merge({"mounts": ["a"]}, {"mounts": ["b"]}), {"mounts": ["b"]})

    def test_base_is_not_mutated(self) -> None:
        base = {"containerEnv": {"A": "1"}}
        cfg.deep_merge(base, {"containerEnv": {"B": "2"}})
        self.assertEqual(base, {"containerEnv": {"A": "1"}})


class TestAlias(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.registry = self.root / "aliases"
        self.registry.mkdir()

    def _register(self, alias: str, workspace: Path) -> None:
        cfg.claim_alias(self.registry, alias, workspace)

    def _workspace(self, *parts: str) -> Path:
        workspace = self.root.joinpath(*parts)
        workspace.mkdir(parents=True)
        return workspace

    def test_plain_name(self) -> None:
        self.assertEqual(cfg.alias_for(Path("/tmp/my-repo")), "my-repo")

    def test_unsafe_characters_are_replaced(self) -> None:
        workspace = self._workspace("weird name!")
        self.assertEqual(cfg.alias_for(workspace), "weird-name")

    def test_workspace_target(self) -> None:
        self.assertEqual(cfg.workspace_target(Path("/tmp/my-repo")), "/workspaces/my-repo")

    def test_free_basename_is_used(self) -> None:
        workspace = self._workspace("work", "web")
        self.assertEqual(cfg.alias_for(workspace, self.registry), "web")

    def test_same_workspace_keeps_its_alias(self) -> None:
        workspace = self._workspace("work", "web")
        self._register("web", workspace)
        self.assertEqual(cfg.alias_for(workspace, self.registry), "web")

    def test_second_workspace_with_same_basename_gets_a_digest(self) -> None:
        first = self._workspace("work", "web")
        second = self._workspace("client", "web")
        self._register("web", first)
        alias = cfg.alias_for(second, self.registry)
        self.assertNotEqual(alias, "web")
        self.assertTrue(alias.startswith("web-"))
        self.assertEqual(len(alias), len("web-") + cfg.DIGEST_LENGTH)

    def test_digest_alias_is_stable_after_the_first_one_disappears(self) -> None:
        first = self._workspace("work", "web")
        second = self._workspace("client", "web")
        self._register("web", first)
        alias = cfg.alias_for(second, self.registry)
        self._register(alias, second)
        (self.registry / "web").unlink()
        self.assertEqual(cfg.alias_for(second, self.registry), alias)


class TestApplyAlias(unittest.TestCase):
    def test_home_volume_carries_the_alias(self) -> None:
        config = cfg.apply_alias(
            {"mounts": [f"source=agentbox-home-{cfg.BASENAME_PLACEHOLDER},target=/home/dev,type=volume"]},
            "web-abc123",
        )
        self.assertEqual(config["name"], "web-abc123")
        self.assertIn("agentbox-home-web-abc123", config["mounts"][0])

    def test_base_config_mounts_the_share_directory(self) -> None:
        config = cfg.base_config(Path("/opt/share"), "demo")
        self.assertEqual(config["postCreateCommand"], cfg.POST_CREATE)
        self.assertTrue(any("/opt/share" in mount for mount in config["mounts"]))


class TestResolveConfig(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "project"
        (self.workspace / ".devcontainer").mkdir(parents=True)
        self.state = self.root / "state"
        self.share = self.root / "share"
        self.share.mkdir()

    def _write(self, relative: str, payload: dict) -> None:
        (self.workspace / relative).write_text(json.dumps(payload), encoding="utf-8")

    def test_base_config_is_materialised_when_project_has_none(self) -> None:
        result = cfg.resolve_config(self.workspace, self.state, self.share, "project")
        self.assertIsNotNone(result)
        config = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(config["postCreateCommand"], cfg.POST_CREATE)
        self.assertTrue(
            any(f"target={cfg.SHARE_MOUNT_TARGET}" in mount for mount in config["mounts"])
        )
        self.assertEqual(config["name"], "project")

    def test_project_config_without_override_is_used_as_is(self) -> None:
        self._write(cfg.PROJECT_CONFIG, {"image": "debian:bookworm"})
        self.assertIsNone(
            cfg.resolve_config(self.workspace, self.state, self.share, "project")
        )

    def test_local_override_merges_over_project_config(self) -> None:
        self._write(cfg.PROJECT_CONFIG, {"image": "debian:bookworm", "features": {"node": {}}})
        self._write(cfg.LOCAL_OVERRIDE, {"features": {"node": {"version": "22"}}})
        result = cfg.resolve_config(self.workspace, self.state, self.share, "project")
        self.assertEqual(result, self.workspace / cfg.MERGED_IN_PROJECT)
        config = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(config["image"], "debian:bookworm")
        self.assertEqual(config["features"], {"node": {"version": "22"}})

    def test_local_override_merges_over_base_config(self) -> None:
        self._write(cfg.LOCAL_OVERRIDE, {"containerEnv": {"AGENTBOX_AGENTS": "@openai/codex"}})
        result = cfg.resolve_config(self.workspace, self.state, self.share, "project")
        config = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(config["containerEnv"]["AGENTBOX_AGENTS"], "@openai/codex")
        self.assertEqual(config["remoteUser"], "dev")


class TestSshConfigBlock(unittest.TestCase):
    def test_block_carries_alias_port_and_key(self) -> None:
        block = cfg.ssh_config_block("demo", 49153, Path("/keys/demo/id_ed25519"), "dev")
        self.assertIn("Host demo", block)
        self.assertIn("Port 49153", block)
        self.assertIn("IdentityFile /keys/demo/id_ed25519", block)
        self.assertIn("User dev", block)


class TestUpCommand(unittest.TestCase):
    def test_project_owned_config_keeps_the_lockfile(self) -> None:
        workspace = Path("/tmp/project")
        override = workspace / cfg.MERGED_IN_PROJECT
        self.assertNotIn("--no-lockfile", cli.up_command(workspace, override, False))

    def test_external_config_disables_the_lockfile(self) -> None:
        command = cli.up_command(Path("/tmp/project"), Path("/state/run/devcontainer.json"), False)
        self.assertIn("--no-lockfile", command)

    def test_rebuild_recreates_the_container(self) -> None:
        self.assertIn(
            "--remove-existing-container", cli.up_command(Path("/tmp/project"), None, True)
        )


class TestExecCommand(unittest.TestCase):
    def test_external_config_is_passed_to_exec(self) -> None:
        command = cli.exec_command(
            Path("/tmp/project"), Path("/state/run/devcontainer.json"), ["claude"]
        )
        self.assertIn("--override-config", command)
        self.assertEqual(command[-1], "claude")

    def test_project_owned_config_needs_no_override(self) -> None:
        command = cli.exec_command(Path("/tmp/project"), None, ["zsh"])
        self.assertNotIn("--override-config", command)


class TestBinaryPrecheck(unittest.TestCase):
    def test_all_present(self) -> None:
        with mock.patch.object(cli.shutil, "which", return_value="/usr/bin/thing"):
            self.assertEqual(cli.missing_binaries(("docker", "npx")), [])

    def test_missing_are_reported(self) -> None:
        with mock.patch.object(cli.shutil, "which", side_effect=[None, "/usr/bin/npx"]):
            self.assertEqual(cli.missing_binaries(("docker", "npx")), ["docker"])

    def test_require_exits_nonzero_when_missing(self) -> None:
        with mock.patch.object(cli.shutil, "which", return_value=None):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cli.require_binaries(("docker",)), 3)
        self.assertIn("docker", err.getvalue())


class TestParser(unittest.TestCase):
    def test_run_keeps_agent_flags(self) -> None:
        args = cli.build_parser().parse_args(["run", "claude", "--dangerously-skip-permissions"])
        self.assertEqual(args.subcommand, "run")
        self.assertEqual(args.command, ["claude", "--dangerously-skip-permissions"])

    def test_up_defaults_to_current_directory(self) -> None:
        args = cli.build_parser().parse_args(["up"])
        self.assertEqual(args.workspace, Path.cwd())
        self.assertFalse(args.rebuild)


if __name__ == "__main__":
    unittest.main()
