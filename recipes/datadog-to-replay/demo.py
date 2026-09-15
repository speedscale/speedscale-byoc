#!/usr/bin/env python3
"""Demonstrate a captured microsvc request with baseline, fault, and recovery replay."""

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import html
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import time
import urllib.parse
import urllib.request

IMAGE = "ghcr.io/speedscale/microsvc/api-gateway@sha256:4c9ace177d3935c90570a162a4cffce3d3d449c08d7530dfd815e1787b9bf7aa"
REDIS = "redis@sha256:ff02b58f971e7d7d156a1267e283fcbbeee91773b6aa36c49dac28ecfe28eadf"


def encode(value):
    return (
        base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def local_auth(record, secret):
    headers = record["http"]["req"].get("headers", {})
    for name, values in headers.items():
        if name.lower() != "authorization":
            continue
        if len(values) != 1 or not values[0].startswith("Bearer "):
            raise ValueError("Expected one Bearer JWT in the captured demo request")
        parts = values[0][7:].split(".")
        if len(parts) != 3:
            raise ValueError("Expected a JWT in the captured demo request")
        claims = json.loads(
            base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        )
        claims.update(iat=int(time.time()), exp=int(time.time()) + 3600)
        claims.pop("nbf", None)
        unsigned = encode({"alg": "HS256", "typ": "JWT"}) + "." + encode(claims)
        signature = (
            base64.urlsafe_b64encode(
                hmac.new(secret.encode(), unsigned.encode(), hashlib.sha256).digest()
            )
            .decode()
            .rstrip("=")
        )
        headers[name] = ["Bearer " + unsigned + "." + signature]
        return
    raise ValueError("Captured request lacks demo authentication")


def verdict(path, phase, exit_code):
    data = json.loads(path.read_text())
    pairs = data.get("pairs", [])
    expected_status = 503 if phase == "fault" else 200
    expected_exit = 1 if phase == "fault" else 0
    valid = exit_code == expected_exit and len(pairs) == 1
    valid = valid and all(
        p.get("recordedStatus") == 200
        and p.get("observedStatus") == expected_status
        and p.get("bodyMatch") == "pass"
        and p.get("match") == ("fail" if phase == "fault" else "pass")
        for p in pairs
    )
    return {
        "phase": phase,
        "expected_behavior": valid,
        "exit_code": exit_code,
        "recorded_status": pairs[0].get("recordedStatus") if pairs else None,
        "observed_status": pairs[0].get("observedStatus") if pairs else None,
        "body_match": pairs[0].get("bodyMatch") if pairs else None,
    }


def report(out, source, results, started):
    trace = source["trace_id"]
    query = "env:partner-demo service:api-gateway"
    stamp = datetime.fromisoformat(source["retrieved_at"])
    window = {
        "from_ts": int(stamp.timestamp() * 1000) - 3600000,
        "to_ts": int(stamp.timestamp() * 1000) + 300000,
    }
    links = {
        "APM trace": "https://app.datadoghq.com/apm/traces?"
        + urllib.parse.urlencode({"query": query + " trace_id:" + trace, **window}),
        "Capture logs": "https://app.datadoghq.com/logs?"
        + urllib.parse.urlencode(
            {"query": query + " @otel.trace_id:" + trace, **window}
        ),
    }
    summary = {
        "started_at": started,
        "source_trace": trace,
        "source_retrieved_at": source["retrieved_at"],
        "endpoint": "GET /api/accounts",
        "image": IMAGE,
        "auth": "JWT re-signed only in a local test copy with a fresh isolated gateway secret; recorded responses unchanged",
        "passthrough": False,
        "phases": results,
        "links": links,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    cards = "".join(
        f'<article><span>{html.escape(r["phase"].upper())}</span><h2>{r["observed_status"]}</h2><p>{"Unexpected result" if not r["expected_behavior"] else "Expected failure detected" if r["phase"] == "fault" else "Response matched"}</p><small>Replay exit {r["exit_code"]} · body {html.escape(str(r["body_match"]))}</small></article>'
        for r in results
    )
    anchors = "".join(
        f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{label} ↗</a>'
        for label, url in links.items()
    )
    title = (
        "Validated"
        if len(results) == 3 and all(r["expected_behavior"] for r in results)
        else "Incomplete validation"
    )
    page = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Speedscale + Datadog demo</title>
<style>body{{background:#101827;color:#eef3fa;font:18px system-ui;margin:0}}main{{max-width:1050px;margin:50px auto;padding:24px}}.eyebrow,small{{color:#b5c6d8}}h1{{font-size:48px;line-height:1.1;max-width:800px}}.flow{{padding:24px 0;color:#70e0c0;font-size:22px}}.cards{{display:flex;gap:18px;flex-wrap:wrap}}article{{background:#1c2a3b;padding:24px;flex:1;min-width:210px;border-radius:12px}}h2{{font-size:48px;margin:12px 0}}a{{display:inline-block;color:#91caff;margin:18px 22px 18px 0}}code{{overflow-wrap:anywhere}}.detail{{line-height:1.6;color:#b5c6d8}}footer{{margin-top:32px;font-size:14px}}</style>
<main><div class="eyebrow">SPEEDSCALE + DATADOG · {html.escape(title)}</div><h1>Turn one observed request into a regression test.</h1><p>GET /api/accounts · microsvc banking application</p><div class="flow">APM trace → Capture log → GCS recording → Test + dependency mock</div><div class="cards">{cards}</div>{anchors}<p class="detail">Select the dedicated Datadog partner account. Links show the original captured request; this local replay exports no telemetry.</p><p class="detail">The application SDK creates APM spans. Speedscale captures request and response payloads. The trace ID joins those records. The same expected response is used in every replay.</p><p class="detail">Dependency passthrough is disabled. Local Redis supports the gateway. Authentication is re-signed in a local copy for an isolated demo gateway. The local mock removes only a recognized DLP marker for an empty GET body. Original recordings and expected responses are unchanged.</p><footer>Run: {html.escape(started)}<br>Capture retrieved: {html.escape(source["retrieved_at"])}<br>Trace: <code>{html.escape(trace)}</code><br>One HTTP scenario, using the development capture harness. This report records an executed run; it is not a live Datadog dashboard.</footer></main></html>"""
    (out / "report.html").write_text(page)


def normalize_empty_get(record):
    req = record["http"]["req"]
    marker = {"$api_key": "REDACTED-UNRECOGNIZED-e3b0c44298fc1c149afb"}
    if req.get("method") != "GET" or not record.get("dlpModified"):
        return False
    try:
        body = json.loads(base64.b64decode(req.get("bodyBase64", "")))
    except (ValueError, UnicodeDecodeError):
        return False
    if body != marker:
        return False
    req.pop("bodyBase64", None)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--app-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--proxymock", default=str(Path.home() / ".speedscale/proxymock")
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Pause before each replay for a live walkthrough",
    )
    args = parser.parse_args()
    os.umask(0o077)
    app = args.app_dir.resolve()
    capture = args.capture.resolve()
    out = args.out.resolve()
    if not (app / "backend/api-gateway").is_dir():
        parser.error("--app-dir must be a microsvc application checkout")
    if not shutil.which("docker") or not shutil.which(args.proxymock):
        parser.error("Docker and proxymock must be installed")
    source = json.loads((capture / "provenance.json").read_text())
    tests = list((capture / "tests").glob("*.json"))
    mocks = list((capture / "mocks").glob("*.json"))
    if source.get("service") != "api-gateway" or len(tests) != 1 or len(mocks) != 1:
        parser.error(
            "Select a gateway trace with one incoming request and one outgoing HTTP capture"
        )
    incoming, outgoing = [json.loads(p.read_text()) for p in (tests[0], mocks[0])]
    for record in (incoming, outgoing):
        req = record["http"]["req"]
        if (
            req.get("method") != "GET"
            or req.get("uri") != "/api/accounts"
            or record["http"]["res"].get("statusCode") != 200
        ):
            parser.error("This demo requires a successful GET /api/accounts recording")
    if outgoing["http"]["req"].get("host") != "banking-accounts":
        parser.error("The recorded dependency must be banking-accounts")
    for port in (18080, 18082, 14140, 14141):
        with socket.socket() as sock:
            try:
                sock.bind(("0.0.0.0", port))
            except OSError:
                parser.error(f"Port {port} is occupied; stop its demo process first")
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    (out / "tests").mkdir()
    (out / "mocks").mkdir()
    normalized = normalize_empty_get(outgoing)
    (out / "mocks" / mocks[0].name).write_text(json.dumps(outgoing))
    if normalized:
        print("Local mock: removed the DLP marker for an empty GET body; archive unchanged.", flush=True)
    secret = secrets.token_urlsafe(48)
    local_auth(incoming, secret)
    (out / "tests" / tests[0].name).write_text(json.dumps(incoming))
    name = "dd-replay-" + secrets.token_hex(4)
    processes, resources, results = [], [], []
    started = datetime.now(timezone.utc).isoformat()
    log = (out / "setup.log").open("w")

    def run(command, **kwargs):
        return subprocess.run(
            command,
            cwd=app,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=120,
            **kwargs,
        )

    def wait_http(url, process=None):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if process and process.poll() is not None:
                raise RuntimeError("Mock process exited before readiness")
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    if response.status == 200:
                        return
            except OSError:
                time.sleep(0.5)
        raise RuntimeError("Demo did not become ready: " + url)

    try:
        print("Starting isolated microsvc gateway and Redis...", flush=True)
        run(["docker", "network", "create", name])
        resources.append(["docker", "network", "rm", name])
        run(
            ["docker", "run", "-d", "--name", name + "-redis", "--network", name, REDIS]
        )
        resources.append(["docker", "rm", "-f", name + "-redis"])
        env = os.environ.copy()
        env["JWT_SECRET"] = secret
        run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name + "-gateway",
                "--network",
                name,
                "--add-host",
                "banking-accounts:host-gateway",
                "-p",
                "127.0.0.1:18080:8080",
                "-e",
                "JWT_SECRET",
                "-e",
                "REDIS_HOST=" + name + "-redis",
                "-e",
                "ACCOUNTS_SERVICE_URL=http://banking-accounts:18082",
                "-e",
                "OTEL_SDK_DISABLED=true",
                "-e",
                "OTEL_TRACES_EXPORTER=none",
                "-e",
                "OTEL_METRICS_EXPORTER=none",
                "-e",
                "OTEL_LOGS_EXPORTER=none",
                "-e",
                "LOGGING_LEVEL_ORG_SPRINGFRAMEWORK_CLOUD_GATEWAY=INFO",
                "--entrypoint",
                "/bin/sh",
                IMAGE,
                "-c",
                "exec java -Xms64m -Xmx128m -XX:MaxMetaspaceSize=128m -XX:+UseSerialGC org.springframework.boot.loader.launch.JarLauncher",
            ],
            env=env,
        )
        resources.append(["docker", "rm", "-f", name + "-gateway"])
        wait_http("http://localhost:18080/actuator/info")
        for phase in ("baseline", "fault", "recovery"):
            if args.interactive:
                input(
                    f"Press Enter for {phase}: "
                    + (
                        "inject dependency HTTP 503"
                        if phase == "fault"
                        else "use recorded dependency response"
                    )
                    + "... "
                )
            command = [
                args.proxymock,
                "mock",
                "--in",
                str(out / "mocks"),
                "--out",
                str(out / ("mock-" + phase)),
                "--no-passthrough",
                "--map",
                "18082=http://banking-accounts:80",
                "--proxy-out-port",
                "14140",
                "--health-port",
                "14141",
                "--log-to",
                str(out / ("mock-" + phase + ".log")),
            ]
            if phase == "fault":
                command.extend(["--chaos", "*:status=503"])
            with (out / (phase + ".console")).open("w") as console:
                mock = subprocess.Popen(
                    command, cwd=app, stdout=console, stderr=subprocess.STDOUT
                )
                processes.append(mock)
                wait_http("http://localhost:14141/", mock)
                replay_dir = out / ("replay-" + phase)
                replay = subprocess.run(
                    [
                        args.proxymock,
                        "replay",
                        "--in",
                        str(out / "tests"),
                        "--test-against",
                        "http://localhost:18080",
                        "--out",
                        str(replay_dir),
                        "--fail-if",
                        "requests.result-match-pct != 100",
                        "--log-to",
                        str(out / ("replay-" + phase + ".log")),
                    ],
                    cwd=app,
                    stdout=console,
                    stderr=subprocess.STDOUT,
                    timeout=90,
                )
                result = verdict(
                    replay_dir / "replay-verdict.json", phase, replay.returncode
                )
                results.append(result)
                mock.terminate()
                mock.wait(timeout=10)
                print(
                    f"{phase.upper()}: HTTP {result['observed_status']}, body {result['body_match']}, replay exit {result['exit_code']}"
                    + (
                        " (expected)"
                        if result["expected_behavior"]
                        else " (UNEXPECTED)"
                    ),
                    flush=True,
                )
                report(out, source, results, started)
                if not result["expected_behavior"]:
                    raise RuntimeError(
                        "Unexpected replay outcome; inspect the local verdict and logs"
                    )
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
        for command in reversed(resources):
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=60)
        log.close()
    print("Demo complete. Open " + str(out / "report.html"), flush=True)


if __name__ == "__main__":
    main()
