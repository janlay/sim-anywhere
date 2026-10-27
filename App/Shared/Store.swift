import SwiftUI

struct Traffic: Sendable {
    var epoch: Int
    var rx: Int64
    var tx: Int64
}

struct RatePoint: Identifiable, Sendable {
    var epoch: Int
    var rx: Double
    var tx: Double

    var id: Int { epoch }
    var date: Date { Date(timeIntervalSince1970: TimeInterval(epoch)) }
}

/// A node address. The app stores it as an endpoint string: "scheme://host:port".
struct NodeAddress: Equatable, Sendable {
    var host: String
    var port: Int
    var scheme: String

    init(host: String, port: Int, scheme: String) {
        self.host = host
        self.port = port
        self.scheme = scheme
    }

    /// Parses a stored endpoint. A value without a scheme or port (saved by older versions) gets http and port 80.
    init(endpoint: String) {
        var rest = endpoint
        scheme = "http"
        port = 80
        if let r = rest.range(of: "://") {
            scheme = String(rest[..<r.lowerBound])
            rest = String(rest[r.upperBound...])
        }
        if let c = rest.lastIndex(of: ":"), let p = Int(rest[rest.index(after: c)...]) {
            port = p
            rest = String(rest[..<c])
        }
        host = rest
    }

    /// Empty when there is no host.
    var endpoint: String { host.isEmpty ? "" : "\(scheme)://\(host):\(port)" }
}

@MainActor @Observable
final class Store {
    static let defaultHost = ""
    static let rateWindow = 120   // seconds

    var host: String = UserDefaults.standard.string(forKey: "host") ?? Store.defaultHost {
        didSet {
            UserDefaults.standard.set(host, forKey: "host")
            status = nil; traffic = nil; rateHistory = []; server = nil; serverError = nil
            isConnecting = false; statusError = nil; smsError = nil; callsError = nil
            history = []; quota = nil; messages = []; unread = 0; calls = []
            cards = []; currentCard = nil; selectedCard = nil
            proxyGroups = nil; proxyError = nil; exitDelay = nil
            helloed = nil
        }
    }

    var status: DongleStatus?     // AT-based fields, updated once a minute
    var traffic: Traffic?         // usb0 bytes, updated every second
    var rateHistory: [RatePoint] = []    // bytes per second, one point per second (the last 2 minutes)
    var server: ServerInfo?              // from the API root, read after the node is set
    var serverError: String?
    var isConnecting = false
    var statusError: String?
    var history: [HistoryPoint] = []
    var historyWindow = 3600
    var quota: DataQuota?         // data plan usage, updated once a minute with the history

    var messages: [SMSMessage] = []
    var unread = 0
    var smsError: String?

    var calls: [CallRecord] = []
    var callsError: String?

    var cards: [SIMCard] = []
    var currentCard: String?
    var selectedCard: String?

    private(set) var simAliases: [String: String] = UserDefaults.standard.dictionary(forKey: "simAliases") as? [String: String] ?? [:] {
        didSet { UserDefaults.standard.set(simAliases, forKey: "simAliases") }
    }
    private(set) var phoneDisplaySIMs = Set(UserDefaults.standard.stringArray(forKey: "phoneDisplaySIMs") ?? []) {
        didSet { UserDefaults.standard.set(phoneDisplaySIMs.sorted(), forKey: "phoneDisplaySIMs") }
    }

    func displayedSIMAlias(for iccid: String?) -> String? {
        guard let iccid, !phoneDisplaySIMs.contains(iccid) else { return nil }
        return simAliases[iccid]
    }

    func setSIMAlias(_ alias: String, for iccid: String) {
        let name = alias.trimmingCharacters(in: .whitespacesAndNewlines)
        simAliases[iccid] = name.isEmpty ? nil : name
        phoneDisplaySIMs.remove(iccid)
    }

    func showSIMAlias(_ show: Bool, for iccid: String) {
        if show { phoneDisplaySIMs.remove(iccid) }
        else { phoneDisplaySIMs.insert(iccid) }
    }

    var proxyGroups: ProxyGroups?
    var proxyError: String?
    var exitDelay: Int?          // latest delay of the 4G exit proxy; nil when unknown or unavailable

    var api: DongleAPI? { DongleAPI(host: host, card: selectedCard) }

    func refreshCards() async {
        let requestedHost = host
        guard let result = try? await DongleAPI(host: requestedHost)?.cards(), host == requestedHost else { return }
        if currentCard != result.current {
            currentCard = result.current
            selectedCard = result.current
            messages = []; unread = 0; calls = []
        }
        cards = result.cards
    }

    func selectCard(_ id: String) async {
        guard cards.contains(where: { $0.id == id }), selectedCard != id else { return }
        selectedCard = id
        messages = []; unread = 0; calls = []
        await refreshSMS()
        await refreshCalls()
    }

    /// The host this app connection already reported to, so the notice goes out once per connection
    /// and not on every reconnect of the status stream.
    private var helloed: String?

    private func require() throws -> DongleAPI {
        guard let api else { throw APIError(message: "节点地址无效") }
        return api
    }

    /// Tells the node an app is connected, so it can stop broadcasting (`stop_on_connect` on the node).
    private func reportPresence() async {
        let requestedHost = host
        guard !requestedHost.isEmpty, helloed != requestedHost,
              let api = DongleAPI(host: requestedHost) else { return }
        do {
            try await api.helloDiscovery()
            helloed = requestedHost
        } catch is APIError {
            // The node answered but has no such endpoint (older build): do not ask again.
            helloed = requestedHost
        } catch {
            // Unreachable right now; the next status record or refresh tries again.
        }
    }

    /// Reads the status stream while the status tab is visible. Reconnects when the stream ends or fails.
    func watchStatus() async {
        guard !host.isEmpty else { statusError = nil; return }
        while !Task.isCancelled {
            do {
                await reportPresence()
                for try await s in try require().statusStream() {
                    if s.sample_epoch != nil { status = s }
                    addTraffic(s)
                    statusError = nil
                }
            } catch {
                // A cancelled watch (tab hidden, app in background) is not a node error.
                if Task.isCancelled { break }
                statusError = error.localizedDescription
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }

    private func addTraffic(_ s: DongleStatus) {
        guard let e = s.epoch, let rx = s.rx, let tx = s.tx else { return }
        let now = Traffic(epoch: e, rx: rx, tx: tx)
        // Expire by time, including after a pause or a node clock reset.
        rateHistory.removeAll { $0.epoch <= e - Store.rateWindow || $0.epoch > e }
        if let p = traffic, e > p.epoch {
            let dt = Double(e - p.epoch)
            // Do not plot an average across a gap longer than the chart window.
            if e - p.epoch <= Store.rateWindow {
                rateHistory.append(RatePoint(epoch: e, rx: Double(max(0, rx - p.rx)) / dt, tx: Double(max(0, tx - p.tx)) / dt))
            }
        }
        traffic = now
    }

    func refreshServer() async {
        let requestedHost = host
        guard !requestedHost.isEmpty else { server = nil; serverError = nil; isConnecting = false; return }
        isConnecting = true
        defer { if host == requestedHost { isConnecting = false } }
        do {
            guard let api = DongleAPI(host: requestedHost) else { throw APIError(message: "节点地址无效") }
            let info = try await api.info()
            guard host == requestedHost else { return }
            server = info
            serverError = nil
            await reportPresence()
        } catch {
            guard host == requestedHost else { return }
            server = nil
            serverError = error.localizedDescription
        }
    }

    func refreshHistory() async {
        guard !host.isEmpty else { history = []; return }
        history = (try? await require().history(window: historyWindow)) ?? history
    }

    func refreshQuota() async {
        guard !host.isEmpty else { quota = nil; return }
        quota = (try? await require().quota()) ?? quota
    }

    func refreshSMS(sync: Bool = false) async {
        guard !host.isEmpty else { messages = []; unread = 0; smsError = nil; return }
        await refreshCards()
        let requestedHost = host, requestedCard = selectedCard
        do {
            let api = try require()
            if sync { try await api.sync() }
            let list = try await api.sms()
            guard host == requestedHost && selectedCard == requestedCard else { return }
            messages = list.messages
            unread = list.unread
            smsError = nil
            if selectedCard == currentCard { await SMSWatch.check(list, host: host) }
        } catch {
            guard host == requestedHost && selectedCard == requestedCard else { return }
            messages = []; unread = 0
            smsError = error.localizedDescription
        }
    }

    func refreshCalls() async {
        guard !host.isEmpty else { calls = []; callsError = nil; return }
        await refreshCards()
        let requestedHost = host, requestedCard = selectedCard
        do {
            let list = try await require().calls()
            guard host == requestedHost && selectedCard == requestedCard else { return }
            calls = list
            callsError = nil
        } catch {
            guard host == requestedHost && selectedCard == requestedCard else { return }
            calls = []
            callsError = error.localizedDescription
        }
    }

    /// Deletes call records, then reloads the list. Returns the error text, if any.
    @discardableResult
    func deleteCalls(_ ids: [String]) async -> String? {
        do {
            try await require().deleteCalls(ids)
            await refreshCalls()
            return nil
        } catch {
            callsError = error.localizedDescription
            return callsError
        }
    }

    /// Runs a write request, then reloads the SMS list. Returns the error text, if any.
    @discardableResult
    func smsAction(_ op: (DongleAPI) async throws -> Void) async -> String? {
        do {
            try await op(try require())
            await refreshSMS()
            return nil
        } catch {
            smsError = error.localizedDescription
            return smsError
        }
    }

    /// Reloads the proxy groups. `quiet` is the 5 s poll of the proxy tab: a failed poll keeps
    /// the list that is already on screen instead of replacing it with the error, because the
    /// next poll is 5 s away.
    func refreshProxy(quiet: Bool = false) async {
        guard !host.isEmpty else { proxyGroups = nil; proxyError = nil; exitDelay = nil; return }
        do {
            let p = try await require().proxyGroups()
            proxyGroups = p
            exitDelay = delay(of: p.exit, in: p)
            proxyError = nil
        } catch {
            if quiet, proxyGroups != nil { return }
            proxyGroups = nil
            exitDelay = nil
            proxyError = error.localizedDescription
        }
    }

    /// Switches a proxy group to a member proxy, then reloads the groups. Returns the error text, if any.
    @discardableResult
    func selectProxy(group: String, proxy: String) async -> String? {
        do {
            try await require().selectProxy(group: group, proxy: proxy)
            await refreshProxy()
            return proxyError
        } catch {
            proxyError = error.localizedDescription
            return proxyError
        }
    }

    /// Delay-tests the 4G exit proxy and puts the new delay into the loaded groups.
    /// Returns the error text, if any; a failed test makes the exit dot red.
    @discardableResult
    func pingExit() async -> String? {
        do {
            let d = try await require().pingProxy()
            exitDelay = d.delay
            setDelay(d.delay, for: d.name)
            return nil
        } catch {
            exitDelay = nil
            if let name = proxyGroups?.exit { setDelay(nil, for: name) }
            return error.localizedDescription
        }
    }

    /// Delay-tests every member of every group on the proxy page, or only `group`, then reloads
    /// the groups. Returns the error text, if any, or a warning when the test failed.
    @discardableResult
    func refreshProxyDelays(_ group: String? = nil) async -> String? {
        do {
            let result = try await require().testProxyGroups(group)
            await refreshProxy()
            if let error = proxyError { return error }
            if let failed = result.failedGroups, failed > 0 {
                return group.map { "组 \($0) 测速失败" } ?? "\(failed) 个组测速失败"
            }
            return nil
        } catch {
            return error.localizedDescription
        }
    }

    private func delay(of name: String, in groups: ProxyGroups) -> Int? {
        for g in groups.groups {
            if let m = g.proxies.first(where: { $0.name == name }) { return m.delay }
        }
        return nil
    }

    private func setDelay(_ delay: Int?, for name: String) {
        guard var p = proxyGroups else { return }
        var changed = false
        for gi in p.groups.indices {
            for mi in p.groups[gi].proxies.indices where p.groups[gi].proxies[mi].name == name {
                p.groups[gi].proxies[mi].delay = delay
                changed = true
            }
        }
        if changed { proxyGroups = p }
    }
}

// MARK: - Display rules (same as the node monitor page)

enum Level: Int {
    case poor = 1, fair, good, excellent
    var label: String { ["差", "中", "良", "优"][rawValue - 1] }
    var color: Color { self >= .good ? .green : self == .fair ? .orange : .red }

    private static let limits: [String: [Int]] = ["rsrp": [-80, -90, -100], "sinr": [20, 13, 0], "rsrq": [-10, -15, -20]]

    init?(_ key: String, _ value: Int?) {
        guard let value, let g = Level.limits[key] else { return nil }
        self = value >= g[0] ? .excellent : value >= g[1] ? .good : value >= g[2] ? .fair : .poor
    }

    /// Combined grade: the lower of RSRP and SINR.
    static func signal(_ s: DongleStatus) -> Level? {
        switch (Level("rsrp", s.rsrp), Level("sinr", s.sinr)) {
        case let (a?, b?): return min(a, b)
        case let (a, b): return a ?? b
        }
    }
}

extension Level: Comparable {
    static func < (a: Level, b: Level) -> Bool { a.rawValue < b.rawValue }
}

enum Health { case good, warn, crit }

extension DongleStatus {
    func verdict(alias: String? = nil) -> (Health, String, String) {
        if present != 1 { return (.crit, "未检测到 Dongle", "USB 2ca3:4006 不在总线上") }
        if sim == "no_sim" { return (.crit, "未插 SIM 卡", "模组在线，但读不到 SIM") }
        if sim == "locked" { return (.crit, "SIM 已锁定", "需要输入 PIN / PUK") }
        if netifd_up != 1 { return (.crit, "链路中断", "usb0 接口未建立 (netifd down)") }
        if at_port == nil { return (.warn, "在线 · 无模组数据", "串口未就绪，无法读取 AT 信息") }
        if reg != 1 && reg != 5 { return (.warn, "网络注册异常", "CEREG 状态 \(reg.map(String.init) ?? "—")") }
        let sub = [alias ?? phone_masked, Fmt.oper(oper), network_mode]
        return (.good, "在线", sub.compactMap { $0 }.joined(separator: " · "))
    }
}

enum Fmt {
    static func oper(_ o: String?) -> String? {
        guard let o else { return nil }
        let map = ["CHINA_MOBILE": "中国移动", "CHN_CT": "中国电信", "CHINA_TELECOM": "中国电信", "CHN_UNICOM": "中国联通", "CHINA_UNICOM": "中国联通"]
        return map[o] ?? o.replacingOccurrences(of: "_", with: " ")
    }

    static func rrc(_ r: String?) -> String? {
        guard let r else { return nil }
        return ["NOCONN": "空闲", "CONNECT": "已连接", "SEARCH": "搜网中", "LIMSRV": "受限服务"][r] ?? r
    }

    static func sim(_ s: String?) -> String? {
        guard let s else { return nil }
        return ["ready": "已就绪", "no_sim": "未插卡", "locked": "PIN 锁定", "unknown": "未知"][s] ?? s
    }

    static func reg(_ r: Int?) -> String? {
        guard let r else { return nil }
        return r == 1 ? "本网" : r == 5 ? "漫游（正常）" : "\(r)"
    }

    static func bytes(_ b: Int64?) -> String {
        guard let b else { return "—" }
        return ByteCountFormatter.string(fromByteCount: b, countStyle: .binary)
    }

    /// Byte count with one decimal in the largest fitting unit, e.g. "15.1 GB"; bytes stay whole.
    static func usage(_ b: Int64?) -> String {
        guard let b else { return "—" }
        let units = ["B", "KB", "MB", "GB", "TB"]
        var v = Double(max(0, b)), i = 0
        while v >= 1024, i < units.count - 1 {
            v /= 1024
            i += 1
        }
        return String(format: i == 0 ? "%.0f %@" : "%.1f %@", v, units[i])
    }

    /// Transfer rate in B/s, KB/s, MB/s or GB/s. KB/s and above get one decimal; B/s is whole.
    /// The units are not localized.
    static func rate(_ bytesPerSecond: Double, decimals: Int = 1) -> String {
        let units = ["B/s", "KB/s", "MB/s", "GB/s"]
        var v = max(0, bytesPerSecond), i = 0
        while v >= 1024, i < units.count - 1 {
            v /= 1024
            i += 1
        }
        return "\(String(format: "%.\(i == 0 ? 0 : decimals)f", v)) \(units[i])"
    }

    static func duration(_ s: Int64?) -> String {
        guard let s else { return "—" }
        let d = s / 86400, h = s % 86400 / 3600, m = s % 3600 / 60
        return d > 0 ? "\(d) 天 \(h) 小时" : h > 0 ? "\(h) 小时 \(m) 分" : "\(m) 分钟"
    }

    /// Call length as m:ss or h:mm:ss.
    static func clock(_ s: Int) -> String {
        s >= 3600 ? String(format: "%d:%02d:%02d", s / 3600, s % 3600 / 60, s % 60) : String(format: "%d:%02d", s / 60, s % 60)
    }

    static func temp(_ t: Int?) -> (String, Color)? {
        guard let t else { return nil }
        return t < 60 ? ("正常", .green) : t < 75 ? ("偏高", .orange) : ("过热", .red)
    }
}
