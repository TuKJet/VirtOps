"""Root-gated, isolated-network integration test for declared VM services.

Run on Linux as root inside an outer isolated namespace (must use
``sudo unshare --net --mount --fork python3
tests/linux_published_services.py``).  The test creates only a temporary
network namespace, bridge, veth pair, private iptables-nft chains, and a guest
HTTP server.  It never touches a libvirt VM or a host namespace interface.
"""
from __future__ import annotations

import importlib.util
import http.client
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill/windows-vm-sandbox/scripts"
RUNTIME = ROOT / "skill/windows-vm-sandbox/assets/runtime"
sys.path[:0] = [str(SCRIPTS), str(ROOT / "tests"), str(RUNTIME)]


def run(args, *, check=True, namespace=None):
    command = list(args)
    if namespace is not None:
        command = ["nsenter", "-t", str(namespace), "-n", "--", *command]
    result = subprocess.run(command, text=True, capture_output=True, timeout=30)
    if check and result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or "command failed")
    return result


def load_network():
    from test_profile import profile_for
    from profile import validate
    from render import runtime_files

    profile = profile_for("alpha", 1)
    profile["network"].update(
        bridge="winbr01", subnet="172.30.61.0/30", bridge_ip="172.30.61.1",
        guest_ip="172.30.61.2", publish_ip="192.0.2.5", client_cidr="198.51.100.0/24",
        published_services=[{
            "id": "cad-mcp", "publish_port": 18000, "guest_port": 8000,
            "access": "host-loopback", "allowed_sources": ["127.0.0.1/32"],
        }],
    )
    validate(profile, new=False)
    source = runtime_files(profile, "22222222-2222-4222-8222-000000000001", 42)[
        "scripts/lib/alpha_network.py"
    ]
    namespace = {"__name__": "alpha_network", "__file__": "/tmp/alpha/scripts/lib/alpha_network.py"}
    exec(compile(source, "alpha_network.py", "exec"), namespace)
    return namespace


@unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0, "requires Linux root")
class PublishedServiceLinuxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for binary in ("ip", "iptables", "iptables-save", "nsenter", "unshare", "sysctl", "python3"):
            if shutil.which(binary) is None:
                raise unittest.SkipTest(f"missing {binary}")
        if os.stat("/proc/self/ns/net").st_ino == os.stat("/proc/1/ns/net").st_ino:
            raise unittest.SkipTest("must run inside an outer network namespace: sudo unshare --net --mount --fork ...")
        cls.network = load_network()
        # This lab has no libvirt domain.  Production remove() retains its
        # stopped-VM guard; only this disposable namespace bypasses it.
        cls.network["require_vm_stopped"] = lambda: None
        cls.bridge = cls.network["BRIDGE"]
        cls.guest_ip = cls.network["GUEST_IP"]
        cls.bridge_created = False
        cls.alias_added = False
        # Without --fork the Popen PID itself owns the child network namespace;
        # using the wrapper PID would otherwise move the veth into the wrong ns.
        cls.guest = subprocess.Popen(["unshare", "--net", "sleep", "120"])
        parent_net = os.stat("/proc/self/ns/net").st_ino
        for _ in range(30):
            if cls.guest.poll() is not None:
                raise RuntimeError("guest network namespace process exited")
            if os.stat(f"/proc/{cls.guest.pid}/ns/net").st_ino != parent_net:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("unshare did not create a distinct network namespace")
        cls.host_veth, cls.guest_veth = "vops-host0", "vops-guest0"
        try:
            run(["ip", "link", "add", cls.bridge, "type", "bridge"])
            cls.bridge_created = True
            run(["ip", "addr", "add", "172.30.61.1/30", "dev", cls.bridge])
            run(["ip", "link", "set", cls.bridge, "up"])
            run(["ip", "link", "add", cls.host_veth, "type", "veth", "peer", "name", cls.guest_veth])
            run(["ip", "link", "set", cls.guest_veth, "netns", str(cls.guest.pid)])
            run(["ip", "link", "set", cls.host_veth, "master", cls.bridge])
            run(["ip", "link", "set", cls.host_veth, "up"])
            run(["ip", "link", "set", cls.bridge, "up"])
            run(["ip", "link", "set", "lo", "up"])
            run(["ip", "addr", "add", "192.0.2.5/32", "dev", "lo"])
            cls.alias_added = True
            run(["ip", "link", "set", "lo", "up"], namespace=cls.guest.pid)
            run(["ip", "addr", "add", "172.30.61.2/30", "dev", cls.guest_veth], namespace=cls.guest.pid)
            run(["ip", "link", "set", cls.guest_veth, "up"], namespace=cls.guest.pid)
            run(["sysctl", "-w", "net.ipv4.ip_forward=1"])
            cls.network["configure_sysctls"]()
            cls.network["apply"]("iptables-nft")
            cls.network["verify"]("iptables-nft")
            server_code = (
                "import http.server\n"
                "class H(http.server.BaseHTTPRequestHandler):\n"
                " protocol_version='HTTP/1.1'\n"
                " def do_GET(self):\n"
                "  body=b'ok'\n"
                "  self.send_response(200)\n"
                "  self.send_header('Content-Length',str(len(body)))\n"
                "  self.end_headers()\n"
                "  self.wfile.write(body)\n"
                " def log_message(self,*args): pass\n"
                "http.server.ThreadingHTTPServer(('172.30.61.2',8000),H).serve_forever()"
            )
            cls.http = subprocess.Popen(
                ["nsenter", "-t", str(cls.guest.pid), "-n", "--", "python3", "-c", server_code],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            time.sleep(0.3)
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        http = getattr(cls, "http", None)
        if http is not None and http.poll() is None:
            http.terminate()
            try: http.wait(timeout=3)
            except subprocess.TimeoutExpired: http.kill()
        network = getattr(cls, "network", None)
        if network is not None:
            try: network["remove"]("iptables-nft")
            except Exception: pass
        bridge = getattr(cls, "bridge", "winbr01")
        if shutil.which("ip"):
            if getattr(cls, "alias_added", False):
                run(["ip", "addr", "del", "192.0.2.5/32", "dev", "lo"], check=False)
            if getattr(cls, "bridge_created", False):
                run(["ip", "link", "del", bridge], check=False)
        guest = getattr(cls, "guest", None)
        if guest is not None and guest.poll() is None:
            guest.send_signal(signal.SIGTERM)
            try: guest.wait(timeout=3)
            except subprocess.TimeoutExpired: guest.kill()

    def get(self, port):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(f"http://127.0.0.1:{port}/", timeout=3).status

    def get_physical_simulation(self):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open("http://192.0.2.5:18000/", timeout=3).status

    @staticmethod
    def keepalive_request(connection):
        connection.request("GET", "/", headers={"Connection": "keep-alive"})
        response = connection.getresponse()
        status = response.status
        body = response.read()  # drain the complete response before reuse
        return status, body

    def test_declared_loopback_service_reaches_guest_without_default_route(self):
        self.assertEqual(self.get(18000), 200)
        routes = run(["ip", "route"], namespace=self.guest.pid).stdout
        self.assertNotIn("default", routes)
        self.assertIn("172.30.61.0/30", routes)

    def test_undeclared_and_physical_ports_are_blocked(self):
        for port in (18001, 18002):
            with self.subTest(port=port), self.assertRaises((urllib.error.URLError, TimeoutError, ConnectionError)):
                self.get(port)
        with self.assertRaises((urllib.error.URLError, TimeoutError, ConnectionError)):
            self.get_physical_simulation()

    def test_reapply_is_idempotent_and_verify_rejects_extra_rule(self):
        self.network["apply"]("iptables-nft")
        self.network["verify"]("iptables-nft")
        chain = self.network["FILTER4"]["output"]
        run(["iptables", "-I", chain, "1", "-j", "ACCEPT"])
        try:
            with self.assertRaises(Exception):
                self.network["verify"]("iptables-nft")
        finally:
            run(["iptables", "-D", chain, "1"])
        self.network["verify"]("iptables-nft")

        # A shared-parent ACCEPT before our guard would bypass the private
        # chain; verification must reject it and cleanup must be exact.
        run(["iptables", "-I", "OUTPUT", "1", "-j", "ACCEPT"])
        try:
            with self.assertRaises(Exception):
                self.network["verify"]("iptables-nft")
        finally:
            run(["iptables", "-D", "OUTPUT", "1"])
        self.network["verify"]("iptables-nft")

        # Duplicate own jumps are equally unsafe: one copy can be reordered
        # around a future shared rule while -C still reports success.
        run(["iptables", "-I", "OUTPUT", "1", "-d", self.network["GUEST_IP"],
             "-j", self.network["FILTER4"]["output"]])
        try:
            with self.assertRaises(Exception):
                self.network["verify"]("iptables-nft")
        finally:
            run(["iptables", "-D", "OUTPUT", "1"])
        self.network["verify"]("iptables-nft")

    def test_guest_new_tcp_udp_and_ipv6_are_counted_as_drops(self):
        before = sum(row["packets"] for row in self.network["drop_counters"]("iptables-nft"))
        run(["ip", "route", "add", "default", "via", "172.30.61.1", "dev", "vops-guest0"], namespace=self.guest.pid)
        try:
            run(["python3", "-c", "import socket; s=socket.socket(); s.settimeout(.5);\ntry: s.connect(('1.1.1.1',80))\nexcept OSError: pass"], check=False, namespace=self.guest.pid)
            run(["python3", "-c", "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.sendto(b'x',('8.8.8.8',53))"], check=False, namespace=self.guest.pid)
            # Enable IPv6 only inside this disposable namespace for one packet;
            # restore the bridge guard immediately after the probe.
            run(["sysctl", "-w", "net.ipv6.conf." + self.bridge + ".disable_ipv6=0"])
            run(["ip", "-6", "addr", "add", "fd00::1/64", "dev", self.bridge])
            run(["ip", "-6", "addr", "add", "fd00::2/64", "dev", "vops-guest0"], namespace=self.guest.pid)
            run(["ip", "-6", "route", "add", "default", "via", "fd00::1", "dev", "vops-guest0"], namespace=self.guest.pid)
            run(["python3", "-c", "import socket; s=socket.socket(socket.AF_INET6,socket.SOCK_DGRAM); s.sendto(b'x',('2001:db8::1',53,0,0))"], check=False, namespace=self.guest.pid)
        finally:
            run(["ip", "-6", "route", "del", "default", "via", "fd00::1", "dev", "vops-guest0"], check=False, namespace=self.guest.pid)
            run(["ip", "-6", "addr", "del", "fd00::2/64", "dev", "vops-guest0"], check=False, namespace=self.guest.pid)
            run(["ip", "-6", "addr", "del", "fd00::1/64", "dev", self.bridge], check=False)
            run(["sysctl", "-w", "net.ipv6.conf." + self.bridge + ".disable_ipv6=1"], check=False)
            run(["ip", "route", "del", "default", "via", "172.30.61.1", "dev", "vops-guest0"], check=False, namespace=self.guest.pid)
        after = sum(row["packets"] for row in self.network["drop_counters"]("iptables-nft"))
        self.assertGreater(after, before)
    def test_maintenance_gate_blocks_existing_keepalive_session(self):
        connection = http.client.HTTPConnection("127.0.0.1", 18000, timeout=3)
        connection.connect()
        gate = self.network["maintenance_gate"]()
        try:
            self.assertEqual(self.keepalive_request(connection), (200, b"ok"))
            gate.install()
            connection.sock.settimeout(1)
            with self.assertRaises((TimeoutError, ConnectionResetError, BrokenPipeError, OSError)):
                self.keepalive_request(connection)
        finally:
            gate.remove()
            connection.close()
        self.assertEqual(self.get(18000), 200)

    def test_z_service_removal_invalidates_existing_keepalive_and_restore_reopens(self):
        original_services = self.network["PUBLISHED_SERVICES"]
        connection = http.client.HTTPConnection("127.0.0.1", 18000, timeout=3)
        connection.connect()
        try:
            self.assertEqual(self.keepalive_request(connection), (200, b"ok"))
            self.network["PUBLISHED_SERVICES"] = ()
            self.network["refresh_private_chains"]()
            connection.sock.settimeout(1)
            with self.assertRaises((TimeoutError, ConnectionResetError, BrokenPipeError, OSError)):
                self.keepalive_request(connection)
        finally:
            connection.close()
            self.network["PUBLISHED_SERVICES"] = original_services
            self.network["refresh_private_chains"]()
        self.assertEqual(self.get(18000), 200)

    def test_zz_instance_rules_are_isolated_when_alpha_publishes_maintains_and_removes(self):
        """A second instance's private chains survive alpha lifecycle changes."""
        beta_filter, beta_nat = "BETA_FILTER", "BETA_DNAT"
        run(["iptables", "-N", beta_filter])
        run(["iptables", "-A", beta_filter, "-s", "198.51.100.0/24", "-j", "RETURN"])
        run(["iptables", "-N", beta_nat], check=False)
        run(["iptables", "-A", beta_nat, "-p", "tcp", "--dport", "19000", "-j", "RETURN"])

        def beta_rules():
            filter_lines = [line for line in run(["iptables-save", "-t", "filter"]).stdout.splitlines()
                            if beta_filter in line]
            nat_lines = [line for line in run(["iptables-save", "-t", "nat"]).stdout.splitlines()
                         if beta_nat in line]
            return filter_lines, nat_lines

        before = beta_rules()
        try:
            self.network["apply"]("iptables-nft")
            gate = self.network["maintenance_gate"]()
            gate.install()
            gate.remove()
            self.network["remove"]("iptables-nft")
            self.assertEqual(beta_rules(), before)
        finally:
            run(["iptables", "-F", beta_filter], check=False)
            run(["iptables", "-X", beta_filter], check=False)
            run(["iptables", "-F", beta_nat], check=False)
            run(["iptables", "-X", beta_nat], check=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
