import Contacts
import ContactsUI
import SwiftUI
import UIKit

struct PhoneView: View {
    @Environment(Store.self) private var store
    @Environment(CallSession.self) private var phone
    @State private var number = ""
    @State private var confirm: String?
    @State private var compose: ComposeDraft?
    @State private var box = ""
    @State private var selection = Set<String>()
    @State private var editMode = EditMode.inactive
    @State private var pendingDelete: [String] = []
    @FocusState private var editing: Bool
    @State private var fieldFrame = CGRect.zero

    private var shown: [CallRecord] { box.isEmpty ? store.calls : store.calls.filter { $0.dir == box } }

    /// Wi-Fi Calling and VoIP on one row. Each icon and name takes the state color.
    private var callingStatus: some View {
        let wifi: Color = switch phone.wifi {
        case "registered": .blue
        case "error": .orange
        default: .secondary
        }
        let voip: Color = switch phone.voipStatus {
        case .active, .ineffective: .blue
        case .unconfigured: .secondary
        }
        return HStack {
            Label("Wi-Fi Calling", systemImage: "wifi").foregroundStyle(wifi)
            Spacer()
            Label("VoIP", systemImage: "iphone.gen3.radiowaves.left.and.right").foregroundStyle(voip)
        }
    }

    var body: some View {
        NavigationStack {
            List(selection: $selection) {
                if !phone.ready {
                    Label(phone.connected ? "节点上的模组未就绪" : "未连接通话服务", systemImage: "exclamationmark.triangle")
                        .foregroundStyle(.orange)
                }
                if let e = phone.error {
                    ErrorLabel(e)
                }
                callingStatus
                Section {
                    HStack {
                        TextField("号码", text: $number)
                            .keyboardType(.phonePad)
                            .font(.title2.monospacedDigit())
                            .focused($editing)
                        // Empty: paste (while editing). With text: dial.
                        // A fixed slot keeps the row height the same when the field gains focus (PasteButton is taller).
                        ZStack {
                            if !number.isEmpty {
                                Button("拨打", systemImage: "phone.fill") { editing = false; confirm = number }
                                    .labelStyle(.iconOnly)
                                    .buttonStyle(.borderedProminent).tint(.green)
                                    .disabled(!ComposeDraft.isNumber(number) || !phone.ready || phone.inCall)
                            } else if editing {
                                // PasteButton draws its own filled background and ignores the plain button style.
                                // The icon is drawn here; the PasteButton on top is invisible but still takes the tap
                                // (it stays the system paste control, so no permission alert).
                                ZStack {
                                    Image(systemName: "doc.on.clipboard").font(.title3).foregroundStyle(.tint)
                                    PasteButton(payloadType: String.self) { s in
                                        Task { @MainActor in number = Self.dialable(s.first ?? "") }
                                    }
                                    .labelStyle(.iconOnly)
                                    .tint(.clear)
                                    .opacity(0.02)
                                }
                            }
                        }
                        .frame(width: 44, height: 34)
                        // The system contacts picker runs out of process, so it needs no contacts permission.
                        Button {
                            PhoneNumberPicker.present { number = Self.dialable($0) }
                        } label: {
                            Image(systemName: "person.crop.circle").font(.title2)
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(.tint)
                        .accessibilityLabel("从通讯录选择")
                    }
                    .onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: { fieldFrame = $0 }
                } footer: {
                    Text((phone.path == "wifi" ? "通话经节点的 Wi-Fi Calling（运营商 IMS）接通，按 SIM 卡资费计费。"
                                                : "通话经节点上的 4G 模组接通，按 SIM 卡资费计费。")
                         + (phone.voipStatus == .active ? "后台来电通知已启用，接听时需能连接节点。" : "仅在 App 打开时可以接听来电。"))
                }
                Section("通话记录") {
                    if let e = store.callsError {
                        ErrorLabel(e)
                    }
                    Picker("方向", selection: $box) {
                        Text("全部").tag("")
                        Text("拨打").tag("out")
                        Text("来电").tag("in")
                    }
                    .pickerStyle(.segmented)
                    .listRowInsets(EdgeInsets(top: 8, leading: 16, bottom: 8, trailing: 16))
                    ForEach(shown) { c in
                        Group {
                            if editMode.isEditing {
                                CallRow(call: c)
                            } else {
                                Button { if ComposeDraft.isNumber(c.number) { confirm = c.number } } label: { CallRow(call: c) }
                                    .tint(.primary)
                                    .contextMenu {
                                        if ComposeDraft.isNumber(c.number) {
                                            Button("拨打", systemImage: "phone") { Task { await phone.dial(c.number) } }   // an explicit menu choice: no confirmation
                                                .disabled(!phone.ready || phone.inCall)
                                            Button("发短信", systemImage: "message") { compose = ComposeDraft(to: c.number) }
                                            Button("复制号码", systemImage: "doc.on.doc") { UIPasteboard.general.string = c.number }
                                        }
                                    }
                            }
                        }
                        .tag(c.id)
                        .swipeActions(edge: .trailing) {
                            Button("删除", systemImage: "trash", role: .destructive) {
                                Task { await store.deleteCalls([c.id]) }
                            }
                        }
                    }
                }
            }
            .overlay {
                if store.host.isEmpty {
                    ContentUnavailableView("未连接API", systemImage: "network.slash")
                        .foregroundStyle(.secondary).background(.background)
                }
            }
            // A tap anywhere outside the number row hides the keyboard. It would eat the row taps
            // of the edit-mode selection, so it is off while selecting. Both sides use global coordinates:
            // a named space on the List mixes scroll-content and frame coordinates once the list scrolls.
            .simultaneousGesture(SpatialTapGesture(coordinateSpace: .global).onEnded {
                if !fieldFrame.contains($0.location) { editing = false }
            }, including: editMode.isEditing ? .none : .all)
            .scrollDismissesKeyboard(.immediately)
            .navigationTitle("电话")
            .environment(\.editMode, $editMode)
            .toolbar { toolbar }
            .refreshable {
                await store.refreshCalls()
                await phone.refreshVoIPStatus(host: store.host)
            }
            .task(id: store.host) {
                while !Task.isCancelled {
                    await store.refreshCalls()
                    await phone.refreshVoIPStatus(host: store.host)
                    try? await Task.sleep(for: .seconds(30))
                }
            }
            .onChange(of: phone.ended) {
                Task {
                    await store.refreshCalls()
                    await phone.refreshVoIPStatus(host: store.host)
                }
            }
            .onChange(of: box) { selection = [] }
            .onChange(of: store.selectedCard) { selection = []; editMode = .inactive }
            .confirmDial($confirm)
            .confirmationDialog("确定要删除所选的 \(pendingDelete.count) 条通话记录吗？此操作不可撤销。",
                                isPresented: .init(get: { !pendingDelete.isEmpty }, set: { if !$0 { pendingDelete = [] } }),
                                titleVisibility: .visible) {
                Button("删除", role: .destructive) {
                    let ids = pendingDelete
                    Task {
                        await store.deleteCalls(ids)
                        selection = []
                        editMode = .inactive
                    }
                }
            }
            .sheet(item: $compose) { ComposeView(draft: $0) }
        }
    }

    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
        ToolbarItemGroup(placement: .topBarLeading) {
            SIMCardMenu()
        }
        ToolbarItemGroup(placement: .topBarTrailing) {
            Button(editMode.isEditing ? "完成" : "选择", systemImage: editMode.isEditing ? "checkmark" : "checkmark.circle") {
                selection = []
                editing = false
                editMode = editMode.isEditing ? .inactive : .active
            }
            .labelStyle(.iconOnly)
            if editMode.isEditing {
                Button("删除", systemImage: "trash", role: .destructive) { pendingDelete = Array(selection) }
                    .labelStyle(.iconOnly)
                    .disabled(selection.isEmpty)
            }
        }
    }
}

extension View {
    /// Asks before it dials `number` through the node; clears it when done.
    func confirmDial(_ number: Binding<String?>) -> some View { modifier(ConfirmDial(number: number)) }
}

private struct ConfirmDial: ViewModifier {
    @Environment(CallSession.self) private var phone
    @Binding var number: String?

    func body(content: Content) -> some View {
        content.confirmationDialog("拨打 \(number ?? "")？", isPresented: .init(get: { number != nil }, set: { if !$0 { number = nil } }),
                                   titleVisibility: .visible) {
            Button("拨打") {
                let n = number ?? ""
                Task { await phone.dial(n) }
            }
        }
    }
}

extension PhoneView {
    /// Keeps a leading + and the digits of pasted text, e.g. "+1 (415) 555-0100" -> "+14155550100".
    static func dialable(_ s: String) -> String {
        let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
        return (t.hasPrefix("+") ? "+" : "") + t.filter(\.isASCII).filter(\.isNumber)
    }
}

private struct CallRow: View {
    var call: CallRecord

    var body: some View {
        HStack {
            Image(systemName: call.incoming ? "phone.arrow.down.left" : "phone.arrow.up.right")
                .foregroundStyle(call.missed ? .red : .secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text(call.number.isEmpty ? "未知号码" : call.number).font(.headline).foregroundStyle(call.missed ? .red : .primary)
                Text(call.answered ? Fmt.clock(call.duration) : call.incoming ? "未接" : "未接通")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Text(call.date, format: .relative(presentation: .named)).font(.caption).foregroundStyle(.secondary)
        }
        .contentShape(Rectangle())
    }
}

/// Full-screen view while a call is in progress.
struct CallScreen: View {
    @Environment(CallSession.self) private var phone
    @State private var keypad = false
    @State private var digits = ""

    var body: some View {
        @Bindable var phone = phone
        VStack(spacing: 24) {
            Spacer()
            Text(phone.number.flatMap { $0.isEmpty ? nil : $0 } ?? "未知号码").font(.largeTitle.weight(.semibold))
            status.font(.title3).foregroundStyle(.secondary)
            if phone.path == "wifi" {
                Label("Wi-Fi Calling", systemImage: "wifi").font(.footnote).foregroundStyle(.secondary)
            }
            if let e = phone.error { ErrorLabel(e).font(.footnote).padding(.horizontal) }
            Spacer()
            if keypad {
                Text(digits).font(.title2.monospacedDigit()).frame(minHeight: 30)
                Keypad { d in digits += d; phone.dtmf(d) }
            }
            if phone.state == "incoming" {
                HStack(spacing: 80) {
                    RoundButton(icon: "phone.down.fill", color: .red) { phone.hangup() }
                    RoundButton(icon: "phone.fill", color: .green) { phone.answer() }
                }
            } else {
                if phone.owner {
                    HStack(spacing: 40) {
                        Toggle("静音", systemImage: phone.muted ? "mic.slash.fill" : "mic.fill",
                               isOn: .init(get: { phone.muted }, set: { phone.setMuted($0) }))
                        Toggle("拨号键盘", systemImage: "circle.grid.3x3.fill", isOn: $keypad)
                            .disabled(phone.state != "active")
                        Toggle("扬声器", systemImage: "speaker.wave.3.fill", isOn: $phone.speaker)
                    }
                    .toggleStyle(.button).labelStyle(.iconOnly).font(.title2)
                }
                RoundButton(icon: "phone.down.fill", color: .red) { phone.hangup() }
            }
        }
        .padding(.bottom, 40)
        .frame(maxWidth: .infinity)
        .background(.background)
    }

    @ViewBuilder private var status: some View {
        switch phone.state {
        case "dialing": Text("正在拨号…")
        case "alerting": Text("对方振铃…")
        case "incoming": Text("来电")
        default:
            if !phone.owner {
                Text("通话中（其他设备）")
            } else if let t = phone.activeSince {
                TimelineView(.periodic(from: .now, by: 1)) { ctx in
                    Text(Fmt.clock(Int(ctx.date.timeIntervalSince(t)))).monospacedDigit()
                }
            } else {
                Text("通话中")
            }
        }
    }
}

private struct RoundButton: View {
    var icon: String
    var color: Color
    var action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: icon).font(.title).foregroundStyle(.white)
                .frame(width: 72, height: 72).background(color, in: .circle)
        }
    }
}

private struct Keypad: View {
    var tap: (String) -> Void
    private let keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#"]

    var body: some View {
        LazyVGrid(columns: Array(repeating: GridItem(.fixed(72), spacing: 24), count: 3), spacing: 16) {
            ForEach(keys, id: \.self) { k in
                Button { tap(k) } label: {
                    Text(k).font(.title.monospacedDigit()).frame(width: 72, height: 72)
                        .background(.quaternary, in: .circle)
                }
                .tint(.primary)
            }
        }
    }
}

/// Shows the system contacts picker and returns the phone number the user taps. No contacts permission is needed.
@MainActor
final class PhoneNumberPicker: NSObject {
    private static var current: PhoneNumberPicker?
    private let onPick: (String) -> Void

    private init(onPick: @escaping (String) -> Void) {
        self.onPick = onPick
    }

    static func present(onPick: @escaping (String) -> Void) {
        guard let top = topViewController() else { return }
        let delegate = PhoneNumberPicker(onPick: onPick)
        current = delegate
        let picker = CNContactPickerViewController()
        picker.delegate = delegate
        picker.predicateForEnablingContact = NSPredicate(format: "phoneNumbers.@count > 0")
        picker.predicateForSelectionOfProperty = NSPredicate(format: "key == 'phoneNumbers'")
        top.present(picker, animated: true)
    }

    private func finish(_ number: String?) {
        if let number { onPick(number) }
        Self.current = nil
    }

    private static func topViewController() -> UIViewController? {
        let scene = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
            .first { $0.activationState == .foregroundActive }
        var vc = scene?.windows.first(where: \.isKeyWindow)?.rootViewController
        while let presented = vc?.presentedViewController { vc = presented }
        return vc
    }
}

// The picker calls back on the main thread.
extension PhoneNumberPicker: @preconcurrency CNContactPickerDelegate {
    func contactPicker(_ picker: CNContactPickerViewController, didSelect contactProperty: CNContactProperty) {
        finish((contactProperty.value as? CNPhoneNumber)?.stringValue)
    }

    func contactPickerDidCancel(_ picker: CNContactPickerViewController) {
        finish(nil)
    }
}
