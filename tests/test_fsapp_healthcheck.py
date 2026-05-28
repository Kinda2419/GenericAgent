import json
import os
import tempfile
import time
import unittest

from tools import fsapp_healthcheck


class FsappHealthcheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.health_file = os.path.join(self.tmp.name, "health.json")
        self.lock_file = os.path.join(self.tmp.name, "fsapp.lock")
        self.pid_file = os.path.join(self.tmp.name, "fsapp.pid")
        self.out_log = os.path.join(self.tmp.name, "out.log")
        self.err_log = os.path.join(self.tmp.name, "err.log")
        self.pid = os.getpid()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_state(self, *, pid=None, status="waiting", out_log="", err_log=""):
        pid = self.pid if pid is None else pid
        data = {
            "status": status,
            "pid": pid,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        with open(self.health_file, "w", encoding="utf-8") as f:
            json.dump(data, f)
        with open(self.lock_file, "w", encoding="utf-8") as f:
            f.write(str(pid))
        with open(self.pid_file, "w", encoding="utf-8") as f:
            f.write(str(pid))
        with open(self.out_log, "w", encoding="utf-8") as f:
            f.write(out_log)
        with open(self.err_log, "w", encoding="utf-8") as f:
            f.write(err_log)

    def _check(self, **kwargs):
        return fsapp_healthcheck.check_health(
            health_file=self.health_file,
            lock_file=self.lock_file,
            pid_file=self.pid_file,
            out_log=self.out_log,
            err_log=self.err_log,
            max_age_sec=300,
            **kwargs,
        )

    def test_ok_when_pid_files_match_running_process_and_logs_clean(self):
        self._write_state(out_log="waiting for messages\n")

        result = self._check()

        self.assertEqual(result["status"], "ok")
        self.assertIn("pid_running", result["checks"])
        self.assertIn("logs_no_secret_patterns", result["checks"])

    def test_fails_on_pid_mismatch(self):
        self._write_state()
        with open(self.lock_file, "w", encoding="utf-8") as f:
            f.write(str(self.pid + 100000))

        result = self._check()

        self.assertEqual(result["status"], "fail")
        self.assertTrue(any("pid mismatch" in p for p in result["problems"]))

    def test_fails_on_secret_like_log_text(self):
        self._write_state(out_log="run --api-key abc123\n")

        result = self._check()

        self.assertEqual(result["status"], "fail")
        self.assertIn("secret-like text found in fsapp logs", result["problems"])

    def test_fails_on_stale_health(self):
        self._write_state()
        stale = {
            "status": "waiting",
            "pid": self.pid,
            "time": "2000-01-01T00:00:00+0800",
        }
        with open(self.health_file, "w", encoding="utf-8") as f:
            json.dump(stale, f)

        result = self._check()

        self.assertEqual(result["status"], "fail")
        self.assertTrue(any("stale health" in p for p in result["problems"]))


if __name__ == "__main__":
    unittest.main()
