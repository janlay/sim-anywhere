import Foundation

// Models and client for the SIM Anywhere API (spec: /sim-anywhere/openapi.json on the node).

struct DongleStatus: Decodable, Sendable {
    var ts: String?
    var epoch: Int?
    var present: Int?
    var usb_port, at_port, model, imei, iccid, imsi, sim: String?
    var rssi, rsrp, rsrq, sinr, reg, temp: Int?
    var oper, network_mode, band, rrc, cellid, pci, earfcn, apn: String?
    var phone_masked: String?    // already masked by the node; no plaintext own number in the HTTP API
    var wan_ip, wan_ip6, dns, usb0_ip, host_ip: String?
    var usb0_carrier: Int?
    var usb0_dev: String?
    var sample_epoch: Int?
    var rx, tx, link_uptime: Int64?
    var disconnects, netifd_up: Int?
}

/// Data plan state from GET /quota. Byte counts; a limit of 0 means no limit.
struct DataQuota: Decodable, Sendable {
    var cycle_on: Bool           // the cycle limit is switched on; plan blocks only then
    var plan: Int64
    var ratio: Int?              // cycle limit as a percentage of plan, absent on older nodes
    var reset_day: Int
    var cycle_mode: String?    // month, date or days; absent on older nodes
    var daily_on: Bool           // the daily limit is switched on; daily blocks only then
    var daily: Int64
    var cycle: String
    var next_reset: String?     // start date of the next cycle, absent on older nodes
    var used: Int64
    var day: String
    var day_used: Int64
    var block: String            // "", "limit" or "daily"
    var attached: Bool?
}

struct ServerInfo: Decodable, Sendable {
    var name: String
    var spec: String
    var endpoints: [String]
}

struct SMSMessage: Decodable, Identifiable, Hashable, Sendable {
    var id: String
    var box: String
    var number: String
    var time: String
    var epoch: Int
    var read: Bool
    var text: String
    var parts: Int
    var total: Int

    var date: Date { Date(timeIntervalSince1970: TimeInterval(epoch)) }
    var incoming: Bool { box == "in" }
}

struct SMSList: Decodable, Sendable {
    var unread: Int
    var messages: [SMSMessage]
}

struct SendResult: Decodable, Sendable {
    var id: String?
    var encoding: String
    var parts: Int
}

struct CallRecord: Decodable, Identifiable, Hashable, Sendable {
    var id: String
    var dir: String
    var number: String
    var epoch: Int
    var answered: Bool
    var duration: Int

    var date: Date { Date(timeIntervalSince1970: TimeInterval(epoch)) }
    var incoming: Bool { dir == "in" }
    var missed: Bool { incoming && !answered }
}

private struct CallList: Decodable { var calls: [CallRecord] }

struct SIMCard: Decodable, Identifiable, Sendable {
    var id: String
    var current: Bool
    var phone_masked: String?

    var title: String {
        phone_masked ?? "号码未知"
    }
}

struct CardList: Decodable { var current: String; var cards: [SIMCard] }

enum VoIPStatus: String, Decodable, Sendable {
    case active, ineffective, unconfigured
}

private struct VoIPStatusResponse: Decodable { var state: VoIPStatus }

struct ATResponse: Decodable, Sendable {
    var command: String
    var lines: [String]
    /// OK, ERROR, +CME ERROR: ..., +CMS ERROR: ... or TIMEOUT
    var result: String
}

/// One history row, keyed by field name. Only numeric fields are kept.
struct HistoryPoint: Identifiable, Sendable {
    var epoch: Int
    var values: [String: Double]
    var id: Int { epoch }
    var date: Date { Date(timeIntervalSince1970: TimeInterval(epoch)) }
}

private struct History: Decodable {
    enum Cell: Decodable {
        case number(Double), other
        init(from decoder: Decoder) throws {
            let c = try decoder.singleValueContainer()
            if let d = try? c.decode(Double.self) { self = .number(d) } else { self = .other }
        }
    }
    var fields: [String]
    var rows: [[Cell]]
}

/// One member of a clash proxy group. `delay` is the latest test result in ms (nil when untested).
struct ProxyMember: Decodable, Hashable, Sendable {
    var name: String
    var type: String
    var delay: Int?
    var selected: Bool
}

/// A clash proxy group that can exit through the 4G exit proxy.
struct ProxyGroup: Decodable, Identifiable, Hashable, Sendable {
    var name: String
    var type: String
    var now: String?
    var proxies: [ProxyMember]

    var id: String { name }
}

/// The integrated proxy core. `core` is the detected name: `mihomo` or `clash`.
struct ProxyServer: Decodable, Sendable {
    var core: String
    var version: String?
}

struct ProxyGroups: Decodable, Sendable {
    var exit: String
    var interface: String?
    var server: ProxyServer?
    var groups: [ProxyGroup]
}

/// Result of `POST /proxy/ping`: the tested proxy and its delay in ms.
struct ProxyDelay: Decodable, Sendable {
    var name: String
    var delay: Int
}

/// Result of `POST /proxy/test`, which tests every member of every listed group.
struct ProxyTestResult: Decodable, Sendable {
    var ok: Bool
    var groups: Int?
    var tested: Int?
    var failedGroups: Int?

    private enum CodingKeys: String, CodingKey {
        case ok, groups, tested
        case failedGroups = "failed_groups"
    }
}

struct APIError: LocalizedError {
    var message: String
    var errorDescription: String? { message }
}

struct DongleAPI: Sendable {
    var base: URL
    var card: String?

    init?(host: String, card: String? = nil) {
        let h = host.trimmingCharacters(in: .whitespaces)
        guard !h.isEmpty, let url = URL(string: (h.contains("://") ? h : "http://" + h) + "/cgi-bin/sim-anywhere-api") else { return nil }
        base = url
        self.card = card
    }

    /// The API root: the node's name, the spec path and the endpoint names. Used to confirm the API is reachable.
    func info() async throws -> ServerInfo { try await call("GET", "") }

    /// One record per second (usb0 bytes). A record with `sample_epoch` also carries the AT-based fields, once a minute.
    /// The node ends the stream after about an hour. The caller reconnects.
    func statusStream() -> AsyncThrowingStream<DongleStatus, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let req = URLRequest(url: base.appendingPathComponent("status"), timeoutInterval: 30)
                    let (bytes, resp) = try await URLSession.shared.bytes(for: req)
                    let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
                    guard (200..<300).contains(code) else { throw APIError(message: "HTTP \(code)") }
                    for try await line in bytes.lines where !line.isEmpty {
                        continuation.yield(try JSONDecoder().decode(DongleStatus.self, from: Data(line.utf8)))
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    func history(window: Int) async throws -> [HistoryPoint] {
        let h: History = try await call("GET", "history", query: ["window": "\(window)"])
        guard let ei = h.fields.firstIndex(of: "epoch") else { return [] }
        return h.rows.compactMap { row in
            guard ei < row.count, case let .number(e) = row[ei] else { return nil }
            var v: [String: Double] = [:]
            for (i, f) in h.fields.enumerated() where i < row.count {
                if case let .number(n) = row[i] { v[f] = n }
            }
            return HistoryPoint(epoch: Int(e), values: v)
        }
    }

    func cards() async throws -> CardList { try await call("GET", "cards") }

    func sms() async throws -> SMSList { try await call("GET", "sms", query: card.map { ["card": $0] } ?? [:]) }

    func send(to: String, text: String, dryRun: Bool) async throws -> SendResult {
        try await call("POST", "sms", body: ["to": .s(to), "text": .s(text), "dry_run": .b(dryRun)])
    }

    func mark(_ ids: [String], read: Bool) async throws {
        let _: Empty = try await call("PATCH", "sms", query: card.map { ["card": $0] } ?? [:], body: ["ids": .a(ids), "read": .b(read)])
    }

    func delete(_ ids: [String]) async throws {
        var query = ["ids": ids.joined(separator: ",")]
        if let card { query["card"] = card }
        let _: Empty = try await call("DELETE", "sms", query: query)
    }

    func sync() async throws {
        let _: Empty = try await call("POST", "sms/sync", body: [:])
    }

    /// Sends one AT command to the modem. `timeout`: seconds to wait for each response line.
    func at(_ command: String, timeout: Int = 5) async throws -> ATResponse {
        try await call("POST", "at", body: ["command": .s(command), "timeout": .i(timeout)], wait: TimeInterval(timeout + 35))
    }

    func calls() async throws -> [CallRecord] {
        let l: CallList = try await call("GET", "calls", query: card.map { ["card": $0] } ?? [:])
        return l.calls
    }

    func deleteCalls(_ ids: [String]) async throws {
        var query = ["ids": ids.joined(separator: ",")]
        if let card { query["card"] = card }
        let _: Empty = try await call("DELETE", "calls", query: query)
    }

    func registerVoIPDevice(_ device: String, token: String, environment: String) async throws {
        let _: Empty = try await call("POST", "push/register",
                                      body: ["device": .s(device), "token": .s(token), "environment": .s(environment)])
    }

    func unregisterVoIPDevice(_ device: String) async throws {
        let _: Empty = try await call("DELETE", "push/register", body: ["device": .s(device)])
    }

    func voipStatus(device: String, bundleID: String, environment: String) async throws -> VoIPStatus {
        let response: VoIPStatusResponse = try await call("GET", "push/status", query: [
            "device": device, "bundle_id": bundleID, "environment": environment,
        ])
        return response.state
    }

    /// Data plan usage of this month and today, in bytes. 0 limits mean no limit.
    func quota() async throws -> DataQuota { try await call("GET", "quota") }

    /// The clash proxy groups that contain the node's auto-detected 4G exit proxy.
    /// 503 when clash is not integrated or no exit proxy exists on the node.
    func proxyGroups() async throws -> ProxyGroups { try await call("GET", "proxy/groups") }

    /// Switches a clash proxy group to the given member proxy and reloads the clash config.
    func selectProxy(group: String, proxy: String) async throws {
        let _: Empty = try await call("POST", "proxy/select", body: ["group": .s(group), "proxy": .s(proxy)], wait: 60)
    }

    /// Delay-tests the 4G exit proxy (default) or a named proxy through the integrated core.
    /// The test updates the core history; 502 when the test fails (for example a timeout).
    func pingProxy(_ name: String? = nil) async throws -> ProxyDelay {
        try await call("POST", "proxy/ping", body: name.map { ["proxy": .s($0)] } ?? [:], wait: 20)
    }

    /// Delay-tests every member of every group on the proxy page, or only `group`. The caller
    /// re-fetches the groups to show the new delays. Several groups are tested one after another.
    func testProxyGroups(_ group: String? = nil) async throws -> ProxyTestResult {
        try await call("POST", "proxy/test", body: group.map { ["group": .s($0)] } ?? [:], wait: 180)
    }

    /// Tells the node that an app is connected. With `dongle.discovery.stop_on_connect` the node
    /// stops its LAN broadcast until the switch on its settings page starts it again. A node from
    /// an older build answers 404; the caller ignores that.
    func helloDiscovery() async throws {
        let _: Empty = try await call("POST", "discovery/hello", body: [:])
    }

    /// The call WebSocket of sim-anywhere-calld on the same node.
    /// An https node is reached through its TLS reverse proxy on the API host/port: `wss://<host>/call`
    /// (the proxy forwards `/call` to sim-anywhere-calld on 8765). A plain-http node uses `ws://<host>:8765/call`.
    var callURL: URL? {
        var c = URLComponents(url: base, resolvingAgainstBaseURL: false)
        if c?.scheme?.lowercased() == "https" {
            c?.scheme = "wss"
            if c?.port == 443 { c?.port = nil }
        } else {
            c?.scheme = "ws"
            c?.port = 8765
        }
        c?.path = "/call"
        c?.query = nil
        c?.fragment = nil
        return c?.url
    }

    // MARK: - Transport

    private struct Empty: Decodable {}
    private struct ErrorBody: Decodable { var error: String }

    enum Value: Encodable {
        case s(String), b(Bool), i(Int), a([String])
        func encode(to encoder: Encoder) throws {
            var c = encoder.singleValueContainer()
            switch self {
            case let .s(v): try c.encode(v)
            case let .b(v): try c.encode(v)
            case let .i(v): try c.encode(v)
            case let .a(v): try c.encode(v)
            }
        }
    }

    private func call<T: Decodable>(_ method: String, _ path: String, query: [String: String] = [:], body: [String: Value]? = nil,
                                     wait: TimeInterval = 30) async throws -> T {
        var comp = URLComponents(url: base.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
        if !query.isEmpty { comp.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) } }
        var req = URLRequest(url: comp.url!, timeoutInterval: wait)
        req.httpMethod = method
        if method != "GET" {
            // The API requires this header on every write request, DELETE included.
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            if let body { req.httpBody = try JSONEncoder().encode(body) }
        }
        let (data, resp) = try await URLSession.shared.data(for: req)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(code) else {
            // 503 is the node saying "no dongle inserted": worth a plain sentence, not a status code.
            if code == 503 { throw APIError(message: "设备未插入，节点暂时不可用") }
            let msg = (try? JSONDecoder().decode(ErrorBody.self, from: data))?.error ?? "HTTP \(code)"
            throw APIError(message: msg)
        }
        return try JSONDecoder().decode(T.self, from: data)
    }
}
