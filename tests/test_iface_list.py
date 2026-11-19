"""Interface picker list: works without the ip command (the GUI container has none)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.link import acft_pos_multicast as apm  # noqa: E402


class IfaceList(unittest.TestCase):
    def test_kernel_list_has_loopback(self):
        if sys.platform == "win32":
            self.skipTest("Linux only")
        self.assertIn("127.0.0.1", apm._linux_iface_addresses())

    def test_missing_net_dir_gives_empty(self):
        self.assertEqual(apm._linux_iface_addresses("/nonexistent_dir_for_test"), [])

    def test_no_ip_command_still_lists_interfaces(self):
        if sys.platform == "win32":
            self.skipTest("Linux only")
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("ip")), \
             mock.patch.object(apm, "_linux_iface_addresses", return_value=["10.9.8.7", "127.0.0.1"]):
            out = apm.local_ipv4_addresses()
        self.assertIn("10.9.8.7", out)
        self.assertEqual(out[-1], "127.0.0.1")
        self.assertEqual(out.count("127.0.0.1"), 1)


if __name__ == "__main__":
    unittest.main()
