"""Local-only real-process Docker check with a synthetic HTTP upstream.

Requires an already-built poe-monitor:review-local image. Uses a fresh isolated
project, an internal Docker network, synthetic keys, and no production mounts.
Leaves evidence under output/stack and cleans up its own containers/volumes.
"""

import json
from pathlib import Path
import subprocess
import time
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output" / "stack"
PROJECT = "poe-review-20261015"
IMAGE = "poe-monitor:review-local"
COMPOSE = ["docker", "compose", "-p", PROJECT, "-f", str(OUT / "compose.json")]
URL = "http://127.0.0.1:15000"


def command(args, **kwargs):
    return subprocess.check_output(args, cwd=ROOT, text=True, encoding="utf8", **kwargs)


def request(path, data=None, token=None, method=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    encoded = json.dumps(data).encode() if data is not None else None
    # Docker Desktop does not publish ports on an internal-only network.
    # Drive the real Waitress HTTP listener from inside its container instead.
    code = (
        "import json,urllib.request,urllib.error\n"
        f"r=urllib.request.Request({'http://127.0.0.1:5000' + path!r},data={encoded!r},headers={headers!r},method={method!r})\n"
        "try:\n"
        " with urllib.request.urlopen(r,timeout=5) as x: result=[x.status,json.load(x)]\n"
        "except urllib.error.HTTPError as e: result=[e.code,json.load(e)]\n"
        "print(json.dumps(result))\n"
    )
    return json.loads(exec_python(code))


def wait_for(predicate, seconds=45):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, urllib.error.URLError, ValueError):
            pass
        time.sleep(0.5)
    raise AssertionError("Local integration condition did not become true")


def exec_python(code):
    return command(COMPOSE + ["exec", "-T", "poe", "python", "-c", code])


def check_host_loopback(environment):
    # Trading stays on an internal network. A separate API-only liveness probe
    # verifies normal bridge publishing without any worker or upstream calls.
    args = [
        "docker",
        "run",
        "-d",
        "--read-only",
        "--tmpfs",
        "/tmp",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--label",
        "com.docker.compose.project=" + PROJECT,
        "-p",
        "127.0.0.1::5000",
    ]
    values = {**environment, "DATABASE_URL": "sqlite:///:memory:"}
    for key, value in values.items():
        args += ["-e", f"{key}={value}"]
    cid = command(args + [IMAGE, "python", "main.py"]).strip()
    try:
        inspected = json.loads(command(["docker", "inspect", cid]))[0]
        port = inspected["NetworkSettings"]["Ports"]["5000/tcp"][0]
        assert port["HostIp"] == "127.0.0.1"

        def live():
            with urllib.request.urlopen(
                "http://127.0.0.1:" + port["HostPort"] + "/healthz", timeout=3
            ) as response:
                return response.status == 200

        wait_for(live, 15)
    finally:
        command(["docker", "rm", "-f", cid])


def main():
    context = json.loads(command(["docker", "context", "inspect"]))[0]
    endpoint = context["Endpoints"]["docker"]["Host"]
    assert endpoint.startswith(("npipe://", "unix://")), "Refusing a remote Docker endpoint"
    assert not command(
        ["docker", "ps", "-a", "-q", "--filter", "label=com.docker.compose.project=" + PROJECT]
    ).strip(), "Test project already exists; inspect it before cleanup/reuse"
    OUT.mkdir(parents=True, exist_ok=True)
    environment = {
        "DATABASE_URL": "mysql+pymysql://poe:synthetic-db-password@mysql:3306/poe_monitor?charset=utf8mb4",
        "SECRET_KEY": "a" * 64,
        "JWT_SECRET_KEY": "b" * 64,
        "POESESSID_ENCRYPTION_KEY": "c" * 64,
        "API_BIND": "0.0.0.0",
        "LOG_DIR": "",
        "POE_API_BASE_URL": "http://upstream:18000",
        "POE_REQUEST_INTERVAL": "0.02",
    }
    # Start from the actual shipped Compose model, then isolate only its I/O.
    config = json.loads(
        command(
            [
                "docker",
                "compose",
                "-f",
                str(ROOT / "backend/docker-compose.yml"),
                "config",
                "--no-env-resolution",
                "--no-interpolate",
                "--format",
                "json",
            ]
        )
    )
    config["name"] = PROJECT
    for key in list(config):
        if key.startswith("x-"):
            del config[key]
    config["networks"] = {"poe-net": {"internal": True}}
    config.pop("volumes", None)
    for role in ["poe", "worker", "migrate"]:
        service = config["services"][role]
        service.pop("build", None)
        service.pop("env_file", None)
        service.update(image=IMAGE, environment={**service["environment"], **environment})
    config["services"]["poe"]["ports"] = ["127.0.0.1:15000:5000"]
    mysql = config["services"]["mysql"]
    mysql.update(
        image="mysql:8.0.45",
        environment={
            "MYSQL_ROOT_PASSWORD": "synthetic-root-password",
            "MYSQL_DATABASE": "poe_monitor",
            "MYSQL_USER": "poe",
            "MYSQL_PASSWORD": "synthetic-db-password",
        },
        tmpfs=["/var/lib/mysql"],
    )
    mysql.pop("volumes", None)
    config["services"]["upstream"] = {
        "image": IMAGE,
        "command": ["python", "/fixtures/upstream.py"],
        "networks": ["poe-net"],
        "read_only": True,
        "volumes": [
            {
                "type": "bind",
                "source": str(ROOT / "backend/tests/upstream_fixture.py"),
                "target": "/fixtures/upstream.py",
                "read_only": True,
            }
        ],
    }
    (OUT / "compose.json").write_text(json.dumps(config, indent=2), encoding="utf8")
    result = {}
    try:
        command(COMPOSE + ["up", "-d"], stderr=subprocess.STDOUT)
        wait_for(lambda: request("/readyz")[0] == 200)
        exec_python(
            "from app import create_app,db;from app.models import User,UserCredential;from app.utils import encrypt_poesessid;from werkzeug.security import generate_password_hash;a=create_app();c=a.app_context();c.push();u=User(username='stack-user',password_hash=generate_password_hash('stack-test-password'),is_admin=True);db.session.add(u);db.session.flush();db.session.add(UserCredential(user_id=u.id,encrypted_poesessid=encrypt_poesessid('SYNTHETIC-ONLY')));db.session.commit()"
        )
        status, login = request(
            "/auth/login", {"username": "stack-user", "password": "stack-test-password"}
        )
        assert status == 200
        token = login["access_token"]
        status, created = request(
            "/api/targets", {"poe_account_name": "fixture-account", "scan_interval": 60}, token
        )
        assert status == 201
        tid = created["id"]
        wait_for(
            lambda: any(
                t["id"] == tid and t["last_scan"] for t in request("/api/targets", token=token)[1]
            )
        )
        events = request(f"/api/targets/{tid}/events", token=token)[1]
        assert len(events) == 2 and all(e["event_type"] == "listed" for e in events)
        result["initial_same_fingerprint_instances"] = len(events)
        exec_python(
            "import urllib.request;urllib.request.urlopen(urllib.request.Request('http://upstream:18000/fixture/phase',data=b'2')).read()"
        )
        assert request(f"/api/targets/{tid}/trigger", {}, token)[0] == 200
        assert request(f"/api/targets/{tid}/trigger", {}, token)[0] == 429
        change = wait_for(
            lambda: next(
                (
                    e
                    for e in request(f"/api/targets/{tid}/events", token=token)[1]
                    if e["event_type"] == "price_change"
                ),
                None,
            )
        )
        assert (
            change["item_id"] == "fixture-A"
            and change["price"] == 12.125
            and change["price_old"] == 10
        )
        result["price_change"] = dict(
            item_id=change["item_id"], price=change["price"], price_old=change["price_old"]
        )
        command(COMPOSE + ["stop", "worker"], stderr=subprocess.STDOUT)
        status, second = request(
            "/api/targets", {"poe_account_name": "second-account", "scan_interval": 60}, token
        )
        assert status == 201
        command(COMPOSE + ["start", "worker"], stderr=subprocess.STDOUT)
        wait_for(
            lambda: any(
                t["id"] == second["id"] and t["last_scan"]
                for t in request("/api/targets", token=token)[1]
            )
        )
        assert len(request(f"/api/targets/{second['id']}/events", token=token)[1]) == 2
        result["pending_job_survives_worker_restart"] = True
        ids = command(COMPOSE + ["ps", "-q", "poe"]).strip()
        inspected = json.loads(command(["docker", "inspect", ids]))[0]
        assert inspected["Config"]["User"] == "10001:10001"
        assert inspected["HostConfig"]["ReadonlyRootfs"]
        assert inspected["HostConfig"]["PortBindings"]["5000/tcp"][0]["HostIp"] == "127.0.0.1"
        check_host_loopback(environment)
        assert (
            exec_python("from pathlib import Path; print(Path('/app/.env').exists())").strip()
            == "False"
        )
        result.update(
            non_root=True,
            read_only_root=True,
            loopback_port=True,
            no_env_in_image=True,
            external_network_disabled_for_trading_stack=True,
            host_loopback_http_verified=True,
        )
        assert (
            request("/auth/logout", {}, token)[0] == 200
            and request("/api/user", token=token)[0] == 401
        )
        result["logout_token_revoked"] = True
        (OUT / "result.json").write_text(json.dumps(result, indent=2), encoding="utf8")
        print(json.dumps(result, indent=2))
    finally:
        try:
            (OUT / "logs.txt").write_text(
                command(COMPOSE + ["logs", "--no-color"]), encoding="utf8"
            )
        finally:
            command(COMPOSE + ["down", "--volumes"], stderr=subprocess.STDOUT)


if __name__ == "__main__":
    main()
