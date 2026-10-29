import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location("tunnel", Path(__file__).with_name("tunnel.py"))
tunnel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tunnel)


class TunnelTests(unittest.TestCase):
    def test_database_workflows_use_shared_tunnel_without_public_secret_overrides(self):
        workflows = Path(__file__).resolve().parents[3] / ".github/workflows"
        text = (workflows / "ingest.yml").read_text()
        self.assertEqual(text.count("uses: ./.github/actions/railway-db-tunnel"), 2)
        self.assertNotIn("secrets.DATABASE_URL", text)
        self.assertNotIn("DATABASE_URL:", text)
        cleanup = text.split("- name: Close private database tunnel", 1)[1]
        self.assertIn("if: always()", cleanup)
        self.assertIn("operation: stop", cleanup)

    def test_url_preserves_credentials_database_and_tls_but_not_routing_overrides(self):
        raw = "postgresql://user:p%40ss@postgres.railway.internal:5432/railway?sslmode=require&host=old&hostaddr=1.2.3.4&port=5432"
        self.assertEqual(
            tunnel.local_database_url(raw, 15432),
            "postgresql://user:p%40ss@127.0.0.1:15432/railway?sslmode=require",
        )

    def test_invalid_url_does_not_expose_credentials(self):
        for raw in ("", "redis://user:secret@host/db", "postgresql://user:secret@host:bad/db"):
            with self.subTest(raw=raw):
                with self.assertRaises(RuntimeError) as error:
                    tunnel.local_database_url(raw, 15432)
                self.assertNotIn("secret", str(error.exception))

    def test_masks_workflow_command_characters(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            tunnel.mask("secret%\r\nvalue")
        self.assertEqual(output.getvalue(), "::add-mask::secret%25%0D%0Avalue\n")

    @patch.object(tunnel.time, "sleep")
    @patch.object(tunnel.subprocess, "run")
    def test_readiness_requires_successful_sql_and_keeps_password_out_of_argv(self, run, sleep):
        process = Mock()
        process.poll.return_value = None
        run.side_effect = [
            subprocess.CompletedProcess([], 1, "", "secret connection error"),
            subprocess.CompletedProcess([], 0, "1\n", ""),
        ]
        tunnel.wait_for_database(process, "postgresql://u:secret@127.0.0.1:15432/db")
        self.assertEqual(run.call_count, 2)
        args, kwargs = run.call_args
        self.assertIn("SELECT 1", args[0])
        self.assertNotIn("secret", " ".join(args[0]))
        self.assertEqual(args[0][args[0].index("--dbname") + 1], "postgresql://u@127.0.0.1:15432/db")
        self.assertEqual(kwargs["env"]["PGPASSWORD"], "secret")
        self.assertEqual(kwargs["env"]["PGCONNECT_TIMEOUT"], "5")

    def test_readiness_fails_if_tunnel_exits(self):
        process = Mock()
        process.poll.return_value = 1
        with self.assertRaisesRegex(RuntimeError, "SSH key"):
            tunnel.wait_for_database(process, "postgresql://u:secret@127.0.0.1/db")

    @patch.object(tunnel.subprocess, "run")
    def test_psql_decodes_password_and_keeps_query_credentials_out_of_arguments(self, run):
        process = Mock()
        process.poll.return_value = None
        run.return_value = subprocess.CompletedProcess([], 0, "1\n", "")
        for query, password in (("sslmode=require", "p@ss"),
                                ("sslmode=require&password=query%20secret", "query secret")):
            with self.subTest(query=query):
                tunnel.wait_for_database(process, f"postgresql://u:p%40ss@127.0.0.1:15432/db?{query}")
                args, kwargs = run.call_args
                self.assertEqual(args[0][args[0].index("--dbname") + 1],
                                 "postgresql://u@127.0.0.1:15432/db?sslmode=require")
                self.assertEqual(kwargs["env"]["PGPASSWORD"], password)

    @patch.object(tunnel.time, "monotonic", side_effect=[0, 61])
    def test_readiness_has_a_deadline(self, monotonic):
        with self.assertRaisesRegex(RuntimeError, "Timed out"):
            tunnel.wait_for_database(Mock(), "postgresql://u:secret@127.0.0.1/db")

    @patch.object(tunnel, "stop")
    @patch.object(tunnel, "wait_for_database")
    @patch.object(tunnel.subprocess, "Popen")
    @patch.object(tunnel, "run_private")
    def test_start_uses_explicit_private_tunnel_and_exports_only_local_url(self, run, popen, ready, stop):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "env"
            env_file.touch()
            env = {
                "RUNNER_TEMP": tmp, "GITHUB_ENV": str(env_file),
                "RAILWAY_API_TOKEN": "token", "RAILWAY_SSH_PRIVATE_KEY": "private-key",
                "RAILWAY_TOKEN": "project-token-must-not-take-precedence",
                "RAILWAY_DB_PROJECT": "project-id", "RAILWAY_DB_ENVIRONMENT": "environment-id",
                "RAILWAY_DB_SERVICE": "Postgres", "RAILWAY_DB_PORT": "15432",
            }
            run.side_effect = ["public-key", '{"DATABASE_URL":"postgresql://u:secret@db.railway.internal:5432/db"}']
            popen.return_value.pid = 12345
            with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(io.StringIO()):
                tunnel.start()
            command = popen.call_args.args[0]
            self.assertEqual(command, [
                "railway", "connect", "Postgres", "--project", "project-id",
                "--environment", "environment-id", "--tunnel-only", "--port", "15432",
            ])
            self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
            self.assertNotIn("RAILWAY_TOKEN", popen.call_args.kwargs["env"])
            self.assertNotIn("RAILWAY_SSH_PRIVATE_KEY", popen.call_args.kwargs["env"])
            self.assertIn("DATABASE_URL=postgresql://u:secret@127.0.0.1:15432/db", env_file.read_text())
            self.assertNotIn("DATABASE_PUBLIC_URL", env_file.read_text())
            stop.assert_not_called()

    @patch.object(tunnel, "stop")
    @patch.object(tunnel, "run_private", side_effect=RuntimeError("SSH key invalid"))
    def test_failed_start_cleans_up_and_does_not_export_database_url(self, run, stop):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "env"
            env_file.touch()
            env = {
                "RUNNER_TEMP": tmp, "GITHUB_ENV": str(env_file),
                "RAILWAY_API_TOKEN": "token", "RAILWAY_SSH_PRIVATE_KEY": "private-key",
                "RAILWAY_DB_PROJECT": "project-id", "RAILWAY_DB_ENVIRONMENT": "environment-id",
                "RAILWAY_DB_SERVICE": "Postgres", "RAILWAY_DB_PORT": "15432",
            }
            with patch.dict(os.environ, env, clear=True), self.assertRaises(RuntimeError):
                tunnel.start()
            stop.assert_called_once()
            self.assertNotIn("DATABASE_URL=", env_file.read_text())

    def test_missing_secrets_fail_before_creating_state(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(RuntimeError, "RAILWAY_API_TOKEN"):
            tunnel.start()

    @patch.object(tunnel.os, "kill")
    @patch.object(tunnel.subprocess, "run")
    def test_stop_terminates_children_and_removes_credentials(self, run, kill):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "tunnel"
            state.mkdir()
            (state / "pid").write_text("12345")
            (state / "private-key").write_text("secret")
            tunnel.stop(state)
            self.assertFalse(state.exists())
            self.assertEqual(run.call_args.args[0], ["pkill", "-TERM", "-P", "12345"])
            kill.assert_called_once()
            tunnel.stop(state)

    def test_full_lifecycle_with_offline_cli_and_sql_clients(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = root / "key"
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                           check=True, capture_output=True)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            railway = bin_dir / "railway"
            railway.write_text(f"#!{sys.executable}\n" + '''
import json, os, subprocess, sys, time
from pathlib import Path
root = Path(os.environ['RUNNER_TEMP'])
if sys.argv[1:3] == ['variable', 'list']:
    print(json.dumps({'DATABASE_URL': 'postgresql://u:secret@db.railway.internal:5432/db'}))
else:
    assert '--tunnel-only' in sys.argv
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
    (root / 'child-pid').write_text(str(child.pid))
    print('CLI credential output must remain private', file=sys.stderr, flush=True)
    time.sleep(120)
''')
            railway.chmod(0o700)
            psql = bin_dir / "psql"
            psql.write_text(f"#!{sys.executable}\n" + '''
import os, sys
from pathlib import Path
assert sys.argv[sys.argv.index('--dbname') + 1] == 'postgresql://u@127.0.0.1:15432/db'
assert os.environ['PGPASSWORD'] == 'secret'
assert 'SELECT 1' in sys.argv
if not (Path(os.environ['RUNNER_TEMP']) / 'child-pid').exists():
    sys.exit(1)
print('1')
''')
            psql.chmod(0o700)
            env_file = root / "env"
            env_file.touch()
            env = {
                "RUNNER_TEMP": tmp, "GITHUB_ENV": str(env_file),
                "RAILWAY_API_TOKEN": "token", "RAILWAY_SSH_PRIVATE_KEY": key.read_text(),
                "RAILWAY_DB_PROJECT": "project-id", "RAILWAY_DB_ENVIRONMENT": "environment-id",
                "RAILWAY_DB_SERVICE": "Postgres", "RAILWAY_DB_PORT": "15432",
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            }
            state = None
            processes = []
            original_popen = subprocess.Popen

            def spawn(*args, **kwargs):
                process = original_popen(*args, **kwargs)
                processes.append(process)
                return process

            try:
                output = io.StringIO()
                with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(output), \
                        patch.object(tunnel.subprocess, "Popen", side_effect=spawn):
                    tunnel.start()
                exported = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
                state = Path(exported["RAILWAY_DB_TUNNEL_DIR"])
                self.assertEqual(exported["DATABASE_URL"], "postgresql://u:secret@127.0.0.1:15432/db")
                self.assertNotIn("CLI credential output", output.getvalue())
                self.assertEqual((state / "home/.ssh/id_ed25519").stat().st_mode & 0o777, 0o600)
                self.assertEqual((state / "tunnel.log").stat().st_mode & 0o777, 0o600)
                tunnel.stop(state)
                self.assertFalse(state.exists())
            finally:
                if state is None:
                    states = list(root.glob("briefvid-db-tunnel-*"))
                    state = states[0] if states else None
                if state is not None:
                    tunnel.stop(state)
                for process in processes:
                    process.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
