#!/usr/bin/env python3
"""Test client for /var/run/dongle-vowifid.sock."""

import argparse
import json
import math
import os
import select
import socket
import struct
import sys
import time
import wave

SOCK = "/var/run/dongle-vowifid.sock"
FRAME = 320


def frame(typ, payload):
    return bytes([typ]) + struct.pack("!H", len(payload)) + payload


def sine(freq, phase, n=160, amp=6000):
    out = bytearray()
    for i in range(n):
        v = int(amp * math.sin(phase + 2 * math.pi * freq * i / 8000.0))
        out += struct.pack("<h", v)
    return bytes(out), (phase + 2 * math.pi * freq * n / 8000.0) % (2 * math.pi)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", default=SOCK)
    ap.add_argument("--dial", default=None)
    ap.add_argument("--answer-after", type=float, default=None)
    ap.add_argument("--seconds", type=float, default=25)
    ap.add_argument("--out", default="/tmp/vowifi-test.wav")
    ap.add_argument("--tone", type=float, default=440)
    ap.add_argument("--dtmf", default=None)
    args = ap.parse_args()

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(args.socket)
    s.setblocking(False)
    buf = b""
    wf = None
    state = None
    dialed = False
    answered = False
    active_at = None
    next_pcm = 0
    phase = 0.0
    hangup_sent = False
    dtmf_sent = False
    dtmf_at = None
    answer_at = None
    deadline = time.time() + 300
    print("connected", args.socket, flush=True)
    while time.time() < deadline:
        r, _, _ = select.select([s], [], [], 0.02)
        if r:
            try:
                data = s.recv(65536)
            except BlockingIOError:
                data = b""
            if not data:
                break
            buf += data
            while len(buf) >= 3:
                typ = buf[0]
                ln = struct.unpack("!H", buf[1:3])[0]
                if len(buf) < 3 + ln:
                    break
                payload = buf[3:3 + ln]
                buf = buf[3 + ln:]
                if typ == 0:
                    try:
                        msg = json.loads(payload.decode())
                    except Exception:
                        continue
                    if msg.get("type") == "state":
                        state = msg
                        print("state", msg.get("state"), "wifi", msg.get("wifi"),
                              "ready", msg.get("ready"), "owner", msg.get("owner"),
                              "number", msg.get("number"), flush=True)
                        if args.dial and not dialed and msg.get("state") == "idle" and msg.get("ready"):
                            s.sendall(frame(0, json.dumps({"op": "dial", "number": args.dial}).encode()))
                            dialed = True
                        if args.answer_after is not None and not answered and answer_at is None and msg.get("state") == "incoming":
                            answer_at = time.time() + args.answer_after
                        if msg.get("state") == "active" and active_at is None:
                            active_at = time.time()
                            next_pcm = time.time()
                            wf = wave.open(args.out, "wb")
                            wf.setnchannels(1)
                            wf.setsampwidth(2)
                            wf.setframerate(8000)
                            print("active; saving downlink to", args.out, flush=True)
                        if msg.get("state") == "idle" and active_at is not None:
                            break
                    elif msg.get("type") == "error":
                        print("error", msg.get("error"), flush=True)
                elif typ == 1:
                    if wf:
                        wf.writeframes(payload)
        now = time.time()
        if answer_at and now >= answer_at and not answered:
            s.sendall(frame(0, json.dumps({"op": "answer"}).encode()))
            answered = True
            answer_at = None
            print("answer sent", flush=True)
        if active_at and now >= next_pcm:
            pcm, phase = sine(args.tone, phase)
            s.sendall(frame(1, pcm))
            next_pcm += 0.02
            if next_pcm < now - 0.2:
                next_pcm = now + 0.02
        if active_at and args.dtmf and not dtmf_sent and now - active_at >= 5:
            s.sendall(frame(0, json.dumps({"op": "dtmf", "digits": args.dtmf}).encode()))
            dtmf_sent = True
            print("dtmf sent", args.dtmf, flush=True)
        if active_at and not hangup_sent and now - active_at >= args.seconds:
            s.sendall(frame(0, json.dumps({"op": "hangup"}).encode()))
            hangup_sent = True
            print("hangup sent", flush=True)
    if wf:
        wf.close()
        print("wav", args.out, os.path.getsize(args.out), "bytes", flush=True)
    s.close()


if __name__ == "__main__":
    import threading
    main()
