import os
import unittest
from unittest.mock import patch

from jev_git_graph.errors import JgError
from jev_git_graph.process_identity import native_process_generation, receipt_generation_matches


class ProcessIdentityTests(unittest.TestCase):
    def test_linux_reads_boot_and_ticks_even_with_spaces_and_parentheses_in_name(self):
        boot = "12345678-1234-1234-1234-123456789abc"
        fields = ["S"] + ["0"] * 18 + ["98765"]
        with patch("jev_git_graph.process_identity.platform.system", return_value="Linux"), \
                patch("jev_git_graph.process_identity.Path.read_text", side_effect=[
                    boot, "123 (worker (test)) " + " ".join(fields)]):
            self.assertEqual(f"linux:{boot}:98765", native_process_generation(123))

    def test_linux_malformed_or_missing_process_fails_closed(self):
        for values in (["bad-boot", "123 (x) S"], ["a" * 36, "wrong"], OSError()):
            with self.subTest(values=values), \
                    patch("jev_git_graph.process_identity.platform.system", return_value="Linux"), \
                    patch("jev_git_graph.process_identity.Path.read_text", side_effect=values):
                with self.assertRaises(JgError):
                    native_process_generation(123)

    def test_darwin_validates_kernel_size_pid_and_time(self):
        class Query:
            def __init__(self, fault):
                self.fault = fault
            def __call__(self, pid, flavor, arg, pointer, size):
                self_tuple = (flavor, arg)
                assert self_tuple == (3, 0)
                info = pointer._obj
                info.pid = pid + (self.fault == "pid")
                info.start_sec = 12345
                info.start_usec = 1_000_000 if self.fault == "time" else 6789
                return size - (self.fault == "size")
        for fault in (None, "size", "pid", "time"):
            with self.subTest(fault=fault), \
                    patch("jev_git_graph.process_identity.platform.system", return_value="Darwin"), \
                    patch("jev_git_graph.process_identity.ctypes.util.find_library", return_value="libproc"), \
                    patch("jev_git_graph.process_identity.ctypes.CDLL") as library:
                library.return_value.proc_pidinfo = Query(fault)
                if fault:
                    with self.assertRaises(JgError):
                        native_process_generation(123)
                else:
                    self.assertEqual("darwin:123:12345:6789", native_process_generation(123))

    def test_native_reused_pid_or_reboot_never_falls_back_to_legacy(self):
        for old, current in (("darwin:123:10:1", "darwin:123:10:2"),
                             ("linux:old-boot:1", "linux:new-boot:1")):
            with self.subTest(old=old), \
                    patch("jev_git_graph.process_identity.native_process_generation", return_value=current):
                self.assertFalse(receipt_generation_matches(old, 123, old))
                self.assertTrue(receipt_generation_matches(current, 123, "legacy text"))
        with patch("jev_git_graph.process_identity.native_process_generation", side_effect=JgError("gone")):
            with self.assertRaises(JgError):
                receipt_generation_matches("darwin:123:10:1", 123, "darwin:123:10:1")

    def test_legacy_requires_valid_format_and_exact_match(self):
        start = "Wed Sep 24 08:00:00 2026"
        with patch("jev_git_graph.process_identity.native_process_generation") as native:
            self.assertTrue(receipt_generation_matches(start, 123, start))
            self.assertFalse(receipt_generation_matches(start, 123, start.replace("08:", "09:")))
            self.assertFalse(receipt_generation_matches("bogus", 123, "bogus"))
            self.assertFalse(receipt_generation_matches(None, 123, start))
            native.assert_not_called()

    def test_invalid_pid_and_unsupported_platform_fail_closed(self):
        for pid in (0, -1, True, "123"):
            with self.subTest(pid=pid), self.assertRaises(JgError):
                native_process_generation(pid)
        with patch("jev_git_graph.process_identity.platform.system", return_value="unknown"):
            with self.assertRaises(JgError):
                native_process_generation(123)

    def test_current_process_has_stable_native_generation(self):
        value = native_process_generation(os.getpid())
        self.assertEqual(value, native_process_generation(os.getpid()))
        self.assertTrue(value.startswith(("darwin:", "linux:")))
