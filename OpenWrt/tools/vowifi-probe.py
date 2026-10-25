#!/usr/bin/env python3
"""Developer-only Wi-Fi Calling probe; not included in the OpenWrt payload."""
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "files/usr/lib/dongle-vowifi"))
import vowifi as V


class RemoteSim:
    def __init__(self, host, key, aid):
        self.host, self.key, self.aid = host, key, aid

    def _ssh(self, command, timeout=40, input_text=None):
        argv = ["ssh", "-i", self.key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "StrictHostKeyChecking=accept-new", f"root@{self.host}", command]
        p = subprocess.run(argv, input=input_text, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            raise V.SimError(f"ssh rc={p.returncode}: {p.stderr.strip()[:160]}")
        return p.stdout

    def install_helper(self):
        script = f"""#!/bin/sh
AID="{self.aid}"
SID=$(dongle-at cmd "AT+CCHO=\\"$AID\\"" | sed -n 's/^+CCHO: *//p' | tr -d '\\r')
if [ -z "$SID" ]; then echo "CCHO_FAIL"; exit 2; fi
OUT=$(dongle-at cmd "AT+CGLA=$SID,78,\\"$1\\"")
I=0
while [ $I -lt 4 ]; do
    HEX=$(printf '%s\\n' "$OUT" | sed -n 's/.*"\\([0-9A-Fa-f]*\\)".*/\\1/p' | tail -n 1)
    SW=$(printf '%s' "$HEX" | sed 's/.*\\(....\\)$/\\1/')
    case "$SW" in
        61*) LEN=${{SW#61}}; OUT=$(dongle-at cmd "AT+CGLA=$SID,10,\\"01C00000$LEN\\"");;
        *) break;;
    esac
    I=$((I+1))
done
dongle-at cmd "AT+CCHC=$SID" >/dev/null 2>&1
printf '%s\\n' "$OUT"
"""
        self._ssh("cat > /tmp/sim_aka.sh && chmod 700 /tmp/sim_aka.sh", input_text=script)

    def remove_helper(self):
        try: self._ssh("rm -f /tmp/sim_aka.sh")
        except Exception: pass

    def imsi(self):
        out = self._ssh("dongle-at cmd AT+CIMI")
        for line in out.splitlines():
            if re.fullmatch(r"\d{15}", line.strip()): return line.strip()
        raise V.SimError("cannot read IMSI")

    def imei(self):
        out = self._ssh("dongle-at cmd AT+GSN")
        for line in out.splitlines():
            if re.fullmatch(r"\d{14,16}", line.strip()): return line.strip()
        raise V.SimError("cannot read IMEI")

    def authenticate(self, rand, autn):
        apdu = "0188008122" + "10" + rand.hex() + "10" + autn.hex()
        out = self._ssh(f"sh /tmp/sim_aka.sh {apdu}", timeout=50)
        m = re.search(r'\+CGLA:\s*\d+,"([0-9A-Fa-f]*)"', out)
        if not m: raise V.SimError(f"no CGLA: {out.strip()[:120]}")
        raw = m.group(1).upper()
        sw = raw[-4:] if len(raw) >= 4 else ""
        data = bytes.fromhex(raw[:-4]) if len(raw) >= 4 else b""
        if sw != "9000": return ("SW", sw)
        if not data: raise V.SimError("empty response")
        vals, i = [], 1
        while i < len(data):
            ln = data[i]; vals.append(data[i + 1:i + 1 + ln]); i += 1 + ln
        if data[0] == 0xDB and len(vals) >= 3: return ("OK", vals[0], vals[1], vals[2])
        if data[0] == 0xDC and vals: return ("SYNC", vals[0])
        raise V.SimError(f"unknown response tag {data[0]:02x}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epdg", required=True, help="ePDG IP address")
    ap.add_argument("--mcc", required=True, help="home PLMN MCC (3 digits)")
    ap.add_argument("--mnc", required=True, help="home PLMN MNC (2 or 3 digits)")
    ap.add_argument("--stage", choices=["esp", "ims-challenge", "ims", "task5"], default="esp")
    ap.add_argument("--pcscf", default=None)
    ap.add_argument("--sec-alg", default="sha1-aes",
                    choices=["sha1-aes", "md5-3des", "sha1-3des", "md5-aes", "md5-null", "sha1-null", "null", "none"])
    ap.add_argument("--sec-zero", action="store_true")
    ap.add_argument("--sec-mod", default="trans", choices=["trans", "tun"])
    ap.add_argument("--fake-user", default=None, help="diagnostic only")
    ap.add_argument("--uc-port", type=int, default=5062)
    ap.add_argument("--us-port", type=int, default=5064)
    ap.add_argument("--no-pani", action="store_true")
    ap.add_argument("--sim-host", required=True, help="OpenWrt node hostname or address")
    ap.add_argument("--sim-key", required=True, help="SSH private-key path")
    ap.add_argument("--sim-aid", default="A0000000871002FF49FF0589")
    args = ap.parse_args()
    if not re.fullmatch(r"\d{3}", args.mcc):
        ap.error("--mcc must contain 3 digits")
    if not re.fullmatch(r"\d{2,3}", args.mnc):
        ap.error("--mnc must contain 2 or 3 digits")
    mnc3 = args.mnc.zfill(3)
    domain = f"ims.mnc{mnc3}.mcc{args.mcc}.3gppnetwork.org"

    sim = RemoteSim(args.sim_host, str(Path(args.sim_key).expanduser()), args.sim_aid)
    sim.install_helper()
    ike = None
    try:
        imsi = sim.imsi(); imei = sim.imei()
        nai = f"0{imsi}@nai.epc.mnc{mnc3}.mcc{args.mcc}.3gppnetwork.org"
        print(f"[sim] IMSI {V.mask_imsi(imsi)}")
        ike = V.IkeEsp(args.epdg, nai, imsi, imei)
        ike.sim = sim
        ike.sa_init()
        ike.ike_auth()
        ike.derive_child()
        if not ike.cp["ip4"] or not ike.cp["pcscf4"]:
            raise RuntimeError("missing CP IPv4/P-CSCF")
        src_ip = ike.cp["ip4"][0]
        pcscf = args.pcscf or ike.cp["pcscf4"][0]
        print(f"[cp] using {src_ip}, P-CSCF {pcscf}")
        if args.stage == "esp":
            sip = V.sip_options(pcscf, src_ip)
            inner = V.build_ipv4_udp(src_ip, pcscf, 5062, 5060, sip)
            ike.send_outer(inner)
            resp = ike.recv_outer(time.time() + 6)
            if resp:
                u = V.parse_ipv4_udp(resp)
                print(f"[esp] inner {u['src']}:{u.get('sport')} -> {u['dst']}:{u.get('dport')} proto={u['proto']}")
                if u["proto"] == 17:
                    print("[sip] " + u["payload"].split(b"\r\n", 1)[0].decode("utf-8", "replace"))
            else:
                print("[esp] no response")
            return
        if args.stage == "task5":
            V.run_task5(ike, sim, imsi, imei, pcscf, domain)
            return
        imsi_domain = domain
        alg_map = {"sha1-aes": ("hmac-sha-1-96", "aes-cbc"),
                   "md5-3des": ("hmac-md5-96", "des-ede3-cbc"),
                   "sha1-3des": ("hmac-sha-1-96", "des-ede3-cbc"),
                   "md5-aes": ("hmac-md5-96", "aes-cbc"),
                   "md5-null": ("hmac-md5-96", "null"),
                   "sha1-null": ("hmac-sha-1-96", "null"),
                   "null": ("null", "null"),
                   "none": None}
        client = V.ImsClient(ike, pcscf, imsi_domain, imsi,
                           f"{imsi}@{imsi_domain}", f"sip:{imsi}@{imsi_domain}", sim,
                           sec_alg=alg_map[args.sec_alg], sec_zero=args.sec_zero, sec_mod=args.sec_mod,
                           uc_port=args.uc_port, us_port=args.us_port, pani=not args.no_pani)
        code, reason, headers = client.run(challenge_only=(args.stage == "ims-challenge"))
        if args.stage == "ims" and code == 200:
            print("[ims] REGISTER 200 OK")
            for v in headers.get("p-associated-uri", []):
                print(f"[ims] P-Associated-URI {V.mask_uri(v)}")
            for v in headers.get("service-route", []):
                print(f"[ims] Service-Route {V.mask_uri(v)}")
            for v in headers.get("expires", []):
                print(f"[ims] Expires {v}")
            resp = client.deregister()
            if resp:
                print(f"[ims] deregister {resp[0]} {resp[1]}")
    finally:
        try:
            if ike: ike.send_ike_delete()
        except Exception as e:
            print(f"[cleanup] {e}")
        sim.remove_helper()


if __name__ == "__main__":
    try: main()
    except Exception as e:
        print(f"[fatal] {type(e).__name__}: {e}", file=sys.stderr); sys.exit(1)
