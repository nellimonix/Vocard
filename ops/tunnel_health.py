import fcntl
import json
import pathlib
import subprocess
import time


ENDPOINTS = {
    "api": ("https://api.music.yandex.net/account/status", {200}),
    "storage": ("https://storage.mds.yandex.net/", {200, 404, 410}),
}
STATE_PATH = pathlib.Path("/var/lib/yamusic-tunnel-health/state.json")


def run(args, timeout=10):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def probe(interface, execute=run):
    checks = {}
    for name, (url, accepted) in ENDPOINTS.items():
        try:
            result = execute([
                "curl", "--interface", interface, "--ipv4", "--silent",
                "--output", "/dev/null", "--write-out", "%{http_code}",
                "--connect-timeout", "3", "--max-time", "5", url,
            ])
            checks[name] = result.returncode == 0 and int(result.stdout.strip()) in accepted
        except (OSError, ValueError, subprocess.TimeoutExpired):
            checks[name] = False
    return checks


def handshake_age(interface, now, execute=run):
    try:
        result = execute(["wg", "show", interface, "latest-handshakes"])
        if result.returncode:
            return None
        stamps = [int(line.split()[1]) for line in result.stdout.splitlines()]
        if not stamps or not all(stamps):
            return None
        return max(max(0, now - stamp) for stamp in stamps)
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def check(state, now, interface="wg0", execute=run):
    checks = probe(interface, execute)
    state = {
        "failures": min(3, max(0, int(state.get("failures", 0)))),
        "last_restart": float(state.get("last_restart", 0)),
    }
    report = {"checks": checks, "restart_attempted": False}
    if all(checks.values()):
        state["failures"] = 0
    else:
        state["failures"] = min(3, state["failures"] + 1)
        age = handshake_age(interface, now, execute)
        report["handshake_age"] = age
        elapsed = now - state["last_restart"]
        if state["failures"] >= 3 and (elapsed >= 300 or elapsed < 0) and (age is None or age >= 180):
            report["restart_attempted"] = True
            state["last_restart"] = now
            try:
                result = execute(["systemctl", "restart", f"wg-quick@{interface}.service"], timeout=20)
                report["restart_succeeded"] = result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                report["restart_succeeded"] = False
            if report["restart_succeeded"]:
                checks = probe(interface, execute)
                report["checks"] = checks
                if all(checks.values()):
                    state["failures"] = 0
    report["healthy"] = all(checks.values())
    report["state"] = state
    return report


def main():
    STATE_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with STATE_PATH.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        try:
            state = json.loads(STATE_PATH.read_text())
            if not isinstance(state, dict):
                state = {}
            state = {"failures": int(state.get("failures", 0)), "last_restart": float(state.get("last_restart", 0))}
        except (OSError, ValueError, TypeError, OverflowError):
            state = {}
        report = check(state, time.time())
        temporary = STATE_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(report["state"]) + "\n")
        temporary.chmod(0o600)
        temporary.replace(STATE_PATH)
        print(json.dumps(report, sort_keys=True), flush=True)
        return 0 if report["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
