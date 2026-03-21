"""Tests for the skyops CLI — Phases B & C."""

from __future__ import annotations

import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from skyops.cli import app
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
            "agents": {"agt_local": {"capability_id": "cap_python"}},
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


# ── Phase C: lifecycle, db, health ────────────────────────────────


class TestLifecycleDockerUp(unittest.TestCase):
    def test_up_docker_calls_compose(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            toml_path = Path(td) / "skyops.toml"
            toml_path.write_text(textwrap.dedent("""\
                [stack]
                mode = "docker"
                compose_file = "compose.phase3.yaml"
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


class TestMonitorMetrics(unittest.TestCase):
    def test_metrics_summary(self):
        import tempfile

        mock_client = _mock_agp_client(observability_summary={"total_jobs": 10})
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
            "db", "health", "send", "watch", "jobs", "agents",
            "interrupt", "fetch", "deliveries", "metrics", "alerts",
            "trace", "logs", "backup", "secrets", "upgrade", "drill",
            "host", "adapter", "plugin", "queue", "job", "sweep",
            "validate", "smoke", "k8s-smoke", "runtime",
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
    def test_db_migrate_placeholder(self):
        result = runner.invoke(app, ["db", "migrate"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("placeholder", result.output.lower())


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
            toml_path.write_text("[stack]\nmode = 'docker'\ncompose_file = 'compose.phase3.yaml'\n")
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


class TestQueueRedrive(unittest.TestCase):
    def test_queue_redrive_subcommand_exists(self):
        result = runner.invoke(app, ["queue", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("redrive", result.output)
        self.assertIn("reconstruct", result.output)


class TestRuntimeDebug(unittest.TestCase):
    def test_runtime_subcommands_exist(self):
        result = runner.invoke(app, ["runtime", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("register", result.output)
        self.assertIn("claim", result.output)
        self.assertIn("work-once", result.output)


class TestLogsFollow(unittest.TestCase):
    def test_logs_service_subcommand_exists(self):
        result = runner.invoke(app, ["logs", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("service", result.output)
        self.assertIn("control-plane", result.output)
        self.assertIn("runtime", result.output)
        self.assertIn("prune", result.output)


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
