import argparse
import json
import os
import re
import sys
import time


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMP_DIR = os.path.join(PROJECT_ROOT, "temp")

DEFAULT_HEALTH_FILE = os.path.join(TEMP_DIR, "fsapp-GA02.health.json")
DEFAULT_LOCK_FILE = os.path.join(TEMP_DIR, "fsapp-GA02.lock")
DEFAULT_PID_FILE = os.path.join(TEMP_DIR, "fsapp-GA02.pid")
DEFAULT_OUT_LOG = os.path.join(TEMP_DIR, "fsapp-GA02.out.log")
DEFAULT_ERR_LOG = os.path.join(TEMP_DIR, "fsapp-GA02.err.log")

SECRET_PATTERNS = (
    re.compile(r"(?i)sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|secret|token|access_key)(['\"\s:=]+)([^'\"\s,}]+)"),
)
ERROR_PATTERNS = (
    re.compile(r"Traceback \(most recent call last\):"),
    re.compile(r"(?i)\b(ERROR|Exception|RuntimeError|Backend Error)\b"),
)


def _read_text(path, max_bytes=None):
    if not path or not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        if max_bytes is not None:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes), os.SEEK_SET)
        data = f.read(max_bytes or -1)
    return data.decode("utf-8", errors="replace")


def _read_int_file(path):
    text = _read_text(path)
    if text is None:
        return None
    try:
        return int(text.strip())
    except (TypeError, ValueError):
        return None


def _pid_is_running(pid):
    if not pid:
        return False
    if os.name == "nt":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def _has_pattern(text, patterns):
    text = text or ""
    return any(p.search(text) for p in patterns)


def check_health(
    health_file=DEFAULT_HEALTH_FILE,
    lock_file=DEFAULT_LOCK_FILE,
    pid_file=DEFAULT_PID_FILE,
    out_log=DEFAULT_OUT_LOG,
    err_log=DEFAULT_ERR_LOG,
    max_age_sec=300,
    log_tail_bytes=200000,
):
    checks = []
    problems = []
    data = {}

    health_raw = _read_text(health_file)
    if health_raw is None:
        problems.append(f"missing health file: {health_file}")
    else:
        try:
            data = json.loads(health_raw)
            checks.append("health_json_valid")
        except json.JSONDecodeError as e:
            problems.append(f"invalid health json: {e}")

    health_pid = data.get("pid")
    lock_pid = _read_int_file(lock_file)
    pid_file_pid = _read_int_file(pid_file)
    if health_pid:
        checks.append("health_pid_present")
    else:
        problems.append("health pid missing")

    if lock_pid is None:
        problems.append(f"missing or invalid lock pid: {lock_file}")
    if pid_file_pid is None:
        problems.append(f"missing or invalid pid file: {pid_file}")
    if lock_pid is not None and pid_file_pid is not None and lock_pid != pid_file_pid:
        problems.append(f"pid mismatch: lock={lock_pid} pid_file={pid_file_pid}")
    if health_pid and lock_pid is not None and int(health_pid) != int(lock_pid):
        problems.append(f"pid mismatch: health={health_pid} lock={lock_pid}")

    running_pid = lock_pid or pid_file_pid or health_pid
    if _pid_is_running(running_pid):
        checks.append("pid_running")
    else:
        problems.append(f"pid not running: {running_pid}")

    health_time = data.get("time")
    health_age_sec = None
    if health_time:
        try:
            parsed = time.strptime(health_time, "%Y-%m-%dT%H:%M:%S%z")
            health_epoch = time.mktime(parsed)
            health_age_sec = max(0, int(time.time() - health_epoch))
            if health_age_sec <= max_age_sec:
                checks.append("health_fresh")
            else:
                problems.append(f"stale health: {health_age_sec}s > {max_age_sec}s")
        except ValueError:
            problems.append(f"invalid health time: {health_time}")
    else:
        problems.append("health time missing")

    log_text = "\n".join(
        x
        for x in (
            _read_text(out_log, max_bytes=log_tail_bytes),
            _read_text(err_log, max_bytes=log_tail_bytes),
        )
        if x is not None
    )
    if _has_pattern(log_text, SECRET_PATTERNS):
        problems.append("secret-like text found in fsapp logs")
    else:
        checks.append("logs_no_secret_patterns")
    if _has_pattern(log_text, ERROR_PATTERNS):
        problems.append("error-like text found in fsapp logs")
    else:
        checks.append("logs_no_error_patterns")

    status = "ok" if not problems else "fail"
    return {
        "status": status,
        "checks": checks,
        "problems": problems,
        "health": data,
        "pid": running_pid,
        "health_age_sec": health_age_sec,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check GA02 Feishu daemon health files and logs.")
    parser.add_argument("--health-file", default=DEFAULT_HEALTH_FILE)
    parser.add_argument("--lock-file", default=DEFAULT_LOCK_FILE)
    parser.add_argument("--pid-file", default=DEFAULT_PID_FILE)
    parser.add_argument("--out-log", default=DEFAULT_OUT_LOG)
    parser.add_argument("--err-log", default=DEFAULT_ERR_LOG)
    parser.add_argument("--max-age-sec", type=int, default=300)
    args = parser.parse_args(argv)

    result = check_health(
        health_file=args.health_file,
        lock_file=args.lock_file,
        pid_file=args.pid_file,
        out_log=args.out_log,
        err_log=args.err_log,
        max_age_sec=args.max_age_sec,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
