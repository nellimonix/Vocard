import importlib.util
import pathlib
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location("tunnel_health", pathlib.Path(__file__).resolve().parents[1] / "ops" / "tunnel_health.py")
health = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"fcntl": Mock()}):
    spec.loader.exec_module(health)


class TunnelHealthTests(unittest.TestCase):
    def execute(self, *, api=200, storage=410, handshake=0, restart=0):
        def call(args, **kwargs):
            if args[0] == "curl":
                status = api if args[-1] == health.ENDPOINTS["api"][0] else storage
                return SimpleNamespace(returncode=0 if status else 28, stdout=str(status))
            if args[0] == "wg":
                return SimpleNamespace(returncode=0, stdout=f"peer\t{handshake}\n")
            return SimpleNamespace(returncode=restart, stdout="")
        return Mock(side_effect=call)

    def test_healthy_tunnel_resets_failures_without_restart(self):
        execute = self.execute()
        report = health.check({"failures": 3, "last_restart": 100}, 1000, execute=execute)
        self.assertTrue(report["healthy"])
        self.assertEqual(report["state"], {"failures": 0, "last_restart": 100})
        self.assertFalse(report["restart_attempted"])
        self.assertEqual(execute.call_count, 2)

    def test_single_timeout_does_not_restart_tunnel(self):
        report = health.check({}, 1000, execute=self.execute(api=0))
        self.assertFalse(report["healthy"])
        self.assertEqual(report["state"]["failures"], 1)
        self.assertFalse(report["restart_attempted"])

    def test_repeated_failure_with_stale_handshake_restarts(self):
        execute = self.execute(api=0, handshake=800)
        report = health.check({"failures": 2}, 1000, execute=execute)
        self.assertTrue(report["restart_attempted"])
        self.assertTrue(report["restart_succeeded"])
        self.assertFalse(report["healthy"])
        self.assertEqual(report["state"]["last_restart"], 1000)

    def test_live_peer_is_not_restarted_for_upstream_failure(self):
        report = health.check({"failures": 3}, 1000, execute=self.execute(api=503, handshake=950))
        self.assertFalse(report["healthy"])
        self.assertFalse(report["restart_attempted"])

    def test_restart_cooldown_prevents_repeated_restarts(self):
        report = health.check({"failures": 3, "last_restart": 900}, 1000, execute=self.execute(storage=0))
        self.assertFalse(report["restart_attempted"])
        self.assertEqual(report["state"]["failures"], 3)

    def test_restart_failure_is_recorded_and_rate_limited(self):
        report = health.check({"failures": 3}, 1000, execute=self.execute(api=0, restart=1))
        self.assertFalse(report["restart_succeeded"])
        self.assertEqual(report["state"]["last_restart"], 1000)
        self.assertFalse(report["healthy"])

    def test_checks_recover_after_restart(self):
        execute = self.execute(api=0)
        calls = 0
        original = execute.side_effect
        def call(args, **kwargs):
            nonlocal calls
            if args[0] == "systemctl":
                calls += 1
            if calls and args[0] == "curl":
                return SimpleNamespace(returncode=0, stdout="200")
            return original(args, **kwargs)
        execute.side_effect = call
        report = health.check({"failures": 2}, 1000, execute=execute)
        self.assertTrue(report["healthy"])
        self.assertEqual(report["state"]["failures"], 0)

    def test_missing_interface_can_be_restarted(self):
        execute = self.execute(api=0)
        original = execute.side_effect
        def call(args, **kwargs):
            if args[0] == "wg":
                return SimpleNamespace(returncode=1, stdout="")
            return original(args, **kwargs)
        execute.side_effect = call
        report = health.check({"failures": 3}, 1000, execute=execute)
        self.assertIsNone(report["handshake_age"])
        self.assertTrue(report["restart_attempted"])

    def test_probe_errors_are_unhealthy(self):
        for error in [FileNotFoundError(), subprocess.TimeoutExpired("curl", 10)]:
            with self.subTest(error=type(error).__name__):
                self.assertEqual(health.probe("wg0", Mock(side_effect=error)), {"api": False, "storage": False})

    def test_clock_rollback_does_not_block_recovery(self):
        report = health.check({"failures": 3, "last_restart": 2000}, 1000, execute=self.execute(api=0))
        self.assertTrue(report["restart_attempted"])
