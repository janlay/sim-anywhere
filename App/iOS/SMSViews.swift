import SwiftUI
import UIKit

struct SMSListView: View {
    @Environment(Store.self) private var store
    @Environment(CallSession.self) private var phone
    @AppStorage(SMSText.codesKey) private var codes = true
    @State private var dial: String?
    @State private var box = ""
    @State private var selection = Set<String>()
    @State private var editMode = EditMode.inactive
    @State private var pendingDelete: [String] = []
    @State private var compose: ComposeDraft?
    @State private var syncing = false

    private var shown: [SMSMessage] { box.isEmpty ? store.messages : store.messages.filter { $0.box == box } }

    // Reply needs exactly one selected SMS from a numeric sender, same rule as the long-press menu.
    private var replyTarget: SMSMessage? {
        guard selection.count == 1, let m = shown.first(where: { selection.contains($0.id) }),
              m.incoming, ComposeDraft.isNumber(m.number) else { return nil }
        return m
    }

    var body: some View {
        NavigationStack {
            List(selection: $selection) {
                if let e = store.smsError {
                    ErrorLabel(e)
                }
                ForEach(shown) { m in
                    NavigationLink(value: m) { SMSRow(message: m) }
                        .swipeActions(edge: .trailing) {
                            Button("删除", systemImage: "trash", role: .destructive) { pendingDelete = [m.id] }
                        }
                        .swipeActions(edge: .leading) {
                            if m.incoming {
                                Button(m.read ? "未读" : "已读", systemImage: m.read ? "envelope.badge" : "envelope.open") {
                                    Task { await store.smsAction { try await $0.mark([m.id], read: !m.read) } }
                                }
                                .tint(.blue)
                            }
                        }
                        .contextMenu { menu(m) }
                }
            }
            .overlay {
                if store.host.isEmpty {
                    ContentUnavailableView("未连接API", systemImage: "network.slash")
                        .foregroundStyle(.secondary).background(.background)
                } else if shown.isEmpty && store.smsError == nil {
                    ContentUnavailableView("没有短信", systemImage: "message")
                }
            }
            .navigationTitle("短信")
            .navigationDestination(for: SMSMessage.self) { SMSDetailView(message: $0, compose: $compose) }
            .environment(\.editMode, $editMode)
            .refreshable { await store.refreshSMS(sync: true) }
            .task(id: store.host) { await store.refreshSMS() }
            .toolbar { toolbar }
            .safeAreaInset(edge: .top) {
                Picker("信箱", selection: $box) {
                    Text("全部").tag("")
                    Text("收件").tag("in")
                    Text("发件").tag("out")
                }
                .pickerStyle(.segmented).padding(.horizontal)
            }
            .confirmationDialog("确定要删除所选的 \(pendingDelete.count) 条短信吗？此操作不可撤销。",
                                isPresented: .init(get: { !pendingDelete.isEmpty }, set: { if !$0 { pendingDelete = [] } }),
                                titleVisibility: .visible) {
                Button("删除", role: .destructive) {
                    let ids = pendingDelete
                    Task {
                        await store.smsAction { try await $0.delete(ids) }
                        selection = []
                        editMode = .inactive
                    }
                }
            }
            .sheet(item: $compose) { ComposeView(draft: $0) }
            .confirmDial($dial)
        }
    }

    @ViewBuilder
    private func menu(_ m: SMSMessage) -> some View {
        if m.incoming {
            Button(m.read ? "标为未读" : "标为已读", systemImage: m.read ? "envelope.badge" : "envelope.open") {
                Task { await store.smsAction { try await $0.mark([m.id], read: !m.read) } }
            }
        }
        // Alphanumeric senders (e.g. "Maxx") cannot receive a call or a reply.
        if ComposeDraft.isNumber(m.number) {
            if m.incoming {
                Button("回复", systemImage: "arrowshape.turn.up.left") { compose = ComposeDraft(to: m.number) }
            }
            Button("拨打", systemImage: "phone") { dial = m.number }.disabled(!phone.ready || phone.inCall)
        }
        Button("复制内容", systemImage: "doc.on.doc") { UIPasteboard.general.string = m.text }
        if codes, let code = SMSText.code(in: m.text) {
            Button("复制验证码 \(code)", systemImage: "number") { UIPasteboard.general.string = code }
        }
        Button("复制号码", systemImage: "person.text.rectangle") { UIPasteboard.general.string = m.number }
        Divider()
        Button("删除", systemImage: "trash", role: .destructive) { pendingDelete = [m.id] }
    }

    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
        ToolbarItem(placement: .topBarLeading) {
            Button(editMode.isEditing ? "完成" : "选择") {
                selection = []
                editMode = editMode.isEditing ? .inactive : .active
            }
        }
        ToolbarItemGroup(placement: .topBarTrailing) {
            if editMode.isEditing {
                let ids = Array(selection)
                if let m = replyTarget {
                    Button("回复", systemImage: "arrowshape.turn.up.left") {
                        compose = ComposeDraft(to: m.number)
                        selection = []
                        editMode = .inactive
                    }
                    .labelStyle(.iconOnly)
                }
                Button("标为已读", systemImage: "envelope.open") { bulkMark(ids, true) }
                    .labelStyle(.iconOnly)
                    .disabled(ids.isEmpty)
                Button("标为未读", systemImage: "envelope.badge") { bulkMark(ids, false) }
                    .labelStyle(.iconOnly)
                    .disabled(ids.isEmpty)
                Button("删除", systemImage: "trash", role: .destructive) { pendingDelete = ids }
                    .labelStyle(.iconOnly)
                    .disabled(ids.isEmpty)
            } else {
                Button("同步", systemImage: "arrow.triangle.2.circlepath") {
                    Task { syncing = true; await store.refreshSMS(sync: true); syncing = false }
                }
                .disabled(syncing)
                Button("新短信", systemImage: "square.and.pencil") { compose = ComposeDraft() }
            }
        }
    }

    private func bulkMark(_ ids: [String], _ read: Bool) {
        Task {
            await store.smsAction { try await $0.mark(ids, read: read) }
            selection = []
            editMode = .inactive
        }
    }
}

private struct SMSRow: View {
    var message: SMSMessage

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Circle().fill(message.incoming && !message.read ? Color.blue : .clear).frame(width: 8, height: 8).padding(.top, 6)
            VStack(alignment: .leading, spacing: 2) {
                HStack {
                    Image(systemName: message.incoming ? "arrow.down.left" : "arrow.up.right")
                        .font(.caption).foregroundStyle(.secondary)
                    Text(message.number).font(.headline).lineLimit(1)
                    Spacer()
                    Text(message.date, format: .relative(presentation: .named)).font(.caption).foregroundStyle(.secondary)
                }
                Text(message.text).font(.subheadline).foregroundStyle(.secondary).lineLimit(2)
                if message.parts < message.total {
                    Text("缺 \(message.total - message.parts) 段").font(.caption).foregroundStyle(.orange)
                }
            }
        }
    }
}

struct SMSDetailView: View {
    @Environment(Store.self) private var store
    @Environment(CallSession.self) private var phone
    @State private var dial: String?
    var message: SMSMessage
    @Binding var compose: ComposeDraft?
    @AppStorage(SMSText.linksKey) private var links = true
    @AppStorage(SMSText.codesKey) private var codes = true

    var body: some View {
        List {
            Section {
                Text(links ? SMSText.linked(message.text) : AttributedString(message.text)).textSelection(.enabled)
            } footer: {
                VStack(alignment: .leading) {
                    Text(message.date, format: .dateTime)
                    if message.total > 1 { Text("长短信 \(message.parts)/\(message.total) 段") }
                }
            }
            if codes, let code = SMSText.code(in: message.text) {
                Section {
                    LabeledContent("验证码") { Text(code).monospaced().textSelection(.enabled) }
                    Button("复制验证码", systemImage: "doc.on.doc") { UIPasteboard.general.string = code }
                }
            }
        }
        .navigationTitle(message.number)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            // Alphanumeric senders (e.g. "Maxx") cannot receive a call or a reply.
            if ComposeDraft.isNumber(message.number) {
                Button("拨打", systemImage: "phone") { dial = message.number }.disabled(!phone.ready || phone.inCall)
                if message.incoming {
                    Button("回复", systemImage: "arrowshape.turn.up.left") { compose = ComposeDraft(to: message.number) }
                }
            }
        }
        .confirmDial($dial)
        .task {
            if message.incoming && !message.read {
                await store.smsAction { try await $0.mark([message.id], read: true) }
            }
        }
    }
}

struct ComposeDraft: Identifiable {
    var id = UUID()
    var to = ""

    static func isNumber(_ s: String) -> Bool {
        s.wholeMatch(of: /\+?[0-9][0-9 -]{1,19}/) != nil
    }
}

struct ComposeView: View {
    @Environment(Store.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var to: String
    @State private var text = ""
    @State private var preview: SendResult?
    @State private var confirming = false
    @State private var sending = false
    @State private var error: String?

    init(draft: ComposeDraft) { _to = State(initialValue: draft.to) }

    private var valid: Bool { ComposeDraft.isNumber(to) && !text.isEmpty }

    var body: some View {
        NavigationStack {
            Form {
                Section("收件人") {
                    TextField("号码", text: $to).keyboardType(.phonePad)
                }
                Section {
                    TextEditor(text: $text).frame(minHeight: 160)
                } header: {
                    Text("内容")
                } footer: {
                    if let p = preview {
                        Text("\(text.count) 字符 · \(p.encoding == "gsm7" ? "GSM 7-bit" : "UCS2") · \(p.parts) 段（运营商可能按段计费）")
                    }
                }
                if let error {
                    ErrorLabel(error)
                }
            }
            .navigationTitle("新短信")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("发送") { confirming = true }.disabled(!valid || sending || preview == nil)
                }
            }
            // Ask the node for the encoding and part count; debounce while typing.
            .task(id: text) {
                guard !text.isEmpty else { preview = nil; return }
                try? await Task.sleep(for: .milliseconds(400))
                guard !Task.isCancelled, let api = store.api else { return }
                do {
                    preview = try await api.send(to: ComposeDraft.isNumber(to) ? to : "+10", text: text, dryRun: true)
                    error = nil
                } catch {
                    preview = nil
                    self.error = error.localizedDescription
                }
            }
            .confirmationDialog("发送到 \(to)，共 \(preview?.parts ?? 0) 段？", isPresented: $confirming, titleVisibility: .visible) {
                Button("发送") { Task { await send() } }
            }
        }
        .interactiveDismissDisabled(!text.isEmpty)
    }

    private func send() async {
        sending = true
        defer { sending = false }
        let (to, text) = (to, text)
        if let e = await store.smsAction({ _ = try await $0.send(to: to, text: text, dryRun: false) }) {
            error = e
        } else {
            dismiss()
        }
    }
}
