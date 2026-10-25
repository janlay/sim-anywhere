#!/usr/bin/env python3
"""
M1 ePDG test client: IKEv2 + EAP-AKA, ESP data plane, SIP OPTIONS.
Secrets (RES/CK/IK/MSK/SK_*) are never printed. IMSI is masked.
"""
import base64
import hashlib
import hmac as hmaclib
import os
import re
import socket
import struct
import subprocess
import time
import builtins

from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.asymmetric import dh
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# IKE payload types
SA, KE, IDI, IDR, CERT, CERTREQ, AUTH = 33, 34, 35, 36, 37, 38, 39
NONCE, NOTIFY, DELETE, VENDOR, TSI, TSR, SK, CP, EAP = 40, 41, 42, 43, 44, 45, 46, 47, 48
IKE_SA_INIT, IKE_AUTH, CREATE_CHILD_SA, INFORMATIONAL = 34, 35, 36, 37
ENCR, PRF, INTEG, DH_T, ESN = 1, 2, 3, 4, 5
AES_CBC = 12
PRF_HMAC_SHA1 = 2
HMAC_SHA1_96 = 2
MODP1024 = 2
AT_RAND, AT_AUTN, AT_RES, AT_AUTS = 1, 2, 3, 4
AT_MAC, AT_NOTIFICATION, AT_IDENTITY = 11, 12, 14
EAP_REQUEST, EAP_RESPONSE, EAP_SUCCESS, EAP_FAILURE = 1, 2, 3, 4
EAP_AKA = 23
AKA_CHALLENGE, AKA_SYNC_FAILURE, AKA_IDENTITY, AKA_NOTIFICATION = 1, 4, 5, 12
COOKIE = 16390
NAT_DETECTION_SOURCE_IP = 16388
NAT_DETECTION_DESTINATION_IP = 16389
EAP_ONLY_AUTHENTICATION = 16417
DEVICE_IDENTITY = 41101

P1024 = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74"
    "020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F1437"
    "4FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE65381FFFFFFFFFFFFFFFF", 16)


def u16(v): return struct.pack("!H", v)
def u32(v): return struct.pack("!I", v)
def sha1(b): return hashlib.sha1(b).digest()
def hmac_sha1(key, data): return hmaclib.new(key, data, hashlib.sha1).digest()
def hmac_sha1_96(key, data): return hmac_sha1(key, data)[:12]
def hmac_md5_96(key, data): return hmaclib.new(key, data, hashlib.md5).digest()[:12]

try:
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES as _TripleDES
except Exception:
    try:
        from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES as _TripleDES
    except Exception:
        _TripleDES = None


def sha1_dss(data):
    h0, h1, h2, h3, h4 = 0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0

    def rol(n, b): return ((n << b) | (n >> (32 - b))) & 0xFFFFFFFF

    padded = data + 44 * b"\x00"
    for off in range(0, len(padded), 64):
        w = list(struct.unpack(">16I", padded[off:off + 64])) + [0] * 64
        for i in range(16, 80):
            w[i] = rol(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1)
        a, b, c, d, e = h0, h1, h2, h3, h4
        for i in range(80):
            if i < 20: f, k = (b & c) | ((~b) & d), 0x5A827999
            elif i < 40: f, k = b ^ c ^ d, 0x6ED9EBA1
            elif i < 60: f, k = (b & c) | (b & d) | (c & d), 0x8F1BBCDC
            else: f, k = b ^ c ^ d, 0xCA62C1D6
            a, b, c, d, e = (rol(a, 5) + f + e + k + w[i]) & 0xFFFFFFFF, a, rol(b, 30), c, d
        h0 = (h0 + a) & 0xFFFFFFFF
        h1 = (h1 + b) & 0xFFFFFFFF
        h2 = (h2 + c) & 0xFFFFFFFF
        h3 = (h3 + d) & 0xFFFFFFFF
        h4 = (h4 + e) & 0xFFFFFFFF
    return struct.pack("!IIIII", h0, h1, h2, h3, h4)


def prf_sha1(key, data): return hmac_sha1(key, data)


def prf_plus(key, stream, size):
    out, t, i = b"", b"", 1
    while len(out) < size:
        t = hmac_sha1(key, t + stream + bytes([i]))
        out += t
        i += 1
    return out[:size]


def eap_keys(identity, ck, ik):
    mk = sha1(identity.encode("utf-8") + ik + ck)
    result, xval = b"", mk
    mod = 1 << 160
    for _ in range(4):
        w0 = sha1_dss(xval)
        xval = ((int.from_bytes(xval, "big") + int.from_bytes(w0, "big") + 1) % mod).to_bytes(20, "big")
        w1 = sha1_dss(xval)
        xval = ((int.from_bytes(xval, "big") + int.from_bytes(w1, "big") + 1) % mod).to_bytes(20, "big")
        result += w0 + w1
    return result[0:16], result[16:32], result[32:96], result[96:160]


def pad16(data):
    n = 16 - (len(data) % 16)
    if n == 0: n = 16
    return data + b"\x00" * (n - 1) + bytes([n - 1])


def unpad16(data):
    n = data[-1]
    if n > 15 or n + 1 > len(data): raise ValueError("bad CBC padding")
    return data[:-(n + 1)]


def payload(nxt, body, critical=False):
    return bytes([nxt, 0x80 if critical else 0]) + u16(4 + len(body)) + body


def parse_payloads(data, start, first_type):
    out, pos, nxt = [], start, first_type
    while nxt != 0:
        if pos + 4 > len(data): raise ValueError("truncated payload")
        plen = struct.unpack("!H", data[pos + 2:pos + 4])[0]
        if plen < 4 or pos + plen > len(data): raise ValueError("bad payload length")
        out.append((nxt, data[pos + 4:pos + plen]))
        nxt = data[pos]
        pos += plen
    return out


def mask_imsi(imsi):
    return f"{imsi[:5]}******{imsi[-2:]}" if len(imsi) > 7 else "****"


class SimError(Exception):
    pass


class LocalSim:
    """USIM access on the node itself, through dongle-at."""
    def __init__(self, aid):
        self.aid = aid

    @staticmethod
    def _at(cmd, timeout=20):
        p = subprocess.run(["dongle-at", "cmd", cmd, str(timeout)],
                           capture_output=True, text=True, timeout=timeout + 20)
        return (p.stdout or "") + (p.stderr or "")

    def install_helper(self):
        pass

    def remove_helper(self):
        pass

    def imsi(self):
        out = self._at("AT+CIMI")
        for line in out.splitlines():
            if re.fullmatch(r"\d{15}", line.strip()):
                return line.strip()
        raise SimError("cannot read IMSI")

    def imei(self):
        out = self._at("AT+CGSN")
        for line in out.splitlines():
            if re.fullmatch(r"\d{14,16}", line.strip()):
                return line.strip()
        raise SimError("cannot read IMEI")

    def mnc_length(self):
        """Read the HPLMN MNC length from USIM EF-AD (TS 31.102)."""
        out = self._at('AT+CCHO="%s"' % self.aid)
        match = re.search(r"\+CCHO:\s*(\d+)", out)
        if not match:
            raise SimError("cannot open USIM application")
        sid = int(match.group(1))

        def apdu(command):
            response = self._at('AT+CGLA=%d,%d,"%s"' %
                                (sid, len(command), command), timeout=20)
            got = re.search(r'\+CGLA:\s*\d+,"([0-9A-Fa-f]*)"', response)
            if not got or len(got.group(1)) < 4:
                raise SimError("USIM EF-AD read failed")
            raw = bytes.fromhex(got.group(1))
            return raw[:-2], raw[-2:]

        try:
            _, sw = apdu("00A4000C026FAD")  # SELECT EF-AD
            if sw != b"\x90\x00":
                raise SimError("USIM EF-AD is unavailable")
            data, sw = apdu("00B0000004")  # READ BINARY, four bytes
            if sw != b"\x90\x00" or len(data) < 4:
                raise SimError("USIM EF-AD is unavailable")
            length = data[3] & 0x03
            if length not in (2, 3):
                raise SimError("invalid USIM MNC length")
            return length
        finally:
            self._at("AT+CCHC=%d" % sid, timeout=10)

    def ensure_auto_registration(self, timeout=120):
        """Keep the modem in automatic PLMN selection and wait for registration."""
        out = self._at("AT+COPS?", timeout=10)
        match = re.search(r"\+COPS:\s*(\d+)", out)
        if not match:
            raise SimError("cannot read network selection mode")
        if int(match.group(1)) != 0:
            result = self._at("AT+COPS=0", timeout=60)
            if re.search(r"(?:^|\s)(?:ERROR|\+CME ERROR)", result):
                raise SimError("cannot enable automatic network selection")
        deadline = time.time() + timeout
        while time.time() < deadline:
            out = self._at("AT+CEREG?", timeout=10)
            states = re.findall(r"\+CEREG:\s*(?:\d+,)?(\d+)", out)
            if states and int(states[-1]) in (1, 5):
                return int(states[-1])
            time.sleep(3)
        raise SimError("modem did not register on an available PLMN")

    def serving_plmn(self):
        """Read the serving PLMN as MCC+MNC without leaving COPS format changed."""
        out = self._at("AT+COPS?", timeout=10)
        match = re.search(r"\+COPS:\s*\d+,(\d+),", out)
        if not match:
            raise SimError("cannot read serving PLMN")
        old_format = int(match.group(1))
        try:
            if old_format != 2:
                result = self._at("AT+COPS=3,2", timeout=10)
                if re.search(r"(?:^|\s)(?:ERROR|\+CME ERROR)", result):
                    raise SimError("cannot request numeric serving PLMN")
            out = self._at("AT+COPS?", timeout=10)
            match = re.search(r'\+COPS:\s*\d+,\d+,"?(\d{5,6})"?', out)
            if not match:
                raise SimError("modem did not report numeric serving PLMN")
            return match.group(1)
        finally:
            if old_format != 2:
                self._at("AT+COPS=3,%d" % old_format, timeout=10)

    def authenticate(self, rand, autn):
        ch_out = self._at('AT+CCHO="%s"' % self.aid)
        m = re.search(r"\+CCHO:\s*(\d+)", ch_out)
        if not m:
            raise SimError("AT+CCHO failed: %s" % ch_out.strip()[:120])
        sid = int(m.group(1))
        apdu = "0188008122" + "10" + rand.hex() + "10" + autn.hex()
        out = self._at('AT+CGLA=%d,%d,"%s"' % (sid, len(apdu), apdu.upper()), timeout=30)
        raw = None
        for _ in range(4):
            mm = re.search(r'\+CGLA:\s*\d+,"([0-9A-Fa-f]*)"', out)
            if not mm:
                break
            raw = mm.group(1).upper()
            sw = raw[-4:] if len(raw) >= 4 else ""
            if sw.startswith("61"):
                le = sw[2:]
                out = self._at('AT+CGLA=%d,10,"01C00000%s"' % (sid, le), timeout=30)
                continue
            break
        self._at("AT+CCHC=%d" % sid, timeout=10)
        if raw is None:
            raise SimError("no CGLA: %s" % out.strip()[:120])
        sw = raw[-4:] if len(raw) >= 4 else ""
        data = bytes.fromhex(raw[:-4]) if len(raw) >= 4 else b""
        if sw != "9000":
            return ("SW", sw)
        if not data:
            raise SimError("empty response")
        vals, i = [], 1
        while i < len(data):
            ln = data[i]
            vals.append(data[i + 1:i + 1 + ln])
            i += 1 + ln
        if data[0] == 0xDB and len(vals) >= 3:
            return ("OK", vals[0], vals[1], vals[2])
        if data[0] == 0xDC and vals:
            return ("SYNC", vals[0])
        raise SimError("unknown response tag %02x" % data[0])


def esp_pack(payload, key_e, key_a, spi, seq, next_header):
    iv = os.urandom(16)
    pad = (16 - ((len(payload) + 2) % 16)) % 16
    plain = payload + b"\x00" * pad + bytes([pad, next_header])
    enc = Cipher(algorithms.AES(key_e), modes.CBC(iv)).encryptor()
    ct = enc.update(plain) + enc.finalize()
    spi_b = spi if isinstance(spi, bytes) else u32(spi)
    hdr = spi_b + u32(seq) + iv + ct
    return hdr + hmac_sha1_96(key_a, hdr)


def esp_unpack(pkt, key_e, key_a, spi, next_header=None):
    if len(pkt) < 4 + 4 + 16 + 16 + 12:
        return None
    got_spi, seq = struct.unpack("!II", pkt[:8])
    if spi is not None:
        spi_i = int.from_bytes(spi, "big") if isinstance(spi, bytes) else spi
        if got_spi != spi_i:
            return None
    iv, ct, icv = pkt[8:24], pkt[24:-12], pkt[-12:]
    if not hmaclib.compare_digest(icv, hmac_sha1_96(key_a, pkt[:-12])):
        print(f"[esp] ICV FAIL spi={got_spi:08x} seq={seq}")
        return None
    dec = Cipher(algorithms.AES(key_e), modes.CBC(iv)).decryptor()
    pt = dec.update(ct) + dec.finalize()
    pad, nxt = pt[-2], pt[-1]
    if next_header is not None and nxt != next_header:
        print(f"[esp] next header {nxt} != {next_header}")
        return None
    return pt[:-(pad + 2)]


def esp_pack_alg(payload, alg, ealg, ck, ik, spi, seq, next_header):
    alg = (alg or "null").lower()
    ealg = (ealg or "null").lower()
    if ealg == "aes-cbc":
        block, iv_len = 16, 16
    elif ealg in ("des-ede3-cbc", "3des"):
        block, iv_len = 8, 8
    elif ealg == "null":
        block, iv_len = 4, 0
    else:
        raise ValueError(f"unsupported ealg {ealg}")
    iv = os.urandom(iv_len) if iv_len else b""
    pad = (block - ((len(payload) + 2) % block)) % block
    plain = payload + b"\x00" * pad + bytes([pad, next_header])
    if ealg == "aes-cbc":
        enc = Cipher(algorithms.AES(ck), modes.CBC(iv)).encryptor()
        ct = enc.update(plain) + enc.finalize()
    elif ealg in ("des-ede3-cbc", "3des"):
        if _TripleDES is None:
            raise ValueError("TripleDES unavailable")
        enc = Cipher(_TripleDES(ck), modes.CBC(iv)).encryptor()
        ct = enc.update(plain) + enc.finalize()
    else:
        ct = plain
    spi_b = spi if isinstance(spi, bytes) else u32(spi)
    hdr = spi_b + u32(seq) + iv + ct
    if alg == "hmac-sha-1-96":
        icv = hmac_sha1_96(ik, hdr)
    elif alg == "hmac-md5-96":
        icv = hmac_md5_96(ik, hdr)
    elif alg == "null":
        icv = b""
    else:
        raise ValueError(f"unsupported alg {alg}")
    return hdr + icv


def esp_unpack_alg(pkt, alg, ealg, ck, ik, spi, next_header=None):
    alg = (alg or "null").lower()
    ealg = (ealg or "null").lower()
    if ealg == "aes-cbc":
        iv_len = 16
    elif ealg in ("des-ede3-cbc", "3des"):
        iv_len = 8
    elif ealg == "null":
        iv_len = 0
    else:
        return None
    if len(pkt) < 8 + iv_len:
        return None
    got_spi, seq = struct.unpack("!II", pkt[:8])
    if spi is not None:
        spi_i = int.from_bytes(spi, "big") if isinstance(spi, bytes) else spi
        if got_spi != spi_i:
            return None
    if alg in ("hmac-sha-1-96", "hmac-md5-96"):
        if len(pkt) < 8 + iv_len + 12:
            return None
        body, icv = pkt[:-12], pkt[-12:]
        calc = hmac_sha1_96(ik, body) if alg == "hmac-sha-1-96" else hmac_md5_96(ik, body)
        if not hmaclib.compare_digest(icv, calc):
            print(f"[ims] inner ESP ICV fail spi={got_spi:08x}")
            return None
    else:
        body = pkt
    iv = body[8:8 + iv_len]
    ct = body[8 + iv_len:]
    if ealg == "aes-cbc":
        dec = Cipher(algorithms.AES(ck), modes.CBC(iv)).decryptor()
        pt = dec.update(ct) + dec.finalize()
    elif ealg in ("des-ede3-cbc", "3des"):
        if _TripleDES is None:
            return None
        dec = Cipher(_TripleDES(ck), modes.CBC(iv)).decryptor()
        pt = dec.update(ct) + dec.finalize()
    else:
        pt = ct
    pad, nxt = pt[-2], pt[-1]
    if next_header is not None and nxt != next_header:
        print(f"[ims] inner ESP next header {nxt}")
        return None
    return pt[:-(pad + 2)]


class IkeEsp:
    def __init__(self, epdg, nai, imsi, imei, timeout=4.0, local_ip=None, mark=0x401):
        self.epdg, self.nai, self.imsi, self.imei = epdg, nai, imsi, imei
        self.force_local_ip = local_ip
        self.mark = mark
        self.timeout = timeout
        self.msgid = 0
        self.spi_i = os.urandom(8)
        self.spi_r = b"\x00" * 8
        self.ni = os.urandom(32)
        self.nr = None
        self.cookie = None
        self.sock = None
        self.port = 500
        self.remote = (epdg, 500)
        self.local_ip = self._local_ip()
        self.ike = {}
        self.eap = {}
        self.sa_init_packet = None
        self.child_spi_i = os.urandom(4)
        self.child_spi_r = None
        self.esp = {}
        self.outer_seq = 0
        self.inner_seq = 0
        self.ike_queue = []
        self.esp_queue = []
        self.cp = {"ip4": [], "dns4": [], "ip6": [], "dns6": [], "pcscf4": [], "pcscf6": []}
        self.device_id_requested = False
        self.last_eap_id = 0
        self.ts_payloads = []

    def _local_ip(self):
        return getattr(self, "force_local_ip", None) or self._detect_local_ip()

    def _detect_local_ip(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((self.epdg, 500)); return s.getsockname()[0]
        finally: s.close()

    def open_socket(self, port):
        if self.sock: self.sock.close()
        self.port = port
        self.remote = (self.epdg, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if getattr(self, "force_local_ip", None):
            self.sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_MARK", 36), self.mark)
            self.sock.bind((self.force_local_ip, port))
        else:
            self.sock.bind(("0.0.0.0", port))
        self.sock.settimeout(self.timeout)
        print(f"[net] {self.local_ip}:{port} -> {self.epdg}:{port}" + (" NAT-T" if port == 4500 else ""))

    def send_ike(self, data):
        if self.port == 4500: data = b"\x00\x00\x00\x00" + data
        self.sock.sendto(data, self.remote)

    def recv_pkt(self):
        data, addr = self.sock.recvfrom(65535)
        if self.port == 4500:
            if data.startswith(b"\x00\x00\x00\x00"):
                return "ike", data[4:]
            if data == b"\xff":
                return "keepalive", data
            return "esp", data
        return "ike", data

    def exchange_ike(self, packet, msgid, label, retries=1):
        for attempt in range(retries + 1):
            self.send_ike(packet)
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                try: kind, data = self.recv_pkt()
                except socket.timeout: break
                if kind == "esp": self.esp_queue.append(data); continue
                if kind == "keepalive": continue
                if len(data) < 28: continue
                rid = struct.unpack("!I", data[20:24])[0]
                if rid != msgid: continue
                return data
        raise TimeoutError(f"{label}: no response")

    # ---------- IKE SA ----------
    def generate_dh(self):
        params = dh.DHParameterNumbers(P1024, 2).parameters()
        self.dh_priv = params.generate_private_key()
        self.dh_pub = self.dh_priv.public_key().public_numbers().y.to_bytes(128, "big")

    def derive_ike(self):
        peer = dh.DHPublicNumbers(int.from_bytes(self.peer_ke, "big"),
                                  dh.DHParameterNumbers(P1024, 2)).public_key()
        shared = self.dh_priv.exchange(peer)
        skeyseed = prf_sha1(self.ni + self.nr, shared)
        stream = self.ni + self.nr + self.spi_i + self.spi_r
        km = prf_plus(skeyseed, stream, 132)
        self.ike = {"SK_d": km[0:20], "SK_ai": km[20:40], "SK_ar": km[40:60],
                    "SK_ei": km[60:76], "SK_er": km[76:92], "SK_pi": km[92:112], "SK_pr": km[112:132]}
        print("[ike] IKE keys derived")

    def derive_child(self):
        km = prf_plus(self.ike["SK_d"], self.ni + self.nr, 72)
        self.esp = {"ei": km[0:16], "ai": km[16:36], "er": km[36:52], "ar": km[52:72]}
        print("[ike] child SA keys derived")

    def build_sa_init(self, cookie=None):
        def tr(t, i, attr=b"", last=3):
            b = bytes([last, 0, 0, 0, t, 0]) + u16(i) + attr
            return b[:2] + u16(len(b)) + b[4:]
        transforms = tr(ENCR, AES_CBC, u16(0x800E) + u16(128))
        transforms += tr(PRF, PRF_HMAC_SHA1)
        transforms += tr(INTEG, HMAC_SHA1_96)
        transforms += tr(DH_T, MODP1024, last=0)
        prop = bytes([0, 0, 0, 0, 1, 1, 0, 4]) + transforms
        prop = prop[:2] + u16(len(prop)) + prop[4:]
        body = payload(KE, prop)
        body += payload(NONCE, u16(MODP1024) + u16(0) + self.dh_pub)
        body += payload(NOTIFY, self.ni)

        def natd(addr, port):
            return sha1(self.spi_i + self.spi_r + socket.inet_aton(addr) + u16(port))
        body += payload(NOTIFY, b"\x00\x00" + u16(NAT_DETECTION_SOURCE_IP) + natd(self.local_ip, self.port))
        body += payload(0, b"\x00\x00" + u16(NAT_DETECTION_DESTINATION_IP) + natd(self.epdg, self.port))
        if cookie is not None:
            body = payload(SA, b"\x00\x00" + u16(COOKIE) + cookie) + body
            return self.spi_i + self.spi_r + bytes([NOTIFY, 0x20, IKE_SA_INIT, 0x08]) + u32(0) + u32(28 + len(body)) + body
        return self.spi_i + self.spi_r + bytes([SA, 0x20, IKE_SA_INIT, 0x08]) + u32(0) + u32(28 + len(body)) + body

    def parse_sa(self, body):
        out, pos = {"proposals": []}, 0
        while pos + 8 <= len(body):
            last, plen = body[pos], struct.unpack("!H", body[pos + 2:pos + 4])[0]
            num, proto, spisz, ntrans = body[pos + 4], body[pos + 5], body[pos + 6], body[pos + 7]
            spi = body[pos + 8:pos + 8 + spisz]
            tpos = pos + 8 + spisz
            trs = []
            for _ in range(ntrans):
                tlen = struct.unpack("!H", body[tpos + 2:tpos + 4])[0]
                ttype, tid = body[tpos + 4], struct.unpack("!H", body[tpos + 6:tpos + 8])[0]
                attrs, apos = {}, tpos + 8
                while apos + 4 <= tpos + tlen:
                    at = struct.unpack("!H", body[apos:apos + 2])[0]
                    if at & 0x8000:
                        attrs[at & 0x7FFF] = struct.unpack("!H", body[apos + 2:apos + 4])[0]; apos += 4
                    else:
                        alen = struct.unpack("!H", body[apos + 2:apos + 4])[0]
                        attrs[at] = body[apos + 4:apos + 2 + alen]; apos += 2 + alen
                trs.append({"type": ttype, "id": tid, "attrs": attrs})
                tpos += tlen
            out["proposals"].append({"proto": proto, "spi": spi, "transforms": trs})
            pos += plen
            if last == 0: break
        return out

    def describe_sa(self, sa):
        names = {(1, 12): "AES-CBC", (1, 20): "AES-GCM-16", (2, 2): "PRF-HMAC-SHA1",
                 (3, 2): "HMAC-SHA1-96", (4, 2): "MODP1024", (5, 0): "NO-ESN"}
        parts = []
        for p in sa["proposals"]:
            for t in p["transforms"]:
                n = names.get((t["type"], t["id"]), f"t{t['type']}={t['id']}")
                if t["type"] == 1 and 14 in t["attrs"]: n += f"-{t['attrs'][14]}"
                parts.append(n)
        return ", ".join(parts)

    def sa_init(self):
        self.generate_dh()
        self.open_socket(500)
        pkt = self.build_sa_init()
        for rnd in range(4):
            if rnd: pkt = self.build_sa_init(self.cookie)
            print(f"[ike] SA_INIT send" + (" +cookie" if rnd else ""))
            resp = self.exchange_ike(pkt, 0, "SA_INIT")
            self.sa_init_packet = pkt
            for ptype, body in parse_payloads(resp, 28, resp[16]):
                if ptype == NOTIFY:
                    ntype = struct.unpack("!H", body[2:4])[0]
                    nd = body[4 + body[1]:]
                    print(f"[ike] notify {ntype} len={len(nd)}")
                    if ntype == COOKIE: self.cookie = nd
                    elif ntype == 17: raise RuntimeError("INVALID_KE_PAYLOAD")
                    elif ntype == 14: raise RuntimeError("NO_PROPOSAL_CHOSEN")
                elif ptype == SA:
                    self.selected_ike = self.parse_sa(body)
                elif ptype == KE:
                    self.peer_ke = body[4:]
                elif ptype == NONCE:
                    self.nr = body
            if self.cookie is not None and not hasattr(self, "selected_ike"):
                continue
            break
        self.spi_r = resp[8:16]
        print(f"[ike] IKE SA: {self.describe_sa(self.selected_ike)}")
        print(f"[ike] SPIi={self.spi_i.hex()} SPIr={self.spi_r.hex()}")
        self.derive_ike()
        # detect NAT from NAT-D notifies
        src = dst = None
        for ptype, body in parse_payloads(resp, 28, resp[16]):
            if ptype == NOTIFY:
                ntype = struct.unpack("!H", body[2:4])[0]
                if ntype == NAT_DETECTION_SOURCE_IP: src = body[4 + body[1]:]
                if ntype == NAT_DETECTION_DESTINATION_IP: dst = body[4 + body[1]:]
        if src and dst:
            calc_s = sha1(self.spi_i + self.spi_r + socket.inet_aton(self.local_ip) + u16(self.port))
            calc_d = sha1(self.spi_i + self.spi_r + socket.inet_aton(self.epdg) + u16(self.port))
            nat = calc_s != src or calc_d != dst
            print(f"[ike] NAT detected={nat}")
            if nat:
                self.open_socket(4500)

    # ---------- IKE_AUTH ----------
    def encrypt(self, inner, msgid, exchange=IKE_AUTH, flags=0x08):
        body = b""
        for i, (t, b) in enumerate(inner):
            body += payload(inner[i + 1][0] if i + 1 < len(inner) else 0, b)
        iv = os.urandom(16)
        enc = Cipher(algorithms.AES(self.ike["SK_ei"]), modes.CBC(iv)).encryptor()
        ct = enc.update(pad16(body)) + enc.finalize()
        sk_len = 4 + 16 + len(ct) + 12
        total = 28 + sk_len
        hdr = self.spi_i + self.spi_r + bytes([SK, 0x20, exchange, flags]) + u32(msgid) + u32(total)
        msg = hdr + bytes([inner[0][0], 0]) + u16(sk_len) + iv + ct
        return msg + hmac_sha1_96(self.ike["SK_ai"], msg)

    def decrypt(self, msg):
        plen = struct.unpack("!H", msg[30:32])[0]
        sk = msg[32:32 + plen - 4]
        iv, ct, mac = sk[:16], sk[16:-12], sk[-12:]
        if not hmaclib.compare_digest(mac, hmac_sha1_96(self.ike["SK_ar"], msg[:-12])):
            raise ValueError("IKE integrity failed")
        dec = Cipher(algorithms.AES(self.ike["SK_er"]), modes.CBC(iv)).decryptor()
        return parse_payloads(unpad16(dec.update(ct) + dec.finalize()), 0, msg[28])

    def send_response(self, inner, msgid, exchange=INFORMATIONAL):
        self.send_ike(self.encrypt(inner, msgid, exchange, flags=0x28))

    def send_empty_response(self, msgid, exchange=INFORMATIONAL, flags=0x28):
        self.send_ike(self.empty_msg(msgid, exchange, flags))

    def empty_msg(self, msgid, exchange=INFORMATIONAL, flags=0x28):
        iv = os.urandom(16)
        plain = pad16(b"")  # empty payload list: no content bytes, only padding (RFC 7296 §3.14)
        enc = Cipher(algorithms.AES(self.ike["SK_ei"]), modes.CBC(iv)).encryptor()
        ct = enc.update(plain) + enc.finalize()
        sk_len = 4 + 16 + len(ct) + 12
        total = 28 + sk_len
        hdr = self.spi_i + self.spi_r + bytes([SK, 0x20, exchange, flags]) + u32(msgid) + u32(total)
        msg = hdr + bytes([0, 0]) + u16(sk_len) + iv + ct
        return msg + hmac_sha1_96(self.ike["SK_ai"], msg)

    def id_payload(self, t, v): return bytes([t, 0, 0, 0]) + v

    def cp_request(self):
        attrs = b"".join(u16(t) + u16(0) for t in (1, 3, 8, 10, 20, 21))
        return bytes([1, 0, 0, 0]) + attrs

    def child_sa(self):
        def tr(t, i, attr=b"", last=3):
            b = bytes([last, 0, 0, 0, t, 0]) + u16(i) + attr
            return b[:2] + u16(len(b)) + b[4:]
        trs = tr(ENCR, AES_CBC, u16(0x800E) + u16(128)) + tr(INTEG, HMAC_SHA1_96) + tr(ESN, 0, last=0)
        prop = bytes([0, 0, 0, 0, 1, 3, 4, 3]) + self.child_spi_i + trs
        return prop[:2] + u16(len(prop)) + prop[4:]

    def ts(self, v6=True):
        ts = bytes([7, 0]) + u16(16) + u16(0) + u16(65535) + socket.inet_aton("0.0.0.0") + socket.inet_aton("255.255.255.255")
        if v6:
            ts += bytes([8, 0]) + u16(40) + u16(0) + u16(65535) + socket.inet_pton(socket.AF_INET6, "::") + socket.inet_pton(socket.AF_INET6, "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff")
        return bytes([2 if v6 else 1, 0, 0, 0]) + ts

    def ike_auth(self):
        self.msgid = 1
        inner = [(IDI, self.id_payload(3, self.nai.encode())), (IDR, self.id_payload(2, b"ims")),
                 (CP, self.cp_request()), (SA, self.child_sa()), (TSI, self.ts()), (TSR, self.ts()),
                 (NOTIFY, b"\x00\x00" + u16(EAP_ONLY_AUTHENTICATION))]
        resp = self.exchange_ike(self.encrypt(inner, self.msgid), self.msgid, "IKE_AUTH(1)")
        action = self.handle_auth(resp)
        while action in ("eap_identity", "eap_notification"):
            eap = self.build_eap_identity(self.last_eap_id) if action == "eap_identity" else self.build_eap_notification(self.last_eap_id)
            inner = [(EAP, eap)]
            if self.device_id_requested:
                inner.append((NOTIFY, self.device_identity_notify())); self.device_id_requested = False
            self.msgid += 1
            resp = self.exchange_ike(self.encrypt(inner, self.msgid), self.msgid, f"IKE_AUTH({action})")
            action = self.handle_auth(resp)
        if action != "eap_success":
            raise RuntimeError(f"IKE_AUTH did not reach EAP success (action={action})")
        self.send_auth()

    def handle_auth(self, resp):
        if resp[16] != SK:
            for ptype, body in parse_payloads(resp, 28, resp[16]):
                if ptype == NOTIFY:
                    ntype = struct.unpack("!H", body[2:4])[0]
                    raise RuntimeError(f"Notify {ntype}")
            raise RuntimeError("unencrypted response")
        payloads = self.decrypt(resp)
        print(f"[ike] IKE_AUTH recv msgid={struct.unpack('!I', resp[20:24])[0]} types={[t for t, _ in payloads]}")
        action, eap_body = None, None
        for ptype, body in payloads:
            if ptype == EAP: eap_body = body; continue
            if ptype == NOTIFY:
                ntype = struct.unpack("!H", body[2:4])[0]
                nd = body[4 + body[1]:]
                print(f"[ike] notify {ntype} len={len(nd)}")
                if ntype == DEVICE_IDENTITY:
                    self.device_id_requested = True
                    print(f"[ike] DEVICE_IDENTITY requested data={nd.hex()}")
                elif ntype < 16384:
                    raise RuntimeError(f"Notify error {ntype}")
            elif ptype == CP:
                self.parse_cp(body)
            elif ptype == SA:
                sa = self.parse_sa(body)
                for p in sa["proposals"]:
                    if p["proto"] == 3 and len(p["spi"]) == 4:
                        self.child_spi_r = p["spi"]
                print(f"[ike] child SA: {self.describe_sa(sa)} spi_r={self.child_spi_r.hex() if self.child_spi_r else '?'}")
            elif ptype == AUTH:
                print(f"[ike] responder AUTH method={body[0]} len={len(body) - 4}")
            elif ptype in (TSI, TSR):
                self.ts_payloads.append((ptype, body))
        if eap_body is not None: action = self.handle_eap(eap_body)
        return action

    def handle_eap(self, eap):
        code, eid, elen = eap[0], eap[1], struct.unpack("!H", eap[2:4])[0]
        self.last_eap_id = eid
        if code == EAP_FAILURE: raise RuntimeError("EAP-Failure")
        if code == EAP_SUCCESS: print("[eap] success"); return "eap_success"
        if code != EAP_REQUEST or len(eap) < 8: raise RuntimeError(f"EAP code {code}")
        subtype, attrs = eap[5], self.parse_attrs(eap[8:elen])
        print(f"[eap] subtype={subtype} attrs={[(t, len(v)) for t, v in attrs.items()]}")
        if subtype == AKA_IDENTITY: return "eap_identity"
        if subtype == AKA_NOTIFICATION:
            n = attrs.get(AT_NOTIFICATION, b"\x00\x00")
            print(f"[eap] notification code={struct.unpack('!H', n[:2])[0] if len(n) >= 2 else 0}")
            return "eap_notification"
        if subtype != AKA_CHALLENGE: raise RuntimeError(f"EAP-AKA subtype {subtype}")
        rand, autn = attrs.get(AT_RAND, b"")[2:18], attrs.get(AT_AUTN, b"")[2:18]
        mac = attrs.get(AT_MAC, b"")[2:18]
        if len(rand) != 16 or len(autn) != 16: raise RuntimeError("no RAND/AUTN")
        res = self.sim.authenticate(rand, autn)
        if res[0] == "SYNC":
            eap_resp = self.build_eap_sync(eid, res[1]); self.msgid += 1
            resp = self.exchange_ike(self.encrypt([(EAP, eap_resp)], self.msgid), self.msgid, "IKE_AUTH(sync)")
            return self.handle_auth(resp)
        if res[0] != "OK": raise RuntimeError(f"SIM auth failed {res}")
        _, R, ck, ik = res
        kencr, kaut, msk, emsk = eap_keys(self.nai, ck, ik)
        self.eap = {"kaut": kaut, "msk": msk}
        eap_z = bytearray(eap); off = self.find_attr(eap, AT_MAC)
        if off is not None:
            got = bytes(eap_z[off + 4:off + 20]); eap_z[off + 4:off + 20] = b"\x00" * 16
            if not hmaclib.compare_digest(got, hmac_sha1(kaut, bytes(eap_z))[:16]):
                raise RuntimeError("AT_MAC verification failed")
            print("[eap] AT_MAC verified")
        eap_resp = self.build_eap_challenge(eid, R, kaut)
        inner = [(EAP, eap_resp)]
        if self.device_id_requested:
            inner.append((NOTIFY, self.device_identity_notify())); self.device_id_requested = False
        self.msgid += 1
        resp = self.exchange_ike(self.encrypt(inner, self.msgid), self.msgid, "IKE_AUTH(challenge)")
        return self.handle_auth(resp)

    @staticmethod
    def parse_attrs(data):
        attrs, pos = {}, 0
        while pos + 2 <= len(data):
            t, l = data[pos], data[pos + 1] * 4
            if l < 4 or pos + l > len(data): break
            attrs[t] = data[pos + 2:pos + l]; pos += l
        return attrs

    @staticmethod
    def find_attr(eap, want):
        pos = 8
        while pos + 2 <= len(eap):
            t, l = eap[pos], eap[pos + 1] * 4
            if l < 4: return None
            if t == want: return pos
            pos += l
        return None

    def device_identity_notify(self):
        digits = self.imei + ("F" if len(self.imei) == 15 else "")
        bcd = b""
        for i in range(0, len(digits), 2):
            bcd += bytes([int(digits[i], 16) | (int(digits[i + 1], 16) << 4)])
        data = bytes([1]) + bcd
        print(f"[ike] DEVICE_IDENTITY response {len(data)} bytes")
        return b"\x00\x00" + u16(DEVICE_IDENTITY) + u16(len(data)) + data

    def build_eap_identity(self, eid):
        ident = self.nai.encode(); full = 4 + len(ident); at_len = (full + 3) // 4
        at = bytes([AT_IDENTITY, at_len]) + u16(len(ident)) + ident + b"\x00" * (at_len * 4 - full)
        body = bytes([EAP_AKA, AKA_IDENTITY, 0, 0]) + at
        return bytes([EAP_RESPONSE, eid]) + u16(4 + len(body)) + body

    def build_eap_challenge(self, eid, res, kaut):
        attr_len = 4 + len(res); pad = (4 - attr_len % 4) % 4; at_len = (attr_len + pad) // 4
        at_res = bytes([AT_RES, at_len]) + u16(len(res) * 8) + res + b"\x00" * pad
        at_mac = bytes([AT_MAC, 5, 0, 0]) + b"\x00" * 16
        body = bytes([EAP_AKA, AKA_CHALLENGE, 0, 0]) + at_res + at_mac
        eap = bytes([EAP_RESPONSE, eid]) + u16(4 + len(body)) + body
        return eap[:-16] + hmac_sha1(kaut, eap)[:16]

    def build_eap_sync(self, eid, auts):
        at = bytes([AT_AUTS, 4]) + auts + b"\x00" * (14 - len(auts))
        body = bytes([EAP_AKA, AKA_SYNC_FAILURE, 0, 0]) + at
        return bytes([EAP_RESPONSE, eid]) + u16(4 + len(body)) + body

    def build_eap_notification(self, eid):
        at_mac = bytes([AT_MAC, 5, 0, 0]) + b"\x00" * 16
        body = bytes([EAP_AKA, AKA_NOTIFICATION, 0, 0]) + at_mac
        eap = bytes([EAP_RESPONSE, eid]) + u16(4 + len(body)) + body
        return eap[:-16] + hmac_sha1(self.eap["kaut"], eap)[:16]

    def send_auth(self):
        prf_id = prf_sha1(self.ike["SK_pi"], self.id_payload(3, self.nai.encode()))
        pad_key = prf_sha1(self.eap["msk"], b"Key Pad for IKEv2")
        auth = prf_sha1(pad_key, self.sa_init_packet + self.nr + prf_id)
        self.msgid += 1
        resp = self.exchange_ike(self.encrypt([(AUTH, bytes([2, 0, 0, 0]) + auth)], self.msgid), self.msgid, "IKE_AUTH(AUTH)")
        action = self.handle_auth(resp)
        if action not in (None, "eap_success"):
            print(f"[ike] final action={action}")

    def parse_cp(self, body):
        print(f"[cp] type={body[0]}")
        pos = 4
        while pos + 4 <= len(body):
            at, ln = struct.unpack("!HH", body[pos:pos + 4]); val = body[pos + 4:pos + 4 + ln]
            if at == 1 and len(val) == 4: self.cp["ip4"].append(socket.inet_ntoa(val)); print(f"[cp] IP4 {socket.inet_ntoa(val)}")
            elif at == 3 and len(val) == 4: self.cp["dns4"].append(socket.inet_ntoa(val)); print(f"[cp] DNS4 {socket.inet_ntoa(val)}")
            elif at == 8 and len(val) >= 16: self.cp["ip6"].append(socket.inet_ntop(socket.AF_INET6, val[:16])); print(f"[cp] IP6 {socket.inet_ntop(socket.AF_INET6, val[:16])}")
            elif at == 10 and len(val) == 16: self.cp["dns6"].append(socket.inet_ntop(socket.AF_INET6, val)); print(f"[cp] DNS6 {socket.inet_ntop(socket.AF_INET6, val)}")
            elif at == 20 and len(val) == 4: self.cp["pcscf4"].append(socket.inet_ntoa(val)); print(f"[cp] P-CSCF4 {socket.inet_ntoa(val)}")
            elif at == 21 and len(val) == 16: self.cp["pcscf6"].append(socket.inet_ntop(socket.AF_INET6, val)); print(f"[cp] P-CSCF6 {socket.inet_ntop(socket.AF_INET6, val)}")
            else: print(f"[cp] attr {at} len={ln}")
            pos += 4 + ln

    # ---------- ESP ----------
    def esp_encrypt(self, inner_ip, key_e, key_a, spi):
        self.outer_seq += 1
        return esp_pack(inner_ip, key_e, key_a, spi, self.outer_seq, 4)

    def esp_decrypt(self, pkt, key_e, key_a, spi):
        return esp_unpack(pkt, key_e, key_a, spi, 4)

    def send_outer(self, inner_ip):
        pkt = self.esp_encrypt(inner_ip, self.esp["ei"], self.esp["ai"], self.child_spi_r)
        self.sock.sendto(pkt, self.remote)

    def send_ike_delete(self):
        if not self.ike:
            return
        self.msgid += 1
        pkt = self.encrypt([(DELETE, bytes([1, 0, 0, 0]))], self.msgid, exchange=INFORMATIONAL)
        try:
            self.exchange_ike(pkt, self.msgid, "DELETE")
            print("[ike] INFORMATIONAL Delete sent")
        except Exception as e:
            print(f"[ike] Delete failed: {e}")

    def recv_outer(self, deadline):
        while time.time() < deadline:
            if self.esp_queue:
                data = self.esp_queue.pop(0)
            else:
                try: kind, data = self.recv_pkt()
                except socket.timeout: continue
                if kind == "ike": self.ike_queue.append(data); continue
                if kind == "keepalive": continue
            inner = self.esp_decrypt(data, self.esp["er"], self.esp["ar"], self.child_spi_i)
            if inner: return inner
        return None


def ip_fragments(pkt, mtu=1280):
    if len(pkt) <= mtu:
        return [pkt]
    hdr, body = pkt[:20], pkt[20:]
    step = (mtu - 20) // 8 * 8
    out = []
    for off in range(0, len(body), step):
        chunk = body[off:off + step]
        more = off + step < len(body)
        frag = bytearray(hdr)
        frag[2:4] = u16(20 + len(chunk))
        frag[6:8] = u16((0x2000 if more else 0) | (off // 8))
        frag[10:12] = b"\x00\x00"
        frag[10:12] = u16(ip_checksum(bytes(frag)))
        out.append(bytes(frag) + chunk)
    return out


def ip_checksum(data):
    if len(data) % 2: data += b"\x00"
    s = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    while s >> 16: s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


def build_udp(src, dst, sport, dport, payload):
    udp_len = 8 + len(payload)
    udp = u16(sport) + u16(dport) + u16(udp_len) + u16(0)
    pseudo = socket.inet_aton(src) + socket.inet_aton(dst) + b"\x00\x11" + u16(udp_len)
    csum = ip_checksum(pseudo + udp + payload)
    return u16(sport) + u16(dport) + u16(udp_len) + u16(csum) + payload


_IP_ID = int.from_bytes(os.urandom(2), "big")


def print(*a, **k):
    builtins.print("sim-anywhere-vowifid:", *a, flush=True, **k)


def build_ipv4(src, dst, proto, payload):
    global _IP_ID
    _IP_ID = (_IP_ID + 1) & 0xFFFF
    total = 20 + len(payload)
    ip = bytes([0x45, 0]) + u16(total) + u16(_IP_ID) + u16(0x4000) + bytes([64, proto]) + u16(0) + socket.inet_aton(src) + socket.inet_aton(dst)
    ip = ip[:10] + u16(ip_checksum(ip)) + ip[12:]
    return ip + payload


def build_ipv4_udp(src, dst, sport, dport, payload):
    return build_ipv4(src, dst, 17, build_udp(src, dst, sport, dport, payload))


def parse_ipv4_udp(pkt):
    ihl = (pkt[0] & 0x0F) * 4
    proto = pkt[9]
    src, dst = socket.inet_ntoa(pkt[12:16]), socket.inet_ntoa(pkt[16:20])
    if proto != 17: return {"proto": proto, "src": src, "dst": dst, "payload": pkt[ihl:]}
    sport, dport, ulen = struct.unpack("!HHH", pkt[ihl:ihl + 6])
    return {"proto": 17, "src": src, "dst": dst, "sport": sport, "dport": dport, "payload": pkt[ihl + 8:ihl + ulen]}


def sip_options(dst_ip, src_ip):
    branch = "z9hG4bK" + os.urandom(6).hex()
    callid = os.urandom(8).hex() + "@" + src_ip
    msg = (f"OPTIONS sip:{dst_ip} SIP/2.0\r\n"
           f"Via: SIP/2.0/UDP {src_ip}:5062;branch={branch};rport\r\n"
           f"Max-Forwards: 70\r\n"
           f"From: <sip:test@{src_ip}>;tag={os.urandom(4).hex()}\r\n"
           f"To: <sip:{dst_ip}>\r\n"
           f"Call-ID: {callid}\r\n"
           f"CSeq: 1 OPTIONS\r\n"
           f"Contact: <sip:test@{src_ip}:5062>\r\n"
           f"Content-Length: 0\r\n\r\n")
    return msg.encode()



def parse_ipv4(pkt):
    ihl = (pkt[0] & 0x0F) * 4
    total = struct.unpack("!H", pkt[2:4])[0]
    return {"proto": pkt[9], "src": socket.inet_ntoa(pkt[12:16]), "dst": socket.inet_ntoa(pkt[16:20]),
            "payload": pkt[ihl:total]}


def parse_sip(data):
    text = data.decode("utf-8", "replace")
    head, _, body = text.partition("\r\n\r\n")
    lines = head.split("\r\n")
    headers = {}
    for line in lines[1:]:
        if ":" in line and not line.startswith((" ", "\t")):
            k, v = line.split(":", 1)
            headers.setdefault(k.strip().lower(), []).append(v.strip())
    m = re.match(r"SIP/2\.0\s+(\d+)\s*(.*)", lines[0]) if lines else None
    if m:
        return int(m.group(1)), m.group(2), headers, body.encode()
    method = lines[0].split()[0] if lines and lines[0] else ""
    headers["__method"] = [method]
    return None, method, headers, body.encode()


def split_quoted(value, sep):
    out, cur, q = [], "", False
    for ch in value:
        if ch == '"':
            q = not q; cur += ch
        elif ch == sep and not q:
            out.append(cur); cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def parse_params(value):
    value = value.strip()
    if value.lower().startswith("digest "):
        value = value[7:]
    out = {}
    for part in split_quoted(value, ","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lower()] = v.strip().strip('"')
    return out


def parse_security(value):
    for part in split_quoted(value, ","):
        part = part.strip()
        if not part.lower().startswith("ipsec-3gpp"):
            continue
        out = {}
        for kv in split_quoted(part, ";"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                out[k.strip().lower()] = v.strip().strip('"')
        return out
    return None


def mask_uri(uri):
    return re.sub(r"\d{5,}", lambda m: "*" * (len(m.group(0)) - 4) + m.group(0)[-4:], uri)


def build_register(impi, impu, imsi, ip, port_uc, port_us, spi_uc, spi_us,
                   domain, call_id, cseq, auth=None, sec_verify=None,
                   expires=600000, contact_expires=None, sec_alg=None, sec_zero=False, sec_mod="trans", pani=True):
    from_hdr = f"<{impu}>;tag={os.urandom(4).hex()}"
    contact = f"<sip:{imsi}@{ip}:{port_uc}>;+g.3gpp.icsi-ref=\"urn%3Aurn-7%3A3gpp-service.ims.icsi.mmtel\""
    if contact_expires is not None:
        contact += f";expires={contact_expires}"
    sec_client = None
    if sec_alg:
        if sec_zero:
            sec_client = (f"ipsec-3gpp; alg={sec_alg[0]}; prot=esp; mod={sec_mod}; ealg={sec_alg[1]}; "
                          f"spi-c=0; spi-s=0; port-c=0; port-s=0")
        else:
            sec_client = (f"ipsec-3gpp; alg={sec_alg[0]}; prot=esp; mod={sec_mod}; ealg={sec_alg[1]}; "
                          f"spi-c={spi_uc}; spi-s={spi_us}; port-c={port_uc}; port-s={port_us}")
    lines = [
        f"REGISTER sip:{domain} SIP/2.0",
        f"Via: SIP/2.0/UDP {ip}:{port_uc};branch=z9hG4bK{os.urandom(6).hex()};rport",
        "Max-Forwards: 70",
        f"From: {from_hdr}",
        f"To: <{impu}>",
        f"Call-ID: {call_id}",
        f"CSeq: {cseq} REGISTER",
        f"Contact: {contact}",
        "Supported: path",
        "User-Agent: SIMAnywhere-Test",
    ]
    if pani:
        lines.append("P-Access-Network-Info: IEEE-802.11")
    if sec_client and not flags.get("no_sec_client"):
        lines.append(f"Security-Client: {sec_client}")
        lines.append("Require: sec-agree")
        lines.append("Proxy-Require: sec-agree")
    if sec_verify:
        lines.append(f"Security-Verify: {sec_verify}")
    if auth:
        lines.append(f"Authorization: {auth}")
    lines.append(f"Expires: {expires}")
    lines.append("Content-Length: 0")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()



def build_register_t5(impi, impu, imsi, imei, ip, port_uc, port_us, spi_uc, spi_us,
                      domain, call_id, cseq, flags=None, auth=None, sec_verify=None,
                      sec_alg=("hmac-sha-1-96", "aes-cbc"), transport="UDP", contact_expires=None,
                      sec_ports=None):
    flags = flags or {}
    imei_gsma = f"{imei[:8]}-{imei[8:14]}-{imei[14]}" if len(imei) >= 15 else imei
    contact = f"<sip:{imsi}@{ip}:{port_uc}>"
    if contact_expires is not None:
        contact += f";expires={contact_expires}"
    contact += ";+g.3gpp.icsi-ref=\"urn%3Aurn-7%3A3gpp-service.ims.icsi.mmtel\""
    if flags.get("sip_instance"):
        contact += f";+sip.instance=\"<urn:gsma:imei:{imei_gsma}>\""
    supported = "path, sec-agree" if flags.get("supported_sec_agree") else "path"
    pani = "IEEE-802.11"
    if flags.get("pani_iwlan"):
        pani += "; i-wlan-node-id=02:00:00:00:00:01"
    ua = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
          if flags.get("phone_ua") else "SIMAnywhere-Test")
    expires = flags.get("expires", 600000)
    sec_client = None
    if sec_alg:
        c_spi, s_spi, c_port, s_port = sec_ports or (spi_uc, spi_us, port_uc, port_us)
        sec_client = (f"ipsec-3gpp; alg={sec_alg[0]}; prot=esp; mod=trans; ealg={sec_alg[1]}; "
                      f"spi-c={c_spi}; spi-s={s_spi}; port-c={c_port}; port-s={s_port}")
    lines = [
        f"REGISTER sip:{domain} SIP/2.0",
        f"Via: SIP/2.0/{transport} {ip}:{port_uc};branch=z9hG4bK{os.urandom(6).hex()};rport",
        "Max-Forwards: 70",
        f"From: <{impu}>;tag={os.urandom(4).hex()}",
        f"To: <{impu}>",
        f"Call-ID: {call_id}",
        f"CSeq: {cseq} REGISTER",
        f"Contact: {contact}",
        f"Supported: {supported}",
        f"P-Access-Network-Info: {pani}",
        f"User-Agent: {ua}",
    ]
    if sec_client and not flags.get("no_sec_client"):
        lines.append(f"Security-Client: {sec_client}")
        lines.append("Require: sec-agree")
        lines.append("Proxy-Require: sec-agree")
    if sec_verify:
        lines.append(f"Security-Verify: {sec_verify}")
    if auth:
        lines.append(f"Authorization: {auth}")
    elif flags.get("auth_placeholder"):
        lines.append(f'Authorization: Digest username="{impi}", realm="{domain}", '
                     f'uri="sip:{domain}", nonce="", response="", algorithm=AKAv1-MD5')
    lines.append(f"Expires: {expires}")
    lines.append("Content-Length: 0")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def mask4(value):
    value = str(value)
    return "*" * max(0, len(value) - 4) + value[-4:] if len(value) > 4 else "****"


def masked_sip(sip, imsi, imei):
    text = sip.decode("utf-8", "replace") if isinstance(sip, bytes) else sip
    imei_gsma = f"{imei[:8]}-{imei[8:14]}-{imei[14]}" if len(imei) >= 15 else imei
    text = text.replace(imei_gsma, "****" + imei_gsma[-4:])
    text = text.replace(imei, "****" + imei[-4:])
    text = text.replace(imsi, "****" + imsi[-4:])
    # Mask the MSISDN (last 4 digits only), including the sip: user form.
    text = re.sub(r"\+65\d{6,}", lambda m: "+65****" + m.group(0)[-4:], text)
    text = re.sub(r"(?<=sip:)\d{7,10}(?=@)", lambda m: "****" + m.group(0)[-4:], text)
    return text


class ImsClient:
    def __init__(self, ike, pcscf, domain, imsi, impi, impu, sim, sec_alg=None, sec_zero=False, sec_mod="trans", uc_port=5062, us_port=5064, pani=True):
        self.ike, self.pcscf, self.domain = ike, pcscf, domain
        self.imsi, self.impi, self.impu, self.sim = imsi, impi, impu, sim
        self.port_uc, self.port_us = uc_port, us_port
        self.pani = pani
        self.spi_uc = int.from_bytes(os.urandom(4), "big")
        self.spi_us = int.from_bytes(os.urandom(4), "big")
        self.call_id = os.urandom(8).hex() + "@" + ike.cp["ip4"][0]
        self.cseq = 1
        self.ck = self.ik = None
        self.inner_seq = 0
        self.sec = None
        self.sec_raw = None
        self.sec_alg = sec_alg
        self.sec_zero = sec_zero
        self.sec_mod = sec_mod
        self.nxt = None

    def send_plain(self, sip):
        ip = build_ipv4_udp(self.ike.cp["ip4"][0], self.pcscf, self.port_uc, 5060, sip)
        self.ike.send_outer(ip)

    def sa_dict(self):
        return {"ck": self.ck, "ik": self.ik, "sec": self.sec, "spi_uc": self.spi_uc,
                "spi_us": self.spi_us, "port_uc": self.port_uc, "port_us": self.port_us}

    def send_protected(self, sip, sa=None):
        sa = sa or self.sa_dict()
        if not sa["sec"] or not sa["ck"]:
            raise RuntimeError("no inner SA")
        dst_port = int(sa["sec"]["port-s"])
        spi = int(sa["sec"]["spi-s"])
        udp = build_udp(self.ike.cp["ip4"][0], self.pcscf, sa["port_uc"], dst_port, sip)
        self.inner_seq += 1
        inner = esp_pack_alg(udp, sa["sec"].get("alg"), sa["sec"].get("ealg"),
                             sa["ck"], sa["ik"], spi, self.inner_seq, 17)
        ip = build_ipv4(self.ike.cp["ip4"][0], self.pcscf, 50, inner)
        for frag in ip_fragments(ip):
            self.ike.send_outer(frag)

    def recv_sip(self, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            pkt = self.ike.recv_outer(deadline)
            if not pkt:
                return None
            ip = parse_ipv4(pkt)
            if ip["proto"] == 17:
                return parse_sip(ip["payload"][8:])
            if ip["proto"] == 50 and self.ck:
                udp = esp_unpack_alg(ip["payload"], self.sec.get("alg"), self.sec.get("ealg"),
                                     self.ck, self.ik, self.spi_us, 17)
                if udp:
                    sport, dport, ulen = struct.unpack("!HHH", udp[:6])
                    print(f"[ims] inner SIP {ip['src']}:{sport} -> {ip['dst']}:{dport}")
                    return parse_sip(udp[8:ulen])
            print(f"[ims] inner proto {ip['proto']} ignored")
        return None

    def initial_register(self):
        sip = build_register(self.impi, self.impu, self.imsi, self.ike.cp["ip4"][0],
                             self.port_uc, self.port_us, self.spi_uc, self.spi_us,
                             self.domain, self.call_id, self.cseq, sec_alg=self.sec_alg, sec_zero=self.sec_zero, sec_mod=self.sec_mod, pani=self.pani)
        print("[ims] REGISTER (initial)")
        self.send_plain(sip)
        return self.recv_sip(8)

    def digest_auth(self, chal, res, auts=None):
        username = self.impi
        realm = chal.get("realm", self.domain)
        nonce = chal.get("nonce", "")
        algo = chal.get("algorithm", "AKAv1-MD5")
        qop = chal.get("qop", "")
        qop_choice = "auth" if "auth" in qop else ("auth-int" if "auth-int" in qop else "")
        cnonce = os.urandom(4).hex()
        nc = "00000001"
        uri = f"sip:{self.domain}"
        password = b"" if auts is not None else res
        ha1 = hashlib.md5(username.encode() + b":" + realm.encode() + b":" + password).hexdigest()
        if qop_choice == "auth-int":
            ha2 = hashlib.md5(b"REGISTER:" + uri.encode() + b":" + hashlib.md5(b"").digest()).hexdigest()
        else:
            ha2 = hashlib.md5(b"REGISTER:" + uri.encode()).hexdigest()
        if qop_choice:
            response = hashlib.md5((ha1 + ":" + nonce + ":" + nc + ":" + cnonce + ":" + qop_choice + ":" + ha2).encode()).hexdigest()
        else:
            response = hashlib.md5((ha1 + ":" + nonce + ":" + ha2).encode()).hexdigest()
        parts = [f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"',
                 f'uri="{uri}"', f'response="{response}"', f"algorithm={algo}"]
        if qop_choice:
            parts += [f"qop={qop_choice}", f"nc={nc}", f'cnonce="{cnonce}"']
        if chal.get("opaque"):
            parts.append(f'opaque="{chal["opaque"]}"')
        if auts is not None:
            parts.append(f'auts="{base64.b64encode(auts).decode()}"')
        return "Digest " + ", ".join(parts)

    def run(self, challenge_only=False):
        code, reason, headers, _ = self.initial_register()
        print(f"[ims] initial response {code} {reason}")
        for k, v in headers.items():
            for item in v:
                print(f"[ims] {k}: {mask_uri(item)}")
        if code != 401:
            return code, reason, headers
        if challenge_only:
            return code, reason, headers
        chal = None
        for v in headers.get("www-authenticate", []):
            if v.lower().startswith("digest"):
                chal = parse_params(v)
                break
        if not chal:
            raise RuntimeError("no Digest challenge")
        sec_raw = None
        for v in headers.get("security-server", []):
            if v.lower().startswith("ipsec-3gpp"):
                sec_raw = v
                self.sec = parse_security(v)
                break
        if not self.sec:
            raise RuntimeError("no Security-Server")
        print(f"[ims] sec-agree {self.sec}")
        nonce = chal.get("nonce", "")
        try:
            raw = base64.b64decode(nonce + "=" * (-len(nonce) % 4))
        except Exception:
            raw = b""
        if len(raw) < 32:
            raise RuntimeError(f"bad nonce length {len(raw)}")
        rand, autn = raw[:16], raw[16:32]
        result = self.sim.authenticate(rand, autn)
        if result[0] == "SYNC":
            print("[ims] AKA sync failure, sending AUTS")
            auth = self.digest_auth(chal, b"", auts=result[1])
            self.cseq += 1
            sip = build_register(self.impi, self.impu, self.imsi, self.ike.cp["ip4"][0],
                                 self.port_uc, self.port_us, self.spi_uc, self.spi_us,
                                 self.domain, self.call_id, self.cseq, auth=auth,
                                 sec_verify=sec_raw, sec_alg=self.sec_alg, sec_zero=self.sec_zero, sec_mod=self.sec_mod, pani=self.pani)
            self.send_plain(sip)
            code, reason, headers, _ = self.recv_sip(8) or (0, "", {}, b"")
            print(f"[ims] after AUTS {code} {reason}")
            if code != 401:
                return code, reason, headers
            chal = parse_params(headers.get("www-authenticate", [""])[0])
            nonce = chal.get("nonce", "")
            raw = base64.b64decode(nonce + "=" * (-len(nonce) % 4))
            rand, autn = raw[:16], raw[16:32]
            result = self.sim.authenticate(rand, autn)
        if result[0] != "OK":
            raise RuntimeError(f"SIM auth failed {result[0]}")
        _, res, ck, ik = result
        self.ck, self.ik = ck, ik
        print("[ims] IMS-AKA vector received")
        auth = self.digest_auth(chal, res)
        self.cseq += 1
        sip = build_register(self.impi, self.impu, self.imsi, self.ike.cp["ip4"][0],
                             self.port_uc, self.port_us, self.spi_uc, self.spi_us,
                             self.domain, self.call_id, self.cseq, auth=auth,
                             sec_verify=sec_raw, sec_alg=self.sec_alg, sec_zero=self.sec_zero, sec_mod=self.sec_mod, pani=self.pani)
        print("[ims] REGISTER (protected)")
        self.send_protected(sip)
        resp = self.recv_sip(10)
        if not resp:
            return 0, "no response", {}
        code, reason, headers, _ = resp
        print(f"[ims] protected response {code} {reason}")
        for k, v in headers.items():
            if k in ("www-authenticate", "p-associated-uri", "service-route", "expires", "contact", "reason"):
                for item in v:
                    print(f"[ims] {k}: {mask_uri(item)}")
        return code, reason, headers

    def deregister(self):
        if not self.ck:
            return None
        self.cseq += 1
        sip = build_register(self.impi, self.impu, self.imsi, self.ike.cp["ip4"][0],
                             self.port_uc, self.port_us, self.spi_uc, self.spi_us,
                             self.domain, self.call_id, self.cseq, expires=0, contact_expires=0,
                             sec_alg=self.sec_alg, sec_zero=self.sec_zero, sec_mod=self.sec_mod, pani=self.pani)
        print("[ims] REGISTER (deregister Expires: 0)")
        self.send_protected(sip)
        return self.recv_sip(8)


def tcp_build(src, dst, sport, dport, seq, ack, flags, payload=b""):
    off = 5 << 4
    tcp = u16(sport) + u16(dport) + u32(seq) + u32(ack) + bytes([off, flags]) + u16(65535) + u16(0) + u16(0) + payload
    pseudo = socket.inet_aton(src) + socket.inet_aton(dst) + b"\x00\x06" + u16(len(tcp))
    csum = ip_checksum(pseudo + tcp)
    tcp = tcp[:16] + u16(csum) + tcp[18:]
    return build_ipv4(src, dst, 6, tcp)


def tcp_parse(pkt):
    ihl = (pkt[0] & 0x0F) * 4
    tcp = pkt[ihl:]
    sport, dport, seq, ack = struct.unpack("!HHII", tcp[:12])
    off = (tcp[12] >> 4) * 4
    flags = tcp[13]
    return {"sport": sport, "dport": dport, "seq": seq, "ack": ack, "flags": flags, "payload": tcp[off:]}


def task5_tcp(ike, client, flags, imsi, imei, sec_alg, domain, pcscf):
    sip = build_register_t5(client.impi, client.impu, imsi, imei, ike.cp["ip4"][0],
                            client.port_uc, client.port_us, client.spi_uc, client.spi_us,
                            domain, client.call_id, client.cseq + 1, flags=flags,
                            sec_alg=sec_alg, transport="TCP")
    client.cseq += 1
    src = ike.cp["ip4"][0]
    print("[t5] TCP SYN ->", pcscf, 5060)
    ike.send_outer(tcp_build(src, pcscf, client.port_uc, 5060, 1000, 0, 0x02))
    deadline = time.time() + 8
    synack = None
    while time.time() < deadline:
        pkt = ike.recv_outer(deadline)
        if not pkt:
            break
        ip = parse_ipv4(pkt)
        if ip["proto"] != 6:
            continue
        t = tcp_parse(ip["payload"])
        print(f"[t5] TCP packet {ip['src']}:{t['sport']} -> {ip['dst']}:{t['dport']} flags=0x{t['flags']:02x}")
        if t["flags"] & 0x12 == 0x12:
            synack = t
            break
    if not synack:
        print("[t5] TCP no SYN-ACK")
        return None
    ackno = synack["seq"] + 1
    ike.send_outer(tcp_build(src, pcscf, client.port_uc, 5060, 1001, ackno, 0x10))
    ike.send_outer(tcp_build(src, pcscf, client.port_uc, 5060, 1001, ackno, 0x18, sip))
    print("[t5] TCP REGISTER sent")
    deadline = time.time() + 10
    while time.time() < deadline:
        pkt = ike.recv_outer(deadline)
        if not pkt:
            break
        ip = parse_ipv4(pkt)
        if ip["proto"] != 6:
            continue
        t = tcp_parse(ip["payload"])
        if t["payload"]:
            print("[t5] TCP response:")
            print(masked_sip(t["payload"], imsi, imei))
            ike.send_outer(tcp_build(src, pcscf, client.port_uc, 5060, 1002, t["seq"] + len(t["payload"]), 0x11))
            return parse_sip(t["payload"])
    print("[t5] TCP no response")
    return None


def task5_deregister(client, imsi, imei, sec_alg, domain, flags):
    dflags = dict(flags)
    dflags["expires"] = 0
    dflags["no_sec_client"] = True
    sip = build_register_t5(client.impi, client.impu, imsi, imei, client.ike.cp["ip4"][0],
                            client.port_uc, client.port_us, client.spi_uc, client.spi_us,
                            domain, client.call_id, client.cseq + 1, flags=dflags,
                            sec_verify=getattr(client, "sec_raw", None),
                            sec_alg=sec_alg, contact_expires=0)
    client.cseq += 1
    print("[t5] deregister over existing SA (no Security-Client)")
    client.send_protected(sip)
    resp = client.recv_sip(10)
    if not resp:
        print("[t5] deregister no response")
        return None
    code, reason, headers, _ = resp
    print(f"[t5] deregister response {code} {reason}")
    if code != 401:
        return resp
    chal = None
    for v in headers.get("www-authenticate", []):
        if v.lower().startswith("digest"):
            chal = parse_params(v)
            break
    if not chal:
        return resp
    sec_raw = None
    for v in headers.get("security-server", []):
        if v.lower().startswith("ipsec-3gpp"):
            sec_raw = v
            client.sec = parse_security(v)
            break
    if not client.sec:
        return resp
    nonce = chal.get("nonce", "")
    raw = base64.b64decode(nonce + "=" * (-len(nonce) % 4))
    if len(raw) < 32:
        return resp
    result = client.sim.authenticate(raw[:16], raw[16:32])
    if result[0] != "OK":
        print(f"[t5] deregister SIM auth {result[0]}")
        return resp
    _, res, ck, ik = result
    client.ck, client.ik = ck, ik
    auth = client.digest_auth(chal, res)
    sip = build_register_t5(client.impi, client.impu, imsi, imei, client.ike.cp["ip4"][0],
                            client.port_uc, client.port_us, client.spi_uc, client.spi_us,
                            domain, client.call_id, client.cseq + 1, flags=dflags, auth=auth,
                            sec_verify=sec_raw, sec_alg=sec_alg, contact_expires=0)
    client.cseq += 1
    print("[t5] protected deregister with fresh AKA")
    client.send_protected(sip)
    return client.recv_sip(12)


def task5_continue(client, code, headers, flags, imsi, imei, sec_alg, domain, pcscf, do_deregister=True):
    chal = None
    for v in headers.get("www-authenticate", []):
        if v.lower().startswith("digest"):
            chal = parse_params(v)
            break
    if not chal:
        raise RuntimeError("401 without Digest challenge")
    sec_raw = None
    for v in headers.get("security-server", []):
        if v.lower().startswith("ipsec-3gpp"):
            sec_raw = v
            client.sec = parse_security(v)
            client.sec_raw = v
            break
    if not client.sec:
        raise RuntimeError("401 without Security-Server")
    print(f"[t5] sec-agree {client.sec}")
    prot_flags = dict(flags)
    prot_flags["sip_instance"] = True
    prot_flags["supported_sec_agree"] = True
    nonce = chal.get("nonce", "")
    raw = base64.b64decode(nonce + "=" * (-len(nonce) % 4))
    if len(raw) < 32:
        raise RuntimeError(f"bad nonce length {len(raw)}")
    result = client.sim.authenticate(raw[:16], raw[16:32])
    if result[0] == "SYNC":
        print("[t5] AKA sync failure, sending AUTS")
        auts = result[1]
        auth = client.digest_auth(chal, b"", auts=auts)
        sip = build_register_t5(client.impi, client.impu, imsi, imei, client.ike.cp["ip4"][0],
                                client.port_uc, client.port_us, client.spi_uc, client.spi_us,
                                domain, client.call_id, client.cseq + 1, flags=prot_flags, auth=auth,
                                sec_verify=sec_raw, sec_alg=sec_alg)
        client.cseq += 1
        client.send_plain(sip)
        code, reason, headers, _ = client.recv_sip(10) or (0, "", {}, b"")
        print(f"[t5] after AUTS {code} {reason}")
        if code != 401:
            return code, reason, headers
        chal = parse_params(headers.get("www-authenticate", [""])[0])
        raw = base64.b64decode(chal.get("nonce", "") + "=" * (-len(chal.get("nonce", "")) % 4))
        result = client.sim.authenticate(raw[:16], raw[16:32])
    if result[0] != "OK":
        raise RuntimeError(f"SIM auth failed {result[0]}")
    _, res, ck, ik = result
    client.ck, client.ik = ck, ik
    auth = client.digest_auth(chal, res)
    sip = build_register_t5(client.impi, client.impu, imsi, imei, client.ike.cp["ip4"][0],
                            client.port_uc, client.port_us, client.spi_uc, client.spi_us,
                            domain, client.call_id, client.cseq + 1, flags=prot_flags, auth=auth,
                            sec_verify=sec_raw, sec_alg=sec_alg)
    client.cseq += 1
    print("[t5] protected REGISTER")
    client.send_protected(sip)
    resp = client.recv_sip(12)
    if not resp:
        return 0, "no response", {}
    code, reason, headers, _ = resp
    print(f"[t5] protected response {code} {reason}")
    for k, v in headers.items():
        for item in v:
            print(f"[t5] {k}: {masked_sip(item, imsi, imei)}")
    if code == 200:
        client.reg_headers = headers
        client.reg_pa = headers.get("p-associated-uri", [])
        client.reg_sr = headers.get("service-route", [])
        client.reg_chal = chal
        client.reg_res = res
        client.reg_sec_raw = sec_raw
        client.reg_flags = prot_flags
        for v in headers.get("p-associated-uri", []):
            print(f"[t5] P-Associated-URI {masked_sip(v, imsi, imei)}")
        for v in headers.get("service-route", []):
            print(f"[t5] Service-Route {masked_sip(v, imsi, imei)}")
        for v in headers.get("expires", []):
            print(f"[t5] Expires {v}")
        resp2 = task5_deregister(client, imsi, imei, sec_alg, domain, prot_flags) if do_deregister else None
        if resp2:
            print(f"[t5] deregister {resp2[0]} {resp2[1]}")
            for k, v in resp2[2].items():
                for item in v:
                    print(f"[t5] deregister {k}: {masked_sip(item, imsi, imei)}")
    return code, reason, headers


def run_task5(ike, sim, imsi, imei, pcscf, domain):
    impi = f"{imsi}@{domain}"
    impu = f"sip:{imsi}@{domain}"
    sec_alg = ("hmac-sha-1-96", "aes-cbc")
    client = ImsClient(ike, pcscf, domain, imsi, impi, impu, sim, sec_alg=sec_alg, pani=True)
    client.imei = imei
    # A previous registration may still hold IP+port 5062; use a fresh port pair.
    client.port_uc, client.port_us = 5078, 5080
    variants = [
        ("baseline", {}),
        ("a Authorization + instance", {"auth_placeholder": True, "sip_instance": True, "supported_sec_agree": True}),
        ("b +sip.instance/Supported", {"auth_placeholder": True, "sip_instance": True, "supported_sec_agree": True}),
        ("c +PANI/UA", {"auth_placeholder": True, "sip_instance": True, "supported_sec_agree": True,
                        "pani_iwlan": True, "phone_ua": True}),
        ("d Expires 3600", {"auth_placeholder": True, "sip_instance": True, "supported_sec_agree": True,
                            "pani_iwlan": True, "phone_ua": True, "expires": 3600}),
    ]
    last = None
    for name, flags in variants:
        client.cseq += 1
        sip = build_register_t5(impi, impu, imsi, imei, ike.cp["ip4"][0],
                                client.port_uc, client.port_us, client.spi_uc, client.spi_us,
                                domain, client.call_id, client.cseq, flags=flags, sec_alg=sec_alg)
        print(f"\n[t5] === {name} ===")
        print(masked_sip(sip, imsi, imei))
        client.send_plain(sip)
        resp = client.recv_sip(10)
        if not resp:
            print("[t5] no response")
            continue
        code, reason, headers, body = resp
        print(f"[t5] response {code} {reason}")
        for k, v in headers.items():
            for item in v:
                print(f"[t5] {k}: {masked_sip(item, imsi, imei)}")
        last = (code, reason, headers)
        if code == 401:
            print("[t5] got 401, stop stacking")
            return task5_continue(client, code, headers, flags, imsi, imei, sec_alg, domain, pcscf)
    print("\n[t5] all UDP variants returned non-401; trying TCP")
    flags = variants[-1][1]
    tcp_resp = task5_tcp(ike, client, flags, imsi, imei, sec_alg, domain, pcscf)
    if tcp_resp:
        return tcp_resp
    return last or (0, "", {})
