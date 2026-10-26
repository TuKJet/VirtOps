from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class BaseImageTests(unittest.TestCase):
    def setUp(self):
        self.base = load(ROOT / "skill/windows-vm-sandbox/scripts/base_image.py", f"base_{id(self)}")

    def manifest(self, directory: Path, **overrides):
        disk = directory / "disk.qcow2"
        disk.write_bytes(b"standalone base image")
        value = {
            "schema_version": 1, "kind": "generalized-windows-qcow2", "image_id": "win11-pro",
            "disk": "disk.qcow2", "sha256": hashlib.sha256(disk.read_bytes()).hexdigest(),
            "virtual_size": 64 * self.base.GIB,
            "windows": {"language": "zh-CN", "edition": "Professional", "architecture": "amd64"},
            "c_partition": {"index": 3, "start_bytes": 1048576, "size_bytes": 32 * self.base.GIB, "unique_guid": "abc"},
            "generalization": {"image_state": "IMAGE_STATE_GENERALIZE_RESEAL_TO_OOBE", "builder_account_removed": True},
        }
        value.update(overrides)
        manifest = directory / "manifest.json"
        manifest.write_text(json.dumps(value), encoding="utf-8")
        return manifest, value, disk

    def test_manifest_rejects_bad_hash_backing_chain_and_mutable_published_file(self):
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            manifest_path, value, disk = self.manifest(directory)
            # The Linux host's root-only path checks are represented by this
            # isolated trusted-path shim; schema, hash and qemu-img checks stay real.
            mutable = {"value": False}
            def trusted(path, *, immutable=False):
                path = Path(path)
                if immutable and mutable["value"]:
                    raise ValueError("published template files must be read-only")
                return path

            with mock.patch.object(self.base, "trusted", side_effect=trusted):
                with mock.patch.object(self.base, "disk_info", return_value={"format": "qcow2", "virtual-size": value["virtual_size"], "backing-filename": None}):
                    self.assertEqual(self.base.inspect_manifest(manifest_path)["image_id"], "win11-pro")
                bad = dict(value); bad["sha256"] = "0" * 64
                manifest_path.write_text(json.dumps(bad), encoding="utf-8")
                with mock.patch.object(self.base, "disk_info", return_value={"format": "qcow2", "virtual-size": value["virtual_size"], "backing-filename": None}):
                    with self.assertRaisesRegex(ValueError, "hash differs"):
                        self.base.inspect_manifest(manifest_path)
                bad["sha256"] = value["sha256"]; manifest_path.write_text(json.dumps(bad), encoding="utf-8")
                with mock.patch.object(self.base, "run", return_value=SimpleNamespace(stdout=json.dumps([{"format": "qcow2", "virtual-size": value["virtual_size"], "backing-filename": "parent.qcow2"}]))):
                    with self.assertRaisesRegex(ValueError, "standalone qcow2"):
                        self.base.disk_info(disk)
                mutable["value"] = True
                with mock.patch.object(self.base, "disk_info", return_value={"format": "qcow2", "virtual-size": value["virtual_size"], "backing-filename": None}):
                    with self.assertRaisesRegex(ValueError, "read-only"):
                        self.base.inspect_manifest(manifest_path)

    def test_materialize_converts_to_independent_standalone_disk(self):
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td); manifest_path, manifest, source = self.manifest(directory)
            target = directory / "instance.qcow2"
            data = {"image": {"manifest": str(manifest_path)}, "resources": {"storage_gib": 64}}

            def fake_run(argv, timeout=600):
                self.assertEqual(argv[:5], ["qemu-img", "convert", "-f", "qcow2", "-O"])
                shutil.copyfile(source, target)
                return SimpleNamespace(stdout="", stderr="", returncode=0)

            with mock.patch.object(self.base, "trusted", side_effect=lambda path, **_: Path(path)), mock.patch.object(self.base, "inspect_manifest", return_value=manifest), mock.patch.object(self.base, "run", side_effect=fake_run), mock.patch.object(self.base, "disk_info", return_value={"format": "qcow2", "virtual-size": manifest["virtual_size"], "backing-filename": None}):
                result = self.base.materialize(data, target)
            self.assertEqual(result["image_id"], "win11-pro")
            self.assertTrue(target.is_file())
            self.assertNotEqual(source.stat().st_ino, target.stat().st_ino)


    def test_offline_disk_binds_the_helper_store_to_the_served_disk(self):
        # The imported disk helper refuses any scratch file outside its own
        # bounded store, so the store has to follow the disk being served.
        with tempfile.TemporaryDirectory() as td:
            disk = Path(td) / "vol" / "current.qcow2"
            disk.parent.mkdir(parents=True)
            disk.write_bytes(b"qcow2")
            with self.base.offline_disk(disk, 64 * self.base.GIB) as helper:
                self.assertEqual(helper.CURRENT, disk)
                self.assertEqual(helper.STORE, disk.parent)
                self.assertEqual(helper.RECOVERY, disk.parent / "recovery")
                helper.RECOVERY.mkdir(parents=True, exist_ok=True)
                scratch = helper.RECOVERY / "clip.bin"
                scratch.write_bytes(b"clip")
                helper.bounded_file(scratch)


if __name__ == "__main__":
    unittest.main()
