import copy
import ipaddress
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill/windows-vm-sandbox/scripts"
sys.path.insert(0, str(SCRIPTS))
from profile import validate  # noqa: E402
from render import domain_xml, runtime_files, seed_files  # noqa: E402
from prepare_host import select_cpu  # noqa: E402
from test_profile import profile_for  # noqa: E402


OLD_VALUES = (
    "8cb9f0f6-3d77-4c1c-8d0b-5ab70ac5f201",
    "e68ccc11-f548-4a28-a341-3707945468b1",
    "/home/vm_run",
    "172.30.61.0/30",
    "172.30.61.1",
    "172.30.61.2",
    "192.168.20.5",
    "13389",
    "15986",
    "73924612",
    "WINVM01",
    "winvm01",
)


def render_profile(name, number):
    data = profile_for(name, number)
    data["windows"].update(language="zh-CN", image_index=3, time_zone="China Standard Time")
    data["resources"].update(
        cpu_set="0-3", cpu_order=[0, 1, 2, 3], threads_per_core=1, amd=False
    )
    data["ovmf_code"] = "/usr/share/OVMF/OVMF_CODE_4M.secboot.fd"
    data["ovmf_vars"] = "/usr/share/OVMF/OVMF_VARS_4M.ms.fd"
    validate(data, new=True)
    return data


class RuntimeRenderingTests(unittest.TestCase):
    def test_two_profiles_render_independent_complete_python(self):
        rendered = []
        with tempfile.TemporaryDirectory(prefix="virtops-runtime-test-", dir=ROOT / "tests") as directory:
            for name, number in (("alpha", 1), ("bravo", 2)):
                data = render_profile(name, number)
                outputs = runtime_files(data, f"22222222-2222-4222-8222-{number:012d}", 50000 + number)
                self.assertIn("scripts/lib/vmctl.py", outputs)
                self.assertIn(f"scripts/lib/{name}_network.py", outputs)
                self.assertIn("scripts/start.sh", outputs)
                instance_root = Path(directory) / name
                for relative, text in outputs.items():
                    target = instance_root / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(text, encoding="utf-8")
                    if relative.endswith(".py"):
                        compile(target.read_text(encoding="utf-8"), str(target), "exec")
                    for old in OLD_VALUES:
                        self.assertNotIn(old, text, f"{relative} retained {old!r}")
                rendered.append((data, outputs))

        first_network = rendered[0][1]["scripts/lib/alpha_network.py"]
        second_network = rendered[1][1]["scripts/lib/bravo_network.py"]
        self.assertIn("ALPHA_INPUT", first_network)
        self.assertIn("BRAVO_INPUT", second_network)
        self.assertNotIn("WINVM01_INPUT", first_network + second_network)
        self.assertIn("PUBLISHED = ((23001, 3389), (24001, 5986))", first_network)
        self.assertIn("PUBLISHED = ((23002, 3389), (24002, 5986))", second_network)
        self.assertNotIn('"13389"', first_network + second_network)
        self.assertNotIn('"15986"', first_network + second_network)

    def test_rdp_is_published_over_udp_and_the_bridge_follows_the_profile_mtu(self):
        data = render_profile("alpha", 1)
        data["network"]["mtu"] = 1360
        outputs = runtime_files(data, "22222222-2222-4222-8222-222222222222", 50001)
        network = outputs["scripts/lib/alpha_network.py"]
        # The RDP port is published over UDP as well: a Windows client that
        # negotiates the 0xA0600 graphics pipeline moves the session to the
        # UDP multitransport and tears it down when UDP is not published.
        self.assertIn("PUBLISHED_UDP = ((23001, 3389),)", network)
        self.assertIn(
            '["-p", "udp", "-s", CLIENT_NET, "-d", PUBLISH_IP, "--dport", '
            'str(original_port), "-j", "DNAT", "--to-destination", f"{GUEST_IP}:{guest_port}"]',
            network,
        )
        self.assertIn(
            '"-p", "udp", "-s", source, "-d", GUEST_IP,', network
        )
        self.assertIn(
            '"-p", "udp", "-s", source, "-d", original_ip, "--dport", str(original_port), '
            '"-j", "DNAT", "--to-destination", f"{GUEST_IP}:{guest_port}"]',
            network,
        )
        # WinRM stays TCP only: it is not part of the UDP publication set.
        self.assertNotIn("PUBLISHED_UDP = ((23001, 3389), (24001, 5986))", network)
        bridge = outputs["scripts/lib/alpha_bridge.py"]
        self.assertIn("MTU = 1360", bridge)
        self.assertIn("'mtu', str(MTU)", bridge)
        compile(bridge, "alpha_bridge.py", "exec")

    def test_provision_template_opens_rdp_udp_and_aligns_the_guest_nic_mtu(self):
        data = render_profile("alpha", 1)
        data["network"]["mtu"] = 1360
        files = seed_files(data, "Safe!Password123")
        provision = files["remote-windows-provision.ps1"]
        self.assertIn(
            "New-NetFirewallRule -DisplayName 'Remote VM baseline RDP UDP' "
            "-Direction Inbound -Action Allow -Protocol UDP -LocalPort 3389",
            provision,
        )
        self.assertIn("[int]$InterfaceMtu = 1360", provision)
        self.assertIn(
            "Set-ItemProperty -Path $interfaceRegistry -Name MTU -Type DWord -Value $InterfaceMtu",
            provision,
        )
        self.assertNotIn("__VM_MTU__", provision)

    def test_domain_xml_cpu_memory_firmware_mac_and_name(self):
        data = render_profile("alpha", 1)
        xml = ET.fromstring(domain_xml(data, installing=True))
        self.assertEqual(xml.findtext("name"), "alpha")
        self.assertEqual(xml.findtext("uuid"), data["uuid"])
        self.assertEqual(xml.findtext("memory"), str(8 * 1024**2))
        self.assertEqual(xml.find("memtune/hard_limit").text, str(10 * 1024**2))
        self.assertEqual(xml.find("os/loader").text, data["ovmf_code"])
        self.assertEqual(xml.find("os/nvram").get("template"), data["ovmf_vars"])
        self.assertTrue(xml.find("os/firmware/feature[@name='secure-boot']") is not None)
        self.assertEqual(xml.find("cputune/global_quota").text, "400000")
        pins = xml.findall("cputune/vcpupin")
        self.assertEqual([int(node.get("vcpu")) for node in pins], [0, 1, 2, 3])
        self.assertEqual([node.get("cpuset") for node in pins], ["0", "1", "2", "3"])
        mac = xml.find("devices/interface/mac").get("address")
        expected = "52:54:00:" + ":".join(f"{byte:02x}" for byte in __import__("uuid").UUID(data["uuid"]).bytes[-3:])
        self.assertEqual(mac, expected)
        self.assertEqual(xml.find("devices/interface/source").get("bridge"), "bralpha")
        self.assertEqual(xml.find("devices/disk/source").get("file"), "/srv/alpha/vol/current.qcow2")
        self.assertEqual(xml.find("devices/disk/boot").get("order"), "2")

    def test_seed_xml_uses_real_image_language_and_escaped_password(self):
        data = render_profile("alpha", 1)
        password = "P@ss&<\"'>123456"
        files = seed_files(data, password)
        root = ET.fromstring(files["Autounattend.xml"])
        ns = {"u": "urn:schemas-microsoft-com:unattend"}
        index = root.find(".//u:MetaData/u:Value", ns).text
        self.assertEqual(index, "3")
        self.assertEqual(root.findtext(".//u:InputLocale", namespaces=ns), "zh-CN")
        self.assertEqual(root.findtext(".//u:UILanguage", namespaces=ns), "zh-CN")
        values = [node.text for node in root.findall(".//u:Password/u:Value", ns)]
        self.assertTrue(values)
        self.assertTrue(all(value == password for value in values))
        self.assertIn("&amp;", files["Autounattend.xml"])
        self.assertIn("&lt;", files["Autounattend.xml"])
        for node in root.findall(".//u:RunSynchronousCommand/u:Path", ns):
            self.assertGreater(len(node.text or ""), 0)
            self.assertLessEqual(len(node.text), 259)

    def test_cpu_selection_requires_matching_count_and_uniform_threads(self):
        data = render_profile("alpha", 1)
        data["resources"].update(vcpus=2, cpu_set="0")
        lscpu = "\n".join(("0,0,0,Y", "1,0,0,Y", "2,1,0,Y", "3,1,0,Y"))
        with self.assertRaisesRegex(RuntimeError, "count does not fit"):
            select_cpu(data, lscpu)

        data["resources"]["cpu_set"] = "0-1"
        selected = select_cpu(data, lscpu)
        self.assertEqual(selected["resources"]["cpu_order"], [0, 1])
        self.assertEqual(selected["resources"]["threads_per_core"], 2)

    def test_cpu_selection_avoids_other_vm_and_can_use_one_thread_per_core(self):
        data=render_profile('alpha',1);data['resources'].update(vcpus=2,cpu_set=None)
        topology='0,0,0,Y\n1,0,0,Y\n2,1,0,Y\n3,1,0,Y'
        self.assertEqual(select_cpu(data,topology,reserved={0,1})['resources']['cpu_order'],[2,3])
        data['resources']['cpu_set']='0,2'
        self.assertEqual(select_cpu(data,topology)['resources']['threads_per_core'],1)


if __name__ == "__main__":
    unittest.main()
