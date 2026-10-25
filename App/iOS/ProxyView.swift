import SwiftUI

/// The 代理 tab: clash proxy groups that can exit through the 4G dongle proxy.
/// The currently selected member of each group is highlighted; tapping another
/// member switches the group to it (POST /proxy/select, then reload).
struct ProxyView: View {
    @Environment(Store.self) private var store
    @Environment(\.scenePhase) private var scenePhase
    @State private var pending: String?
    @State private var pinging = false
    @State private var testing = false
    @State private var testingGroups: Set<String> = []
    @State private var actionError: String?

    var body: some View {
        NavigationStack {
            List {
                if store.host.isEmpty {
                    ContentUnavailableView("未连接API", systemImage: "network.slash")
                        .foregroundStyle(.secondary)
                } else if let e = store.proxyError, store.proxyGroups == nil {
                    ErrorLabel(e)
                } else if let p = store.proxyGroups {
                    Section {
                        exitRow(p)
                        if let actionError {
                            ErrorLabel(actionError)
                        }
                    } footer: {
                        if let s = p.server {
                            Text(serverText(s))
                                .textSelection(.enabled)
                        }
                    }
                    if p.groups.isEmpty {
                        ContentUnavailableView("没有可调代理组", systemImage: "circle.dashed")
                            .foregroundStyle(.secondary)
                    } else {
                        ForEach(p.groups) { g in
                            Section {
                                ForEach(g.proxies, id: \.name) { m in
                                    row(g, m)
                                }
                            } header: {
                                HStack(spacing: 8) {
                                    Text(g.name).font(.subheadline.weight(.semibold)).foregroundStyle(.primary)
                                    Text(g.type)
                                        .font(.caption2.weight(.medium))
                                        .padding(.horizontal, 6).padding(.vertical, 1)
                                        .background(Color(.tertiarySystemFill), in: Capsule())
                                    Spacer(minLength: 8)
                                    if testingGroups.contains(g.name) {
                                        ProgressView().controlSize(.small)
                                    } else {
                                        Button {
                                            Task { await testGroup(g.name) }
                                        } label: {
                                            Image(systemName: "arrow.clockwise")
                                        }
                                        .buttonStyle(.borderless)
                                        .disabled(pinging || testing || testingGroups.contains(g.name) || pending != nil)
                                        .accessibilityLabel("测试 \(g.name) 的延迟")
                                    }
                                }
                                .textCase(nil)
                            }
                        }
                    }
                } else {
                    ProgressView().frame(maxWidth: .infinity)
                }
            }
            .navigationTitle("代理")
            .refreshable { await store.refreshProxy() }
            // Loads on every visit, then polls every 5 s while the tab is visible, so a switch
            // made elsewhere (the node page, another phone, a url-test group re-picking its
            // member) shows up by itself. The poll stops with the tab and in the background.
            .task(id: [store.host, "\(scenePhase == .background)"]) {
                actionError = nil
                guard !store.host.isEmpty, scenePhase != .background else { return }
                while !Task.isCancelled {
                    await store.refreshProxy(quiet: true)
                    try? await Task.sleep(for: .seconds(5))
                }
            }
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    if testing {
                        ProgressView()
                    } else {
                        Button {
                            Task { await testAll() }
                        } label: {
                            Image(systemName: "arrow.clockwise")
                        }
                        .disabled(pinging || !testingGroups.isEmpty || (store.proxyGroups?.groups.isEmpty ?? true))
                        .accessibilityLabel("刷新所有组的延迟")
                    }
                }
            }
        }
    }

    private func exitRow(_ p: ProxyGroups) -> some View {
        let color = Self.delayColor(store.exitDelay)
        return HStack(spacing: 12) {
            Image(systemName: "antenna.radiowaves.left.and.right")
                .font(.title3.weight(.semibold))
                .foregroundStyle(color)
                .frame(width: 44, height: 44)
                .background(color.opacity(0.15), in: .rect(cornerRadius: 12))
            VStack(alignment: .leading, spacing: 2) {
                Text(p.exit).font(.headline).lineLimit(1)
                if let i = p.interface {
                    Text("Proxy via \(i)").font(.caption.monospaced()).foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 8)
            if pinging {
                ProgressView().controlSize(.small)
            } else {
                Button {
                    Task { await pingExit() }
                } label: {
                    delayPill(store.exitDelay)
                }
                .buttonStyle(.borderless)
                .disabled(testing || !testingGroups.isEmpty)
                .accessibilityLabel("刷新 4G 出口延迟")
                .accessibilityValue(store.exitDelay.map { "延迟 \($0) 毫秒" } ?? "延迟不可用")
            }
        }
        .padding(.vertical, 4)
    }

    /// The delay as a colored pill; "超时" for a failed test, "—" when never tested.
    private func delayPill(_ d: Int?) -> some View {
        Pill(text: d.map { $0 > 0 ? "\($0) ms" : "超时" } ?? "—", color: d == nil ? .secondary : Self.delayColor(d))
            .monospacedDigit()
    }

    /// "Server: <core> v<version>"; the API version already starts with "v".
    private func serverText(_ s: ProxyServer) -> String {
        guard let raw = s.version?.trimmingCharacters(in: .whitespaces), !raw.isEmpty else {
            return "Server: \(s.core)"
        }
        return "Server: \(s.core) \(raw.hasPrefix("v") ? raw : "v" + raw)"
    }

    /// Red: no delay or above 8000 ms; green: below 400 ms; orange: everything else.
    private static func delayColor(_ d: Int?) -> Color {
        guard let d, d > 0, d <= 8000 else { return .red }
        return d < 400 ? .green : .orange
    }

    @MainActor
    private func pingExit() async {
        guard !pinging, !testing, testingGroups.isEmpty else { return }
        pinging = true
        actionError = nil
        defer { pinging = false }
        if let error = await store.pingExit() { actionError = error }
    }

    @MainActor
    private func testAll() async {
        guard !testing, !pinging, testingGroups.isEmpty else { return }
        testing = true
        actionError = nil
        defer { testing = false }
        if let error = await store.refreshProxyDelays() { actionError = error }
    }

    @MainActor
    private func testGroup(_ name: String) async {
        guard !testing, !pinging, !testingGroups.contains(name) else { return }
        testingGroups.insert(name)
        actionError = nil
        defer { testingGroups.remove(name) }
        if let error = await store.refreshProxyDelays(name) { actionError = error }
    }

    private func row(_ g: ProxyGroup, _ m: ProxyMember) -> some View {
        let key = "\(g.name)\u{0}\(m.name)"
        return Button {
            pending = key
            Task {
                await store.selectProxy(group: g.name, proxy: m.name)
                pending = nil
            }
        } label: {
            HStack(spacing: 12) {
                Group {
                    if pending == key {
                        ProgressView().controlSize(.small)
                    } else {
                        Image(systemName: m.selected ? "checkmark.circle.fill" : "circle")
                            .font(.title3)
                            .foregroundStyle(m.selected ? Color.accentColor : Color(.tertiaryLabel))
                    }
                }
                .frame(width: 24)
                VStack(alignment: .leading, spacing: 1) {
                    Text(m.name)
                        .font(m.selected ? .body.weight(.semibold) : .body)
                        .lineLimit(1)
                    Text(m.type).font(.caption).foregroundStyle(.secondary)
                }
                Spacer(minLength: 8)
                delayPill(m.delay)
            }
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .disabled(m.selected || pending != nil || testing || !testingGroups.isEmpty)
        .listRowBackground(m.selected ? Color.accentColor.opacity(0.08) : nil)
        .accessibilityAddTraits(m.selected ? .isSelected : [])
    }
}
