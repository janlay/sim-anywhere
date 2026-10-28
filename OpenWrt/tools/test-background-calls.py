#!/usr/bin/env python3
"""Check call identity and APNs payload without placing a real call or sending a push."""

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
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
            push.subprocess = types.SimpleNamespace(run=fake_curl, TimeoutExpired=subprocess.TimeoutExpired)
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
        push.subprocess = types.SimpleNamespace(run=lambda args, **kw: types.SimpleNamespace(
            returncode=0, stdout=values[args[-1].rsplit(".", 1)[-1]]), TimeoutExpired=subprocess.TimeoutExpired)
        config = push.load_config()
        self.assertEqual(config["bundle_id"] + ".voip", "org.example.calls.voip")
        self.assertEqual(config["private_key_path"], push.KEY)
        values["bundle_id"] = "invalid topic"
        with self.assertRaises(ValueError):
            push.load_config()

    def test_voip_status_needs_current_device_and_real_apns_acceptance(self):
        push = load_script("sim_anywhere_push_status", ROOT / "files/usr/bin/sim-anywhere-push")
        values = {"team_id": "ABCDEFGHIJ", "key_id": "1234567890", "bundle_id": "org.example.calls"}
        push.subprocess = types.SimpleNamespace(run=lambda args, **kw: types.SimpleNamespace(
            returncode=0, stdout=values[args[-1].rsplit(".", 1)[-1]]), TimeoutExpired=subprocess.TimeoutExpired)
        device = {"device": str(uuid.uuid4()), "token": "a" * 64, "environment": "development",
                  "bundle_id": values["bundle_id"], "team_id": values["team_id"], "selected": True}
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


class PushDeviceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.push = load_script("push_devices_test", ROOT / "files/usr/bin/sim-anywhere-push")
        for name, filename in (("KEY", "key.p8"), ("DEVICES", "devices.json"),
                               ("DELIVERY", "delivery.json"), ("LOCK", "devices.lock")):
            setattr(self.push, name, str(Path(self.folder.name) / filename))
        key = ec.generate_private_key(ec.SECP256R1())
        Path(self.push.KEY).write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
        self.config = {"team_id": "ABCDEFGHIJ", "key_id": "1234567890", "bundle_id": "org.example.calls",
                       "private_key_path": self.push.KEY}
        self.push.load_config = lambda: self.config
        self.a = self.device("a", True)
        self.b = self.device("b", False)
        self.save([self.a, self.b])

    def device(self, token, selected):
        return dict(device=str(uuid.uuid4()), token=token * 64, selected=selected,
                    bundle_id="org.example.calls", team_id="ABCDEFGHIJ", environment="development",
                    name="iPhone", model="iPhone", os_version="18.0", app_version="1.6", app_build="6")

    def save(self, devices):
        Path(self.push.DEVICES).write_text(json.dumps(devices))

    def test_registration_checks_identity_and_hides_credentials(self):
        self.assertIsNone(self.push.registration_reason(self.config, self.a))
        for field, value, reason in (("team_id", "XXXXXXXXXX", "team_mismatch"),
                                     ("bundle_id", "org.other.app", "bundle_mismatch"),
                                     ("environment", "unknown", "invalid_environment"),
                                     ("token", "invalid", "invalid_token")):
            changed = dict(self.a, **{field: value})
            self.assertEqual(self.push.registration_reason(self.config, changed), reason)
        self.assertEqual(self.push.registration_reason(self.config, dict(device="legacy")), "missing_metadata")
        result = self.push.list_devices()
        self.assertEqual(result["devices"][0]["state"], "pending")
        self.assertNotIn(self.a["token"], json.dumps(result))
        self.assertNotIn("private_key_path", json.dumps(result))
        Path(self.push.KEY).unlink()
        self.assertEqual(self.push.list_devices()["devices"][0]["reason"], "unconfigured")

    def test_selection_is_exclusive_and_rejects_mismatch_or_missing_device(self):
        self.push.select_device(self.b["device"])
        devices = json.loads(Path(self.push.DEVICES).read_text())
        self.assertEqual([d["device"] for d in devices if d["selected"]], [self.b["device"]])
        self.assertEqual(stat.S_IMODE(Path(self.push.DEVICES).stat().st_mode), 0o600)
        self.b["team_id"] = "XXXXXXXXXX"
        self.save([self.a, self.b])
        with self.assertRaisesRegex(ValueError, "team_mismatch"):
            self.push.select_device(self.b["device"])
        with self.assertRaisesRegex(ValueError, "device_not_found"):
            self.push.select_device(str(uuid.uuid4()))
        self.assertTrue(json.loads(Path(self.push.DEVICES).read_text())[0]["selected"])

    def test_only_selected_device_is_pushed_and_snapshot_survives_switch(self):
        sent = []
        self.push.send = lambda device, *args: sent.append(device["device"]) or 200
        self.push.main(str(uuid.uuid4()))
        self.assertEqual(sent, [self.a["device"]])
        self.push.select_device(self.b["device"])
        self.push.main(str(uuid.uuid4()), self.a["device"])
        self.assertEqual(sent[-1], self.a["device"])
        self.push.main(str(uuid.uuid4()))
        self.assertEqual(sent[-1], self.b["device"])
        self.push.main(str(uuid.uuid4()), "")
        self.assertEqual(len(sent), 3)

    def test_acceptance_failure_token_rotation_and_unselected_status(self):
        self.push.record_delivery(self.a, self.config, 200)
        query = (self.a["device"], self.a["bundle_id"], self.a["environment"])
        self.assertEqual(self.push.delivery_status(*query), "active")
        self.push.select_device(self.b["device"])
        self.assertEqual(self.push.delivery_status(*query), "ineffective")
        self.assertEqual(self.push.list_devices()["devices"][0]["state"], "accepted")
        self.push.record_delivery(self.a, self.config, 403)
        row = self.push.list_devices()["devices"][0]
        self.assertEqual((row["state"], row["apns_code"]), ("rejected", 403))
        self.push.record_delivery(self.a, self.config, 410)
        self.assertEqual(self.push.registration_reason(self.config, self.a), "token_expired")
        self.a["token"] = "c" * 64
        self.save([self.a, self.b])
        self.assertIsNone(self.push.registration_reason(self.config, self.a))
        self.assertEqual(self.push.list_devices()["devices"][0]["state"], "pending")

    def test_status_details_explain_registration_selection_and_delivery_failures(self):
        query = (self.a["device"], self.a["bundle_id"], self.a["environment"])
        self.assertEqual(self.push.status_details(*query)["reason"], "pending")
        self.assertEqual(self.push.status_details(str(uuid.uuid4()), *query[1:])["reason"], "not_registered")
        self.assertEqual(self.push.status_details(query[0], "org.other.app", query[2])["reason"], "bundle_mismatch")
        self.assertEqual(self.push.status_details(query[0], query[1], "production")["reason"], "environment_mismatch")
        self.a["team_id"] = "XXXXXXXXXX"
        self.save([self.a, self.b])
        self.assertEqual(self.push.status_details(*query)["reason"], "team_mismatch")
        self.a["team_id"] = "ABCDEFGHIJ"
        self.save([self.a, self.b])
        self.push.select_device(self.b["device"])
        self.assertEqual(self.push.status_details(*query)["reason"], "not_selected")
        self.push.select_device(self.a["device"])
        for code, reason in ((0, "connection_failed"), (403, "apns_rejected"), (200, "accepted")):
            self.push.record_delivery(self.a, self.config, code)
            result = self.push.status_details(*query)
            self.assertEqual((result["reason"], result["apns_code"]), (reason, code))
            self.assertNotIn(self.a["token"], json.dumps(result))
        original = self.push.bearer
        def unavailable(config):
            raise ImportError("signer unavailable")
        self.push.bearer = unavailable
        self.assertEqual(self.push.status_details(*query)["reason"], "signing_unavailable")
        self.assertEqual(self.push.list_devices()["devices"][0]["reason"], "signing_unavailable")
        self.push.bearer = original
        Path(self.push.KEY).unlink()
        self.assertEqual(self.push.status_details(*query)["reason"], "unconfigured")

    @unittest.skipUnless(os.environ.get("UCODE_BIN"), "set UCODE_BIN to run CGI integration checks")
    def test_cgi_registration_preserves_selection_and_rejects_bad_metadata(self):
        source = (ROOT / "files/www/cgi-bin/sim-anywhere-api").read_text()
        source = source.replace("/root/sim-anywhere-voip-tokens.json", self.push.DEVICES)
        source = source.replace("/var/lock/sim-anywhere-voip-tokens.lock", self.push.LOCK)
        script = Path(self.folder.name) / "api.uc"
        script.write_text(source)
        Path(self.push.DEVICES).unlink()

        def request(method, body):
            data = json.dumps(body).encode()
            env = dict(os.environ, REQUEST_METHOD=method, PATH_INFO="/push/register",
                       CONTENT_TYPE="application/json", CONTENT_LENGTH=str(len(data)), QUERY_STRING="")
            args = [os.environ["UCODE_BIN"]]
            if os.environ.get("UCODE_LIB"):
                args += ["-L", os.environ["UCODE_LIB"]]
            result = subprocess.run(args + [str(script)], input=data, capture_output=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            return result.stdout.decode()

        self.assertIn("Status: 200", request("POST", self.a))
        self.assertTrue(json.loads(Path(self.push.DEVICES).read_text())[0]["selected"])
        self.assertIn("Status: 200", request("POST", dict(self.b, selected=True)))
        self.assertEqual([d["device"] for d in self.push.read_devices() if d["selected"]], [self.a["device"]])
        self.push.select_device(self.b["device"])
        self.a["token"] = "c" * 64
        self.assertIn("Status: 200", request("POST", self.a))
        self.assertEqual([d["device"] for d in self.push.read_devices() if d["selected"]], [self.b["device"]])
        for field, value in (("name", "x" * 129), ("name", "bad\x01name"), ("team_id", "bad"),
                             ("bundle_id", "invalid"), ("environment", "unknown"), ("token", "no")):
            self.assertIn("Status: 400", request("POST", dict(self.a, **{field: value})))
        self.assertIn("Status: 200", request("DELETE", {"device": self.b["device"]}))
        self.assertIn("Status: 200", request("POST", self.a))
        self.assertFalse(any(d["selected"] for d in self.push.read_devices()))
        self.assertEqual(stat.S_IMODE(Path(self.push.DEVICES).stat().st_mode), 0o600)

    def test_foreground_routing_and_call_controls_use_same_frozen_device(self):
        source = (ROOT / "files/usr/bin/sim-anywhere-calld").read_text().rsplit("\nmain()", 1)[0]
        ns = {"__name__": "calld_routing_test"}
        exec(compile(source, "sim-anywhere-calld", "exec"), ns)
        ns["PUSH_DEVICES"] = self.push.DEVICES
        ns["modem"].ready = lambda: True
        ns["modem"].cmd = lambda *args: None
        ns["log"] = lambda *args: None
        sent = []
        ns["subprocess"] = types.SimpleNamespace(Popen=lambda args, **kw: sent.append(args), DEVNULL=None)
        events_a, events_b, errors = [], [], []
        a = types.SimpleNamespace(device_id=self.a["device"], addr=("local", 0), text=events_a.append, error=errors.append)
        b = types.SimpleNamespace(device_id=self.b["device"], addr=("local", 0), text=events_b.append, error=errors.append)
        ns["clients"] = [a, b]
        call = ns["Call"]()
        call.poll = lambda: None
        call.state, call.dir, call.call_id = "incoming", "in", str(uuid.uuid4())
        call.notify()
        self.assertEqual(events_a[-1]["state"], "incoming")
        self.assertEqual(events_b[-1]["state"], "idle")
        self.assertEqual(sent[-1][2], self.a["device"])
        self.push.select_device(self.b["device"])
        call.notify()
        self.assertEqual(events_a[-1]["state"], "incoming")
        self.assertEqual(events_b[-1]["state"], "idle")
        for wifi in (False, True):
            call.wifi = wifi
            for op in ("answer", "hangup"):
                call.op(b, {"op": op, "call_id": call.call_id})
                self.assertEqual(errors[-1], "incoming call belongs to another device")
        call.wifi = False
        call.op(a, {"op": "answer", "call_id": call.call_id})
        self.assertIs(call.owner, a)
        call.reset()
        call.state, call.dir, call.call_id = "incoming", "in", str(uuid.uuid4())
        call.notify()
        self.assertEqual(events_a[-1]["state"], "idle")
        self.assertEqual(events_b[-1]["state"], "incoming")
        self.assertEqual(sent[-1][2], self.b["device"])
        self.save([{"device": self.a["device"], "token": self.a["token"], "environment": "development", "selected": False}])
        call.reset()
        call.state, call.dir, call.call_id = "incoming", "in", str(uuid.uuid4())
        call.notify()
        self.assertIsNone(call.recipient)
        self.assertEqual(events_a[-1]["state"], "incoming")
        self.assertEqual(events_b[-1]["state"], "incoming")
        self.assertEqual(sent[-1][2], "")


if __name__ == "__main__":
    unittest.main()
