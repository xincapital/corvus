"""Runner-local Railway SSH tunnel lifecycle; never print CLI credential output."""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit


def mask(value):
    escaped = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::add-mask::{escaped}", flush=True)


def local_database_url(raw, port):
    try:
        parsed = urlsplit(raw)
        if parsed.scheme not in ("postgres", "postgresql") or not parsed.hostname or not parsed.username:
            raise ValueError()
        # Validate the original port without including the URL in an exception.
        _ = parsed.port
        credentials = parsed.netloc.rsplit("@", 1)[0]
        query = urlencode([
            (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key not in ("host", "hostaddr", "port")
        ])
        return urlunsplit((parsed.scheme, f"{credentials}@127.0.0.1:{port}", parsed.path, query, ""))
    except (ValueError, TypeError):
        raise RuntimeError("Postgres service DATABASE_URL is missing or invalid") from None


def run_private(command, env):
    try:
        result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{command[0]} timed out; check Railway access and connectivity") from None
    if result.returncode:
        raise RuntimeError(f"{command[0]} failed; check API token permissions and the unencrypted SSH key")
    return result.stdout


def wait_for_database(process, database_url):
    deadline = time.monotonic() + 60
    parsed = urlsplit(database_url)
    password = unquote(parsed.password or "")
    query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key == "password":
            password = value
        else:
            query.append((key, value))
    client_url = urlunsplit((parsed.scheme, f"{parsed.username}@{parsed.hostname}:{parsed.port}",
                            parsed.path, urlencode(query), ""))
    env = {**os.environ, "PGPASSWORD": password, "PGCONNECT_TIMEOUT": "5"}
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Railway tunnel exited; check API token access and SSH key registration")
        try:
            # PGDATABASE alone does not expand a URI into connection options.
            # Pass the password-free URI explicitly; keep credentials in env.
            result = subprocess.run(["psql", "-X", "--no-password", "--dbname", client_url,
                                     "-At", "-c", "SELECT 1"],
                                    env=env, capture_output=True, text=True, timeout=8)
            if result.returncode == 0 and result.stdout.strip() == "1" and process.poll() is None:
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(1)
    raise RuntimeError("Timed out verifying SQL through the tunnel; check Postgres health and connection settings")


def stop(state=None):
    if state is None:
        directory = os.environ.get("RAILWAY_DB_TUNNEL_DIR")
        if not directory:
            return
        state = Path(directory)
    if not state.exists():
        return
    pid_file = state / "pid"
    if pid_file.exists():
        pid = int(pid_file.read_text())
        # Railway's native ssh child uses a separate process group. Kill it
        # explicitly before its parent so cancelling a job cannot orphan it.
        subprocess.run(["pkill", "-TERM", "-P", str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    shutil.rmtree(state)


def start():
    for name in ("RAILWAY_API_TOKEN", "RAILWAY_SSH_PRIVATE_KEY"):
        if not os.environ.get(name):
            raise RuntimeError(f"Set the {name} GitHub secret before running this workflow")
    port = int(os.environ["RAILWAY_DB_PORT"])
    if not 1024 <= port <= 65535:
        raise RuntimeError("Tunnel port must be between 1024 and 65535")
    state = Path(tempfile.mkdtemp(prefix="briefvid-db-tunnel-", dir=os.environ["RUNNER_TEMP"]))
    # Export cleanup state before any fallible setup so always() can clean up.
    with open(os.environ["GITHUB_ENV"], "a") as handle:
        handle.write(f"RAILWAY_DB_TUNNEL_DIR={state}\n")
    process = None
    try:
        # Isolate the CI key from any pre-existing runner SSH keys/configuration.
        home = state / "home"
        ssh_dir = home / ".ssh"
        ssh_dir.mkdir(parents=True, mode=0o700)
        key = ssh_dir / "id_ed25519"
        key.write_text(os.environ["RAILWAY_SSH_PRIVATE_KEY"].strip() + "\n")
        key.chmod(0o600)
        env = {**os.environ, "HOME": str(home)}
        # A project-access token takes precedence over the API token in the CLI.
        env.pop("RAILWAY_TOKEN", None)
        env.pop("RAILWAY_SSH_PRIVATE_KEY", None)
        public_key = run_private(["ssh-keygen", "-y", "-P", "", "-f", str(key)], env)
        key.with_suffix(".pub").write_text(public_key)
        scope = ["--project", os.environ["RAILWAY_DB_PROJECT"],
                 "--environment", os.environ["RAILWAY_DB_ENVIRONMENT"]]
        raw_variables = run_private(["railway", "variable", "list", "--service",
                                     os.environ["RAILWAY_DB_SERVICE"], *scope, "--json"], env)
        try:
            raw_url = json.loads(raw_variables)["DATABASE_URL"]
        except (ValueError, KeyError, TypeError):
            raise RuntimeError("Could not read Postgres service DATABASE_URL from Railway") from None
        database_url = local_database_url(raw_url, port)
        for value in (raw_url, database_url, urlsplit(raw_url).password):
            if value:
                mask(value)
                mask(unquote(value))
        # --tunnel-only prints passwords; retain output only in a private temp
        # directory, never echo it or upload it as a workflow artifact.
        with open(state / "tunnel.log", "w") as log:
            os.chmod(log.name, 0o600)
            process = subprocess.Popen(["railway", "connect", os.environ["RAILWAY_DB_SERVICE"],
                                        *scope, "--tunnel-only", "--port", str(port)],
                                       env=env, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=log, start_new_session=True)
        (state / "pid").write_text(str(process.pid))
        wait_for_database(process, database_url)
        with open(os.environ["GITHUB_ENV"], "a") as handle:
            handle.write(f"DATABASE_URL={database_url}\n")
        print(f"Postgres SQL verified through SSH at 127.0.0.1:{port}")
    except BaseException:
        stop(state)
        if process is not None:
            process.wait(timeout=10)
        raise


if __name__ == "__main__":
    try:
        if sys.argv[1] == "start":
            start()
        elif sys.argv[1] == "stop":
            stop()
        else:
            raise RuntimeError("Operation must be start or stop")
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        # No traceback or captured CLI output: both can contain credentials.
        print(f"::error::{error}")
        sys.exit(1)
