"""Best-effort 3GPP ePDG discovery over DNS (stdlib only)."""

import ipaddress
import random
import re
import socket
import struct

DNS_SERVERS = ("1.1.1.1", "8.8.8.8")


def _name_wire(name):
    return b"".join(bytes((len(part),)) + part.encode("ascii")
                    for part in name.rstrip(".").split(".")) + b"\0"


def _read_name(packet, pos):
    labels, end, jumped, seen = [], None, False, set()
    while True:
        if pos >= len(packet):
            raise ValueError("truncated DNS name")
        n = packet[pos]
        if n == 0:
            pos += 1
            return ".".join(labels), (end if jumped else pos)
        if n & 0xc0 == 0xc0:
            if pos + 1 >= len(packet):
                raise ValueError("truncated DNS pointer")
            ptr = ((n & 0x3f) << 8) | packet[pos + 1]
            if ptr in seen:
                raise ValueError("DNS pointer loop")
            seen.add(ptr)
            if not jumped:
                end = pos + 2
                jumped = True
            pos = ptr
            continue
        if n & 0xc0 or pos + n + 1 > len(packet):
            raise ValueError("invalid DNS label")
        labels.append(packet[pos + 1:pos + n + 1].decode("ascii"))
        pos += n + 1


def _query(server, name, qtype, source_ip, mark, timeout=2):
    ident = random.randrange(65536)
    packet = struct.pack("!HHHHHH", ident, 0x0100, 1, 0, 0, 0)
    packet += _name_wire(name) + struct.pack("!HH", qtype, 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        if hasattr(socket, "SO_MARK"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, mark)
        if source_ip:
            sock.bind((source_ip, 0))
        sock.sendto(packet, (server, 53))
        data, _ = sock.recvfrom(4096)
    finally:
        sock.close()
    if len(data) < 12:
        raise ValueError("short DNS response")
    rid, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", data[:12])
    if rid != ident or flags & 0x8000 == 0 or flags & 0x000f:
        return []
    pos = 12
    for _ in range(qd):
        _, pos = _read_name(data, pos)
        pos += 4
    out = []
    for _ in range(an + ns + ar):
        owner, pos = _read_name(data, pos)
        if pos + 10 > len(data):
            raise ValueError("truncated DNS record")
        typ, cls, ttl, size = struct.unpack("!HHIH", data[pos:pos + 10])
        pos += 10
        end = pos + size
        if end > len(data):
            raise ValueError("truncated DNS data")
        if cls == 1 and typ == qtype:
            if typ == 35 and size >= 7:  # NAPTR
                order, pref = struct.unpack("!HH", data[pos:pos + 4])
                p = pos + 4
                strings = []
                for _ in range(3):
                    ln = data[p]
                    p += 1
                    strings.append(data[p:p + ln].decode("ascii"))
                    p += ln
                replacement, _ = _read_name(data, p)
                out.append((order, pref, strings[0], strings[1], strings[2], replacement))
            elif typ == 1 and size == 4:
                out.append(socket.inet_ntoa(data[pos:end]))
            elif typ == 28 and size == 16:
                out.append(socket.inet_ntop(socket.AF_INET6, data[pos:end]))
            elif typ == 33 and size >= 7:  # SRV
                priority, weight, port = struct.unpack("!HHH", data[pos:pos + 6])
                target, _ = _read_name(data, pos + 6)
                out.append((priority, weight, port, target))
        pos = end
    return out


def _resolve_target(server, target, flags, source_ip, mark):
    if flags.upper() == "S":
        targets = [row[3] for row in _query(server, target, 33, source_ip, mark)]
    else:
        targets = [target]
    addresses = []
    for host in targets:
        try:
            for addr in _query(server, host, 1, source_ip, mark):
                if ipaddress.IPv4Address(addr).is_global and addr not in addresses:
                    addresses.append(addr)
        except (OSError, ValueError):
            continue
    return addresses


def discover_epdg(mcc, mnc, source_ip=None, mark=0x401, visited_mcc=None):
    """Return unique reachable ePDG IP candidates in DNS preference order."""
    home_fqdn = "epdg.epc.mnc%s.mcc%s.pub.3gppnetwork.org" % (mnc.zfill(3), mcc)
    result = []
    visited_name = ("epdg.epc.mcc%s.visited-country.pub.3gppnetwork.org" % visited_mcc
                    if visited_mcc and visited_mcc != mcc else None)
    for server in DNS_SERVERS:
        try:
            if visited_name:
                visited = _query(server, visited_name, 35, source_ip, mark)
                targets = [row[5] for row in sorted(visited)
                           if re.fullmatch(r"epdg\.epc\.mnc\d{3}\.mcc\d{3}\.pub\.3gppnetwork\.org\.?", row[5], re.I)]
                for target in targets:
                    result.extend(_resolve_target(server, target, "A", source_ip, mark))
            naptr = _query(server, home_fqdn, 35, source_ip, mark)
            targets = [(row[5], row[2]) for row in sorted(naptr)
                       if "epdg" in row[3].lower() and row[2].upper() in ("A", "S")]
            if not targets:
                targets = [(home_fqdn, "A")]
            for target, flags in targets:
                result.extend(_resolve_target(server, target, flags, source_ip, mark))
            result = list(dict.fromkeys(result))
            if result:
                break
        except (OSError, ValueError, IndexError):
            continue
    return result
