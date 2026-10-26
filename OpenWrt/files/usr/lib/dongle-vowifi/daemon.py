#!/usr/bin/env python3
"""sim-anywhere-vowifid: Wi-Fi Calling daemon for the M1 IMS over the dongle data link.

Runs on N1.  Keeps IKEv2, ESP, IMS REGISTER and the AMR media in user space.
The control socket is /var/run/dongle-vowifid.sock.
"""

import array
import base64
import hashlib
import json
import math
import os
import random
import re
import select
import socket
import struct
import subprocess
import sys
import threading
import time
import wave

sys.path.insert(0, "/usr/lib/dongle-vowifi")
import vowifi as V
from amr import AmrNB, FRAME_SAMPLES
import discovery as D

SOCK_PATH = "/var/run/dongle-vowifid.sock"
PHONE_PATH = "/tmp/dongle-vowifi-phone.json"
STATUS_PATH = "/tmp/dongle-web/status.json"
AMR_PT = 104
TE_PT = 101
RTP_PORT = 50000
RTCP_PORT = 50001
# Compatibility profile for the current M1 roaming SIM. Use only when
# standardized DNS discovery returns no usable endpoint.
EPDG_PROFILES = {("525", "03"): ("202.65.241.82", "202.65.240.210")}


class NoEPDG(Exception):
    pass


def subscription_rejected(error):
    reason = str(error)
    return ("Notify 24" in reason or "Notify error 24" in reason or "EAP-Failure" in reason
            or reason.startswith("REGISTER failed 403 "))
MARK = 0x401
SEC_ALG = ("hmac-sha-1-96", "aes-cbc")
EPDG_MEMO = "/var/lib/dongle-vowifid/last_epdg"
DPD_INTERVAL = 30      # s between IKE liveness probes (empty INFORMATIONAL)
DPD_TIMEOUT = 5        # s to wait for the response
DPD_MISSES = 3         # consecutive misses before the SA is declared dead

_SYSLOG = None


def setup_log():
    pass


def mask_num(n):
    if not n:
        return n
    return "****" + str(n)[-4:]


def log(*a):
    msg = "sim-anywhere-vowifid: " + " ".join(str(x) for x in a)
    print(msg, flush=True)
    global _SYSLOG
    try:
        if _SYSLOG is None:
            _SYSLOG = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            _SYSLOG.connect("/dev/log")
        _SYSLOG.send(b"<30>" + msg.encode())
    except Exception:
        pass


def uci_get(key, default=None):
    try:
        p = subprocess.run(["uci", "-q", "get", "dongle.vowifi." + key],
                           capture_output=True, text=True, timeout=5)
        v = p.stdout.strip()
        return v if v else default
    except Exception:
        return default


def detect_usb0():
    try:
        p = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", "usb0"],
                           capture_output=True, text=True, timeout=5)
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", p.stdout)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- SIP helpers

def parse_sip(data):
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
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


def hget(headers, name, default=None):
    v = headers.get(name.lower())
    return v[0] if v else default


def to_tag(headers):
    t = hget(headers, "to", "")
    m = re.search(r";tag=([^;>\s]+)", t)
    return m.group(1) if m else None


def sdp_te_pt(sd):
    """telephone-event PT at 8 kHz (the AMR-NB clock); an offer can also list one at 16 kHz for AMR-WB."""
    te = [(clock != 8000, pt) for pt, (codec, clock) in (sd.get("rtpmap") or {}).items()
          if codec.lower() == "telephone-event"]
    return min(te)[1] if te else 101


def parse_sdp(body):
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    out = {"ip": None, "port": None, "pt": None, "codec": None, "clock": None,
           "ptime": 20, "telephone_event": False, "rtcp_port": None, "direction": "sendrecv",
           "formats": []}
    cip = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("c=IN IP4 "):
            cip = line[9:].strip()
        elif line.startswith("m=audio "):
            p = line.split()
            if len(p) >= 4:
                out["port"] = int(p[1])
                out["formats"] = [int(x) for x in p[3:] if x.isdigit()]
        elif line.startswith("a=rtpmap:"):
            m = re.match(r"a=rtpmap:(\d+) ([^/]+)/(\d+)", line)
            if m:
                out.setdefault("rtpmap", {})[int(m.group(1))] = (m.group(2), int(m.group(3)))
                if m.group(2).lower() == "telephone-event":
                    out["telephone_event"] = True
        elif line.startswith("a=fmtp:"):
            m = re.match(r"a=fmtp:(\d+) (.*)", line)
            if m:
                out.setdefault("fmtp", {})[int(m.group(1))] = m.group(2)
        elif line.startswith("a=ptime:"):
            try:
                out["ptime"] = int(float(line.split(":", 1)[1]))
            except Exception:
                pass
        elif line.startswith("a=rtcp:"):
            try:
                out["rtcp_port"] = int(line.split(":", 1)[1].split()[0])
            except Exception:
                pass
        elif line.startswith("a=sendonly"):
            out["direction"] = "sendonly"
        elif line.startswith("a=recvonly"):
            out["direction"] = "recvonly"
        elif line.startswith("a=inactive"):
            out["direction"] = "inactive"
    out["ip"] = cip
    for p in out["formats"]:
        if p in out.get("rtpmap", {}):
            codec, clock = out["rtpmap"][p]
            if codec.upper() == "AMR":                  # AMR-NB only; AMR-WB has another clock and frame set
                out["pt"], out["codec"], out["clock"] = p, codec, clock
                break
    if out["pt"] is None and out["formats"]:
        out["pt"] = out["formats"][0]
    # RFC 4867: without octet-align=1 the payload is bandwidth-efficient
    out["octet_align"] = "octet-align=1" in (out.get("fmtp", {}).get(out["pt"]) or "").replace(" ", "")
    return out


def build_sdp(ue_ip, telephone_event=False, rtp_port=RTP_PORT, amr=None, te_pt=TE_PT):
    """Offer (amr None): AMR-NB octet-aligned (104) and bandwidth-efficient (105).
    Answer (amr = (pt, octet_align)): only the payload format that the offer chose."""
    te = " %d" % te_pt if telephone_event else ""
    if amr is None:
        fmts = ["m=audio %d RTP/AVP 104 105%s" % (rtp_port, te),
                "a=rtpmap:104 AMR/8000/1", "a=fmtp:104 octet-align=1", "a=rtpmap:105 AMR/8000/1"]
    else:
        pt, oa = amr
        fmts = ["m=audio %d RTP/AVP %d%s" % (rtp_port, pt, te), "a=rtpmap:%d AMR/8000/1" % pt]
        if oa:
            fmts.append("a=fmtp:%d octet-align=1" % pt)
    lines = ["v=0", "o=- 0 0 IN IP4 %s" % ue_ip, "s=-", "c=IN IP4 %s" % ue_ip, "t=0 0"] + fmts + ["a=ptime:20"]
    if telephone_event:
        lines.append("a=rtpmap:%d telephone-event/8000" % te_pt)
    return "\r\n".join(lines) + "\r\n"


def build_invite(client, target, local_tag, call_id):
    from_uri = getattr(client, "local_uri", None) or client.impu
    p_pref = getattr(client, "pref_uri", None) or from_uri
    route = client.reg_sr[0] if getattr(client, "reg_sr", None) else ""
    m = re.search(r"<([^>]+)>", route)
    route = m.group(1) if m else route
    contact = (client.reg_headers.get("contact", [""])[0] or "")
    contact = re.sub(r";expires=\d+", "", contact)
    sdp = build_sdp(client.ike.cp["ip4"][0], telephone_event=True)
    branch = "z9hG4bK" + os.urandom(6).hex()
    lines = [
        "INVITE tel:%s SIP/2.0" % target,
        "v: SIP/2.0/UDP %s:%d;branch=%s;rport" %
        (client.ike.cp["ip4"][0], client.port_uc, branch),
        "Max-Forwards: 70",
    ]
    if route:
        lines.append("Route: <%s>" % route)
    lines += [
        "f: <%s>;tag=%s" % (from_uri, local_tag),
        "t: <tel:%s>" % target,
        "i: %s" % call_id,
        "CSeq: 1 INVITE",
        "m: %s" % contact,
        "P-Preferred-Identity: <%s>" % p_pref,
        "P-Preferred-Service: urn:urn-7:3gpp-service.ims.icsi.mmtel",
        "k: 100rel, timer",
        "Security-Verify: %s" % client.reg_sec_raw,
        "Require: sec-agree",
        "Proxy-Require: sec-agree",
        "c: application/sdp",
        "l: %d" % len(sdp), "", sdp]
    return "\r\n".join(lines).encode(), route, contact, branch


def sip_msg(method, uri, headers, body=b"", content_type=None):
    lines = ["%s %s SIP/2.0" % (method, uri)]
    for k, v in headers:
        lines.append("%s: %s" % (k, v))
    if content_type:
        lines.append("Content-Type: %s" % content_type)
    lines.append("Content-Length: %d" % len(body))
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def sip_resp(code, reason, req_headers, body=b"", content_type=None, contact=None,
             extra=None):
    lines = ["SIP/2.0 %d %s" % (code, reason)]
    for v in req_headers.get("via", []):
        lines.append("Via: %s" % v)
    lines.append("From: %s" % hget(req_headers, "from", ""))
    lines.append("To: %s" % hget(req_headers, "to", ""))
    lines.append("Call-ID: %s" % hget(req_headers, "call-id", ""))
    lines.append("CSeq: %s" % hget(req_headers, "cseq", ""))
    if contact:
        lines.append("Contact: %s" % contact)
    if extra:
        lines += extra
    if content_type:
        lines.append("Content-Type: %s" % content_type)
    lines.append("Content-Length: %d" % len(body))
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def digest_nc(client, chal, res, nc):
    username = client.impi
    realm = chal.get("realm", client.domain)
    nonce = chal.get("nonce", "")
    algo = chal.get("algorithm", "AKAv1-MD5")
    qop = chal.get("qop", "")
    qop_choice = "auth" if "auth" in qop else ("auth-int" if "auth-int" in qop else "")
    cnonce = os.urandom(4).hex()
    uri = "sip:%s" % client.domain
    ha1 = hashlib.md5(username.encode() + b":" + realm.encode() + b":" + res).hexdigest()
    ha2 = hashlib.md5(b"REGISTER:" + uri.encode()).hexdigest()
    if qop_choice:
        response = hashlib.md5((ha1 + ":" + nonce + ":" + nc + ":" + cnonce + ":" + qop_choice + ":" + ha2).encode()).hexdigest()
    else:
        response = hashlib.md5((ha1 + ":" + nonce + ":" + ha2).encode()).hexdigest()
    parts = ['username="%s"' % username, 'realm="%s"' % realm, 'nonce="%s"' % nonce,
             'uri="%s"' % uri, 'response="%s"' % response, "algorithm=%s" % algo]
    if qop_choice:
        parts += ["qop=%s" % qop_choice, "nc=%s" % nc, 'cnonce="%s"' % cnonce]
    if chal.get("opaque"):
        parts.append('opaque="%s"' % chal["opaque"])
    return "Digest " + ", ".join(parts)


def parse_rtp(data):
    if len(data) < 12:
        return None
    cc = data[0] & 0x0f
    pt = data[1] & 0x7f
    seq, ts, ssrc = struct.unpack("!HII", data[2:12])
    return pt, seq, ts, ssrc, data[12 + 4 * cc:]


# Speech bytes of an octet-aligned AMR-NB frame, FT 0-7 (4.75 ... 12.2 kbit/s). The old table was reversed and
# had wrong sizes; it worked only for 4.75 kbit/s (the China IVR) and cut 12.2 kbit/s frames to 13 bytes.
AMR_NB = [12, 13, 15, 17, 19, 20, 26, 31]


def amr_frames(payload):
    if not payload:
        return []
    tocs, i = [], 1
    while i < len(payload):
        b = payload[i]
        tocs.append(b)
        i += 1
        if not (b & 0x80):
            break
    out, data = [], payload[i:]
    for b in tocs:
        ft = (b >> 3) & 0x0f
        sz = AMR_NB[ft] if ft < 8 else (5 if ft == 8 else 0)
        out.append(bytes([b & 0x7F]) + data[:sz])
        data = data[sz:]
    return out


AMR_BITS = [95, 103, 118, 134, 148, 159, 204, 244]   # speech bits of FT 0-7; SID (FT 8) has 39


def amr_bits(ft):
    return AMR_BITS[ft] if ft < 8 else (39 if ft == 8 else 0)


def amr_frames_be(payload):
    """Bandwidth-efficient payload (RFC 4867 4.3) -> frames in storage format (TOC byte, octet-aligned speech)."""
    n = len(payload) * 8
    bits = int.from_bytes(payload, "big")
    take = lambda pos, k: (bits >> (n - pos - k)) & ((1 << k) - 1)
    pos, tocs = 4, []                                  # 4 bits CMR
    while pos + 6 <= n:
        t = take(pos, 6)
        pos += 6
        tocs.append(t)
        if not t & 0x20:                               # F = 0: last ToC entry
            break
    out = []
    for t in tocs:
        ft, q = (t >> 1) & 0x0F, t & 1
        k = amr_bits(ft)
        if pos + k > n:
            break
        size = (k + 7) // 8
        speech = (take(pos, k) << (size * 8 - k)).to_bytes(size, "big") if k else b""
        pos += k
        out.append(bytes([(ft << 3) | (q << 2)]) + speech)
    return out


def amr_payload_be(frame):
    """Storage-format frame from the encoder -> bandwidth-efficient payload with CMR 15 and one ToC entry."""
    ft, q = (frame[0] >> 3) & 0x0F, (frame[0] >> 2) & 1
    k = amr_bits(ft)
    speech = int.from_bytes(frame[1:], "big") >> (len(frame[1:]) * 8 - k) if k else 0
    total = 4 + 6 + k
    pad = -total % 8
    v = ((((0xF << 6) | (ft << 1) | q) << k) | speech) << pad
    return v.to_bytes((total + pad) // 8, "big")


def build_rtcp_sr(ssrc, rtp_ts, packet_count, octet_count, cname="vowifi@sim-anywhere"):
    ntp = time.time() + 2208988800
    sec = int(ntp)
    frac = int((ntp - sec) * (1 << 32)) & 0xFFFFFFFF
    sr = struct.pack("!BBHIIIIII", 0x80, 200, 6, ssrc, sec, frac,
                     rtp_ts & 0xFFFFFFFF, packet_count & 0xFFFFFFFF, octet_count & 0xFFFFFFFF)
    cb = cname.encode()
    item = bytes([1, len(cb)]) + cb + b"\x00"
    item += b"\x00" * ((4 - len(item) % 4) % 4)
    sdes = struct.pack("!BBHI", 0x81, 202, (8 + len(item)) // 4 - 1, ssrc) + item
    return sr + sdes


def parse_rtcp(data):
    out, i = [], 0
    while i + 4 <= len(data):
        pt = data[i + 1]
        ln = struct.unpack("!H", data[i + 2:i + 4])[0]
        plen = 4 * (ln + 1)
        if plen < 4 or i + plen > len(data):
            break
        out.append((pt, data[i:i + plen]))
        i += plen
    return out


DTMF_FREQS = {"1": (697, 1209), "2": (697, 1336), "3": (697, 1477),
              "4": (770, 1209), "5": (770, 1336), "6": (770, 1477),
              "7": (852, 1209), "8": (852, 1336), "9": (852, 1477),
              "*": (941, 1209), "0": (941, 1336), "#": (941, 1477)}


def dtmf_pcm(digit, phases, n=160, amp=5000):
    f1, f2 = DTMF_FREQS[digit]
    p1, p2 = phases.get(digit, (0.0, 0.0))
    out = bytearray()
    for i in range(n):
        v = int(amp * math.sin(p1 + 2 * math.pi * f1 * i / 8000.0) +
                amp * math.sin(p2 + 2 * math.pi * f2 * i / 8000.0))
        out += struct.pack("<h", max(-32768, min(32767, v)))
    phases[digit] = ((p1 + 2 * math.pi * f1 * n / 8000.0) % (2 * math.pi),
                     (p2 + 2 * math.pi * f2 * n / 8000.0) % (2 * math.pi))
    return bytes(out)


# ---------------------------------------------------------------- daemon

class Daemon:
    def __init__(self):
        self.running = True
        self.ike = None
        self.client = None
        self.sim = None
        self.imsi = self.imei = self.nai = ""
        self.domain = ""
        self.epdg = None
        self.epdg_override = uci_get("epdg")
        self.epdg_fallback_override = uci_get("epdg_fallback")
        self.fwmark = int(uci_get("fwmark", "0x401"), 0)
        self.local_ip = uci_get("local_ip") or detect_usb0()
        self.refresh = int(uci_get("refresh", "3600"))
        self.enabled = uci_get("enabled", "1") in ("1", "true", "yes", "on")
        self.state = "off"
        self.ready = False
        self.reason = None
        self.call = self.new_call()
        self.sock = None
        self.conn = None
        self.owner = None
        self.pcm_q = []
        self.dtmf_q = []
        self.dtmf_events = []
        self.dtmf_active = None
        self.dtmf_phases = {}
        self.last_keepalive = 0
        self.last_ike_rx = 0
        self.next_refresh = 0
        self.next_media = 0
        self.next_rtcp = 0
        self.connecting = False
        self.stop_flag = False
        self.last_invite_headers = None
        self.rtcp_rx = 0
        self.rr_blocks = []
        self.dpd = None
        self.dpd_miss = 0
        self.next_dpd = 0
        self.rf = None          # re-REGISTER in progress (3GPP sec-agree, non-blocking)
        self.stat = {}
        self.next_stat = 0
        self.full_pending = False
        self.prev_sa = None
        self.prev_until = 0

    def new_call(self):
        return {"state": "idle", "dir": None, "number": None, "since": None,
                "active_since": None, "reason": None, "callid": None,
                "local_tag": None, "remote_tag": None, "remote_target": None,
                "route": None, "invite_cseq": 1, "local_cseq": 1,
                "sdp": None, "remote_ip": None, "remote_port": None,
                "remote_rtcp": None, "te": False, "seq": 0, "ts": 0, "ssrc": 0,
                "rtp_count": 0, "octet_count": 0, "rx": 0, "sent": 0,
                "lost": 0, "last_seq": None, "first": True, "ringing": False,
                "cancel": False, "pracked": False, "invite_200": False, "te_pt": 101,
                "amr_pt": AMR_PT, "amr_oa": True}

    # ---------------- state / socket ----------------
    def view(self):
        c = self.call
        return {"type": "state", "ready": self.ready, "state": c["state"],
                "dir": c["dir"], "number": c["number"], "since": c["since"],
                "active_since": c["active_since"],
                "owner": self.conn is not None and self.conn is self.owner,
                "reason": c["reason"], "path": "wifi", "wifi": self.state}

    def notify(self):
        if self.conn:
            self.send_json(self.view())

    def set_state(self, state, reason=None):
        c = self.call
        if c["state"] != state or reason is not None:
            log("state", c["dir"], mask_num(c["number"]), c["state"], "->", state, reason or "")
            c["state"] = state
            if state == "idle":
                c["reason"] = reason or c["reason"]
            self.notify()

    def set_wifi(self, state):
        if self.state != state:
            log("wifi", self.state, "->", state)
            self.state = state
            self.ready = state == "registered"
            self.notify()

    def send_json(self, obj):
        if not self.conn:
            return
        data = json.dumps(obj, ensure_ascii=False).encode()
        try:
            self.conn.sendall(bytes([0]) + struct.pack("!H", len(data)) + data)
        except OSError:
            self.drop_client()

    def send_pcm(self, pcm):
        if not self.conn:
            return
        try:
            self.conn.sendall(bytes([1]) + struct.pack("!H", len(pcm)) + pcm)
        except OSError:
            self.drop_client()

    def count(self, key, n=1):
        self.stat[key] = self.stat.get(key, 0) + n

    def flush_stat(self, now):
        if now < self.next_stat:
            return
        self.next_stat = now + 60
        if self.stat:
            log("stat 60s", " ".join("%s=%d" % kv for kv in sorted(self.stat.items())))
            self.stat = {}

    def error(self, msg):
        log("error", msg)
        self.send_json({"type": "error", "error": msg})

    def drop_client(self):
        if self.conn:
            try:
                self.conn.close()
            except OSError:
                pass
            if self.conn is self.owner and self.call["state"] != "idle":
                log("owner left; hangup")
                self.hangup("owner left")
            self.conn = None
            self.owner = None

    # ---------------- lifecycle ----------------
    def start_server(self):
        try:
            os.unlink(SOCK_PATH)
        except OSError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(SOCK_PATH)
        os.chmod(SOCK_PATH, 0o600)
        self.sock.listen(2)
        self.sock.setblocking(False)
        log("listening on", SOCK_PATH)

    def start(self):
        setup_log()
        log("starting; enabled=%s epdg=%s local_ip=%s refresh=%s" %
            (self.enabled, self.epdg, self.local_ip, self.refresh))
        self.start_server()
        if self.enabled:
            threading.Thread(target=self.connect_loop, daemon=True).start()
        else:
            self.set_wifi("off")
        self.loop()

    def connect_loop(self):
        backoff = [10, 30, 60, 300]
        i = 0
        no_epdg = 0
        no_epdg_imsi = None
        while self.running and not self.stop_flag:
            try:
                self.connect()
                i = 0
                no_epdg = 0
                no_epdg_imsi = None
                time.sleep(1)
                while self.running and self.ike is not None and not self.stop_flag:
                    time.sleep(2)
                if self.stop_flag:
                    break
            except Exception as e:
                log("connect failed:", type(e).__name__, str(e)[:200])
                self.ike = None
                self.client = None
                if isinstance(e, NoEPDG):
                    no_epdg = no_epdg + 1 if no_epdg_imsi == self.imsi else 1
                    no_epdg_imsi = self.imsi
                else:
                    no_epdg = 0
                rejected = subscription_rejected(e)
                if rejected or no_epdg >= 3:
                    self.set_wifi("unsupported" if rejected else "unavailable")
                    self.wait_for_new_sim()
                    i = no_epdg = 0
                    no_epdg_imsi = None
                    continue
                self.set_wifi("error")
            if not self.running or self.stop_flag:
                break
            wait = backoff[min(i, len(backoff) - 1)]
            i += 1
            log("reconnect in %d s" % wait)
            time.sleep(wait)

    def wait_for_new_sim(self):
        since = time.time()
        while self.running and not self.stop_flag:
            try:
                with open(STATUS_PATH) as source:
                    status = json.load(source)
                if status.get("epoch", 0) > since and status.get("imsi") and status["imsi"] != self.imsi:
                    log("SIM changed; retry Wi-Fi Calling")
                    return
            except (OSError, ValueError):
                pass
            time.sleep(10)

    def epdg_order(self):
        cands = list(self.epdg_candidates)
        try:
            with open(EPDG_MEMO) as f:
                last = f.read().strip()
            if last in cands:
                cands.remove(last)
                cands.insert(0, last)
        except OSError:
            pass
        return cands

    def remember_epdg(self, epdg):
        try:
            os.makedirs(os.path.dirname(EPDG_MEMO), exist_ok=True)
            with open(EPDG_MEMO, "w") as f:
                f.write(epdg)
        except OSError:
            pass

    def connect(self):
        self.set_wifi("connecting")
        self.local_ip = uci_get("local_ip") or detect_usb0()
        if not self.local_ip:
            raise RuntimeError("usb0 is down")
        self.sim = V.LocalSim("A0000000871002FF49FF0589")
        self.imsi = self.sim.imsi()
        reg_state = self.sim.ensure_auto_registration()
        serving_plmn = self.sim.serving_plmn()
        self.imei = self.sim.imei()
        mnc_length = self.sim.mnc_length()
        mcc, mnc = self.imsi[:3], self.imsi[3:3 + mnc_length]
        mnc3 = mnc.zfill(3)
        self.domain = "ims.mnc%s.mcc%s.3gppnetwork.org" % (mnc3, mcc)
        self.nai = "0%s@nai.epc.mnc%s.mcc%s.3gppnetwork.org" % (self.imsi, mnc3, mcc)
        discovered = D.discover_epdg(mcc, mnc, self.local_ip, self.fwmark,
                                     visited_mcc=serving_plmn[:3])
        profile = EPDG_PROFILES.get((mcc, mnc), ())
        self.epdg_candidates = list(dict.fromkeys(
            ([self.epdg_override] if self.epdg_override else []) + discovered +
            ([self.epdg_fallback_override] if self.epdg_fallback_override else []) +
            list(profile)))
        if not self.epdg_candidates:
            raise NoEPDG("no ePDG found for HPLMN %s-%s" % (mcc, mnc))
        log("HPLMN", mcc + mnc, "serving MCC", serving_plmn[:3],
            "registration", "roaming" if reg_state == 5 else "home",
            "ePDG candidates", len(self.epdg_candidates),
            "source", "manual" if self.epdg_override else
            ("DNS" if discovered else "compatibility profile" if profile else "none"),
            "IMS domain", self.domain)
        log("IMSI", V.mask4(self.imsi), "IMEI", V.mask4(self.imei))
        ike = None
        last = None
        for epdg in self.epdg_order():
            try:
                log("ePDG", epdg)
                cand = V.IkeEsp(epdg, self.nai, self.imsi, self.imei, timeout=8.0,
                                local_ip=self.local_ip, mark=self.fwmark)
                cand.sim = self.sim
                cand.sa_init()
                cand.ike_auth()
                cand.derive_child()
                if not cand.cp["ip4"] or not cand.cp["pcscf4"]:
                    raise RuntimeError("no CP IPv4/P-CSCF")
                ike = cand
                self.epdg = epdg
                self.remember_epdg(epdg)
                break
            except Exception as e:
                last = e
                log("ePDG", epdg, "failed:", type(e).__name__, str(e)[:120])
        if ike is None:
            raise last or RuntimeError("no ePDG")
        pcscf = ike.cp["pcscf4"][0]
        log("connected: CP", ike.cp["ip4"][0], "P-CSCF", pcscf)
        self.last_ike_rx = time.time()
        uc = 5200 + 2 * random.randint(0, 400)
        client = V.ImsClient(ike, pcscf, self.domain, self.imsi,
                             "%s@%s" % (self.imsi, self.domain),
                             "sip:%s@%s" % (self.imsi, self.domain), self.sim,
                             sec_alg=("hmac-sha-1-96", "aes-cbc"), pani=True,
                             uc_port=uc, us_port=uc + 2)
        client.imei = self.imei
        self.register_new(client)
        self.ike = ike
        self.client = client
        self.next_refresh = time.time() + self.refresh
        self.next_dpd = time.time() + DPD_INTERVAL
        self.dpd = None
        self.dpd_miss = 0
        self.rf = None
        self.set_wifi("registered")

    def register_new(self, client, flags=None):
        flags = flags or {"auth_placeholder": True, "sip_instance": True,
                          "supported_sec_agree": True}
        client.cseq += 1
        sip = V.build_register_t5(client.impi, client.impu, self.imsi, self.imei,
                                  client.ike.cp["ip4"][0], client.port_uc, client.port_us,
                                  client.spi_uc, client.spi_us, self.domain, client.call_id,
                                  client.cseq, flags=flags, sec_alg=("hmac-sha-1-96", "aes-cbc"))
        client.send_plain(sip)
        resp = client.recv_sip(12)
        if not resp:
            raise RuntimeError("REGISTER no response")
        code, reason, headers, _ = resp
        log("REGISTER", code, reason)
        if code == 401:
            code, reason, headers = V.task5_continue(
                client, code, headers, flags, self.imsi, self.imei,
                ("hmac-sha-1-96", "aes-cbc"), self.domain, client.pcscf, do_deregister=False)
        if code != 200:
            raise RuntimeError("REGISTER failed %s %s" % (code, reason))
        log("REGISTER 200 OK; contact", V.masked_sip(
            client.reg_headers.get("contact", [""])[0], self.imsi, self.imei))
        uris = []
        for v in getattr(client, "reg_pa", []) or []:
            uris += re.findall(r"<([^>]+)>", v)
        client.local_uri = uris[0] if uris else client.impu
        client.pref_uri = next((u for u in uris if "user=phone" in u), client.local_uri)
        # Some SIMs leave AT+CNUM empty. Keep the IMS-assigned number local to the node.
        number = next((m[1] for u in uris
                       if (m := re.match(r"(?:sip:|tel:)(\+[1-9][0-9]{5,14})(?=[@;]|$)", u))), None)
        try:
            fd = os.open(PHONE_PATH + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"imsi": self.imsi, "msisdn": number}, f)
            os.replace(PHONE_PATH + ".tmp", PHONE_PATH)
        except OSError:
            log("own number cache unavailable")
        log("local URI", V.masked_sip(client.local_uri, self.imsi, self.imei))
        for q in client.ike.ike_queue:
            self.handle_ike(q)
        client.ike.ike_queue.clear()

    # ---------------- re-REGISTER: TS 24.229 5.1.1.4, TS 33.203 7.4 ----------------
    # Non-blocking.  Stage 1 sends REGISTER on the current SA with a new Security-Client.
    # The 401 gives a new challenge and Security-Server; AKA gives new CK/IK.  Stage 2
    # sends the protected REGISTER on the new SA.  The 200 switches the active SA.

    def new_port(self, c):
        while True:
            p = 5200 + 2 * random.randint(0, 400)
            if p not in (c.port_uc, c.port_us):
                return p

    def start_refresh(self):
        c = self.client
        old = c.sa_dict()
        spi_c, spi_s = random.getrandbits(32), random.getrandbits(32)
        sec_ports = (spi_c, spi_s, self.new_port(c), c.port_us)
        self.rf = {"stage": 1, "deadline": time.time() + 10, "old": old,
                   "old_chal": c.reg_chal, "old_res": c.reg_res, "old_sec": c.reg_sec_raw,
                   "old_flags": c.reg_flags, "sec_ports": sec_ports}
        log("refresh stage 1: REGISTER on current SA, new Security-Client")
        c.cseq += 1
        flags = dict(c.reg_flags)
        flags["expires"] = 600000
        flags["auth_placeholder"] = True
        sip = V.build_register_t5(c.impi, c.impu, self.imsi, self.imei, c.ike.cp["ip4"][0],
                                  c.port_uc, c.port_us, c.spi_uc, c.spi_us, self.domain, c.call_id,
                                  c.cseq, flags=flags, sec_verify=c.reg_sec_raw,
                                  sec_alg=SEC_ALG, sec_ports=sec_ports)
        c.send_protected(sip, old)

    def on_register_response(self, code, reason, headers):
        rf, c = self.rf, self.client
        if rf is None or c is None:
            log("REGISTER %d outside refresh" % code)
            return
        log("refresh stage %d -> %d %s" % (rf["stage"], code, reason))
        if rf["stage"] == 1 and code == 200:
            log("refresh 200 OK on current SA; P-CSCF did not renew the SA")
            self.rf = None
            self.next_refresh = time.time() + self.refresh
        elif rf["stage"] == 1 and code == 401:
            self.refresh_stage2(headers)
        elif rf["stage"] == 2 and code == 200:
            self.refresh_commit(headers)
        else:
            self.refresh_failed("stage %d -> %d" % (rf["stage"], code))

    def refresh_stage2(self, headers):
        rf, c = self.rf, self.client
        chal = None
        for v in headers.get("www-authenticate", []):
            if v.lower().startswith("digest"):
                chal = V.parse_params(v)
                break
        sec_raw = next((v for v in headers.get("security-server", [])
                        if v.lower().startswith("ipsec-3gpp")), None)
        if not chal or not sec_raw:
            return self.refresh_failed("401 without challenge or Security-Server")
        nonce = chal.get("nonce", "")
        raw = base64.b64decode(nonce + "=" * (-len(nonce) % 4))
        r = c.sim.authenticate(raw[:16], raw[16:32])
        if r[0] != "OK":
            return self.refresh_failed("AKA %s" % r[0])
        _, res, ck, ik = r
        spi_c, spi_s, port_c, port_s = rf["sec_ports"]
        c.nxt = {"ck": ck, "ik": ik, "sec": V.parse_security(sec_raw), "spi_uc": spi_c,
                 "spi_us": spi_s, "port_uc": port_c, "port_us": port_s}
        rf.update(stage=2, deadline=time.time() + 10, chal=chal, res=res, sec_raw=sec_raw)
        log("refresh stage 2: protected REGISTER on new SA")
        c.cseq += 1
        flags = dict(rf["old_flags"])
        flags["expires"] = 600000
        auth = digest_nc(c, chal, res, "00000001")
        sip = V.build_register_t5(c.impi, c.impu, self.imsi, self.imei, c.ike.cp["ip4"][0],
                                  port_c, port_s, spi_c, spi_s, self.domain, c.call_id, c.cseq,
                                  flags=flags, auth=auth, sec_verify=sec_raw, sec_alg=SEC_ALG)
        c.send_protected(sip, c.nxt)

    def refresh_commit(self, headers):
        rf, c = self.rf, self.client
        nxt = c.nxt
        log("refresh 200 OK; Contacts on P-CSCF:", len(headers.get("contact", [])))
        self.prev_sa = rf["old"]
        self.prev_until = time.time() + 10
        c.ck, c.ik, c.sec = nxt["ck"], nxt["ik"], nxt["sec"]
        c.spi_uc, c.spi_us = nxt["spi_uc"], nxt["spi_us"]
        c.port_uc, c.port_us = nxt["port_uc"], nxt["port_us"]
        c.reg_chal, c.reg_res, c.reg_sec_raw = rf["chal"], rf["res"], rf["sec_raw"]
        c.nxt = None
        self.rf = None
        self.next_refresh = time.time() + self.refresh
        self.deregister_old(rf)

    def deregister_old(self, rf):
        # Remove the previous Contact over the previous SA, so the P-CSCF keeps one binding.
        c, old = self.client, rf["old"]
        c.cseq += 1
        flags = dict(rf["old_flags"])
        flags["expires"] = 0
        auth = digest_nc(c, rf["old_chal"], rf["old_res"], "00000003")
        sip = V.build_register_t5(c.impi, c.impu, self.imsi, self.imei, c.ike.cp["ip4"][0],
                                  old["port_uc"], old["port_us"], old["spi_uc"], old["spi_us"],
                                  self.domain, c.call_id, c.cseq, flags=flags, auth=auth,
                                  sec_verify=rf["old_sec"], sec_alg=SEC_ALG, contact_expires=0)
        try:
            c.send_protected(sip, old)
            log("old Contact de-REGISTER sent")
        except Exception as e:
            log("old Contact de-REGISTER failed:", e)

    def refresh_failed(self, why):
        log("refresh failed:", why)
        if self.client:
            self.client.nxt = None
        self.rf = None
        self.full_pending = True
        self.next_refresh = time.time() + 10

    def full_reregister(self):
        # Fallback only: de-REGISTER the old Contact, then a new full registration.
        self.full_pending = False
        old = self.client
        log("full registration with new ports")
        self.deregister()
        self.set_wifi("connecting")
        p = self.new_port(old)
        newc = V.ImsClient(old.ike, old.pcscf, self.domain, self.imsi, old.impi, old.impu, self.sim,
                           sec_alg=SEC_ALG, pani=True, uc_port=p, us_port=p + 2)
        newc.imei = self.imei
        try:
            self.register_new(newc)
            self.client = newc
            self.next_refresh = time.time() + self.refresh
            self.set_wifi("registered")
        except Exception as e:
            log("full registration failed:", e)
            self.reconnect("full registration failed")

    def send_dpd(self, now):
        ike = self.ike
        ike.msgid += 1
        self.dpd = {"mid": ike.msgid, "deadline": now + DPD_TIMEOUT}
        self.next_dpd = now + DPD_INTERVAL
        self.count("dpd_tx")
        log("DPD tx mid", ike.msgid)
        try:
            ike.send_empty_response(ike.msgid, 37, flags=0x08)
        except OSError as e:
            log("DPD send failed:", e)

    def probe_sa(self):
        ike = self.ike
        ike.msgid += 1
        pkt = ike.empty_msg(ike.msgid, 37, 0x08)
        ike.timeout = DPD_TIMEOUT
        try:
            ike.exchange_ike(pkt, ike.msgid, "DPD", retries=0)
            log("SA alive (DPD)")
            return True
        except Exception as e:
            log("SA dead (DPD):", e)
            return False

    def deregister(self):
        c = self.client
        if not c:
            return
        log("de-REGISTER")
        try:
            c.cseq += 1
            base = dict(getattr(c, "reg_flags", {}) or {})
            base["sip_instance"] = True
            base["expires"] = 0
            first = dict(base)
            first["no_sec_client"] = True
            auth = digest_nc(c, c.reg_chal, c.reg_res, "00000002")
            sip = V.build_register_t5(c.impi, c.impu, self.imsi, self.imei,
                                      c.ike.cp["ip4"][0], c.port_uc, c.port_us,
                                      c.spi_uc, c.spi_us, self.domain, c.call_id, c.cseq,
                                      flags=first, auth=auth, sec_verify=c.reg_sec_raw,
                                      sec_alg=("hmac-sha-1-96", "aes-cbc"), contact_expires=0)
            c.send_protected(sip)
            resp = c.recv_sip(10)
            if not resp:
                log("de-REGISTER no response")
                return
            code, reason, headers, _ = resp
            log("de-REGISTER ->", code, reason)
            if code == 200:
                return
            if code != 401:
                return
            chal = None
            for v in headers.get("www-authenticate", []):
                if v.lower().startswith("digest"):
                    chal = V.parse_params(v)
                    break
            if not chal:
                return
            raw = base64.b64decode(chal.get("nonce", "") + "=" * (-len(chal.get("nonce", "")) % 4))
            r = c.sim.authenticate(raw[:16], raw[16:32])
            if r[0] != "OK":
                return
            _, res, ck, ik = r
            old_ck, old_ik = c.ck, c.ik
            auth = digest_nc(c, chal, res, "00000001")
            c.cseq += 1
            sip = V.build_register_t5(c.impi, c.impu, self.imsi, self.imei,
                                      c.ike.cp["ip4"][0], c.port_uc, c.port_us,
                                      c.spi_uc, c.spi_us, self.domain, c.call_id, c.cseq,
                                      flags=first, auth=auth, sec_verify=c.reg_sec_raw,
                                      sec_alg=("hmac-sha-1-96", "aes-cbc"), contact_expires=0)
            c.send_protected(sip)
            resp = c.recv_sip(10)
            if resp and resp[0] == 200:
                log("de-REGISTER 200 OK")
                return
            c.ck, c.ik = old_ck, old_ik
            c.cseq += 1
            c.send_protected(sip)
            resp = c.recv_sip(10)
            log("de-REGISTER retry ->", resp[0] if resp else "no response")
        except Exception as e:
            log("de-REGISTER failed:", e)

    # ---------------- main loop ----------------
    def loop(self):
        while self.running:
            ike = self.ike
            fds = [self.sock]
            if self.conn:
                fds.append(self.conn)
            if ike and ike.sock:
                fds.append(ike.sock)
            try:
                r, _, _ = select.select(fds, [], [], 0.02)
            except OSError:
                continue
            for s in r:
                if s is self.sock:
                    self.accept()
                elif s is self.conn:
                    self.read_client()
                elif ike and s is ike.sock:
                    self.read_ike()
            now = time.time()
            self.flush_stat(now)
            if self.ike and now - self.last_keepalive >= 20:
                self.last_keepalive = now
                try:
                    self.ike.sock.sendto(b"\xff", (self.epdg, 4500))
                    self.count("ka_tx")
                except OSError as e:
                    self.count("ka_err")
                    log("keepalive send failed:", e)
            if self.ike and now >= self.next_dpd and self.dpd is None:
                self.send_dpd(now)
            if self.dpd and now > self.dpd["deadline"]:
                self.dpd = None
                self.dpd_miss += 1
                self.count("dpd_miss")
                log("DPD no response; misses=%d" % self.dpd_miss)
                if self.dpd_miss >= DPD_MISSES and self.ike:
                    self.reconnect("DPD: SA lost")
            if self.ike and self.client and self.rf is None and now >= self.next_refresh:
                try:
                    self.start_refresh()
                except Exception as e:
                    self.refresh_failed(str(e))
            if self.rf and now > self.rf["deadline"]:
                self.refresh_failed("timeout in stage %d" % self.rf["stage"])
            if self.full_pending and self.rf is None and self.ike and self.call["state"] == "idle":
                self.full_reregister()
            if self.prev_sa and now >= self.prev_until:
                self.prev_sa = None
            if self.call["state"] in ("dialing", "alerting"):
                self.call_timeout()
            if self.call["state"] == "active" and now >= self.next_media:
                self.next_media = now + 0.02
                self.media_tick()

    def accept(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        if self.conn:
            try:
                conn.close()
            except OSError:
                pass
            return
        conn.setblocking(False)
        self.conn = conn
        log("client connected")
        self.send_json(self.view())

    def read_client(self):
        if not self.conn:
            return
        try:
            data = self.conn.recv(65536)
        except BlockingIOError:
            return
        except OSError:
            self.drop_client()
            return
        if not data:
            self.drop_client()
            return
        self.buf = getattr(self, "buf", b"") + data
        while len(self.buf) >= 3:
            typ = self.buf[0]
            ln = struct.unpack("!H", self.buf[1:3])[0]
            if len(self.buf) < 3 + ln:
                break
            payload = self.buf[3:3 + ln]
            self.buf = self.buf[3 + ln:]
            if typ == 0:
                try:
                    msg = json.loads(payload.decode())
                except Exception:
                    continue
                self.op(msg)
            elif typ == 1:
                if self.call["state"] == "active" and self.conn is self.owner:
                    self.pcm_q.append(payload)
                    if len(self.pcm_q) > 10:
                        self.pcm_q.pop(0)

    def op(self, m):
        o = m.get("op")
        log("op", o)
        if o == "dial":
            self.dial(str(m.get("number", "")))
        elif o == "answer":
            self.answer()
        elif o == "hangup":
            self.hangup("hangup")
        elif o == "dtmf":
            self.dtmf(str(m.get("digits", "")))
        elif o == "refresh":  # test hook: run a re-REGISTER now
            self.next_refresh = 0
        else:
            self.error("unknown op")

    # ---------------- IKE handling ----------------
    def read_ike(self):
        ike = self.ike
        if not ike or not ike.sock:
            return
        try:
            kind, data = ike.recv_pkt()
        except socket.timeout:
            return
        except BlockingIOError:
            return
        except OSError:
            return
        self.last_ike_rx = time.time()
        if kind == "esp":
            self.count("esp_rx")
            inner = ike.esp_decrypt(data, ike.esp["er"], ike.esp["ar"], ike.child_spi_i)
            if inner:
                self.on_inner(inner)
            else:
                self.count("esp_bad")
                log("ESP bad spi", data[:4].hex(), "len", len(data))
        elif kind == "ike":
            self.handle_ike(data)
        elif kind == "keepalive":
            self.count("ka_rx")
            log("NAT keepalive rx")

    def handle_ike(self, data):
        ike = self.ike
        if not ike or len(data) < 28:
            return
        spi_i, spi_r, nxt, ver, exch, flags, mid, length = struct.unpack("!8s8sBBBBII", data[:28])
        if spi_i != ike.spi_i or spi_r != ike.spi_r:
            self.count("ike_old_spi")
            log("IKE rx other SA exch", exch, "mid", mid, "flags", hex(flags), "spi_i", spi_i.hex(), "spi_r", spi_r.hex())
            return  # an old SA; ignore its DPD/delete
        log("IKE rx exch", exch, "mid", mid, "resp" if flags & 0x20 else "req", "flags", hex(flags), "len", length, "spi_r", spi_r.hex())
        if flags & 0x20:
            if exch == 37 and self.dpd and mid == self.dpd["mid"]:
                self.dpd = None
                self.dpd_miss = 0
                self.count("dpd_ok")
                log("DPD ok mid", mid)
            return
        self.count("ike_rx")
        try:
            pls = ike.decrypt(data)
        except Exception as e:
            log("IKE request decrypt failed:", e)
            return
        types = [t for t, _ in pls]
        log("IKE request exch", exch, "mid", mid, "types", types)
        if exch == 37:  # INFORMATIONAL: DPD or delete
            for t, b in pls:
                if t == 42:  # DELETE
                    ike.send_empty_response(mid, 37)
                    log("ePDG DELETE; reconnect")
                    self.reconnect("epdg delete")
                    return
            ike.send_empty_response(mid, 37)
        elif exch == 36:  # CREATE_CHILD_SA: reconnect rather than rekey
            try:
                ike.send_response([(41, b"\x00\x00\x00\x0e")], mid, exchange=36)
            except Exception:
                pass
            log("ePDG CREATE_CHILD_SA; reconnect")
            self.reconnect("rekey requested")

    def reconnect(self, why):
        log("reconnect:", why)
        self.set_wifi("connecting")
        self.stop_call("network")
        old = self.ike
        self.ike = None
        self.client = None
        self.rf = None
        self.dpd = None
        self.full_pending = False
        self.prev_sa = None
        try:
            if old and old.sock:
                old.sock.close()
        except OSError:
            pass

    def inbound_sas(self, c):
        sas = [c.sa_dict()]
        if c.nxt:
            sas.append(c.nxt)
        if self.prev_sa:
            sas.append(self.prev_sa)
        return sas

    def on_inner(self, pkt):
        ip = V.parse_ipv4(pkt)
        if ip is None:
            return
        if struct.unpack("!H", pkt[6:8])[0] & 0x3FFF:
            self.count("ip_frag_rx")
            return
        if ip["proto"] == 50 and self.client and self.client.ck:
            udp = None
            for sa in self.inbound_sas(self.client):
                udp = V.esp_unpack_alg(ip["payload"], sa["sec"].get("alg"), sa["sec"].get("ealg"),
                                       sa["ck"], sa["ik"], sa["spi_us"], 17)
                if udp:
                    break
            if not udp:
                self.count("ims_esp_bad")
                return
        elif ip["proto"] == 17:
            udp = ip["payload"]
        else:
            return
        sport, dport, ulen = struct.unpack("!HHH", udp[:6])
        payload = udp[8:ulen]
        if payload.startswith((b"SIP/2.0", b"INVITE", b"ACK ", b"BYE ", b"CANCEL",
                               b"UPDATE", b"PRACK", b"OPTIONS", b"INFO",
                               b"NOTIFY", b"REFER", b"MESSAGE")):
            self.on_sip(payload)
        elif dport == RTP_PORT:
            self.on_rtp(payload)
        elif dport == RTCP_PORT:
            self.on_rtcp(payload)

    # ---------------- SIP ----------------
    def send_sip(self, data):
        if self.client:
            self.client.send_protected(data)

    def on_sip(self, data):
        code, reason, headers, body = parse_sip(data)
        cseq = hget(headers, "cseq", "")
        if code is None:
            log("SIP rx request", headers.get("__method", [""])[0], cseq)
            self.on_request(headers.get("__method", [""])[0], headers, body)
            return
        log("SIP rx", code, cseq)
        if cseq.split()[-1:] == ["REGISTER"] and self.client and \
                hget(headers, "call-id", "") == self.client.call_id:
            self.on_register_response(code, reason, headers)
        else:
            self.on_response(code, reason, headers, body)

    def call_timeout(self):
        c = self.call
        if c["state"] in ("dialing", "alerting") and c.get("deadline") and time.time() > c["deadline"]:
            log("call timeout")
            self.hangup("timeout")

    def on_response(self, code, reason, headers, body):
        c = self.call
        if c["state"] == "idle":
            return
        callid = hget(headers, "call-id", "")
        if c["callid"] and callid != c["callid"]:
            return
        cseq = hget(headers, "cseq", "")
        method = cseq.split()[1] if len(cseq.split()) > 1 else ""
        log("<-", code, reason, method)
        if method == "INVITE":
            if code == 100:
                return
            if code in (180, 183):
                tag = to_tag(headers)
                if tag:
                    c["remote_tag"] = tag
                self.update_dialog(headers)
                if code == 183:
                    sd = parse_sdp(body)
                    if sd.get("ip") and sd.get("port"):
                        c["sdp"] = sd
                        c["te"] = bool(sd.get("telephone_event"))
                        c["te_pt"] = sdp_te_pt(sd)
                    rseq = hget(headers, "rseq")
                    if rseq and not c["pracked"]:
                        self.send_prack(rseq.split()[0])
                        c["pracked"] = True
                if c["state"] == "dialing":
                    c["state"] = "alerting"
                    self.notify()
                return
            if code == 200:
                tag = to_tag(headers)
                if tag:
                    c["remote_tag"] = tag
                self.update_dialog(headers)
                sd = parse_sdp(body)
                if sd.get("ip") and sd.get("port"):
                    c["sdp"] = sd
                    c["te"] = bool(sd.get("telephone_event"))
                    c["te_pt"] = sdp_te_pt(sd)
                self.send_ack()
                c["invite_200"] = True
                c["active_since"] = int(time.time())
                self.set_state("active")
                self.start_media()
                return
            if code >= 300:
                self.send_ack(c.get("invite_branch"))
                self.stop_call("rejected %s" % code)
                self.error("call failed: %s %s" % (code, reason))
                return
        if method == "PRACK" and code == 200:
            return
        if method == "UPDATE" and code == 200:
            sd = parse_sdp(body)
            if sd.get("ip") and sd.get("port"):
                c["sdp"] = sd
            return
        if method == "BYE" and code == 200:
            self.stop_call("ended")
            return
        if method == "CANCEL":
            return
        if code >= 300:
            log("in-dialog failure", code, method)

    def on_request(self, method, headers, body):
        c = self.call
        log("request", method)
        if method == "INVITE":
            if c["state"] == "idle":
                self.on_invite(headers, body)
            elif c["state"] in ("active", "incoming") and hget(headers, "call-id") == c["callid"]:
                sd = parse_sdp(body)
                if sd.get("ip") and sd.get("port"):
                    c["sdp"] = sd
                    self.update_remote_media(sd)
                self.send_sip(sip_resp(200, "OK", headers,
                                       self.answer_sdp().encode(),
                                       "application/sdp"))
            else:
                self.send_sip(sip_resp(486, "Busy Here", headers))
            return
        elif method == "ACK":
            if c["state"] == "active":
                pass
        elif method == "BYE":
            self.send_sip(sip_resp(200, "OK", headers))
            self.stop_call("remote bye")
        elif method == "CANCEL":
            self.send_sip(sip_resp(200, "OK", headers))
            if c["state"] == "incoming":
                self.send_sip(sip_resp(487, "Request Terminated", self.last_invite_headers or headers))
                self.stop_call("cancel")
        elif method == "PRACK":
            self.send_sip(sip_resp(200, "OK", headers))
        elif method == "UPDATE":
            sd = parse_sdp(body)
            if sd.get("ip") and sd.get("port"):
                c["sdp"] = sd
                self.update_remote_media(sd)
            self.send_sip(sip_resp(200, "OK", headers,
                                   self.answer_sdp().encode(),
                                   "application/sdp"))
        elif method == "OPTIONS":
            self.send_sip(sip_resp(200, "OK", headers))
        else:
            self.send_sip(sip_resp(200, "OK", headers))

    def on_invite(self, headers, body):
        c = self.call
        self.last_invite_headers = headers
        offer = parse_sdp(body)
        frm = hget(headers, "from", "")
        m = re.search(r"<([^>]+)>", frm)
        uri = m.group(1) if m else frm.split(";")[0]
        number = re.sub(r"^(sip:|tel:)", "", uri).split("@")[0]
        ftag = re.search(r";tag=([^;>\s]+)", frm)
        c["dir"] = "in"
        c["number"] = number
        c["callid"] = hget(headers, "call-id")
        c["remote_tag"] = ftag.group(1) if ftag else None
        c["local_tag"] = os.urandom(4).hex()
        c["since"] = int(time.time())
        c["reason"] = None
        c["sdp"] = offer
        c["te"] = bool(offer.get("telephone_event"))
        c["te_pt"] = sdp_te_pt(offer)
        contact = hget(headers, "contact", "")
        m = re.search(r"<([^>]+)>", contact)
        c["remote_target"] = m.group(1) if m else self.sip_uri(number)
        c["to_uri"] = "<%s>" % uri
        rr = headers.get("record-route", [])
        if rr:
            m = re.search(r"<([^>]+)>", rr[0])
            if m:
                c["route"] = m.group(1)
        c["invite_cseq"] = int(hget(headers, "cseq", "1 INVITE").split()[0])
        c["via"] = hget(headers, "via", "")
        c["from"] = frm
        c["to"] = hget(headers, "to", "")
        # Every response must add our To tag.
        resp_headers = dict(headers)
        resp_headers["to"] = [c["to"] + ";tag=" + c["local_tag"]]
        self.last_invite_headers = resp_headers
        self.send_sip(sip_resp(100, "Trying", headers))
        sdp = self.answer_sdp()
        contact_hdr = self.call_contact()
        rr_extra = ["Record-Route: %s" % v for v in headers.get("record-route", [])]
        self.send_sip(sip_resp(183, "Session Progress", resp_headers, sdp.encode(),
                               "application/sdp", contact_hdr,
                               rr_extra + ["Require: 100rel", "RSeq: 1"]))
        self.send_sip(sip_resp(180, "Ringing", resp_headers, b"", None, contact_hdr,
                               rr_extra + ["Require: 100rel", "RSeq: 2"]))
        self.set_state("incoming")
        self.notify()

    def answer_sdp(self):
        c = self.call
        sd = c["sdp"] or {}
        return build_sdp(self.ike.cp["ip4"][0], c["te"], amr=(sd.get("pt") or AMR_PT, sd.get("octet_align", True)),
                         te_pt=c.get("te_pt", TE_PT))

    def sip_uri(self, number):
        if number.startswith("+"):
            return "tel:%s" % number
        return "sip:%s@%s" % (number, self.domain)

    def update_remote_media(self, sd):
        c = self.call
        if sd.get("ip") and sd.get("port"):
            c["remote_ip"] = sd["ip"]
            c["remote_port"] = sd["port"]
            c["remote_rtcp"] = sd.get("rtcp_port") or (sd["port"] + 1)
            c["te"] = bool(sd.get("telephone_event"))
            c["te_pt"] = sdp_te_pt(sd)

    def update_dialog(self, headers):
        c = self.call
        ct = hget(headers, "contact", "")
        m = re.search(r"<([^>]+)>", ct)
        if m:
            c["remote_target"] = m.group(1)
        rr = headers.get("record-route", [])
        if rr:
            m = re.search(r"<([^>]+)>", rr[0])
            if m:
                c["route"] = m.group(1)

    def dialog_request(self, method, cseq, body=b"", content_type=None, branch=None):
        c = self.call
        uri = c["remote_target"] or "tel:%s" % (c["number"] or "")
        branch = branch or ("z9hG4bK" + os.urandom(6).hex())
        headers = [("Via", "SIP/2.0/UDP %s:%d;branch=%s;rport" %
                    (self.ike.cp["ip4"][0], self.client.port_uc, branch)),
                   ("Max-Forwards", "70")]
        if c["route"]:
            headers.append(("Route", "<%s>" % c["route"]))
        to_uri = c.get("to_uri") or ("<tel:%s>" % (c["number"] or ""))
        local_uri = getattr(self.client, "local_uri", None) or self.client.impu
        headers += [("From", "<%s>;tag=%s" % (local_uri, c["local_tag"])),
                    ("To", "%s%s" % (to_uri, (";tag=" + c["remote_tag"]) if c["remote_tag"] else "")),
                    ("Call-ID", c["callid"]),
                    ("CSeq", "%d %s" % (cseq, method))]
        if method == "ACK":
            headers.append(("Contact", "<sip:%s@%s:%d>" % (self.imsi, self.ike.cp["ip4"][0], self.client.port_uc)))
        return sip_msg(method, uri, headers, body, content_type)

    def send_prack(self, rseq):
        c = self.call
        c["local_cseq"] = 2
        data = self.dialog_request("PRACK", 2)
        # add RAck before the empty line: rebuild
        data = data.replace(b"Content-Length:", b"RAck: %s 1 INVITE\r\nContent-Length:" % rseq.encode())
        self.send_sip(data)

    def send_ack(self, branch=None):
        c = self.call
        self.send_sip(self.dialog_request("ACK", c["invite_cseq"], branch=branch))

    def call_contact(self):
        imei_gsma = (f"{self.imei[:8]}-{self.imei[8:14]}-{self.imei[14]}"
                     if len(self.imei) >= 15 else self.imei)
        return ('<sip:%s@%s:%d>;+g.3gpp.icsi-ref="urn%%3Aurn-7%%3A3gpp-service.ims.icsi.mmtel";'
                '+sip.instance="<urn:gsma:imei:%s>"' %
                (self.imsi, self.ike.cp["ip4"][0], self.client.port_uc, imei_gsma))

    def answer(self):
        c = self.call
        if c["state"] != "incoming":
            self.error("no incoming call")
            return
        self.owner = self.conn
        c["active_since"] = int(time.time())
        sdp = self.answer_sdp()
        contact = self.call_contact()
        # 200 OK to the INVITE, using the stored request headers.
        # Rebuild from the dialog (the request headers are no longer kept).
        rr = (self.last_invite_headers or {}).get("record-route", [])
        lines = ["SIP/2.0 200 OK"]
        lines += ["Record-Route: %s" % v for v in rr]
        lines += ["Via: %s" % c.get("via", ""),
                 "From: %s" % c.get("from", ""),
                 "To: %s;tag=%s" % (c.get("to", ""), c["local_tag"]),
                 "Call-ID: %s" % c["callid"],
                 "CSeq: %d INVITE" % c["invite_cseq"],
                 "Contact: %s" % contact,
                 "Allow: INVITE, ACK, CANCEL, BYE, PRACK, UPDATE, OPTIONS, INFO",
                 "Supported: 100rel, timer",
                 "Content-Type: application/sdp",
                 "Content-Length: %d" % len(sdp)]
        data = ("\r\n".join(lines) + "\r\n\r\n" + sdp).encode()
        self.send_sip(data)
        self.set_state("active")
        self.start_media()

    def start_call(self, number, owner):
        self.owner = owner
        c = self.call
        c["dir"] = "out"
        c["number"] = number
        c["local_tag"] = os.urandom(4).hex()
        c["callid"] = os.urandom(8).hex() + "@" + self.ike.cp["ip4"][0]
        c["since"] = int(time.time())
        c["reason"] = None
        c["invite_cseq"] = 1
        c["local_cseq"] = 1
        c["deadline"] = time.time() + 30
        data, route, contact, branch = build_invite(self.client, number, c["local_tag"], c["callid"])
        c["invite_branch"] = branch
        c["route"] = route
        c["remote_target"] = "tel:%s" % number
        c["to_uri"] = "<tel:%s>" % number
        log("INVITE", mask_num(number))
        self.send_sip(data)
        self.set_state("dialing")

    def dial(self, number):
        if not self.ready:
            self.error("wifi not registered")
            return
        if self.call["state"] != "idle":
            self.error("line busy")
            return
        if not re.fullmatch(r"\+?[0-9]{3,20}", number):
            self.error("bad number")
            return
        self.start_call(number, self.conn)

    def hangup(self, reason="hangup"):
        c = self.call
        if c["state"] == "idle":
            self.error("no call")
            return
        if c["state"] in ("dialing", "alerting"):
            if c["dir"] == "out":
                self.send_cancel()
            else:
                self.send_sip(sip_resp(486, "Busy Here", self.last_invite_headers or {}))
            self.stop_call(reason)
            return
        if c["state"] == "active":
            byec = c["local_cseq"] + 1
            c["local_cseq"] = byec
            self.send_sip(self.dialog_request("BYE", byec))
            self.stop_call(reason)
        elif c["state"] == "incoming":
            self.send_sip(sip_resp(486, "Busy Here", self.last_invite_headers or {}))
            self.stop_call(reason)

    def send_cancel(self):
        c = self.call
        data = self.dialog_request("CANCEL", c["invite_cseq"], branch=c.get("invite_branch"))
        self.send_sip(data)

    def stop_call(self, reason):
        c = self.call
        if c["state"] != "idle":
            rr = self.rr_blocks[-1] if getattr(self, "rr_blocks", None) else {}
            summary = "%s %s %s %s amr=%s/%s rx=%d sent=%d lost=%d rr_fraction=%s rr_cum=%s ft=%s peak=%s pcm_out=%s owner=%s" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), c["dir"], mask_num(c["number"]), reason, c.get("amr_pt"),
                "oa" if c.get("amr_oa") else "be", c["rx"], c["sent"], c["lost"], rr.get("fraction"), rr.get("cum"),
                c.get("ft"), c.get("peak"), c.get("pcm_out"), self.owner is not None)
            log("call end", summary)
            try:
                with open("/root/dongle-vowifi-calls.log", "a") as f:
                    f.write(summary + "\n")
            except OSError:
                pass
            self.call = self.new_call()
            self.call["reason"] = reason
            self.pcm_q.clear()
            self.dtmf_q.clear()
            self.dtmf_events.clear()
            self.dtmf_active = None
            self.next_media = 0
            if getattr(self, "amr", None):
                try:
                    self.amr.close()
                except Exception:
                    pass
                self.amr = None
            self.owner = None
            self.set_state("idle", reason)

    # ---------------- media ----------------
    def start_media(self):
        c = self.call
        sd = c["sdp"] or {}
        c["remote_ip"] = sd.get("ip")
        c["remote_port"] = sd.get("port")
        c["remote_rtcp"] = sd.get("rtcp_port") or ((sd.get("port") or 0) + 1)
        c["te"] = bool(sd.get("telephone_event"))
        c["amr_pt"] = sd.get("pt") or AMR_PT
        c["amr_oa"] = sd.get("octet_align", True)
        c["ft"], c["peak"], c["pcm_out"] = {}, 0, 0
        c["seq"] = int.from_bytes(os.urandom(2), "big")
        c["ts"] = int.from_bytes(os.urandom(4), "big")
        c["ssrc"] = int.from_bytes(os.urandom(4), "big")
        c["first"] = True
        c["rtp_count"] = 0
        c["octet_count"] = 0
        c["last_seq"] = None
        c["lost"] = 0
        c["rx"] = 0
        self.next_media = time.time()
        self.next_rtcp = time.time() + 1
        log("media start", c["remote_ip"], c["remote_port"], "te", c["te"], "amr pt", c["amr_pt"],
            "octet-aligned" if c["amr_oa"] else "bandwidth-efficient", "offer/answer formats", sd.get("formats"),
            "fmtp", sd.get("fmtp"))
        if getattr(self, "amr", None):
            try:
                self.amr.close()
            except Exception:
                pass
        self.amr = AmrNB()

    def media_tick(self):
        c = self.call
        if c["state"] != "active" or not c["remote_ip"]:
            return
        now = time.time()
        if self.dtmf_events:
            ev = self.dtmf_events[0]
            if now >= ev["next"]:
                if "ts" not in ev:
                    ev["ts"] = c["ts"]
                end = ev["sent"] >= 7
                dur = ev["dur"]
                payload = bytes([ev["code"], (0x80 if end else 0) | 10,
                                 (dur >> 8) & 0xff, dur & 0xff])
                rtp = bytes([0x80, c.get("te_pt", 101) & 0x7f]) + struct.pack(
                    "!HII", c["seq"] & 0xffff, ev["ts"] & 0xffffffff, c["ssrc"]) + payload
                pkt = V.build_ipv4_udp(self.ike.cp["ip4"][0], c["remote_ip"], RTP_PORT,
                                       c["remote_port"], rtp)
                self.ike.send_outer(pkt)
                c["seq"] = (c["seq"] + 1) & 0xffff
                c["rtp_count"] += 1
                c["octet_count"] += len(payload)
                c["sent"] += 1
                ev["sent"] += 1
                ev["dur"] += 160
                ev["next"] += 0.02
                if end:
                    self.dtmf_events.pop(0)
                    c["ts"] = (ev["ts"] + 8 * 160) & 0xffffffff
                    log("RFC4733 DTMF sent", ev["digit"])
                return
        tone = None
        if self.dtmf_q:
            d = self.dtmf_q[0]
            if now < d["end"]:
                tone = d["digit"]
            elif now < d["end"] + 0.16:
                tone = None
            else:
                self.dtmf_q.pop(0)
        if tone:
            pcm = dtmf_pcm(tone, self.dtmf_phases)
        elif self.pcm_q:
            pcm = self.pcm_q.pop(0)
        else:
            pcm = b"\x00" * (FRAME_SAMPLES * 2)
        frame = self.amr.encode(pcm)
        payload = b"\xf0" + frame if c["amr_oa"] else amr_payload_be(frame)
        seq = c["seq"]
        ts = c["ts"]
        marker = 0x80 if c["first"] else 0
        c["first"] = False
        rtp = bytes([0x80, marker | (c["amr_pt"] & 0x7f)]) + struct.pack("!HII", seq & 0xffff, ts & 0xffffffff, c["ssrc"]) + payload
        pkt = V.build_ipv4_udp(self.ike.cp["ip4"][0], c["remote_ip"], RTP_PORT,
                               c["remote_port"], rtp)
        self.ike.send_outer(pkt)
        c["seq"] = (seq + 1) & 0xffff
        c["ts"] = (ts + 160) & 0xffffffff
        c["rtp_count"] += 1
        c["octet_count"] += len(payload)
        c["sent"] += 1
        if now >= self.next_rtcp:
            rtcp = build_rtcp_sr(c["ssrc"], c["ts"], c["rtp_count"], c["octet_count"])
            pkt = V.build_ipv4_udp(self.ike.cp["ip4"][0], c["remote_ip"], RTCP_PORT,
                                   c["remote_rtcp"], rtcp)
            self.ike.send_outer(pkt)
            self.next_rtcp = now + 5
            log("RTCP SR sent", c["rtp_count"])

    def on_rtp(self, payload):
        c = self.call
        if c["state"] != "active":
            return
        p = parse_rtp(payload)
        if not p:
            return
        pt, seq, ts, ssrc, data = p
        c["rx"] += 1
        if c["last_seq"] is not None and 0 < ((seq - c["last_seq"] - 1) & 0xffff) < 1000:
            c["lost"] += (seq - c["last_seq"] - 1) & 0xffff
        c["last_seq"] = seq
        if pt == c["amr_pt"] and self.conn is self.owner:
            for fr in (amr_frames(data) if c["amr_oa"] else amr_frames_be(data)):
                ft = (fr[0] >> 3) & 0x0F
                c["ft"][ft] = c["ft"].get(ft, 0) + 1
                pcm = self.amr.decode(fr)
                c["peak"] = max(c["peak"], max(map(abs, array.array("h", pcm)), default=0))
                c["pcm_out"] += 1
                self.send_pcm(pcm)

    def on_rtcp(self, payload):
        c = self.call
        for pt, pkt in parse_rtcp(payload):
            if pt not in (200, 201):
                continue
            rc = pkt[0] & 0x1f
            off = 8 if pt == 201 else 28
            for _ in range(rc):
                if off + 24 > len(pkt):
                    break
                ssrc_n, frac = struct.unpack("!IB", pkt[off:off + 5])
                cum = int.from_bytes(pkt[off + 5:off + 8], "big", signed=True)
                ext = struct.unpack("!I", pkt[off + 8:off + 12])[0]
                if ssrc_n == c["ssrc"]:
                    self.rr_blocks.append({"fraction": frac, "cum": cum, "ext": ext})
                    log("RTCP RR about us: fraction", frac, "cum", cum, "ext", ext)
                off += 24

    def dtmf(self, digits):
        if self.call["state"] != "active" or self.conn is not self.owner:
            self.error("no active call")
            return
        c = self.call
        now = time.time()
        if c.get("te"):
            for d in digits:
                if d not in DTMF_FREQS:
                    continue
                code = int(d) if d.isdigit() else (10 if d == "*" else 11)
                self.dtmf_events.append({"code": code, "digit": d, "start": now,
                                         "end": now + 0.16, "next": now, "dur": 0,
                                         "sent": 0})
                now += 0.32
            log("RFC4733 DTMF queued", digits)
        else:
            for d in digits:
                if d not in DTMF_FREQS:
                    continue
                self.dtmf_q.append({"digit": d, "end": now + 0.16})
                now += 0.32
            log("in-band DTMF queued", digits)

    def stop(self):
        if getattr(self, "_stopped", False):
            return
        self._stopped = True
        log("stopping")
        self.stop_flag = True
        if self.call["state"] != "idle":
            self.hangup("daemon stop")
        alive = bool(self.ike) and self.probe_sa()
        if alive and self.client:
            self.deregister()
        if alive:
            try:
                self.ike.send_ike_delete()
            except Exception as e:
                log("delete failed:", e)
        self.running = False


def main():
    import signal
    d = Daemon()

    def _stop(*_):
        d.stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        d.start()
    finally:
        try:
            d.stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()
