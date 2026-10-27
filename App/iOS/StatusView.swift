import Charts
import SwiftUI
import UIKit

struct StatusView: View {
    @Environment(Store.self) private var store
    @Environment(\.scenePhase) private var scenePhase
    var onConnect: () -> Void = {}
    @State private var discovery = NodeDiscovery()
    @State private var editingSIMAlias = false
    @State private var aliasCard: String?
    @State private var aliasDraft = ""

    var body: some View {
        NavigationStack {
            Group {
                if store.host.isEmpty {
                    connectView
                } else {
                    List {
                        if let e = store.statusError { ErrorLabel(e) }
                        if let s = store.status {
                            hero(s)
                            if let q = store.quota, q.cycle_on || q.daily_on { quotaSection(q) }
                            SpeedSection()
                            TrendSection()
                        } else if store.statusError == nil {
                            ProgressView().frame(maxWidth: .infinity)
                        }
                    }
                    .refreshable { await store.refreshHistory() }
                    // Reads the status stream while this tab is visible (one traffic record per second, AT fields once a minute).
                    // The stream stops in the background: iOS stalls the connection there, and it would time out
                    // after the app returns. It restarts at once when the app is back.
                    .task(id: [store.host, "\(scenePhase == .background)"]) {
                        guard scenePhase != .background else { return }
                        await store.watchStatus()
                    }
                    // History changes once a minute (the collector rate), so reload it on the same cadence.
                    .task(id: store.host) {
                        guard !store.host.isEmpty else { return }
                        while !Task.isCancelled {
                            await store.refreshHistory()
                            await store.refreshQuota()
                            try? await Task.sleep(for: .seconds(60))
                        }
                    }
                }
            }
            .navigationTitle("SIM Anywhere")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    if !store.host.isEmpty {
                        NavigationLink("更多") { MoreStatusPage() }
                    }
                }
            }
            .alert("SIM 卡别名", isPresented: $editingSIMAlias) {
                TextField("别名", text: $aliasDraft)
                Button("取消", role: .cancel) { }
                Button("保存") {
                    if let aliasCard { store.setSIMAlias(aliasDraft, for: aliasCard) }
                }
            } message: {
                Text("保存后优先显示别名，留空可清除别名。")
            }
        }
    }

    private var connectView: some View {
        GeometryReader { geo in
            ScrollView {
                VStack(spacing: 20) {
                    Image(systemName: "antenna.radiowaves.left.and.right")
                        .font(.system(size: 56, weight: .light))
                        .foregroundStyle(.tint)
                        .frame(width: 120, height: 120)
                        .padding(.top, 40)
                    Text(discovery.nodes.isEmpty ? "还没有连接节点" : "请选择节点")
                        .font(.title2.weight(.semibold))
                    foundNodes
                    Button("手动输入地址", action: onConnect)
                        .font(.body.weight(.medium))
                        .buttonStyle(.plain)
                        .foregroundStyle(.tint)
                        .padding(.vertical, 8)
                }
                .padding()
            }
            .background {
                DiscoveryHalo(center: CGPoint(x: geo.size.width / 2, y: 116),
                              maxRadius: sqrt(geo.size.width * geo.size.width
                                              + geo.size.height * geo.size.height))
            }
            // Scans until the screen goes away (a connection replaces this view).
            .task { discovery.start() }
            .onDisappear { discovery.stop() }
        }
    }

    /// The nodes that announced themselves on this network, one tap to connect.
    private var foundNodes: some View {
        VStack(alignment: .leading, spacing: 0) {
            if discovery.nodes.isEmpty {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("正在搜索…")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(16)
            } else {
                ForEach(discovery.nodes) { node in
                    Divider().padding(.leading, 60)
                    Button { connect(node) } label: { nodeRow(node) }
                        .buttonStyle(.plain)
                }
            }
        }
        .background(Color(.secondarySystemGroupedBackground), in: .rect(cornerRadius: 16))
    }

    private func nodeRow(_ node: DiscoveredNode) -> some View {
        HStack(spacing: 12) {
            Image(systemName: "server.rack")
                .font(.system(size: 15, weight: .medium))
                .foregroundStyle(.tint)
                .frame(width: 32, height: 32)
                .background(Color.accentColor.opacity(0.12), in: .circle)
            VStack(alignment: .leading, spacing: 2) {
                Text(node.name).font(.headline)
                Text(node.address).font(.caption.monospaced()).foregroundStyle(.secondary)
                if let model = node.model, !model.isEmpty {
                    Text(model).font(.caption2).foregroundStyle(.tertiary)
                }
            }
            Spacer()
            Image(systemName: "chevron.right")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.tertiary)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .contentShape(.rect)
    }

    private func connect(_ node: DiscoveredNode) {
        discovery.stop()
        store.host = node.endpoint
        Task { await store.refreshServer() }
    }

    private func hero(_ s: DongleStatus) -> some View {
        let (h, title, sub) = s.verdict(alias: store.displayedSIMAlias(for: s.iccid))
        let color: Color = h == .good ? .green : h == .warn ? .orange : .red
        let signal = Level.signal(s)
        return Section {
            HStack(spacing: 12) {
                Image(systemName: h == .good ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                    .font(.largeTitle).foregroundStyle(color)
                VStack(alignment: .leading, spacing: 4) {
                    // Online: the green icon says it, so show only the details. Problems keep their title.
                    if h == .good {
                        infoRow("antenna.radiowaves.left.and.right", sub)
                            .fontWeight(.medium)
                            .lineLimit(1)
                            .truncationMode(.tail)
                    } else {
                        Text(title).font(.title2.weight(.semibold))
                        Text(sub).font(.subheadline).foregroundStyle(.secondary)
                    }
                    if (s.netifd_up == 1 && s.link_uptime != nil) || signal != nil {
                        HStack {
                            if s.netifd_up == 1, s.link_uptime != nil {
                                infoRow("clock", "连续在线 \(Fmt.duration(s.link_uptime))")
                                    .foregroundStyle(.secondary).monospacedDigit()
                            }
                            Spacer(minLength: 8)
                            if let signal { Pill(text: "信号\(signal.label)", color: signal.color) }
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .contentShape(.rect)
            .contextMenu {
                if h == .good { simAliasActions(s) }
            }
            .accessibilityElement(children: .combine)
            .accessibilityActions {
                if h == .good { simAliasActions(s) }
            }
        }
    }

    @ViewBuilder
    private func simAliasActions(_ s: DongleStatus) -> some View {
        if let iccid = s.iccid, !iccid.isEmpty {
            if store.simAliases[iccid] != nil {
                if store.displayedSIMAlias(for: iccid) != nil {
                    Button("显示手机号", systemImage: "phone") { store.showSIMAlias(false, for: iccid) }
                        .disabled(s.phone_masked == nil)
                } else {
                    Button("显示 SIM 卡别名", systemImage: "tag") { store.showSIMAlias(true, for: iccid) }
                }
            }
            Button(store.simAliases[iccid] == nil ? "设置 SIM 卡别名" : "编辑 SIM 卡别名", systemImage: "pencil") {
                aliasCard = iccid
                aliasDraft = store.simAliases[iccid] ?? ""
                editingSIMAlias = true
            }
        }
    }

    /// Data plan: this cycle and today, each with its limit when one is set. Shown below the connection card.
    private func quotaSection(_ q: DataQuota) -> some View {
        let ratio = min(100, max(1, q.ratio ?? 100))
        let cycleOn = q.cycle_on && q.plan > 0
        return Section {
            QuotaRow(title: q.cycle_mode == "month" ? "本月" : "本周期", used: q.used, limit: cycleOn ? q.plan : 0,
                     elapsed: cycleElapsed(q),
                     stop: cycleOn && ratio < 100 ? Double(ratio) / 100 : nil,
                     ratio: cycleOn ? ratio : nil,
                     reset: resetText(q))
            QuotaRow(title: "今日", used: q.day_used, limit: q.daily_on ? q.daily : 0,
                     elapsed: todayElapsed(), stop: nil, ratio: nil, reset: "每日 0 点重置")
            if let text = blockText(q.block) {
                Label(text, systemImage: "exclamationmark.triangle.fill")
                    .font(.subheadline).foregroundStyle(.orange)
            }
        } header: {
            Text("流量")
        }
    }

    /// A node date (yyyy-MM-dd), parsed at local midnight.
    private func parseDay(_ text: String) -> Date? {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f.date(from: text)
    }

    private func cycleElapsed(_ q: DataQuota) -> Double? {
        guard let start = parseDay(q.cycle) else { return nil }
        // next_reset covers every cycle kind; older nodes only know monthly cycles
        guard let end = q.next_reset.flatMap(parseDay)
            ?? Calendar.current.date(byAdding: .month, value: 1, to: start) else { return nil }
        let total = end.timeIntervalSince(start)
        guard total > 0 else { return nil }
        return min(1, max(0, Date().timeIntervalSince(start) / total))
    }

    /// The cycle reset line shows the next reset date in the current locale's short date
    /// format; older nodes only give the day of month.
    private func resetText(_ q: DataQuota) -> String {
        guard let next = q.next_reset.flatMap(parseDay) else { return "每月 \(q.reset_day) 日重置" }
        let f = DateFormatter()
        f.dateStyle = .short
        f.timeStyle = .none
        return f.string(from: next) + " 重置"
    }

    private func todayElapsed() -> Double? {
        let cal = Calendar.current
        let start = cal.startOfDay(for: Date())
        guard let end = cal.date(byAdding: .day, value: 1, to: start) else { return nil }
        let total = end.timeIntervalSince(start)
        guard total > 0 else { return nil }
        return min(1, max(0, Date().timeIntervalSince(start) / total))
    }

    private func blockText(_ block: String) -> String? {
        switch block {
        case "limit": "已达流量上限，移动数据已断开"
        case "daily": "已达每日上限，移动数据已断开"
        default: nil
        }
    }

    /// One detail line of the verdict card. A fixed icon column keeps the texts aligned.
    private func infoRow(_ icon: String, _ text: String) -> some View {
        HStack(spacing: 6) {
            Image(systemName: icon).frame(width: 20)
            Text(text)
        }
        .font(.subheadline)
    }

}

/// Blue rings that grow from the antenna across the whole screen while scanning for nodes.
private struct DiscoveryHalo: View {
    let center: CGPoint
    let maxRadius: CGFloat
    private let minDiameter: CGFloat = 86
    private let period: Double = 5.2
    private let rings = 3

    var body: some View {
        TimelineView(.animation) { context in
            let t = context.date.timeIntervalSinceReferenceDate
            ZStack {
                Circle()
                    .fill(.blue.opacity(0.18))
                    .frame(width: 100, height: 100)
                    .blur(radius: 12)
                    .position(center)
                ForEach(0..<rings, id: \.self) { i in
                    let progress = ((t + Double(i) * period / Double(rings))
                        .truncatingRemainder(dividingBy: period)) / period
                    ring(progress).position(center)
                }
            }
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }

    private func ring(_ progress: Double) -> some View {
        let diameter = minDiameter + (maxRadius * 2 - minDiameter) * CGFloat(progress)
        let fade = 1 - progress
        return ZStack {
            Circle()
                .stroke(.blue.opacity(0.35 * fade), lineWidth: 9)
                .blur(radius: 5)
            Circle()
                .stroke(.blue.opacity(0.8 * fade), lineWidth: 3.5)
        }
        .frame(width: diameter, height: diameter)
    }
}

/// One quota line. Tapping the bar opens a popover with the usage, the projection, the plan
/// ratio and the reset information, so the row itself only carries the name, the total and the bar.
private struct QuotaRow: View {
    let title: String
    let used: Int64
    let limit: Int64
    let elapsed: Double?
    let stop: Double?
    let ratio: Int?
    let reset: String
    @State private var details = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if limit > 0 {
                HStack {
                    Text(title)
                    Spacer()
                    Text(Fmt.bytes(limit)).monospacedDigit().foregroundStyle(.secondary)
                }
                QuotaBar(fraction: fraction, forecast: forecast, stop: stop)
            } else {
                Text(title)
                Text(Fmt.usage(used)).monospacedDigit().foregroundStyle(.secondary)
                Text(reset).font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
        .contentShape(Rectangle())
        .onTapGesture {
            if limit > 0 { details.toggle() }
        }
        .popover(isPresented: $details) {
            VStack(alignment: .leading, spacing: 4) {
                Text("已用 \(Fmt.usage(used)) · 预计 \(Fmt.usage(projected))")
                    .monospacedDigit()
                Text(ratio.map { "达到 \($0)% 时断流 · \(reset)" } ?? reset)
                    .foregroundStyle(.secondary)
            }
            .font(.subheadline)
            .padding(12)
            .presentationCompactAdaptation(.popover)
        }
    }

    private var fraction: Double { limit > 0 ? Double(used) / Double(limit) : 0 }

    /// Usage at the end of the period if the current pace holds.
    private var projected: Int64? {
        guard let e = elapsed, e > 0, limit > 0 else { return nil }
        return Int64(Double(used) / e)
    }

    /// Usage projected at the end of the period, as a share of the bar.
    private var forecast: Double? {
        guard used > 0, let e = elapsed, e > 0, limit > 0 else { return nil }
        return Double(used) / e / Double(limit)
    }
}

/// Usage bar with a marker for the usage projected at the end of the period and, when the
/// cycle is capped below the plan, a white minus.diamond at the cutoff. The fill is blue below 80% of
/// the cutoff, orange below 100% and red above; the forecast marker is green below 80%,
/// yellow below 100% and red above, pinned to the right end when over. Markers are as tall
/// as the bar, with a background gap on each side, and the minus.diamond icon is one bar tall.
private struct QuotaBar: View {
    let fraction: Double
    let forecast: Double?
    let stop: Double?

    private let height: CGFloat = 8
    private let tick: CGFloat = 2
    private let gap: CGFloat = 2

    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width
            ZStack(alignment: .leading) {
                Canvas { ctx, size in
                    let radius = size.height / 2
                    ctx.fill(Path(roundedRect: CGRect(origin: .zero, size: size), cornerRadius: radius),
                             with: .color(Color(.systemFill)))
                    let filled = size.width * min(1, max(0, fraction))
                    if filled > 0 {
                        ctx.fill(Path(roundedRect: CGRect(x: 0, y: 0, width: filled, height: size.height),
                                      cornerRadius: radius),
                                 with: .color(fillColor))
                    }
                    let tickX = forecast.map { markerX($0, width: size.width, inset: tick / 2) }
                    let stopX = stop.map { markerX($0, width: size.width, inset: height / 2) }
                    if let x = stopX {
                        ctx.blendMode = .destinationOut
                        ctx.fill(Path(CGRect(x: x, y: 0, width: size.width - x, height: size.height)),
                                 with: .color(.black.opacity(0.75)))
                        ctx.blendMode = .normal
                    }
                    let cuts = [tickX.map { cut($0, width: tick) }, stopX.map { cut($0, width: height) }]
                        .compactMap { $0 }
                    if !cuts.isEmpty {
                        ctx.blendMode = .destinationOut
                        for r in cuts {
                            ctx.fill(Path(r), with: .color(.black))
                        }
                        ctx.blendMode = .normal
                    }
                    if let x = tickX, let forecast {
                        ctx.fill(Path(roundedRect: CGRect(x: x - tick / 2, y: 0, width: tick, height: size.height),
                                      cornerRadius: tick / 2),
                                 with: .color(markerColor(forecast)))
                    }
                }
                .frame(width: w, height: height)
                if let stop {
                    Image(systemName: "minus.diamond")
                        .font(.system(size: height, weight: .bold))
                        .foregroundStyle(.white)
                        .frame(width: height, height: height)
                        .offset(x: markerX(stop, width: w, inset: height / 2) - height / 2)
                }
            }
            .frame(width: w, height: height)
        }
        .frame(height: height)
    }

    private var cutoff: Double { stop ?? 1 }

    private var fillColor: Color {
        fraction < cutoff * 0.8 ? .blue : fraction < cutoff ? .orange : .red
    }

    private func markerColor(_ forecast: Double) -> Color {
        forecast < cutoff * 0.8 ? .green : forecast < cutoff ? .yellow : .red
    }

    private func markerX(_ p: Double, width: CGFloat, inset: CGFloat) -> CGFloat {
        min(width - inset, max(inset, width * min(1, max(0, p))))
    }

    /// Gap around a marker, spanning the full bar height.
    private func cut(_ center: CGFloat, width: CGFloat) -> CGRect {
        CGRect(x: center - width / 2 - gap, y: 0, width: width + gap * 2, height: height)
    }
}

/// 更多状态: the signal, link, network, cell, device and API details flattened onto one page, one section each.
/// Opened from Settings > 更多状态 and from the 更多 button on the status tab.
struct MoreStatusPage: View {
    @Environment(Store.self) private var store

    var body: some View {
        List {
            if store.host.isEmpty {
                Text("未连接API").foregroundStyle(.secondary)
            } else {
                if let s = store.status {
                    signal(s)
                    link(s)
                    network(s)
                    cell(s)
                    device(s)
                } else {
                    ProgressView().frame(maxWidth: .infinity)
                }
                api()
            }
        }
        .navigationTitle("更多状态")
    }

    private func signal(_ s: DongleStatus) -> some View {
        Section("信号") {
            metric("RSRP", s.rsrp, "dBm", Level("rsrp", s.rsrp))
            metric("SINR", s.sinr, "dB", Level("sinr", s.sinr))
            metric("RSRQ", s.rsrq, "dB", Level("rsrq", s.rsrq))
            metric("RSSI", s.rssi, "dBm", nil)
        }
    }

    private func metric(_ k: String, _ v: Int?, _ unit: String, _ l: Level?) -> some View {
        CopyRow(title: k, value: v.map { "\($0) \(unit)" }, pill: l.map { Pill(text: $0.label, color: $0.color) })
    }

    private func link(_ s: DongleStatus) -> some View {
        Section("链路") {
            CopyRow(title: "连续在线", value: s.netifd_up == 1 ? Fmt.duration(s.link_uptime) : "—")
            CopyRow(title: "USB 断连", value: "\(s.disconnects.map(String.init) ?? "—") 次")
            CopyRow(title: "模组温度", value: s.temp.map { "\($0) °C" },
                    pill: Fmt.temp(s.temp).map { Pill(text: $0.0, color: $0.1) })
            CopyRow(title: "出口网卡 usb0", value: s.usb0_carrier == 1 ? "有载波" : "无载波")
            row("Dongle 内网地址", s.usb0_ip, mono: true)
        }
    }

    private func network(_ s: DongleStatus) -> some View {
        Section("运营商网络") {
            row("SIM", Fmt.sim(s.sim))
            row("运营商", Fmt.oper(s.oper))
            row("注册", Fmt.reg(s.reg))
            row("APN", s.apn)
            row("IPv4", s.wan_ip, mono: true)
            row("IPv6", s.wan_ip6, mono: true)
            row("DNS", s.dns, mono: true)
        }
    }

    private func cell(_ s: DongleStatus) -> some View {
        Section("服务小区") {
            row("频段", s.band.map { "B\($0)" })
            row("EARFCN", s.earfcn, mono: true)
            row("小区 ID", s.cellid, mono: true)
            row("PCI", s.pci, mono: true)
            row("RRC 状态", Fmt.rrc(s.rrc))
        }
    }

    private func device(_ s: DongleStatus) -> some View {
        Section("设备") {
            row("型号", s.model)
            row("IMEI", s.imei, mono: true)
            row("ICCID", s.iccid, mono: true)
            row("IMSI", s.imsi, mono: true)
            row("USB 端口", s.usb_port)
            row("AT 端口", s.at_port)
        }
    }

    private func api() -> some View {
        Section("API") {
            if let sv = store.server {
                CopyRow(title: "Endpoint", value: nodeURL("/cgi-bin/sim-anywhere-api"), monospaced: true)
                CopyRow(title: "Wi-Fi Calling", value: store.api?.callURL?.absoluteString, monospaced: true)
                CopyRow(title: "接口", value: sv.endpoints.joined(separator: " · "))
                CopyRow(title: "规范", value: sv.spec, monospaced: true, copyValue: nodeURL(sv.spec))
            } else if store.isConnecting {
                ProgressView().frame(maxWidth: .infinity)
            } else if let error = store.serverError {
                Text(error).foregroundStyle(.red).textSelection(.enabled)
            } else {
                Text("未连接API").foregroundStyle(.secondary)
            }
        }
    }

    private func row(_ k: String, _ v: String?, mono: Bool = false) -> some View {
        CopyRow(title: k, value: v, monospaced: mono)
    }

    private func nodeURL(_ path: String) -> String {
        if URLComponents(string: path)?.scheme != nil { return path }
        guard var components = URLComponents(string: store.host) else { return path }
        if components.port == (components.scheme?.lowercased() == "https" ? 443 : 80) {
            components.port = nil
        }
        components.path = path.hasPrefix("/") ? path : "/" + path
        return components.string ?? path
    }
}

/// A row of the status cards and settings cards. A tap copies the value and shows "已复制" for a moment.
struct CopyRow: View {
    var title: String
    var value: String?
    var monospaced = false
    var pill: Pill? = nil
    var copyValue: String? = nil
    @State private var copied = false

    var body: some View {
        Button {
            guard let value else { return }
            UIPasteboard.general.string = copyValue ?? value
            copied = true
        } label: {
            HStack(spacing: 8) {
                Text(title).fixedSize()
                HStack(spacing: 6) {
                    Text(copied ? "已复制" : (value ?? "—"))
                        .font(monospaced ? .system(.body, design: .monospaced) : .body)
                        .foregroundStyle(copied ? Color.green : Color.secondary)
                        .lineLimit(1)
                        .minimumScaleFactor(0.4)
                        .multilineTextAlignment(.trailing)
                    if let pill, !copied { pill }
                }
                .frame(maxWidth: .infinity, alignment: .trailing)
            }
        }
        .buttonStyle(.plain)
        .accessibilityHint("点按复制")
        .task(id: copied) {
            guard copied else { return }
            try? await Task.sleep(for: .seconds(1.5))
            copied = false
        }
    }
}

struct Pill: View {
    var text: String
    var color: Color
    var body: some View {
        Text(text).font(.caption.weight(.semibold))
            .padding(.horizontal, 8).padding(.vertical, 2)
            .background(color.opacity(0.18), in: Capsule())
            .foregroundStyle(color)
    }
}

/// Error text in red. A tap copies it.
struct ErrorLabel: View {
    var text: String
    @State private var copied = false

    init(_ text: String) { self.text = text }

    var body: some View {
        Button {
            UIPasteboard.general.string = text
            copied = true
        } label: {
            Label(copied ? "已复制：\(text)" : text, systemImage: copied ? "doc.on.doc" : "exclamationmark.triangle")
                .foregroundStyle(.red)
        }
        .buttonStyle(.plain)
        .accessibilityHint("点按复制")
        .task(id: copied) {
            guard copied else { return }
            try? await Task.sleep(for: .seconds(2))
            copied = false
        }
    }
}

/// Download or upload rate, one point per second for the last two minutes. Tap a direction tile to switch the chart.
private struct SpeedSection: View {
    @Environment(Store.self) private var store
    @State private var upload = false
    @State private var arrival = Date.now   // when the latest rate point arrived

    private var tint: Color { upload ? .green : .blue }

    var body: some View {
        let pts = store.rateHistory
        let values = pts.map { upload ? $0.tx : $0.rx }
        Section {
            VStack(spacing: 14) {
                HStack(spacing: 10) {
                    tile("下载", "arrow.down", .blue, rate: pts.last?.rx, total: store.traffic?.rx, selected: !upload) { upload = false }
                    tile("上传", "arrow.up", .green, rate: pts.last?.tx, total: store.traffic?.tx, selected: upload) { upload = true }
                }
                if !pts.isEmpty {
                    chart(pts)
                    HStack {
                        Text("2 分钟前")
                        Spacer()
                        Text("峰值 \(Fmt.rate(values.max() ?? 0)) · 平均 \(Fmt.rate(values.reduce(0, +) / Double(values.count)))")
                        Spacer()
                        Text("现在")
                    }
                    .font(.caption2).foregroundStyle(.secondary).monospacedDigit()
                } else {
                    Label("等待数据", systemImage: "waveform.path.ecg")
                        .font(.subheadline).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, minHeight: 150)
                }
            }
            .padding(.vertical, 6)
            .animation(.snappy, value: upload)
        } header: {
            HStack(spacing: 6) {
                Text("实时流量")
                if !pts.isEmpty {
                    Image(systemName: "circle.fill").font(.system(size: 6)).foregroundStyle(.green)
                        .symbolEffect(.pulse)
                }
            }
        }
    }

    /// The line head grows from the previous point to the latest one over one second (one record per second).
    /// Each frame builds the points with that moving head, so the line, the area, the dot and the scrolling
    /// X domain are always drawn from the same data. Chart implicit animations animate them apart (gaps).
    private func chart(_ pts: [RatePoint]) -> some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30)) { ctx in
            let raw = pts.map { ($0.date, upload ? $0.tx : $0.rx) }
            let t = min(1, max(0, ctx.date.timeIntervalSince(arrival)))
            let shown = raw.count < 2 ? raw : raw.dropLast() + [Self.lerp(raw[raw.count - 2], raw[raw.count - 1], t)]
            let head = shown[shown.count - 1]
            Chart {
                ForEach(shown, id: \.0) { p in
                    AreaMark(x: .value("时间", p.0), y: .value("速率", p.1))
                        .foregroundStyle(.linearGradient(colors: [tint.opacity(0.35), tint.opacity(0.02)],
                                                         startPoint: .top, endPoint: .bottom))
                        .interpolationMethod(.monotone)
                    LineMark(x: .value("时间", p.0), y: .value("速率", p.1))
                        .foregroundStyle(tint)
                        .lineStyle(StrokeStyle(lineWidth: 2, lineCap: .round, lineJoin: .round))
                        .interpolationMethod(.monotone)
                }
                PointMark(x: .value("时间", head.0), y: .value("速率", head.1))
                    .foregroundStyle(tint)
                    .symbolSize(36)
            }
            .chartXScale(domain: head.0.addingTimeInterval(-Double(Store.rateWindow))...head.0)
            .chartYScale(domain: 0...max(1, shown.map(\.1).max() ?? 0))
            .chartXAxis(.hidden)
            .chartYAxis {
                AxisMarks(position: .trailing, values: .automatic(desiredCount: 3)) { v in
                    AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5, dash: [3, 3]))
                    AxisValueLabel { if let d = v.as(Double.self) { Text(Fmt.rate(d, decimals: 0)).font(.caption2) } }
                }
            }
        }
        .frame(height: 150)
        .onChange(of: pts.last?.epoch, initial: true) { arrival = .now }
    }

    private static func lerp(_ a: (Date, Double), _ b: (Date, Double), _ t: Double) -> (Date, Double) {
        (a.0.addingTimeInterval(b.0.timeIntervalSince(a.0) * t), a.1 + (b.1 - a.1) * t)
    }

    private func tile(_ name: String, _ icon: String, _ color: Color, rate: Double?, total: Int64?, selected: Bool,
                      action: @escaping () -> Void) -> some View {
        let parts = (rate.map { Fmt.rate($0) } ?? "—").split(separator: " ")
        return Button(action: action) {
            VStack(alignment: .leading, spacing: 6) {
                Label(name, systemImage: icon)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(selected ? color : .secondary)
                HStack(alignment: .firstTextBaseline, spacing: 3) {
                    Text(parts.first ?? "—")
                        .font(.system(size: 28, weight: .semibold, design: .rounded))
                        .monospacedDigit()
                        .contentTransition(.numericText())
                        .foregroundStyle(selected ? .primary : .secondary)
                    if parts.count > 1 {
                        Text(parts[1]).font(.footnote.weight(.medium)).foregroundStyle(.secondary)
                    }
                }
                .lineLimit(1).minimumScaleFactor(0.6)
                .animation(.default, value: rate)
                Text("累计 \(Fmt.bytes(total))").font(.caption).foregroundStyle(.secondary).monospacedDigit()
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(12)
            .background(selected ? color.opacity(0.12) : Color(.tertiarySystemFill), in: .rect(cornerRadius: 14))
            .overlay {
                RoundedRectangle(cornerRadius: 14).strokeBorder(selected ? color.opacity(0.45) : .clear, lineWidth: 1)
            }
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

/// Temperature or a signal value over the selected window. Tap a metric tile to switch the chart.
private struct TrendSection: View {
    @Environment(Store.self) private var store
    @State private var metric = "temp"

    private static let metrics: [(key: String, name: String, unit: String, color: Color)] = [
        ("temp", "温度", "°C", .orange), ("rsrp", "RSRP", "dBm", .blue), ("sinr", "SINR", "dB", .purple), ("rsrq", "RSRQ", "dB", .teal),
    ]
    private static let windows = [(3600, "1 小时"), (21600, "6 小时"), (86400, "24 小时")]

    var body: some View {
        @Bindable var store = store
        let m = Self.metrics.first { $0.key == metric }!
        let pts = store.history.compactMap { p in p.values[metric].map { (p.date, $0) } }
        Section {
            VStack(spacing: 14) {
                HStack(spacing: 8) {
                    ForEach(Self.metrics, id: \.key) { t in
                        tile(t.name, t.unit, t.color, value: current(t.key), selected: t.key == metric) { metric = t.key }
                    }
                }
                if let lo = pts.map(\.1).min(), let hi = pts.map(\.1).max() {
                    chart(pts, m, lo: lo, hi: hi)
                    HStack {
                        Text("\(Self.windows.first { $0.0 == store.historyWindow }?.1 ?? "")前")
                        Spacer()
                        Text("最低 \(Self.format(lo)) · 最高 \(Self.format(hi)) \(m.unit)")
                        Spacer()
                        Text("现在")
                    }
                    .font(.caption2).foregroundStyle(.secondary).monospacedDigit()
                } else {
                    Label("该时间段无数据", systemImage: "chart.xyaxis.line")
                        .font(.subheadline).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, minHeight: 150)
                }
            }
            .padding(.vertical, 6)
            .animation(.snappy, value: metric)
        } header: {
            HStack {
                Text("趋势")
                if let epoch = store.status?.sample_epoch {
                    Text("采样 \(Date(timeIntervalSince1970: TimeInterval(epoch)).formatted(date: .omitted, time: .shortened))")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Picker("时间范围", selection: $store.historyWindow) {
                    ForEach(Self.windows, id: \.0) { Text($0.1).tag($0.0) }
                }
                .pickerStyle(.menu)
                .textCase(nil)
            }
        }
        .onChange(of: store.historyWindow) { Task { await store.refreshHistory() } }
    }

    /// The latest sample (once a minute), the same one the header time refers to.
    private func current(_ key: String) -> Int? {
        guard let s = store.status else { return nil }
        switch key {
        case "temp": return s.temp
        case "rsrp": return s.rsrp
        case "sinr": return s.sinr
        default: return s.rsrq
        }
    }

    private static func format(_ v: Double) -> String { v.formatted(.number.precision(.fractionLength(0...1))) }

    private func chart(_ pts: [(Date, Double)], _ m: (key: String, name: String, unit: String, color: Color),
                       lo: Double, hi: Double) -> some View {
        // Signal values are negative, so the area fills from the bottom of the scale, not from zero.
        let pad = max(1, (hi - lo) * 0.15)
        let (bottom, top) = (lo - pad, hi + pad)
        return Chart {
            ForEach(pts, id: \.0) { p in
                AreaMark(x: .value("时间", p.0), yStart: .value(m.name, bottom), yEnd: .value(m.name, p.1))
                    .foregroundStyle(.linearGradient(colors: [m.color.opacity(0.35), m.color.opacity(0.02)],
                                                     startPoint: .top, endPoint: .bottom))
                    .interpolationMethod(.monotone)
                LineMark(x: .value("时间", p.0), y: .value(m.name, p.1))
                    .foregroundStyle(m.color)
                    .lineStyle(StrokeStyle(lineWidth: 2, lineCap: .round, lineJoin: .round))
                    .interpolationMethod(.monotone)
            }
            if let last = pts.last {
                PointMark(x: .value("时间", last.0), y: .value(m.name, last.1))
                    .foregroundStyle(m.color)
                    .symbolSize(36)
            }
        }
        .chartYScale(domain: bottom...top)
        .chartXAxis(.hidden)
        .chartYAxis {
            AxisMarks(position: .trailing, values: .automatic(desiredCount: 3)) { v in
                AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5, dash: [3, 3]))
                AxisValueLabel { if let d = v.as(Double.self) { Text(Self.format(d)).font(.caption2) } }
            }
        }
        .frame(height: 150)
    }

    private func tile(_ name: String, _ unit: String, _ color: Color, value: Int?, selected: Bool,
                      action: @escaping () -> Void) -> some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 4) {
                Text(name)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(selected ? color : .secondary)
                Text(value.map(String.init) ?? "—")
                    .font(.system(size: 20, weight: .semibold, design: .rounded))
                    .monospacedDigit()
                    .contentTransition(.numericText())
                    .foregroundStyle(selected ? .primary : .secondary)
                    .lineLimit(1).minimumScaleFactor(0.6)
                    .animation(.default, value: value)
                Text(unit).font(.caption2).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(10)
            .background(selected ? color.opacity(0.12) : Color(.tertiarySystemFill), in: .rect(cornerRadius: 12))
            .overlay {
                RoundedRectangle(cornerRadius: 12).strokeBorder(selected ? color.opacity(0.45) : .clear, lineWidth: 1)
            }
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}
