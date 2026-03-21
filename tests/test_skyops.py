"""Tests for the skyops CLI — Phase B: skeleton, config, init, status."""

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


if __name__ == "__main__":
    unittest.main()
