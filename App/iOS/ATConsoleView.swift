import SwiftUI
import UIKit

/// Settings > 高级: send AT commands to the modem (POST /at).
struct ATConsoleView: View {
    @Environment(Store.self) private var store
    @State private var command = ""
    @State private var log: [ATEntry] = []          // newest first
    @State private var running = false
    @State private var risky: ATPreset?
    @FocusState private var editing: Bool

    private var valid: Bool { command.trimmingCharacters(in: .whitespaces).uppercased().hasPrefix("AT") }

    var body: some View {
        List {
            Section {
                HStack {
                    TextField("AT+CSQ", text: $command)
                        .monospaced()
                        .keyboardType(.asciiCapable)
                        .textInputAutocapitalization(.characters)
                        .autocorrectionDisabled()
                        .submitLabel(.send)
                        .focused($editing)
                        .onSubmit { send(command) }
                    if running {
                        ProgressView()
                    } else {
                        Button("发送", systemImage: "paperplane.fill") { send(command) }
                            .labelStyle(.iconOnly)
                            .disabled(!valid)
                    }
                }
            } footer: {
                Text("命令直接发给模组。设置类命令会改变模组配置，部分命令会让模组重启。")
            }
            if !log.isEmpty {
                Section("输出") {
                    ForEach(log) { ATEntryRow(entry: $0) }
                }
            }
            Section("常用命令") {
                ForEach(ATPreset.all) { p in
                    Button {
                        if p.warning != nil { risky = p } else { send(p.command, timeout: p.timeout) }
                    } label: {
                        LabeledContent {
                            Text(p.command).monospaced().font(.caption).lineLimit(1)
                        } label: {
                            Text(p.title).foregroundStyle(p.warning != nil ? .red : .primary)
                        }
                    }
                    .disabled(running)
                }
            }
        }
        .navigationTitle("高级")
        .scrollDismissesKeyboard(.immediately)
        .toolbar {
            if !log.isEmpty { Button("清空") { log = [] } }
        }
        .confirmationDialog(risky?.warning ?? "", isPresented: .init(get: { risky != nil }, set: { if !$0 { risky = nil } }),
                            titleVisibility: .visible) {
            Button("发送 \(risky?.command ?? "")", role: .destructive) {
                if let p = risky { send(p.command, timeout: p.timeout) }
            }
        }
    }

    private func send(_ cmd: String, timeout: Int = 5) {
        let c = cmd.trimmingCharacters(in: .whitespaces)
        guard c.uppercased().hasPrefix("AT"), !running, let api = store.api else { return }
        editing = false
        running = true
        Task {
            var e = ATEntry(command: c)
            do {
                let r = try await api.at(c, timeout: timeout)
                e.lines = r.lines
                e.result = r.result
            } catch {
                e.result = error.localizedDescription
                e.failed = true
            }
            log.insert(e, at: 0)
            running = false
        }
    }
}

struct ATEntry: Identifiable {
    var id = UUID()
    var command: String
    var lines: [String] = []
    var result = ""
    var failed = false

    var ok: Bool { !failed && result == "OK" }
    var text: String { ([command] + lines + [result]).joined(separator: "\n") }
}

private struct ATEntryRow: View {
    var entry: ATEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(entry.command).font(.footnote.monospaced().weight(.semibold))
            if !entry.lines.isEmpty {
                Text(entry.lines.joined(separator: "\n")).font(.footnote.monospaced())
            }
            Text(entry.result).font(.footnote.monospaced()).foregroundStyle(entry.ok ? .green : .red)
        }
        .textSelection(.enabled)
        .contextMenu {
            Button("复制", systemImage: "doc.on.doc") { UIPasteboard.general.string = entry.text }
        }
    }
}

struct ATPreset: Identifiable {
    var title: String
    var command: String
    var timeout = 5
    /// Set for commands that change or restart the modem; the app asks first.
    var warning: String?
    var id: String { command }

    static let all: [ATPreset] = [
        .init(title: "模组信息", command: "ATI"),
        .init(title: "信号强度", command: "AT+CSQ"),
        .init(title: "服务小区", command: "AT+QENG=\"servingcell\""),
        .init(title: "邻区", command: "AT+QENG=\"neighbourcell\""),
        .init(title: "网络制式与频段", command: "AT+QNWINFO"),
        .init(title: "运营商", command: "AT+COPS?"),
        .init(title: "LTE 注册状态", command: "AT+CEREG?"),
        .init(title: "CS 注册状态", command: "AT+CREG?"),
        .init(title: "SIM 状态", command: "AT+CPIN?"),
        .init(title: "ICCID", command: "AT+QCCID"),
        .init(title: "本机号码", command: "AT+CNUM"),
        .init(title: "VoLTE 状态", command: "AT+QCFG=\"ims\""),
        .init(title: "当前通话", command: "AT+CLCC"),
        .init(title: "上次通话结束原因", command: "AT+CEER"),
        .init(title: "模组温度", command: "AT+QTEMP"),
        .init(title: "数据连接配置", command: "AT+CGDCONT?"),
        .init(title: "数据连接地址", command: "AT+CGPADDR"),
        .init(title: "开启移动数据", command: "AT+CGATT=1"),
        .init(title: "关闭移动数据", command: "AT+CGATT=0",
              warning: "关闭移动数据会断开 4G 和 Wi-Fi Calling。"),
        .init(title: "USB 配置", command: "AT+QCFG=\"usbcfg\""),
        .init(title: "MBN 配置列表", command: "AT+QMBNCFG=\"list\""),
        .init(title: "重启模组", command: "AT+CFUN=1,1",
              warning: "重启模组会中断 4G 约 30 秒。"),
    ]
}
