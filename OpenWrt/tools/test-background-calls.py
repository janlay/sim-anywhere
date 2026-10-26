#!/usr/bin/env python3
"""Check call identity and APNs payload without placing a real call or sending a push."""

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import types
import unittest
import uuid

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat


ROOT = Path(__file__).resolve().parents[1]


def load_script(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class BackgroundCallTests(unittest.TestCase):
    def test_one_id_one_push_and_stale_answer_rejected(self):
        source = (ROOT / "files/usr/bin/sim-anywhere-calld").read_text().rsplit("\nmain()", 1)[0]
        ns = {"__name__": "calld_test"}
        exec(compile(source, "sim-anywhere-calld", "exec"), ns)
        sent = []
        ns["subprocess"] = types.SimpleNamespace(Popen=lambda args, **kw: sent.append(args), DEVNULL=None)
        ns["log"] = lambda *args: None
        ns["modem"].ready = lambda: True
        ns["modem"].cmd = lambda *args: None
        call = ns["Call"]()
        call.update((1, 4, "+15550000123"))
        call.update((1, 4, "+15550000456"))  # caller ID can arrive after the first ring
        first = call.call_id
        self.assertEqual(uuid.UUID(first).version, 4)
        self.assertEqual(call.state, "incoming")
        self.assertEqual(call.view(None)["call_id"], first)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1], first)
        client = types.SimpleNamespace(addr=("local", 0), error=lambda msg: sent.append(msg))
        call.op(client, {"op": "answer", "call_id": str(uuid.uuid4())})
        self.assertEqual(sent[-1], "call changed")
        self.assertIsNone(call.owner)
        call.op(client, {"op": "hangup", "call_id": str(uuid.uuid4())})
        self.assertEqual(sent[-1], "call changed")
        self.assertEqual(call.state, "incoming")
        call.op(client, {"op": "answer", "call_id": first})
        self.assertIs(call.owner, client)

    def test_apns_token_is_signed_and_payload_has_no_caller_number(self):
        push = load_script("sim_anywhere_push", ROOT / "files/usr/bin/sim-anywhere-push")
        key = ec.generate_private_key(ec.SECP256R1())
        with tempfile.TemporaryDirectory() as folder:
            key_path = Path(folder) / "key.p8"
            key_path.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
            config = {"private_key_path": str(key_path), "team_id": "TESTTEAM", "key_id": "TESTKEY",
                      "bundle_id": "test.example.app"}
            jwt = push.bearer(config)
            import base64
            head, body, signature = jwt.split(".")
            raw = base64.urlsafe_b64decode(signature + "==")
            self.assertEqual(len(raw), 64)
            der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
            key.public_key().verify(der, (head + "." + body).encode(), ec.ECDSA(hashes.SHA256()))
            call_id = str(uuid.uuid4())
            def fake_curl(args, **kw):
                headers = Path(args[args.index("--config") + 1]).read_text()
                payload = json.loads(args[args.index("--data-binary") + 1])
                self.assertIn("authorization: bearer ", headers)
                self.assertEqual(payload, {"call_id": call_id})
                self.assertIn("apns-expiration: 0", args)
                return types.SimpleNamespace(stdout="200", returncode=0)
            push.subprocess.run = fake_curl
            self.assertEqual(push.send({"environment": "development", "token": "a" * 64}, call_id,
                                       config, jwt), 200)

    def test_luci_key_install_is_private_and_rejects_invalid_key(self):
        helper = load_script("sim_anywhere_push_key", ROOT / "files/usr/bin/sim-anywhere-push-key")
        with tempfile.TemporaryDirectory() as folder:
            helper.KEY = str(Path(folder) / "apns-key.p8")
            helper.PENDING = helper.KEY + ".pending"
            helper.prepare()
            self.assertEqual(stat.S_IMODE(os.stat(helper.PENDING).st_mode), 0o600)
            Path(helper.PENDING).write_text("invalid")
            with self.assertRaises(ValueError):
                helper.activate()
            self.assertFalse(Path(helper.KEY).exists())
            key = ec.generate_private_key(ec.SECP256R1())
            Path(helper.PENDING).write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
            helper.activate()
            self.assertFalse(Path(helper.PENDING).exists())
            self.assertEqual(stat.S_IMODE(os.stat(helper.KEY).st_mode), 0o600)

    def test_push_reads_luci_identifiers_and_rejects_bad_topic(self):
        push = load_script("sim_anywhere_push_config", ROOT / "files/usr/bin/sim-anywhere-push")
        values = {"team_id": "ABCDEFGHIJ", "key_id": "1234567890", "bundle_id": "org.example.calls"}
        push.subprocess.run = lambda args, **kw: types.SimpleNamespace(
            returncode=0, stdout=values[args[-1].rsplit(".", 1)[-1]])
        config = push.load_config()
        self.assertEqual(config["bundle_id"] + ".voip", "org.example.calls.voip")
        self.assertEqual(config["private_key_path"], push.KEY)
        values["bundle_id"] = "invalid topic"
        with self.assertRaises(ValueError):
            push.load_config()

    def test_voip_status_needs_current_device_and_real_apns_acceptance(self):
        push = load_script("sim_anywhere_push_status", ROOT / "files/usr/bin/sim-anywhere-push")
        values = {"team_id": "ABCDEFGHIJ", "key_id": "1234567890", "bundle_id": "org.example.calls"}
        push.subprocess.run = lambda args, **kw: types.SimpleNamespace(
            returncode=0, stdout=values[args[-1].rsplit(".", 1)[-1]])
        device = {"device": str(uuid.uuid4()), "token": "a" * 64, "environment": "development"}
        with tempfile.TemporaryDirectory() as folder:
            push.KEY = str(Path(folder) / "key.p8")
            push.DEVICES = str(Path(folder) / "devices.json")
            push.DELIVERY = str(Path(folder) / "delivery.json")
            push.LOCK = str(Path(folder) / "devices.lock")
            query = (device["device"], values["bundle_id"], "development")
            self.assertEqual(push.delivery_status(*query), "unconfigured")
            key = ec.generate_private_key(ec.SECP256R1())
            Path(push.KEY).write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
            self.assertEqual(push.delivery_status(*query), "ineffective")
            Path(push.DEVICES).write_text(json.dumps([device]))
            self.assertEqual(push.delivery_status(*query), "ineffective")
            push.record_delivery(device, push.load_config(), 200)
            self.assertEqual(push.delivery_status(*query), "active")
            self.assertEqual(push.delivery_status(device["device"], "org.other.app", "development"), "ineffective")
            device["token"] = "b" * 64
            Path(push.DEVICES).write_text(json.dumps([device]))
            self.assertEqual(push.delivery_status(*query), "ineffective")


if __name__ == "__main__":
    unittest.main()
