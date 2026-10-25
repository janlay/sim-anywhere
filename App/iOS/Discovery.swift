import Darwin
import Foundation

/// A node that announced itself on the LAN (`dongle-announce` on the OpenWrt node).
struct DiscoveredNode: Identifiable, Hashable, Sendable {
    var name: String
    var host: String
    var port: Int
    var scheme: String
    var model: String?
    var lastSeen: Date

    var id: String { host }

    /// The endpoint in the form the settings page stores ("http://openwrt.local:80").
    var endpoint: String { NodeAddress(host: host, port: port, scheme: scheme).endpoint }

    /// Host and port as shown in the list. The port is left out when it is the default.
    var address: String {
        port == (scheme.lowercased() == "https" ? 443 : 80) ? host : "\(host):\(port)"
    }
}

/// Finds nodes on the same network while the home screen is open: it listens for the
/// broadcasts of `dongle-announce` and probes the local subnets, so a node is found even
/// when the system drops broadcast traffic. Nothing runs while the home screen is closed.
@MainActor @Observable
final class NodeDiscovery {
    /// Must match PORT in `dongle-announce` on the node.
    static let port: UInt16 = 8766

    private(set) var nodes: [DiscoveredNode] = []
    private var task: Task<Void, Never>?

    func start() {
        guard task == nil else { return }
        task = Task { [weak self] in
            for await found in Scanner.scan(port: NodeDiscovery.port) {
                guard let self else { return }
                self.nodes = found
            }
            // A cancelled scan was replaced or stopped: it must not clear the new state.
            guard let self, !Task.isCancelled else { return }
            self.nodes = []
            self.task = nil
        }
    }

    func stop() {
        task?.cancel()
        task = nil
        nodes = []
    }
}

// MARK: - UDP scan

/// One long-lived scan: a bound UDP socket, a probe every few seconds, and a snapshot of the
/// nodes seen in the last few seconds whenever that set changes.
private enum Scanner {
    private static let broadcastEvery: TimeInterval = 3
    private static let sweepEvery: TimeInterval = 10
    private static let expiry: TimeInterval = 15

    private struct Announcement: Decodable {
        var simanywhere: Int?
        var name: String?
        var host: String?
        var port: Int?
        var scheme: String?
        var model: String?
    }

    static func scan(port: UInt16) -> AsyncStream<[DiscoveredNode]> {
        AsyncStream { continuation in
            let task = Task.detached(priority: .userInitiated) {
                run(port: port) { continuation.yield($0) }
                continuation.finish()
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    private static func run(port: UInt16, emit: @escaping @Sendable ([DiscoveredNode]) -> Void) {
        let fd = socket(AF_INET, SOCK_DGRAM, 0)
        guard fd >= 0 else { return }
        defer { close(fd) }

        var on: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &on, socklen_t(MemoryLayout<Int32>.size))
        setsockopt(fd, SOL_SOCKET, SO_BROADCAST, &on, socklen_t(MemoryLayout<Int32>.size))
        var local = address(0, port: port)
        let bound = withUnsafePointer(to: &local) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0 else { return }

        var seen: [String: DiscoveredNode] = [:]
        var emitted: Set<String> = []
        var lastBroadcast = Date.distantPast
        var lastSweep = Date.distantPast
        while !Task.isCancelled {
            let now = Date()
            if now.timeIntervalSince(lastBroadcast) >= broadcastEvery {
                lastBroadcast = now
                broadcastProbe(fd, port: port)
            }
            // A sweep touches every address of the subnet, so it runs far less often. It is
            // the fallback for a system that drops the broadcast (iOS needs an entitlement
            // for broadcast traffic; unicast always works).
            if now.timeIntervalSince(lastSweep) >= sweepEvery {
                lastSweep = now
                sweep(fd, port: port)
            }
            if ready(fd), let node = receive(fd) {
                seen[node.id] = node
            }
            seen = seen.filter { now.timeIntervalSince($0.value.lastSeen) <= expiry }
            if Set(seen.keys) != emitted {
                emitted = Set(seen.keys)
                emit(seen.values.sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending })
            }
        }
    }

    /// Waits up to 250 ms for a datagram.
    private static func ready(_ fd: Int32) -> Bool {
        var p = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
        return poll(&p, 1, 250) > 0 && p.revents & Int16(POLLIN) != 0
    }

    private static func receive(_ fd: Int32) -> DiscoveredNode? {
        var buf = [UInt8](repeating: 0, count: 2048)
        var from = sockaddr_in()
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        let n = withUnsafeMutablePointer(to: &from) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                recvfrom(fd, &buf, buf.count, 0, $0, &len)
            }
        }
        guard n > 0, let a = try? JSONDecoder().decode(Announcement.self, from: Data(buf[0..<n])),
              a.simanywhere == 1, let name = a.name, let host = a.host, !host.isEmpty else { return nil }
        return DiscoveredNode(name: name, host: host, port: a.port ?? 80,
                              scheme: a.scheme ?? "http", model: a.model, lastSeen: Date())
    }

    /// Asks every node on the broadcast domain to answer now.
    private static func broadcastProbe(_ fd: Int32, port: UInt16) {
        let payload = Data(#"{"simanywhere":1,"probe":1}"#.utf8)
        var sent = Set<UInt32>()
        for (ip, mask) in localIPv4() {
            let m: UInt32 = mask.nonzeroBitCount >= 24 ? mask : 0xFFFF_FF00
            let target = (ip & m) | ~m
            if sent.insert(target).inserted { send(fd, address(target, port: port), payload) }
        }
        if sent.insert(0xFFFF_FFFF).inserted {
            send(fd, address(0xFFFF_FFFF, port: port), payload)
        }
    }

    /// Probes every host of the /24 around our address: a broadcast may be filtered by the
    /// system, unicast is not.
    private static func sweep(_ fd: Int32, port: UInt16) {
        let payload = Data(#"{"simanywhere":1,"probe":1}"#.utf8)
        for (ip, mask) in localIPv4() {
            let m: UInt32 = mask.nonzeroBitCount >= 24 ? mask : 0xFFFF_FF00
            let base = ip & m
            for h in 1..<255 where base | UInt32(h) != ip {
                send(fd, address(base | UInt32(h), port: port), payload)
            }
        }
    }

    /// IPv4 addresses of the active interfaces, in host byte order.
    private static func localIPv4() -> [(UInt32, UInt32)] {
        var head: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&head) == 0, let first = head else { return [] }
        defer { freeifaddrs(head) }
        var out: [(UInt32, UInt32)] = []
        var cur: UnsafeMutablePointer<ifaddrs>? = first
        while let p = cur {
            let flags = Int32(p.pointee.ifa_flags)
            if flags & IFF_UP != 0, flags & IFF_LOOPBACK == 0,
               let sa = p.pointee.ifa_addr, sa.pointee.sa_family == UInt8(AF_INET),
               let nm = p.pointee.ifa_netmask {
                let ip = sa.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { UInt32(bigEndian: $0.pointee.sin_addr.s_addr) }
                let mask = nm.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { UInt32(bigEndian: $0.pointee.sin_addr.s_addr) }
                if mask != 0 { out.append((ip, mask)) }
            }
            cur = p.pointee.ifa_next
        }
        return out
    }

    /// A socket address for an IPv4 address in host byte order.
    private static func address(_ ip: UInt32, port: UInt16) -> sockaddr_in {
        var a = sockaddr_in()
        a.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        a.sin_family = sa_family_t(AF_INET)
        a.sin_port = port.bigEndian
        a.sin_addr = in_addr(s_addr: ip.bigEndian)
        return a
    }

    private static func send(_ fd: Int32, _ to: sockaddr_in, _ data: Data) {
        var a = to
        data.withUnsafeBytes { buf in
            guard let base = buf.baseAddress else { return }
            _ = withUnsafePointer(to: &a) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    sendto(fd, base, buf.count, 0, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
                }
            }
        }
    }
}
