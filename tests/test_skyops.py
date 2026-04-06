"""Tests for the skyops CLI — Phases B & C."""

from __future__ import annotations

import json
import subprocess
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from skyops.cli import app
from skyops._client import build_profile
from skyops._db import db_seed
from skyops._runtime_deploy import _build_docker_run, _build_script, _build_systemd
from skyops.config import (
    SkyopsConfig,
    _deep_merge,
    load_config,
)


runner = CliRunner()


class TestDeepMerge(unittest.TestCase):
    def test_simple_override(self):
        base = {"a": 1, "b": 2}
        over = {"b": 99}
        self.assertEqual(_deep_merge(base, over), {"a": 1, "b": 99})

    def test_nested_override(self):
        base = {"s3": {"bucket": "old", "key": "k1"}}
        over = {"s3": {"bucket": "new"}}
        self.assertEqual(
            _deep_merge(base, over),
            {"s3": {"bucket": "new", "key": "k1"}},
        )

    def test_add_new_key(self):
        result = _deep_merge({"a": 1}, {"b": 2})
        self.assertEqual(result, {"a": 1, "b": 2})


class TestSkyopsConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = SkyopsConfig()
        self.assertEqual(cfg.stack.mode, "docker")
        self.assertEqual(cfg.server.port, 7860)
        self.assertEqual(cfg.s3.bucket, "agp-artifacts")

    def test_from_dict(self):
        data = {
            "stack": {"mode": "bare-metal"},
            "server": {"port": 9999},
            "agents": {"agt_local": {"capabilities": ["python"]}},
        }
        cfg = SkyopsConfig.from_dict(data)
        self.assertEqual(cfg.stack.mode, "bare-metal")
        self.assertEqual(cfg.server.port, 9999)
        self.assertIn("agt_local", cfg.agents)

    def test_from_dict_ignores_unknown_keys(self):
        data = {"server": {"port": 8080, "bogus_key": True}}
        cfg = SkyopsConfig.from_dict(data)
        self.assertEqual(cfg.server.port, 8080)

    def test_display_dict_masks_secrets(self):
        cfg = SkyopsConfig()
        cfg.security.operator_token = "super-secret-token"
        cfg.s3.secret_access_key = "secret123"
        display = cfg.to_display_dict(mask_secrets=True)
        self.assertNotEqual(display["security"]["operator_token"], "super-secret-token")
        self.assertIn("***", display["security"]["operator_token"])
        self.assertNotEqual(display["s3"]["secret_access_key"], "secret123")

    def test_display_dict_unmask(self):
        cfg = SkyopsConfig()
        cfg.security.operator_token = "super-secret-token"
        display = cfg.to_display_dict(mask_secrets=False)
        self.assertEqual(display["security"]["operator_token"], "super-secret-token")

    def test_resolve_agent_workspace_from_profile(self):
        cfg = SkyopsConfig.from_dict({
            "host_profiles": {
                "server": {
                    "mount_sources": {
                        "repo_worktree_a": "/srv/worktrees/feature-a",
                        "shared_docs": "/srv/shared-docs",
                    }
                }
            },
            "workspace_profiles": {
                "feature_a": {
                    "workspace_ref": "/workspace/wt-feature-a",
                    "mounts": [
                        "@repo_worktree_a:/workspace/wt-feature-a",
                        "@shared_docs:/workspace/shared-docs",
                    ],
                }
            },
            "agents": {
                "agt_feature_a": {
                    "capabilities": ["python"],
                    "workspace_profile": "feature_a",
                }
            },
        })
        resolved = cfg.resolve_agent_workspace("agt_feature_a", host_profile="server")
        self.assertEqual(resolved["workspace_ref"], "/workspace/wt-feature-a")
        self.assertEqual(
            resolved["mounts"],
            [
                "/srv/worktrees/feature-a:/workspace/wt-feature-a",
                "/srv/shared-docs:/workspace/shared-docs",
            ],
        )

    def test_resolve_agent_workspace_allows_agent_mount_overrides(self):
        cfg = SkyopsConfig.from_dict({
            "host_profiles": {
                "server": {
                    "mount_sources": {
                        "repo": "/srv/repo",
                        "shared_docs": "/srv/shared-docs",
                    }
                }
            },
            "workspace_profiles": {
                "main": {
                    "workspace_ref": "/workspace/main",
                    "mounts": ["@repo:/workspace/main"],
                }
            },
            "agents": {
                "agt_orc": {
                    "capabilities": ["python"],
                    "workspace_profile": "main",
                    "mounts": ["@shared_docs:/workspace/shared-docs"],
                }
            },
        })
        resolved = cfg.resolve_agent_workspace("agt_orc", host_profile="server")
        self.assertEqual(resolved["workspace_ref"], "/workspace/main")
        self.assertEqual(
            resolved["mounts"],
            [
                "/srv/repo:/workspace/main",
                "/srv/shared-docs:/workspace/shared-docs",
            ],
        )

    def test_resolve_agent_workspace_ref_does_not_require_host_profile(self):
        cfg = SkyopsConfig.from_dict({
            "workspace_profiles": {
                "main": {
                    "workspace_ref": "/workspace/main",
                    "mounts": ["@repo:/workspace/main"],
                }
            },
            "agents": {
                "agt_local": {
                    "capabilities": ["python"],
                    "workspace_profile": "main",
                }
            },
        })
        self.assertEqual(cfg.resolve_agent_workspace_ref("agt_local"), "/workspace/main")

    def test_resolve_agent_workspace_unknown_agent_raises(self):
        cfg = SkyopsConfig.from_dict({
            "workspace_profiles": {
                "main": {
                    "workspace_ref": "/workspace/main",
                }
            },
            "agents": {
                "agt_local": {
                    "capabilities": ["python"],
                    "workspace_profile": "main",
                }
            },
        })
        with self.assertRaises(KeyError):
            cfg.resolve_agent_workspace("agt_missing")

    def test_resolve_agent_workspace_uses_default_host_profile(self):
        cfg = SkyopsConfig.from_dict({
            "host_profiles": {
                "default": {
                    "mount_sources": {
                        "repo": "/srv/repo",
                    }
                }
            },
            "workspace_profiles": {
                "main": {
                    "workspace_ref": "/workspace/main",
                    "mounts": ["@repo:/workspace/main"],
                }
            },
            "agents": {
                "agt_local": {
                    "capabilities": ["python"],
                    "workspace_profile": "main",
                }
            },
        })
        resolved = cfg.resolve_agent_workspace("agt_local")
        self.assertEqual(resolved["host_profile"], "default")
        self.assertEqual(resolved["mounts"], ["/srv/repo:/workspace/main"])

    def test_resolve_agent_workspace_uses_only_host_profile_when_unique(self):
        cfg = SkyopsConfig.from_dict({
            "host_profiles": {
                "server-a": {
                    "mount_sources": {
                        "repo": "/srv/repo",
                    }
                }
            },
            "workspace_profiles": {
                "main": {
                    "workspace_ref": "/workspace/main",
                    "mounts": ["@repo:/workspace/main"],
                }
            },
            "agents": {
                "agt_local": {
                    "capabilities": ["python"],
                    "workspace_profile": "main",
                }
            },
        })
        resolved = cfg.resolve_agent_workspace("agt_local")
        self.assertEqual(resolved["host_profile"], "server-a")
        self.assertEqual(resolved["mounts"], ["/srv/repo:/workspace/main"])

    def test_mount_target_ignores_mount_options(self):
        cfg = SkyopsConfig()
        self.assertEqual(cfg._mount_target("/host/path:/workspace/main:ro"), "/workspace/main")

    def test_resolve_agent_workspace_git_override_uses_host_git_root(self):
        cfg = SkyopsConfig.from_dict({
            "host_profiles": {
                "server": {
                    "git_root": "/srv/agp/git",
                    "mount_sources": {
                        "shared_docs": "/srv/shared-docs",
                    },
                }
            },
            "workspace_profiles": {
                "main": {
                    "mode": "shared_fs",
                    "workspace_ref": "/workspace/main",
                    "mounts": ["@shared_docs:/workspace/shared-docs"],
                    "repo_url": "git@github.com:example/skynet.git",
                    "repo_ref": "master",
                }
            },
            "agents": {
                "agt_git": {
                    "capabilities": ["python"],
                    "workspace_profile": "main",
                    "workspace_mode": "git",
                    "repo_ref": "feature-a",
                }
            },
        })
        resolved = cfg.resolve_agent_workspace("agt_git", host_profile="server")
        self.assertEqual(resolved["workspace_mode"], "git")
        self.assertEqual(resolved["workspace_ref"], "/workspace/main")
        self.assertEqual(
            resolved["mounts"],
            [
                "/srv/agp/git/agt_git:/workspace/main",
                "/srv/shared-docs:/workspace/shared-docs",
            ],
        )
        self.assertTrue(any('git clone "git@github.com:example/skynet.git"' in cmd for cmd in resolved["prepare_commands"]))
        self.assertTrue(any('checkout "feature-a"' in cmd for cmd in resolved["prepare_commands"]))

    def test_resolve_agent_workspace_worktree_override_uses_host_roots(self):
        cfg = SkyopsConfig.from_dict({
            "host_profiles": {
                "server": {
                    "git_root": "/srv/agp/git",
                    "worktree_root": "/srv/agp/worktrees",
                    "mount_sources": {
                        "shared_docs": "/srv/shared-docs",
                    },
                }
            },
            "workspace_profiles": {
                "main": {
                    "workspace_ref": "/workspace/main",
                    "mounts": ["@shared_docs:/workspace/shared-docs"],
                    "repo_url": "git@github.com:example/skynet.git",
                    "repo_ref": "master",
                    "repo_name": "skynet",
                }
            },
            "agents": {
                "agt_feature_a": {
                    "capabilities": ["python"],
                    "workspace_profile": "main",
                    "workspace_mode": "worktree",
                    "worktree_name": "feature-a",
                    "repo_ref": "feature-a",
                }
            },
        })
        resolved = cfg.resolve_agent_workspace("agt_feature_a", host_profile="server")
        self.assertEqual(resolved["workspace_mode"], "worktree")
        self.assertEqual(
            resolved["mounts"],
            [
                "/srv/agp/worktrees/feature-a:/workspace/main",
                "/srv/agp/git:/srv/agp/git",
                "/srv/agp/worktrees:/srv/agp/worktrees",
                "/srv/shared-docs:/workspace/shared-docs",
            ],
        )
        self.assertTrue(any('worktree add "/srv/agp/worktrees/feature-a" "feature-a"' in cmd for cmd in resolved["prepare_commands"]))

    def test_worktree_override_does_not_inherit_profile_mount_at_workspace_target(self):
        cfg = SkyopsConfig.from_dict({
            "host_profiles": {
                "server": {
                    "git_root": "/srv/agp/git",
                    "worktree_root": "/srv/agp/worktrees",
                    "mount_sources": {
                        "repo": "/srv/repo",
                        "shared_docs": "/srv/shared-docs",
                    },
                }
            },
            "workspace_profiles": {
                "main": {
                    "workspace_ref": "/workspace/main",
                    "mounts": [
                        "@repo:/workspace/main",
                        "@shared_docs:/workspace/shared-docs",
                    ],
                    "repo_url": "git@github.com:example/skynet.git",
                    "repo_name": "skynet",
                }
            },
            "agents": {
                "agt_feature_a": {
                    "capabilities": ["python"],
                    "workspace_profile": "main",
                    "workspace_mode": "worktree",
                    "worktree_name": "feature-a",
                    "repo_ref": "feature-a",
                }
            },
        })
        resolved = cfg.resolve_agent_workspace("agt_feature_a", host_profile="server")
        self.assertEqual(
            resolved["mounts"],
            [
                "/srv/agp/worktrees/feature-a:/workspace/main",
                "/srv/agp/git:/srv/agp/git",
                "/srv/agp/worktrees:/srv/agp/worktrees",
                "/srv/shared-docs:/workspace/shared-docs",
            ],
        )


class TestLoadConfig(unittest.TestCase):
    def test_load_from_file(self, tmp_path=None):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            toml_path = td / "skyops.toml"
            toml_path.write_text(textwrap.dedent("""\
                [stack]
                mode = "bare-metal"

                [server]
                port = 8888
            """))

            cfg = load_config(toml_path)
            self.assertEqual(cfg.stack.mode, "bare-metal")
            self.assertEqual(cfg.server.port, 8888)
            # Defaults for sections not in file
            self.assertEqual(cfg.s3.bucket, "agp-artifacts")

    def test_load_merges_local(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "skyops.toml").write_text(textwrap.dedent("""\
                [server]
                port = 7860
                [s3]
                bucket = "base-bucket"
            """))
            (td / "skyops.local.toml").write_text(textwrap.dedent("""\
                [s3]
                bucket = "local-bucket"
                secret_access_key = "localsecret"
            """))

            cfg = load_config(td / "skyops.toml")
            self.assertEqual(cfg.s3.bucket, "local-bucket")
            self.assertEqual(cfg.s3.secret_access_key, "localsecret")
            # Base value preserved
            self.assertEqual(cfg.server.port, 7860)

    def test_missing_config_raises(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(FileNotFoundError):
                load_config(Path(td) / "does-not-exist.toml")

    def test_load_workspace_profiles(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            toml_path = Path(td) / "skyops.toml"
            toml_path.write_text(textwrap.dedent("""\
                [host_profiles.default.mount_sources]
                repo = "/host/repo"

                [workspace_profiles.main]
                workspace_ref = "/workspace/main"
                mounts = ["@repo:/workspace/main"]

                [agents.agt_local]
                capability_id = "cap_python"
                workspace_profile = "main"
            """))
            cfg = load_config(toml_path)
            resolved = cfg.resolve_agent_workspace("agt_local", host_profile="default")
            self.assertEqual(resolved["workspace_ref"], "/workspace/main")
            self.assertEqual(resolved["mounts"], ["/host/repo:/workspace/main"])


class TestRuntimeDeploy(unittest.TestCase):
    def test_build_docker_run_includes_mounts_and_workspace_env(self):
        cmd = _build_docker_run(
            runtime_id="rtm_orc",
            server_url="http://control-plane:7860",
            host_kind="tmux",
            adapter_kind="codex",
            agent_id="agt_orc",
            runtime_token="",
            image="agp-runtime:latest",
            workspace_ref="/workspace/main",
            mounts=[
                "/host/repo:/workspace/main",
                "/host/shared-docs:/workspace/shared-docs",
            ],
            prepare_commands=[],
        )
        self.assertIn("-e AGP_RUNTIME_AGENT_ID=agt_orc", cmd)
        self.assertIn("-e AGP_TMUX_DEFAULT_CWD=/workspace/main", cmd)
        self.assertIn("-e AGP_WEZTERM_DEFAULT_CWD=/workspace/main", cmd)
        self.assertIn("-v /host/repo:/workspace/main", cmd)
        self.assertIn("-v /host/shared-docs:/workspace/shared-docs", cmd)
        self.assertIn("-e OPENAI_API_KEY", cmd)
        self.assertIn("-e OPENROUTER_API_KEY", cmd)
        self.assertIn("-e OPENAI_BASE_URL", cmd)
        self.assertTrue(cmd.strip().endswith("agp-runtime:latest"))

    def test_build_docker_run_quotes_shell_sensitive_values(self):
        cmd = _build_docker_run(
            runtime_id="rtm risky",
            server_url="http://host:7860/$TOKEN",
            host_kind="tmux",
            adapter_kind="codex",
            agent_id="agt risky",
            runtime_token="tok`rm -rf /`",
            image="agp-runtime:latest",
            workspace_ref="/workspace/main",
            mounts=["/host path:/workspace/main:ro"],
            prepare_commands=[],
        )
        self.assertIn("'rtm risky'", cmd)
        self.assertIn("'AGP_SERVER_URL=http://host:7860/$TOKEN'", cmd)
        self.assertIn("'AGP_RUNTIME_BEARER_TOKEN=tok`rm -rf /`'", cmd)
        self.assertIn("'/host path:/workspace/main:ro'", cmd)

    def test_build_docker_run_uses_host_docker_internal_for_localhost(self):
        cmd = _build_docker_run(
            runtime_id="rtm_local",
            server_url="http://127.0.0.1:7860",
            host_kind="tmux",
            adapter_kind="codex",
            agent_id="agt_local",
            runtime_token="",
            image="agp-runtime:latest",
            workspace_ref="/workspace/main",
            mounts=[],
            prepare_commands=[],
        )
        self.assertIn("host.docker.internal:7860", cmd)
        self.assertIn("--add-host host.docker.internal:host-gateway", cmd)

    def test_runtime_deploy_script_can_include_prepare_commands(self):
        script = _build_script(
            runtime_id="rtm_feature_a",
            server_url="http://control-plane:7860",
            host_kind="tmux",
            adapter_kind="codex",
            agent_id="agt_feature_a",
            runtime_token="",
            prepare_commands=[
                'mkdir -p "/srv/agp/git" "/srv/agp/worktrees"',
                'git -C "/srv/agp/git/skynet" fetch --all --prune',
            ],
            workspace_ref="/workspace/main",
        )
        self.assertIn("# --- Prepare workspace ---", script)
        self.assertIn('mkdir -p "/srv/agp/git" "/srv/agp/worktrees"', script)
        self.assertIn('git -C "/srv/agp/git/skynet" fetch --all --prune', script)
        self.assertIn("python3 -m pip install 'agp[server]'", script)
        self.assertIn("export AGP_ARTIFACT_BACKEND=http", script)
        self.assertIn("export AGP_TMUX_DEFAULT_CWD=/workspace/main", script)

    def test_build_systemd_includes_runtime_env(self):
        unit = _build_systemd(
            runtime_id="rtm_orc",
            server_url="http://control-plane:7860",
            host_kind="tmux",
            adapter_kind="codex",
            agent_id="agt_orc",
            runtime_token="tok",
            workspace_ref="/workspace/main",
            prepare_commands=['mkdir -p "/srv/agp/git"'],
        )
        self.assertIn('Environment="AGP_ARTIFACT_BACKEND=http"', unit)
        self.assertIn('Environment="AGP_TMUX_DEFAULT_CWD=/workspace/main"', unit)
        self.assertIn('Environment="AGP_WEZTERM_DEFAULT_CWD=/workspace/main"', unit)
        self.assertIn("PassEnvironment=OPENAI_API_KEY OPENROUTER_API_KEY OPENAI_BASE_URL ANTHROPIC_API_KEY", unit)
        self.assertIn("ExecStart=agp runtime-work-loop", unit)
        self.assertIn("ExecStartPre=/bin/sh -lc", unit)
        self.assertIn('mkdir -p "/srv/agp/git"', unit)
        self.assertNotIn("ExecStart=AGP_ARTIFACT_BACKEND=http", unit)

    def test_build_systemd_escapes_quoted_values(self):
        unit = _build_systemd(
            runtime_id="rtm_orc",
            server_url='http://control-plane:7860/"bad"',
            host_kind="tmux",
            adapter_kind="codex",
            agent_id="agt_orc",
            runtime_token='tok"bad',
            workspace_ref='/workspace/"main"',
            prepare_commands=[],
        )
        self.assertIn('Environment="AGP_RUNTIME_BEARER_TOKEN=tok\\"bad"', unit)
        self.assertIn('Environment="AGP_TMUX_DEFAULT_CWD=/workspace/\\"main\\""', unit)

    def test_build_docker_run_includes_prepare_commands(self):
        cmd = _build_docker_run(
            runtime_id="rtm_feature_a",
            server_url="http://control-plane:7860",
            host_kind="tmux",
            adapter_kind="codex",
            agent_id="agt_feature_a",
            runtime_token="",
            image="agp-runtime:latest",
            workspace_ref="/workspace/main",
            mounts=["/srv/agp/worktrees/feature-a:/workspace/main"],
            prepare_commands=['mkdir -p "/srv/agp/worktrees"', 'git -C "/srv/agp/git/skynet" fetch --all --prune'],
        )
        self.assertIn("# --- Prepare workspace ---", cmd)
        self.assertIn("set -euo pipefail", cmd)
        self.assertIn('mkdir -p "/srv/agp/worktrees"', cmd)
        self.assertIn("docker run", cmd)

    def test_runtime_deploy_unknown_agent_fails(self):
        cfg = SkyopsConfig()
        with patch("skyops._runtime_deploy.load_config", return_value=cfg):
            result = runner.invoke(
                app,
                ["runtime", "deploy", "rtm_local", "--agent-id", "agt_missing"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("agent not found", result.output)

    def test_runtime_deploy_command_includes_runtime_token(self):
        cfg = SkyopsConfig()
        cfg.security.runtime_token = "rtok"
        with patch("skyops._runtime_deploy.load_config", return_value=cfg):
            result = runner.invoke(app, ["runtime", "deploy", "rtm_local", "--format", "command"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("AGP_RUNTIME_BEARER_TOKEN=rtok", result.output)


class TestSkyopsClient(unittest.TestCase):
    def test_build_profile_preserves_existing_token_when_config_has_none(self):
        cfg = SkyopsConfig()
        cfg.server.port = 9999
        cfg.security.operator_token = ""
        with patch.dict("os.environ", {}, clear=True), \
             patch("skyops._client.AgpProfile.load", return_value=type("Profile", (), {"server_url": "http://127.0.0.1:7860", "token": "persisted"})()), \
             patch("pathlib.Path.exists", return_value=False):
            profile = build_profile(cfg)
        self.assertEqual(profile.server_url, "http://127.0.0.1:9999")
        self.assertEqual(profile.token, "persisted")

    def test_build_profile_uses_existing_profile_url_when_present(self):
        cfg = SkyopsConfig()
        cfg.server.host = "0.0.0.0"
        cfg.server.port = 9999

        fake_home = Path("/tmp/test-home")
        profile_path = fake_home / ".agp" / "profiles" / "default.toml"

        def fake_exists(path_obj: Path) -> bool:
            return str(path_obj) == str(profile_path)

        with patch.dict("os.environ", {}, clear=True), \
             patch("skyops._client.AgpProfile.load", return_value=type("Profile", (), {"server_url": "http://cp.example:7860", "token": "persisted"})()), \
             patch("skyops._client.Path.home", return_value=fake_home), \
             patch("pathlib.Path.exists", fake_exists):
            profile = build_profile(cfg)
        self.assertEqual(profile.server_url, "http://cp.example:7860")

    def test_build_profile_prefers_host_port_env_over_existing_profile_url(self):
        cfg = SkyopsConfig()

        fake_home = Path("/tmp/test-home")
        profile_path = fake_home / ".agp" / "profiles" / "default.toml"

        def fake_exists(path_obj: Path) -> bool:
            return str(path_obj) == str(profile_path)

        with patch.dict("os.environ", {"AGP_HOST": "0.0.0.0", "AGP_PORT": "9001"}, clear=True), \
             patch("skyops._client.AgpProfile.load", return_value=type("Profile", (), {"server_url": "http://cp.example:7860", "token": "persisted"})()), \
             patch("skyops._client.Path.home", return_value=fake_home), \
             patch("pathlib.Path.exists", fake_exists):
            profile = build_profile(cfg)
        self.assertEqual(profile.server_url, "http://127.0.0.1:9001")


class TestDbSeed(unittest.TestCase):
    def test_db_seed_can_clear_workspace_ref_on_existing_agent(self):
        import tempfile

        mock_client = unittest.mock.MagicMock()
        mock_client.health.return_value = {"status": "ok"}
        mock_client.list_agents.return_value = {
            "items": [{"agent_id": "agt_local", "workspace_ref": "/workspace/main"}]
        }
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = lambda s, *a: None

        with tempfile.TemporaryDirectory() as td:
            toml_path = Path(td) / "skyops.toml"
            toml_path.write_text(textwrap.dedent("""\
                [agents.agt_local]
                capability_id = "cap_python"
            """))
            cfg = load_config(toml_path)
            with patch("skyops._db.load_config", return_value=cfg), \
                 patch("skyops._client.build_client", return_value=mock_client):
                db_seed()
        mock_client.patch_agent.assert_called_once_with("agt_local", workspace_ref=None)


class TestInitCommand(unittest.TestCase):
    def test_init_creates_files(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            result = runner.invoke(app, ["init", "--dir", td, "--mode", "docker"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("Created", result.output)
            self.assertTrue((Path(td) / "skyops.toml").exists())
            self.assertTrue((Path(td) / "skyops.local.toml").exists())

            # Verify created config is valid
            cfg = load_config(Path(td) / "skyops.toml")
            self.assertEqual(cfg.stack.mode, "docker")

    def test_init_refuses_overwrite(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "skyops.toml").write_text("[stack]\nmode = 'docker'\n")
            result = runner.invoke(app, ["init", "--dir", td])
            self.assertEqual(result.exit_code, 1)
            self.assertIn("already exists", result.output)

    def test_init_force_preserves_existing_profile_token(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            profiles_dir = home / ".agp" / "profiles"
            profiles_dir.mkdir(parents=True, exist_ok=True)
            (profiles_dir / "default.toml").write_text(
                'server_url = "http://127.0.0.1:7860"\n'
                'token = "persisted-token"\n'
            )
            with patch("skyops._init_cmd.Path.home", return_value=home):
                result = runner.invoke(app, ["init", "--dir", td, "--mode", "docker", "--force"])
            self.assertEqual(result.exit_code, 0, result.output)
            content = (profiles_dir / "default.toml").read_text()
            self.assertIn("persisted-token", content)

    def test_init_force_overwrites(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "skyops.toml").write_text("[stack]\nmode = 'docker'\n")
            result = runner.invoke(app, ["init", "--dir", td, "--force", "--mode", "bare-metal"])
            self.assertEqual(result.exit_code, 0, result.output)
            cfg = load_config(Path(td) / "skyops.toml")
            self.assertEqual(cfg.stack.mode, "bare-metal")


class TestConfigShow(unittest.TestCase):
    def test_config_show(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            toml_path = Path(td) / "skyops.toml"
            toml_path.write_text(textwrap.dedent("""\
                [server]
                port = 7860
                [security]
                operator_token = "secret123"
            """))
            with patch("skyops._config_cmd.find_config", return_value=toml_path):
                with patch("skyops._config_cmd.load_config", return_value=load_config(toml_path)):
                    result = runner.invoke(app, ["config", "show"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("[server]", result.output)
            # Secret should be masked
            self.assertNotIn("secret123", result.output)
            self.assertIn("***", result.output)


class TestConfigSet(unittest.TestCase):
    def test_config_set_creates_local(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            toml_path = Path(td) / "skyops.toml"
            toml_path.write_text("[server]\nport = 7860\n")
            with patch("skyops._config_cmd.find_config", return_value=toml_path):
                result = runner.invoke(app, ["config", "set", "server.port", "9999"])
            self.assertEqual(result.exit_code, 0, result.output)
            local_path = Path(td) / "skyops.local.toml"
            self.assertTrue(local_path.exists())
            content = local_path.read_text()
            self.assertIn("9999", content)

    def test_config_set_bool(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            toml_path = Path(td) / "skyops.toml"
            toml_path.write_text("[monitoring]\nprometheus = true\n")
            with patch("skyops._config_cmd.find_config", return_value=toml_path):
                result = runner.invoke(app, ["config", "set", "monitoring.prometheus", "false"])
            self.assertEqual(result.exit_code, 0, result.output)
            content = (Path(td) / "skyops.local.toml").read_text()
            self.assertIn("false", content)


class TestStatus(unittest.TestCase):
    def test_status_no_config(self):
        with patch("skyops._status.find_config", return_value=None):
            with patch("skyops._status.load_config", side_effect=FileNotFoundError("no config")):
                result = runner.invoke(app, ["status"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("not found", result.output)

    def test_status_bare_metal_format(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            toml_path = Path(td) / "skyops.toml"
            toml_path.write_text(textwrap.dedent("""\
                [stack]
                mode = "bare-metal"
                [server]
                port = 7860
            """))
            cfg = load_config(toml_path)
            with patch("skyops._status.load_config", return_value=cfg):
                with patch("skyops._status._probe_tcp", return_value=False):
                    result = runner.invoke(app, ["status"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("bare-metal", result.output)
            self.assertIn("SERVICE", result.output)
            self.assertIn("postgres", result.output)
            self.assertIn("control-plane", result.output)

    def test_status_uses_observability_job_buckets(self):
        cfg = SkyopsConfig()
        cfg.stack.mode = "bare-metal"
        mock_client = _mock_agp_client(
            ops_health={
                "jobs": {
                    "queued": 1,
                    "running": 2,
                    "completed": 3,
                    "failed": 1,
                },
                "queue": {"depth": 1},
            },
            list_agents={"items": [{"agent_id": "agt_local", "status": "idle"}]},
        )
        with patch("skyops._status.load_config", return_value=cfg), \
             patch("skyops._status._bare_metal_services", return_value=[]), \
             patch("skyops._client.build_client", return_value=mock_client):
            result = runner.invoke(app, ["status"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Platform:  7 jobs total, 2 running, 1 queued", result.output)
        self.assertIn("Agents:    1 live (agt_local)", result.output)


# ── Phase C: lifecycle, db, health ────────────────────────────────


class TestLifecycleDockerUp(unittest.TestCase):
    def test_up_docker_calls_compose(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            toml_path = Path(td) / "skyops.toml"
            toml_path.write_text(textwrap.dedent("""\
                [stack]
                mode = "docker"
                compose_file = "compose.yaml"
                project_name = "agp"
            """))
            cfg = load_config(toml_path)
            with patch("skyops._lifecycle.load_config", return_value=cfg):
                with patch("skyops._lifecycle.subprocess") as mock_sub:
                    mock_sub.run.return_value = None
                    result = runner.invoke(app, ["up"])
            self.assertEqual(result.exit_code, 0, result.output)
            # Check that docker compose up was called
            calls = mock_sub.run.call_args_list
            self.assertTrue(any("up" in str(c) for c in calls))

    def test_down_docker_calls_compose(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            toml_path = Path(td) / "skyops.toml"
            toml_path.write_text("[stack]\nmode = 'docker'\n")
            cfg = load_config(toml_path)
            with patch("skyops._lifecycle.load_config", return_value=cfg):
                with patch("skyops._lifecycle.subprocess") as mock_sub:
                    mock_sub.run.return_value = None
                    result = runner.invoke(app, ["down"])
            self.assertEqual(result.exit_code, 0, result.output)
            calls = mock_sub.run.call_args_list
            self.assertTrue(any("down" in str(c) for c in calls))

    def test_restart_docker(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            toml_path = Path(td) / "skyops.toml"
            toml_path.write_text("[stack]\nmode = 'docker'\n")
            cfg = load_config(toml_path)
            with patch("skyops._lifecycle.load_config", return_value=cfg):
                with patch("skyops._lifecycle.subprocess") as mock_sub:
                    mock_sub.run.return_value = None
                    result = runner.invoke(app, ["restart"])
            self.assertEqual(result.exit_code, 0, result.output)
            calls = mock_sub.run.call_args_list
            # Should have both down and up calls
            call_str = str(calls)
            self.assertIn("down", call_str)
            self.assertIn("up", call_str)


class TestLifecycleDockerSingleService(unittest.TestCase):
    def test_up_single_service(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            toml_path = Path(td) / "skyops.toml"
            toml_path.write_text("[stack]\nmode = 'docker'\n")
            cfg = load_config(toml_path)
            with patch("skyops._lifecycle.load_config", return_value=cfg):
                with patch("skyops._lifecycle.subprocess") as mock_sub:
                    mock_sub.run.return_value = None
                    result = runner.invoke(app, ["up", "control-plane"])
            self.assertEqual(result.exit_code, 0, result.output)
            # Should include the service name
            call_args = mock_sub.run.call_args_list[0][0][0]
            self.assertIn("control-plane", call_args)


class TestDbSeed(unittest.TestCase):
    def test_db_init_docker_mode(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            toml_path = Path(td) / "skyops.toml"
            toml_path.write_text("[stack]\nmode = 'docker'\n")
            cfg = load_config(toml_path)
            with patch("skyops._db.load_config", return_value=cfg):
                with patch("skyops._db.subprocess") as mock_sub:
                    mock_sub.run.return_value = None
                    result = runner.invoke(app, ["db", "init"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("initialized", result.output.lower())

    def test_db_init_bare_metal_uses_configured_database_url(self):
        cfg = SkyopsConfig()
        cfg.stack.mode = "bare-metal"
        cfg.database.url = "postgresql+psycopg://agp:agp@db:5432/agp"
        with patch("skyops._db.load_config", return_value=cfg), \
             patch("skyops._db.subprocess.run") as mock_run:
            mock_run.return_value = None
            result = runner.invoke(app, ["db", "init"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            mock_run.call_args.kwargs["env"]["AGP_DATABASE_URL"],
            "postgresql+psycopg://agp:agp@db:5432/agp",
        )


class TestHealth(unittest.TestCase):
    def test_health_all_down(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            toml_path = Path(td) / "skyops.toml"
            toml_path.write_text("[stack]\nmode = 'bare-metal'\n[server]\nport = 7860\n")
            cfg = load_config(toml_path)
            with patch("skyops._health.load_config", return_value=cfg):
                with patch("skyops._health._probe_tcp", return_value=False):
                    with patch("skyops._health._probe_http_health", return_value=False):
                        result = runner.invoke(app, ["health"])
            self.assertEqual(result.exit_code, 1)
            self.assertIn("FAIL", result.output)

    def test_health_all_up(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            toml_path = Path(td) / "skyops.toml"
            toml_path.write_text("[stack]\nmode = 'bare-metal'\n[server]\nport = 7860\n")
            cfg = load_config(toml_path)
            with patch("skyops._health.load_config", return_value=cfg), \
                 patch("skyops._health._probe_tcp", return_value=True), \
                 patch("skyops._health._probe_http_health", return_value=True), \
                 patch("skyops._health._redis_ping", return_value=True), \
                 patch("skyops._health._minio_bucket_access", return_value=True):
                        # Mock the AgpClient observability call
                        mock_client = unittest.mock.MagicMock()
                        mock_client.observability_summary.return_value = {
                            "total_jobs": 42,
                            "active_agents": 2,
                        }
                        mock_client.list_agents.return_value = {"items": []}
                        mock_client.__enter__ = lambda s: mock_client
                        mock_client.__exit__ = lambda s, *a: None
                        with patch("skyops._client.build_client", return_value=mock_client):
                            result = runner.invoke(app, ["health"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("PASS", result.output)
            self.assertIn("All checks passed", result.output)


class TestWriteProfile(unittest.TestCase):
    def test_write_profile_creates_file(self):
        import tempfile

        from skyops._lifecycle import _write_profile

        cfg = SkyopsConfig()
        cfg.server.port = 9999
        cfg.security.operator_token = "tok123"

        with tempfile.TemporaryDirectory() as td:
            profiles_dir = Path(td) / ".agp" / "profiles"
            with patch("skyops._lifecycle.Path.home", return_value=Path(td)):
                _write_profile(cfg)
            profile = profiles_dir / "default.toml"
            self.assertTrue(profile.exists())
            content = profile.read_text()
            self.assertIn("9999", content)
            self.assertIn("tok123", content)

    def test_write_profile_preserves_existing_token_when_config_token_empty(self):
        from skyops._lifecycle import _write_profile

        cfg = SkyopsConfig()
        cfg.server.port = 9999
        cfg.security.operator_token = ""

        import tempfile

        with tempfile.TemporaryDirectory() as td:
            profiles_dir = Path(td) / ".agp" / "profiles"
            profiles_dir.mkdir(parents=True, exist_ok=True)
            (profiles_dir / "default.toml").write_text(
                'server_url = "http://127.0.0.1:7860"\n'
                'token = "persisted-token"\n'
            )
            with patch("skyops._lifecycle.Path.home", return_value=Path(td)):
                _write_profile(cfg)
            content = (profiles_dir / "default.toml").read_text()
            self.assertIn("persisted-token", content)


# ── Phase D: dispatch, monitor, backup, security, upgrade, drill, queue ──


def _mock_agp_client(**method_returns):
    """Create a mock AgpClient with context manager support."""
    mock_client = unittest.mock.MagicMock()
    for method, retval in method_returns.items():
        getattr(mock_client, method).return_value = retval
    mock_client.__enter__ = lambda s: mock_client
    mock_client.__exit__ = lambda s, *a: None
    return mock_client


def _dispatch_patches(cfg, mock_client):
    """Return stacked patches for dispatch module."""
    return [
        patch("skyops._dispatch.load_config", return_value=cfg),
        patch("skyops._dispatch._client", return_value=mock_client),
    ]


class TestDispatchSend(unittest.TestCase):
    def test_send_command(self):
        import tempfile

        mock_client = _mock_agp_client(send={"job_id": "job_123", "status": "queued"})
        with tempfile.TemporaryDirectory() as td:
            toml_path = Path(td) / "skyops.toml"
            toml_path.write_text("[server]\nport = 7860\n")
            cfg = load_config(toml_path)
            with patch("skyops._dispatch._client", return_value=mock_client):
                    result = runner.invoke(app, ["send", "agt_local", "hello world"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("job_123", result.output)
        mock_client.send.assert_called_once()


class TestDispatchJobs(unittest.TestCase):
    def test_list_jobs_command(self):
        import tempfile

        mock_client = _mock_agp_client(list_jobs={"items": [], "total": 0})
        with tempfile.TemporaryDirectory() as td:
            toml_path = Path(td) / "skyops.toml"
            toml_path.write_text("[server]\nport = 7860\n")
            cfg = load_config(toml_path)
            with patch("skyops._dispatch._client", return_value=mock_client):
                    result = runner.invoke(app, ["jobs"])
        self.assertEqual(result.exit_code, 0, result.output)
        mock_client.list_jobs.assert_called_once()


class TestDispatchAgents(unittest.TestCase):
    def test_list_agents_command(self):
        import tempfile

        mock_client = _mock_agp_client(list_agents={"items": [{"agent_id": "agt_local"}]})
        with tempfile.TemporaryDirectory() as td:
            toml_path = Path(td) / "skyops.toml"
            toml_path.write_text("[server]\nport = 7860\n")
            cfg = load_config(toml_path)
            with patch("skyops._dispatch._client", return_value=mock_client):
                    result = runner.invoke(app, ["agents"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("agt_local", result.output)


class TestDispatchCapabilities(unittest.TestCase):
    def test_list_capabilities_command(self):
        mock_client = _mock_agp_client(list_capabilities={"items": [{"capability_id": "cap_python", "name": "python", "version": "v1"}]})
        with patch("skyops._dispatch._client", return_value=mock_client):
            result = runner.invoke(app, ["capabilities"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("cap_python", result.output)

    def test_capability_inspect_by_name(self):
        not_found = httpx.HTTPStatusError(
            "not found",
            request=httpx.Request("GET", "http://test/capabilities/python"),
            response=httpx.Response(404),
        )
        mock_client = _mock_agp_client(
            list_capabilities={"items": [{"capability_id": "cap_python", "name": "python", "version": "v1"}]},
            get_capability={"capability_id": "cap_python", "name": "python"},
        )
        mock_client.get_capability.side_effect = [not_found, {"capability_id": "cap_python", "name": "python"}]
        with patch("skyops._dispatch._client", return_value=mock_client):
            result = runner.invoke(app, ["capability", "python"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("cap_python", result.output)
        mock_client.list_capabilities.assert_called_once_with(name="python", limit=100)

    def test_capability_inspect_non_404_error_propagates(self):
        server_error = httpx.HTTPStatusError(
            "server error",
            request=httpx.Request("GET", "http://test/capabilities/cap_python"),
            response=httpx.Response(500),
        )
        mock_client = _mock_agp_client()
        mock_client.get_capability.side_effect = server_error
        with patch("skyops._dispatch._client", return_value=mock_client):
            result = runner.invoke(app, ["capability", "cap_python"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertNotIn("Capability not found", result.output)

    def test_capability_inspect_ambiguous_name_fails(self):
        not_found = httpx.HTTPStatusError(
            "not found",
            request=httpx.Request("GET", "http://test/capabilities/python"),
            response=httpx.Response(404),
        )
        mock_client = _mock_agp_client(
            list_capabilities={
                "items": [
                    {"capability_id": "cap_python_v1", "name": "python", "version": "v1"},
                    {"capability_id": "cap_python_v2", "name": "python", "version": "v2"},
                ]
            }
        )
        mock_client.get_capability.side_effect = not_found
        with patch("skyops._dispatch._client", return_value=mock_client):
            result = runner.invoke(app, ["capability", "python"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("ambiguous", result.output.lower())
        self.assertIn("cap_python_v1", result.output)
        self.assertIn("cap_python_v2", result.output)


class TestMonitorMetrics(unittest.TestCase):
    def test_metrics_summary(self):
        import tempfile

        mock_client = _mock_agp_client(ops_health={"total_jobs": 10})
        with tempfile.TemporaryDirectory() as td:
            toml_path = Path(td) / "skyops.toml"
            toml_path.write_text("[server]\nport = 7860\n")
            cfg = load_config(toml_path)
            with patch("skyops._monitor._client", return_value=mock_client):
                    result = runner.invoke(app, ["metrics"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("total_jobs", result.output)


class TestMonitorAlerts(unittest.TestCase):
    def test_alerts_command(self):
        import tempfile

        mock_client = _mock_agp_client(observability_alerts={"items": []})
        with tempfile.TemporaryDirectory() as td:
            toml_path = Path(td) / "skyops.toml"
            toml_path.write_text("[server]\nport = 7860\n")
            cfg = load_config(toml_path)
            with patch("skyops._monitor._client", return_value=mock_client):
                    result = runner.invoke(app, ["alerts"])
        self.assertEqual(result.exit_code, 0, result.output)


class TestMonitorEvents(unittest.TestCase):
    def test_events_command(self):
        mock_client = _mock_agp_client(
            get_job_events={
                "items": [{"event_type": "job.accepted"}, {"event_type": "job.queued"}],
                "page": {"next_cursor": None},
            }
        )
        with patch("skyops._monitor._client", return_value=mock_client):
            result = runner.invoke(app, ["events", "job_123"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("job.accepted", result.output)
        self.assertIn("job.queued", result.output)
        mock_client.get_job_events.assert_called_once()


class TestMonitorDockerHelpers(unittest.TestCase):
    def test_docker_runtime_container_raises_on_timeout(self):
        from skyops._monitor import DockerCommandTimeout, _docker_runtime_container

        with patch("skyops._monitor._run_output", side_effect=subprocess.TimeoutExpired(cmd=["docker"], timeout=10)):
            with self.assertRaises(DockerCommandTimeout):
                _docker_runtime_container("agp", "compose.yaml", "rtm_local")


class TestUpgradeStatus(unittest.TestCase):
    def test_upgrade_status(self):
        with patch("agp._ops_helpers.get_upgrade_status", return_value={
            "release_version": "0.1.0",
            "schema_version": "0001_initial",
        }):
            result = runner.invoke(app, ["upgrade", "status"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("0.1.0", result.output)


class TestDrillList(unittest.TestCase):
    def test_drill_list(self):
        result = runner.invoke(app, ["drill", "list"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("lease_expiry_requeue", result.output)
        self.assertIn("control_plane_restart_active_work", result.output)


class TestSecretsShow(unittest.TestCase):
    def test_secrets_show(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            toml_path = Path(td) / "skyops.toml"
            toml_path.write_text("[security]\noperator_token = 'tok123'\n")
            cfg = load_config(toml_path)
            with patch("skyops._security.load_config", return_value=cfg):
                # Don't try to connect to control plane
                with patch("skyops._security._client", side_effect=Exception("no server")):
                    result = runner.invoke(app, ["secrets", "show"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("operator_token", result.output)


class TestSecretsGenerate(unittest.TestCase):
    def test_secrets_generate(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            toml_path = Path(td) / "skyops.toml"
            toml_path.write_text("[security]\n")
            with patch("skyops.config.find_config", return_value=toml_path):
                result = runner.invoke(app, ["secrets", "generate"])
            self.assertEqual(result.exit_code, 0, result.output)
            local_path = Path(td) / "skyops.local.toml"
            self.assertTrue(local_path.exists())
            content = local_path.read_text()
            self.assertIn("operator_token", content)
            self.assertIn("secret_access_key", content)


class TestCLIHelp(unittest.TestCase):
    """Verify all command groups appear in --help."""

    def test_all_commands_registered(self):
        result = runner.invoke(app, ["--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        for cmd in [
            "init", "config", "status", "deps", "up", "down", "restart", "ps",
            "db", "health", "send", "watch", "jobs", "agents", "capabilities", "capability",
            "interrupt", "fetch", "deliveries", "metrics", "alerts",
            "trace", "events", "logs", "backup", "secrets", "upgrade", "drill",
            "host", "adapter", "plugin", "queue", "job", "sweep",
            "validate", "smoke", "k8s-smoke", "runtime", "workspace",
        ]:
            self.assertIn(cmd, result.output, f"Command '{cmd}' not found in --help output")


# ── Gap fixes: new commands ──────────────────────────────────────


class TestDepsCheck(unittest.TestCase):
    def test_deps_check(self):
        result = runner.invoke(app, ["deps", "check"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("DEPENDENCY", result.output)
        self.assertIn("docker", result.output)


class TestDbMigrate(unittest.TestCase):
    def test_db_migrate_runs_migrations(self):
        cfg = SkyopsConfig()
        cfg.stack.mode = "bare-metal"
        completed = unittest.mock.Mock(stdout='{"applied":["0001_initial"],"current_version":"0001_initial"}')
        with patch("skyops._db.load_config", return_value=cfg), \
             patch("skyops._db.subprocess.run", return_value=completed):
            result = runner.invoke(app, ["db", "migrate"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("version", result.output.lower())

    def test_db_migrate_bare_metal_uses_configured_database_url(self):
        cfg = SkyopsConfig()
        cfg.stack.mode = "bare-metal"
        cfg.database.url = "postgresql+psycopg://agp:agp@db:5432/agp"
        completed = unittest.mock.Mock(stdout='{"applied":[],"current_version":"0001_initial"}')
        with patch("skyops._db.load_config", return_value=cfg), \
             patch("skyops._db.subprocess.run", return_value=completed) as mock_run:
            result = runner.invoke(app, ["db", "migrate"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            mock_run.call_args.kwargs["env"]["AGP_DATABASE_URL"],
            "postgresql+psycopg://agp:agp@db:5432/agp",
        )


class TestBackupList(unittest.TestCase):
    def test_backup_list_no_dir(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            result = runner.invoke(app, ["backup", "list", f"{td}/nonexistent"])
        self.assertEqual(result.exit_code, 1)

    def test_backup_list_empty(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            result = runner.invoke(app, ["backup", "list", td])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("No backup", result.output)


class TestSecretsGenerateK8s(unittest.TestCase):
    def test_generate_k8s(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            toml_path = Path(td) / "skyops.toml"
            toml_path.write_text("[security]\noperator_token = 'tok'\n[s3]\naccess_key_id = 'ak'\nsecret_access_key = 'sk'\n")
            cfg = load_config(toml_path)
            out_path = Path(td) / "secret.yaml"
            with patch("skyops._security.load_config", return_value=cfg):
                result = runner.invoke(app, ["secrets", "generate-k8s", str(out_path)])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue(out_path.exists())
            content = out_path.read_text()
            self.assertIn("apiVersion: v1", content)
            self.assertIn("agp-secrets", content)


class TestValidateCommand(unittest.TestCase):
    def test_validate_with_mocked_docker(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            toml_path = Path(td) / "skyops.toml"
            toml_path.write_text("[stack]\nmode = 'docker'\ncompose_file = 'compose.yaml'\n")
            cfg = load_config(toml_path)
            with patch("skyops._validate.load_config", return_value=cfg):
                with patch("skyops._validate.shutil") as mock_shutil:
                    mock_shutil.which.return_value = None  # no docker/kubectl
                    result = runner.invoke(app, ["validate"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("ok", result.output)


class TestSweepIdle(unittest.TestCase):
    def test_sweep_idle_subcommand_exists(self):
        result = runner.invoke(app, ["sweep", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("idle", result.output)
        self.assertIn("draining", result.output)


class TestJobSubcommands(unittest.TestCase):
    def test_job_subcommand_exists(self):
        result = runner.invoke(app, ["job", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("block", result.output)
        self.assertIn("unblock", result.output)

    def test_job_block_docker_execs_into_control_plane(self):
        cfg = SkyopsConfig()
        cfg.stack.mode = "docker"
        cfg.stack.compose_file = "compose.yaml"
        cfg.stack.project_name = "agp"
        proc = unittest.mock.Mock(returncode=0)
        with patch("skyops._queue.load_config", return_value=cfg), \
             patch("skyops._queue.subprocess.run", return_value=proc) as mock_run:
            result = runner.invoke(app, ["job", "block", "job_123"])
        self.assertEqual(result.exit_code, 0, result.output)
        cmd = mock_run.call_args.args[0]
        self.assertIn("control-plane", cmd)
        # Verify env vars are passed via -e flags, not interpolated into code
        self.assertIn("-e", cmd)
        env_args = [cmd[i + 1] for i, v in enumerate(cmd) if v == "-e"]
        self.assertIn("_JOB_ID=job_123", env_args)
        self.assertIn("_REASON=operator-block", env_args)


class TestQueueRedrive(unittest.TestCase):
    def test_queue_redrive_subcommand_exists(self):
        result = runner.invoke(app, ["queue", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("redrive", result.output)
        self.assertIn("reconstruct", result.output)
        self.assertIn("inspect", result.output)

    def test_queue_redrive_docker_execs_into_control_plane(self):
        cfg = SkyopsConfig()
        cfg.stack.mode = "docker"
        cfg.stack.compose_file = "compose.yaml"
        cfg.stack.project_name = "agp"
        proc = unittest.mock.Mock(returncode=0)
        with patch("skyops._queue.load_config", return_value=cfg), \
             patch("skyops._queue.subprocess.run", return_value=proc) as mock_run:
            result = runner.invoke(app, ["queue", "redrive"])
        self.assertEqual(result.exit_code, 0, result.output)
        cmd = mock_run.call_args.args[0]
        self.assertEqual(cmd[:7], ["docker", "compose", "-f", "compose.yaml", "-p", "agp", "exec"])
        self.assertIn("control-plane", cmd)


class TestQueueInspect(unittest.TestCase):
    def test_queue_inspect_inmemory_backend(self):
        from agp.queue_backend import InMemoryBrokerQueueBackend

        backend = InMemoryBrokerQueueBackend()
        backend._queued("agent:agt_local").append("job_1")
        backend._dead_lettered_jobs.add("job_dead")

        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

        class _Session:
            def __init__(self):
                self.calls = 0

            def execute(self, query):  # noqa: ARG002
                self.calls += 1
                if self.calls == 1:
                    return _Result([("pending", 1)])
                if self.calls == 2:
                    return _Result([("agent:agt_local", "pending", 1)])
                return _Result([])

            def close(self):
                return None

        cfg = SkyopsConfig()
        cfg.stack.mode = "bare-metal"

        with patch("skyops._queue.load_config", return_value=cfg), \
             patch("agp.queue_backend.get_queue_backend", return_value=backend), \
             patch("agp.db.SessionLocal", return_value=_Session()), \
             patch("agp.config.settings.queue_backend", "inmemory_broker"):
            result = runner.invoke(app, ["queue", "inspect"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("agent:agt_local", result.output)
        self.assertIn("job_dead", result.output)

    def test_queue_inspect_redis_includes_transport_only_queue(self):
        from agp.queue_backend import RedisQueueBackend
        from tests._base import FakeRedisClient

        client = FakeRedisClient()
        backend = RedisQueueBackend(redis_url="redis://test", key_prefix="agp-test")
        backend.client = client
        client.rpush("agp-test:queue:agent:stale", "job_stale")
        client.sadd("agp-test:queue:agent:stale:pending", "job_stale")

        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

        class _Session:
            def __init__(self):
                self.calls = 0

            def execute(self, query):  # noqa: ARG002
                self.calls += 1
                if self.calls == 1:
                    return _Result([("pending", 1)])
                if self.calls == 2:
                    return _Result([])
                return _Result([])

            def close(self):
                return None

        cfg = SkyopsConfig()
        cfg.stack.mode = "bare-metal"

        with patch("skyops._queue.load_config", return_value=cfg), \
             patch("agp.queue_backend.get_queue_backend", return_value=backend), \
             patch("agp.db.SessionLocal", return_value=_Session()), \
             patch("agp.config.settings.queue_backend", "redis"):
            result = runner.invoke(app, ["queue", "inspect"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("agent:stale", result.output)
        self.assertIn("job_stale", result.output)

    def test_queue_inspect_docker_execs_into_control_plane(self):
        cfg = SkyopsConfig()
        cfg.stack.mode = "docker"
        cfg.stack.compose_file = "compose.yaml"
        cfg.stack.project_name = "agp"
        proc = unittest.mock.Mock(returncode=0)
        with patch("skyops._queue.load_config", return_value=cfg), \
             patch("skyops._queue.subprocess.run", return_value=proc) as mock_run:
            result = runner.invoke(app, ["queue", "inspect"])
        self.assertEqual(result.exit_code, 0, result.output)
        mock_run.assert_called_once()
        cmd = mock_run.call_args.args[0]
        self.assertEqual(cmd[:7], ["docker", "compose", "-f", "compose.yaml", "-p", "agp", "exec"])
        self.assertIn("control-plane", cmd)


class TestRuntimeDebug(unittest.TestCase):
    def test_runtime_subcommands_exist(self):
        result = runner.invoke(app, ["runtime", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        for name in ["register", "claim", "work-once", "work-loop", "deploy", "list", "inspect"]:
            self.assertIn(name, result.output)

    def test_runtime_list_command(self):
        mock_client = _mock_agp_client(list_runtimes={"items": [{"runtime_id": "rtm_local"}]})
        with patch("skyops._runtime_debug.build_client", return_value=mock_client):
            result = runner.invoke(app, ["runtime", "list", "--cursor", "cur_123"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("rtm_local", result.output)
        mock_client.list_runtimes.assert_called_once_with(
            status=None, health_status=None, limit=50, cursor="cur_123"
        )

    def test_runtime_inspect_command(self):
        mock_client = _mock_agp_client(get_runtime={"runtime_id": "rtm_local", "status": "idle"})
        with patch("skyops._runtime_debug.build_client", return_value=mock_client):
            result = runner.invoke(app, ["runtime", "inspect", "rtm_local"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("rtm_local", result.output)
        self.assertIn("idle", result.output)

    def test_runtime_work_loop_uses_configured_runtime_token(self):
        cfg = SkyopsConfig()
        cfg.security.runtime_token = "rtok"
        observed: dict[str, str | None] = {}

        def _fake_work_loop(**kwargs):
            import os

            observed["token"] = os.environ.get("AGP_RUNTIME_BEARER_TOKEN")
            observed["server_url"] = kwargs["server_url"]

        with patch("skyops._runtime_debug.load_config", return_value=cfg), \
             patch("skyops._runtime_debug.build_profile", return_value=type("Profile", (), {"server_url": "http://cp:7860"})()), \
             patch("agp.cli.runtime_work_loop", side_effect=_fake_work_loop):
            result = runner.invoke(app, ["runtime", "work-loop", "rtm_local"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(observed["token"], "rtok")
        self.assertEqual(observed["server_url"], "http://cp:7860")

    def test_runtime_work_loop_empty_env_token_falls_back_to_config(self):
        cfg = SkyopsConfig()
        cfg.security.runtime_token = "rtok"
        observed: dict[str, str | None] = {}

        def _fake_work_loop(**kwargs):
            import os

            observed["token"] = os.environ.get("AGP_RUNTIME_BEARER_TOKEN")
            observed["server_url"] = kwargs["server_url"]

        with patch.dict("os.environ", {"AGP_RUNTIME_BEARER_TOKEN": ""}, clear=False), \
             patch("skyops._runtime_debug.load_config", return_value=cfg), \
             patch("skyops._runtime_debug.build_profile", return_value=type("Profile", (), {"server_url": "http://cp:7860"})()), \
             patch("agp.cli.runtime_work_loop", side_effect=_fake_work_loop):
            result = runner.invoke(app, ["runtime", "work-loop", "rtm_local"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(observed["token"], "rtok")
        self.assertEqual(observed["server_url"], "http://cp:7860")



class TestWorkspaceCommands(unittest.TestCase):
    def test_workspace_resolve_command(self):
        cfg = SkyopsConfig.from_dict({
            "host_profiles": {"default": {"mount_sources": {"repo": "/srv/repo"}}},
            "workspace_profiles": {"main": {"workspace_ref": "/workspace/main", "mounts": ["@repo:/workspace/main"]}},
            "agents": {"agt_local": {"capabilities": ["python"], "workspace_profile": "main"}},
        })
        with patch("skyops._workspace.load_config", return_value=cfg):
            result = runner.invoke(app, ["workspace", "resolve", "agt_local"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("/workspace/main", result.output)
        self.assertIn("/srv/repo:/workspace/main", result.output)

    def test_workspace_validate_command(self):
        cfg = SkyopsConfig.from_dict({
            "host_profiles": {"default": {"mount_sources": {"repo": "/srv/repo"}}},
            "workspace_profiles": {"main": {"workspace_ref": "/workspace/main", "mounts": ["@repo:/workspace/main"]}},
            "agents": {"agt_local": {"capabilities": ["python"], "workspace_profile": "main"}},
        })
        with patch("skyops._workspace.load_config", return_value=cfg), \
             patch("skyops._workspace.Path.exists", return_value=True):
            result = runner.invoke(app, ["workspace", "validate", "agt_local"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('"ok": true', result.output.lower())

    def test_workspace_validate_explicit_host_profile_is_not_authoritative_locally(self):
        cfg = SkyopsConfig.from_dict({
            "host_profiles": {"server-a": {"mount_sources": {"repo": "/srv/repo"}}},
            "workspace_profiles": {"main": {"workspace_ref": "/workspace/main", "mounts": ["@repo:/workspace/main"]}},
            "agents": {"agt_local": {"capabilities": ["python"], "workspace_profile": "main"}},
        })
        with patch("skyops._workspace.load_config", return_value=cfg), \
             patch("skyops._workspace.Path.exists", return_value=False):
            result = runner.invoke(app, ["workspace", "validate", "agt_local", "--host-profile", "server-a"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('"verified_locally": false', result.output.lower())

    def test_workspace_validate_git_requires_missing_supplemental_mount(self):
        cfg = SkyopsConfig.from_dict({
            "host_profiles": {
                "default": {
                    "mount_sources": {"shared_docs": "/srv/shared-docs"},
                    "git_root": "/srv/git",
                }
            },
            "workspace_profiles": {
                "main": {
                    "workspace_ref": "/workspace/main",
                    "mounts": ["@shared_docs:/workspace/shared-docs"],
                }
            },
            "agents": {
                "agt_local": {
                    "capabilities": ["python"],
                    "workspace_profile": "main",
                    "workspace_mode": "git",
                    "repo_url": "git@github.com:example/repo.git",
                }
            },
        })
        with patch("skyops._workspace.load_config", return_value=cfg), \
             patch("skyops._workspace.shutil.which", return_value="/usr/bin/git"), \
             patch("skyops._workspace.Path.exists", side_effect=lambda *args, **kwargs: str(args[-1]) == "/srv/git/agt_local"):
            result = runner.invoke(app, ["workspace", "validate", "agt_local"])
        self.assertNotEqual(result.exit_code, 0)

    def test_workspace_resolve_unknown_agent_fails(self):
        cfg = SkyopsConfig.from_dict({
            "agents": {"agt_local": {"capabilities": ["python"]}},
        })
        with patch("skyops._workspace.load_config", return_value=cfg):
            result = runner.invoke(app, ["workspace", "resolve", "agt_missing"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("agent not found", result.output)

    def test_workspace_resolve_requires_host_profile_when_ambiguous(self):
        cfg = SkyopsConfig.from_dict({
            "host_profiles": {
                "server_a": {"mount_sources": {"repo": "/srv/a/repo"}},
                "server_b": {"mount_sources": {"repo": "/srv/b/repo"}},
            },
            "workspace_profiles": {
                "main": {"workspace_ref": "/workspace/main", "mounts": ["@repo:/workspace/main"]},
            },
            "agents": {"agt_local": {"capabilities": ["python"], "workspace_profile": "main"}},
        })
        with patch("skyops._workspace.load_config", return_value=cfg):
            result = runner.invoke(app, ["workspace", "resolve", "agt_local"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("host_profile is required", result.output)


class TestLogsFollow(unittest.TestCase):
    def test_logs_service_subcommand_exists(self):
        result = runner.invoke(app, ["logs", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("service", result.output)
        self.assertIn("control-plane", result.output)
        self.assertIn("runtime", result.output)
        self.assertIn("prune", result.output)

    def test_logs_runtime_follow_uses_runtime_specific_container(self):
        cfg = SkyopsConfig()
        cfg.stack.mode = "docker"
        cfg.stack.compose_file = "compose.yaml"
        cfg.stack.project_name = "agp"

        runtime_client = _mock_agp_client(get_runtime={"runtime_id": "rtm_local", "hostname": "runtime-1"})

        compose_ps = unittest.mock.Mock(returncode=0, stdout="cid123\n", stderr="")
        inspect_payload = json.dumps([{
            "Name": "/agp-runtime-1",
            "Config": {
                "Hostname": "runtime-1",
                "Env": [
                    "AGP_RUNTIME_ID=rtm_local",
                    "AGP_RUNTIME_HOSTNAME=runtime-1",
                ],
            },
        }])
        docker_inspect = unittest.mock.Mock(returncode=0, stdout=inspect_payload, stderr="")
        docker_logs = unittest.mock.Mock(returncode=0, stdout="", stderr="")

        with patch("skyops._monitor.load_config", return_value=cfg), \
             patch("skyops._monitor._client", return_value=runtime_client), \
             patch("skyops._monitor.subprocess.run", side_effect=[compose_ps, docker_inspect, docker_logs]) as mock_run:
            result = runner.invoke(app, ["logs", "runtime", "rtm_local", "--follow"])
        self.assertEqual(result.exit_code, 0, result.output)
        calls = [call.args[0] for call in mock_run.call_args_list]
        self.assertEqual(calls[-1], ["docker", "logs", "--follow", "cid123"])

    def test_logs_runtime_follow_falls_back_without_control_plane_lookup(self):
        cfg = SkyopsConfig()
        cfg.stack.mode = "docker"
        cfg.stack.compose_file = "compose.yaml"
        cfg.stack.project_name = "agp"

        compose_ps = unittest.mock.Mock(returncode=0, stdout="cid123\n", stderr="")
        inspect_payload = json.dumps([{
            "Name": "/agp-runtime-1",
            "Config": {
                "Hostname": "runtime-1",
                "Env": [
                    "AGP_RUNTIME_ID=rtm_local",
                ],
            },
        }])
        docker_inspect = unittest.mock.Mock(returncode=0, stdout=inspect_payload, stderr="")
        docker_logs = unittest.mock.Mock(returncode=0, stdout="", stderr="")

        with patch("skyops._monitor.load_config", return_value=cfg), \
             patch("skyops._monitor._client", side_effect=RuntimeError("cp unavailable")), \
             patch("skyops._monitor.subprocess.run", side_effect=[compose_ps, docker_inspect, docker_logs]) as mock_run:
            result = runner.invoke(app, ["logs", "runtime", "rtm_local", "--follow"])
        self.assertEqual(result.exit_code, 0, result.output)
        calls = [call.args[0] for call in mock_run.call_args_list]
        self.assertEqual(calls[-1], ["docker", "logs", "--follow", "cid123"])

    def test_logs_runtime_follow_reports_timeout_separately(self):
        cfg = SkyopsConfig()
        cfg.stack.mode = "docker"
        cfg.stack.compose_file = "compose.yaml"
        cfg.stack.project_name = "agp"

        with patch("skyops._monitor.load_config", return_value=cfg), \
             patch("skyops._monitor._client", side_effect=RuntimeError("cp unavailable")), \
             patch(
                 "skyops._monitor._docker_runtime_container",
                 side_effect=__import__("skyops._monitor", fromlist=["DockerCommandTimeout"]).DockerCommandTimeout("docker compose ps timed out"),
             ):
            result = runner.invoke(app, ["logs", "runtime", "rtm_local", "--follow"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("timed out", result.output.lower())

    def test_logs_control_plane_follow_exits_nonzero_on_subprocess_failure(self):
        cfg = SkyopsConfig()
        cfg.stack.mode = "docker"
        cfg.stack.compose_file = "compose.yaml"
        cfg.stack.project_name = "agp"
        with patch("skyops._monitor.load_config", return_value=cfg), \
             patch("skyops._monitor.subprocess.run", return_value=unittest.mock.Mock(returncode=7)):
            result = runner.invoke(app, ["logs", "control-plane", "--follow"])
        self.assertEqual(result.exit_code, 7)


class TestInitChecksDeps(unittest.TestCase):
    def test_init_reports_deps(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            result = runner.invoke(app, ["init", "--dir", td, "--mode", "docker"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Checking dependencies", result.output)


class TestPsCommand(unittest.TestCase):
    def test_ps_docker_mode(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            toml_path = Path(td) / "skyops.toml"
            toml_path.write_text("[stack]\nmode = 'docker'\n")
            cfg = load_config(toml_path)
            with patch("skyops._lifecycle.load_config", return_value=cfg):
                with patch("skyops._lifecycle.subprocess") as mock_sub:
                    mock_sub.run.return_value = None
                    result = runner.invoke(app, ["ps"])
        self.assertEqual(result.exit_code, 0, result.output)

    def test_ps_docker_mode_nonzero_exits(self):
        cfg = SkyopsConfig()
        cfg.stack.mode = "docker"
        with patch("skyops._lifecycle.load_config", return_value=cfg), \
             patch("skyops._lifecycle.subprocess.run", side_effect=subprocess.CalledProcessError(2, ["docker", "compose", "ps"])):
            result = runner.invoke(app, ["ps"])
        self.assertNotEqual(result.exit_code, 0)


class TestFirstBootDetection(unittest.TestCase):
    def test_up_marks_first_boot(self):
        """skyops up sets .skyops-initialized marker on first boot."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            toml_path = Path(td) / "skyops.toml"
            toml_path.write_text("[stack]\nmode = 'docker'\n")
            cfg = load_config(toml_path)
            marker = Path(td) / ".skyops-initialized"
            self.assertFalse(marker.exists())

            with patch("skyops._lifecycle.load_config", return_value=cfg):
                with patch("skyops._lifecycle.subprocess") as mock_sub:
                    mock_sub.run.return_value = None
                    result = runner.invoke(app, ["up"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue(marker.exists(), "First boot marker not created")

    def test_second_boot_skips_init(self):
        """skyops up does not re-init on subsequent boots."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            toml_path = Path(td) / "skyops.toml"
            toml_path.write_text("[stack]\nmode = 'docker'\n")
            cfg = load_config(toml_path)
            # Pre-create the marker
            (Path(td) / ".skyops-initialized").write_text("initialized\n")

            with patch("skyops._lifecycle.load_config", return_value=cfg):
                with patch("skyops._lifecycle.subprocess") as mock_sub:
                    mock_sub.run.return_value = None
                    result = runner.invoke(app, ["up"])
            self.assertEqual(result.exit_code, 0, result.output)


class TestStatusUptime(unittest.TestCase):
    def test_status_table_has_uptime_column(self):
        """skyops status output includes an UPTIME column."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            toml_path = Path(td) / "skyops.toml"
            toml_path.write_text(textwrap.dedent("""\
                [stack]
                mode = "bare-metal"
                [server]
                port = 7860
            """))
            cfg = load_config(toml_path)
            with patch("skyops._status.load_config", return_value=cfg):
                with patch("skyops._status._probe_tcp", return_value=False):
                    with patch("skyops._status._process_running", return_value=False):
                        with patch("skyops._status._platform_summary", return_value=[]):
                            result = runner.invoke(app, ["status"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("UPTIME", result.output)


if __name__ == "__main__":
    unittest.main()
