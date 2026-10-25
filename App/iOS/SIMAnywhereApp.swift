import SwiftUI
import UIKit

private enum AppTab: Hashable {
    case status, sms, phone, proxy, settings
}

@main
struct SIMAnywhereApp: App {
    @State private var store = Store()
    @State private var phone = CallSession()
    @State private var tab: AppTab = .status
    @Environment(\.scenePhase) private var scenePhase

    init() { SMSWatch.register() }

    var body: some Scene {
        WindowGroup {
            TabView(selection: $tab) {
                Tab("状态", systemImage: "antenna.radiowaves.left.and.right", value: .status) {
                    StatusView(onConnect: { tab = .settings })
                }
                if !store.host.isEmpty {
                    Tab("短信", systemImage: "message", value: .sms) { SMSListView() }
                        .badge(store.unread)
                    Tab("电话", systemImage: "phone", value: .phone) { PhoneView() }
                    Tab("代理", systemImage: "globe", value: .proxy) { ProxyView() }
                }
                Tab("设置", systemImage: "gear", value: .settings) { SettingsView() }
            }
            .onChange(of: store.host.isEmpty) { _, empty in
                if empty, tab != .status, tab != .settings { tab = .status }
            }
            .environment(store)
            .environment(phone)
            .fullScreenCover(isPresented: .init(get: { phone.inCall }, set: { _ in })) { CallScreen().environment(phone) }
            // Poll every minute. Keeps the unread badge and new-SMS notifications current.
            .task(id: store.host) {
                while !Task.isCancelled {
                    await store.refreshSMS()
                    try? await Task.sleep(for: .seconds(60))
                }
            }
            .onChange(of: scenePhase) { _, phase in
                if phase == .background { SMSWatch.schedule() }
            }
            // Incoming calls ring only while the call WebSocket is open: in the foreground, or during a call.
            .onChange(of: [store.host, "\(scenePhase)"], initial: true) {
                phone.update(host: store.host, active: scenePhase != .background)
            }
        }
    }
}

struct SettingsView: View {
    @Environment(Store.self) private var store
    @AppStorage(SMSText.linksKey) private var links = true
    @AppStorage(SMSText.codesKey) private var codes = true
    @State private var host = ""
    @State private var port = ""
    @State private var scheme = "http"
    @State private var formError: String?
    @State private var confirmDisconnect = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    HStack(spacing: 4) {
                        Picker("协议", selection: $scheme) {
                            Text("http").tag("http")
                            Text("https").tag("https")
                        }
                        .labelsHidden()
                        .fixedSize()
                        .tint(.primary)
                        Text("://").foregroundStyle(.secondary)
                        TextField("主机（IP 或域名）", text: $host)
                            .keyboardType(.URL)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                        Text(":").foregroundStyle(.secondary)
                        TextField(portPlaceholder, text: $port)
                            .keyboardType(.numberPad)
                            .frame(width: 64)
                    }
                    if let formError {
                        Text(formError).foregroundStyle(.red)
                    }
                    HStack {
                        if store.server != nil && !changed {
                            Button("断开", role: .destructive) {
                                confirmDisconnect = true
                            }
                        } else {
                            Button("应用", action: apply).disabled(!changed)
                        }
                        Spacer()
                        HStack(spacing: 8) {
                            Circle()
                                .fill(store.server != nil ? .green : store.isConnecting ? .orange : .secondary)
                                .frame(width: 8, height: 8)
                            Text(store.server != nil ? "已连接" : store.isConnecting ? "连接中" : "未连接")
                                .foregroundStyle(.secondary)
                        }
                    }
                    if let error = store.serverError {
                        Text(error).font(.footnote).foregroundStyle(.red).textSelection(.enabled)
                    }
                } header: {
                    Text("节点")
                } footer: {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("节点地址为 OpenWrt Web URL，主机名不要写页面路径。")
                        if scheme == "https" {
                            Text("使用 HTTPS 时，请确保通话 WebSocket 可连接：wss://<host>[:port]/call")
                        }
                    }
                }
                Section {
                    Toggle("识别链接", isOn: $links)
                    Toggle("识别验证码", isOn: $codes)
                } header: {
                    Text("短信")
                } footer: {
                    Text("链接可点开。验证码显示在短信详情中，可一键复制。")
                }
                if !store.host.isEmpty {
                    Section {
                        NavigationLink("更多状态") { MoreStatusPage() }
                    }
                }
                if !store.host.isEmpty {
                    Section {
                        NavigationLink("高级") { ATConsoleView() }
                    } footer: {
                        Text("向 Dongle 发送 AT 命令。")
                    }
                }
                Section {
                    LabeledContent("App", value: appName)
                    LabeledContent("版本", value: appVersion)
                    if let url = URL(string: "https://x.com/janlay") {
                        Link(destination: url) {
                            HStack {
                                Text("作者").foregroundStyle(.primary)
                                Spacer()
                                Text("@janlay").foregroundStyle(.secondary)
                                Image(systemName: "arrow.up.right")
                                    .font(.caption)
                                    .foregroundStyle(.tertiary)
                            }
                        }
                    } else {
                        LabeledContent("作者", value: "janlay")
                    }
                } header: {
                    Text("关于")
                }
            }
            .navigationTitle("设置")
            .scrollDismissesKeyboard(.interactively)
            .background(TapToDismissKeyboard())
            .onAppear { load() }
            .task(id: store.host) { await store.refreshServer() }
            .confirmationDialog("断开当前节点？", isPresented: $confirmDisconnect, titleVisibility: .visible) {
                Button("断开连接", role: .destructive, action: disconnect)
                Button("取消", role: .cancel) {}
            }
        }
    }

    private var appName: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String ?? "SIM Anywhere"
    }

    private var appVersion: String {
        let short = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "—"
        let build = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "—"
        return "\(short) (\(build))"
    }

    /// True when the form differs from the stored address.
    private var changed: Bool {
        let current = NodeAddress(endpoint: store.host)
        return host.trimmingCharacters(in: .whitespaces) != current.host
            || portNumber != current.port
            || scheme != current.scheme
    }

    /// The typed port. An empty field means the default, 80.
    private var portNumber: Int? {
        let t = port.trimmingCharacters(in: .whitespaces)
        return t.isEmpty ? defaultPort : Int(t)
    }

    private var defaultPort: Int { scheme == "https" ? 443 : 80 }
    private var portPlaceholder: String { String(defaultPort) }

    private func load() {
        let n = NodeAddress(endpoint: store.host)
        host = n.host
        scheme = n.scheme
        port = n.port == defaultPort ? "" : "\(n.port)"
    }

    private func apply() {
        let h = host.trimmingCharacters(in: .whitespaces)
        guard !h.isEmpty else { formError = "请填写主机"; return }
        guard let p = portNumber, (1...65535).contains(p) else {
            formError = "端口须为 1–65535"; return
        }
        formError = nil
        store.host = NodeAddress(host: h, port: p, scheme: scheme).endpoint
        load()
    }

    private func disconnect() {
        store.host = ""
        formError = nil
    }
}

/// Hides the keyboard on a tap outside text inputs. Buttons and links still get their taps.
struct TapToDismissKeyboard: UIViewRepresentable {
    func makeUIView(context: Context) -> KeyboardTapView { KeyboardTapView() }
    func updateUIView(_ uiView: KeyboardTapView, context: Context) {}
}

final class KeyboardTapView: UIView, UIGestureRecognizerDelegate {
    private let tap = UITapGestureRecognizer()

    override init(frame: CGRect) {
        super.init(frame: frame)
        tap.cancelsTouchesInView = false
        tap.delegate = self
        tap.addTarget(self, action: #selector(tapped))
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func willMove(toWindow newWindow: UIWindow?) {
        window?.removeGestureRecognizer(tap)
        super.willMove(toWindow: newWindow)
    }

    override func didMoveToWindow() {
        super.didMoveToWindow()
        window?.addGestureRecognizer(tap)
    }

    @objc private func tapped() {
        window?.endEditing(true)
    }

    func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer, shouldReceive touch: UITouch) -> Bool {
        var view = touch.view
        while let current = view {
            if current is UITextField || current is UITextView { return false }
            view = current.superview
        }
        return true
    }

    func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer,
                           shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool { true }
}
