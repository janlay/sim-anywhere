import AVFoundation
import CallKit
import Foundation

// Voice calls through sim-anywhere-calld (ws://<host>:8765/call, or wss://<host>/call through an https proxy;
// spec in /sim-anywhere/openapi.json).
// CallKit shows the system call UI and owns the audio session. The node is the only source of call state.

private struct CallEvent: Decodable {
    var type: String
    var ready: Bool?
    var state: String?
    var dir: String?
    var number: String?
    var active_since: Int?
    var owner: Bool?
    var reason: String?
    var error: String?
    var path: String?
    var wifi: String?
    var call_id: String?
}

@MainActor @Observable
final class CallSession: NSObject {
    /// The WebSocket is open.
    var connected = false
    /// The node has the modem AT port open.
    var ready = false
    /// idle, dialing, alerting, incoming or active
    var state = "idle"
    var incoming = false
    var number: String?
    var activeSince: Date?
    /// This app carries the audio (it dialed or answered).
    var owner = false
    var reason: String?
    var error: String?
    /// Voice path of the current call, or of the next call while idle: wifi or modem. Nil from a node without Wi-Fi Calling.
    var path: String?
    /// Wi-Fi Calling registration on the node: registered, connecting, error or off. Nil from a node without Wi-Fi Calling.
    var wifi: String?
    var voipStatus: VoIPStatus = .ineffective
    /// Counts ended calls, so views can reload the call log.
    var ended = 0
    private(set) var muted = false
    var speaker = false { didSet { audio.speaker = speaker } }

    var inCall: Bool { state != "idle" }

    private var url: URL?
    private var active = false
    private var want = false
    private var awaitingPush: UUID?
    private var pendingDecline: UUID?
    private var declineSent = false
    private var callID: String?
    private var task: URLSessionWebSocketTask?
    private var loop: Task<Void, Never>?
    private let audio = CallAudio()

    private let provider: CXProvider
    private let controller = CXCallController()
    /// The CallKit call for the node call, if any.
    private var uuid: UUID?
    /// CallKit has activated the audio session.
    private var sessionActive = false
    /// The local user ended the call (CallKit already knows).
    private var endedHere = false
    private var push: VoIPPushRegistry?

    override init() {
        let config = CXProviderConfiguration()
        config.supportsVideo = false
        config.maximumCallGroups = 1
        config.maximumCallsPerCallGroup = 1
        config.supportedHandleTypes = [.phoneNumber]
        config.includesCallsInRecents = false   // a Recents entry could not start a call through the node
        provider = CXProvider(configuration: config)
        super.init()
        provider.setDelegate(self, queue: .main)
        push = VoIPPushRegistry(calls: self)
    }

    /// Keeps the WebSocket open while the app is in the foreground or a call is in progress.
    func update(host: String, active: Bool) {
        push?.sync(host: host)
        let u = DongleAPI(host: host)?.callURL
        if u != url { voipStatus = .ineffective }
        self.active = active
        want = active || inCall || awaitingPush != nil || pendingDecline != nil
        if u != url {
            url = u
            task?.cancel(with: .goingAway, reason: nil)
        }
        if !want {
            task?.cancel(with: .goingAway, reason: nil)
        } else if loop == nil {
            loop = Task { await run() }
        }
    }

    func refreshVoIPStatus(host: String) async {
        guard let api = DongleAPI(host: host), let push,
              let bundleID = Bundle.main.bundleIdentifier else {
            voipStatus = .ineffective
            return
        }
        let status = (try? await api.voipStatus(device: push.statusDeviceID,
                                               bundleID: bundleID,
                                               environment: push.statusEnvironment)) ?? .ineffective
        if api.callURL == url { voipStatus = status }
    }

    private func run() async {
        while want, let url {
            let t = URLSession.shared.webSocketTask(with: url)
            task = t
            t.resume()
            do {
                while true {
                    switch try await t.receive() {
                    case let .string(s): handle(s)
                    case let .data(d): audio.play(d)
                    @unknown default: break
                    }
                }
            } catch {}
            if task === t { task = nil }
            connected = false
            ready = false
            wifi = nil
            if (inCall || uuid != nil) && awaitingPush == nil && pendingDecline == nil {
                apply(CallEvent(type: "state", state: "idle", reason: "连接中断"))
            }
            if want { try? await Task.sleep(for: .seconds(3)) }
        }
        loop = nil
    }

    private func handle(_ s: String) {
        guard let e = try? JSONDecoder().decode(CallEvent.self, from: Data(s.utf8)) else { return }
        if e.type == "error" {
            error = e.error
            if state == "idle", let uuid {   // the dial was refused before any call existed
                provider.reportCall(with: uuid, endedAt: nil, reason: .failed)
                self.uuid = nil
            }
        } else if e.type == "state" {
            connected = true
            ready = e.ready ?? false
            wifi = e.wifi
            if let pendingDecline {
                if e.call_id == pendingDecline.uuidString.lowercased(), e.state == "incoming" {
                    if !declineSent {
                        declineSent = send(["op": "hangup", "call_id": pendingDecline.uuidString.lowercased()])
                    }
                    return
                }
                self.pendingDecline = nil
                self.awaitingPush = nil
                declineSent = false
                if e.state == "idle" {
                    want = active
                    if !want { task?.cancel(with: .goingAway, reason: nil) }
                    return
                }
            }
            if let awaitingPush {
                if e.call_id != awaitingPush.uuidString.lowercased() || e.state != "incoming" {
                    self.awaitingPush = nil
                    apply(CallEvent(type: "state", state: "idle", reason: "来电已结束"))
                    return
                }
                self.awaitingPush = nil
            }
            apply(e)
        }
    }

    private func apply(_ e: CallEvent) {
        let was = state
        if e.state == "incoming", let uuid, let nextID = e.call_id.flatMap(UUID.init(uuidString:)), uuid != nextID {
            provider.reportCall(with: uuid, endedAt: nil, reason: .remoteEnded)
            self.uuid = nil
        }
        state = e.state ?? "idle"
        incoming = e.dir == "in"
        number = e.number
        activeSince = e.active_since.map { Date(timeIntervalSince1970: TimeInterval($0)) }
        owner = e.owner ?? false
        reason = e.reason
        path = e.path
        callID = e.call_id
        report(was)
        updateAudio()
        if state == "idle" && was != "idle" {
            ended += 1
            muted = false
            audio.muted = false
            speaker = false
            want = active
            if !want { task?.cancel(with: .goingAway, reason: nil) }
        }
    }

    /// Tells CallKit about node state changes.
    private func report(_ was: String) {
        if state == "incoming" && uuid == nil {
            let id = eCallID ?? UUID()
            uuid = id
            endedHere = false
            provider.reportNewIncomingCall(with: id, update: callUpdate()) { [weak self] error in
                guard error != nil else { return }
                Task { @MainActor in if self?.uuid == id { self?.uuid = nil } }   // e.g. Do Not Disturb: the node keeps ringing
            }
            return
        }
        guard let uuid else { return }
        if state == "incoming" && was == "incoming" {
            provider.reportCall(with: uuid, updated: callUpdate())
        }
        if state == "idle" {
            if !endedHere {
                let r: CXCallEndedReason = switch reason {
                case "busy", "no answer": .unanswered
                case "no carrier", "hangup", "ended": .remoteEnded
                default: .failed
                }
                provider.reportCall(with: uuid, endedAt: nil, reason: r)
            }
            self.uuid = nil
        } else if state == "active" && !owner {
            provider.reportCall(with: uuid, endedAt: nil, reason: .answeredElsewhere)
            self.uuid = nil
        } else if state == "active" && was != "active" && !incoming {
            provider.reportOutgoingCall(with: uuid, connectedAt: activeSince)
        } else if state == "dialing" || state == "alerting" {
            provider.reportCall(with: uuid, updated: callUpdate())
        }
    }

    private var eCallID: UUID? { callID.flatMap(UUID.init(uuidString:)) }

    func receiveVoIPPush(id: UUID, completion: @escaping () -> Void) {
        if uuid == id { completion(); return }
        nonisolated(unsafe) let done = completion
        guard uuid == nil else {
            provider.reportNewIncomingCall(with: id, update: callUpdate()) { [weak self] error in
                done()
                if error == nil {
                    Task { @MainActor in self?.provider.reportCall(with: id, endedAt: nil, reason: .failed) }
                }
            }
            return
        }
        uuid = id
        awaitingPush = id
        state = "incoming"
        incoming = true
        number = nil
        endedHere = false
        provider.reportNewIncomingCall(with: id, update: callUpdate()) { [weak self] error in
            done()
            if let error {
                Task { @MainActor in
                    guard self?.uuid == id else { return }
                    self?.error = error.localizedDescription
                    self?.uuid = nil
                    self?.awaitingPush = nil
                    self?.state = "idle"
                    self?.incoming = false
                }
            }
        }
        update(host: UserDefaults.standard.string(forKey: "host") ?? "", active: active)
        Task {
            try? await Task.sleep(for: .seconds(30))
            guard awaitingPush == id else { return }
            provider.reportCall(with: id, endedAt: nil, reason: .failed)
            uuid = nil
            awaitingPush = nil
            pendingDecline = nil
            declineSent = false
            state = "idle"
            want = active
            if !want { task?.cancel(with: .goingAway, reason: nil) }
        }
    }

    private func callUpdate() -> CXCallUpdate {
        let u = CXCallUpdate()
        u.remoteHandle = CXHandle(type: .phoneNumber, value: number ?? "")
        u.localizedCallerName = (number?.isEmpty ?? true) ? "未知号码" : nil
        u.hasVideo = false
        u.supportsDTMF = true
        u.supportsHolding = false
        u.supportsGrouping = false
        u.supportsUngrouping = false
        return u
    }

    /// Audio runs from alerting (ringback, network announcements) to the end of the call,
    /// while this app owns the call and CallKit has activated the session.
    private func updateAudio() {
        if (state == "alerting" || state == "active") && owner && sessionActive, let t = task {
            do { try audio.start { t.send(.data($0)) { _ in } } } catch { self.error = "音频启动失败：\(error.localizedDescription)" }
        } else {
            audio.stop()
        }
    }

    // MARK: - Operations (through CallKit, so the system UI stays in step)

    func dial(_ number: String) async {
        guard connected else { error = "未连接通话服务"; return }
        guard await microphone() else { return }
        request(CXStartCallAction(call: UUID(), handle: CXHandle(type: .phoneNumber, value: number)))
    }

    func answer() {
        if let uuid { request(CXAnswerCallAction(call: uuid)) }
    }

    func hangup() {
        if let uuid { request(CXEndCallAction(call: uuid)) } else { send(["op": "hangup"]) }
    }

    func setMuted(_ on: Bool) {
        if let uuid { request(CXSetMutedCallAction(call: uuid, muted: on)) }
    }

    /// In-app keypad. The system keypad goes through CXPlayDTMFCallAction.
    func dtmf(_ digits: String) {
        if send(["op": "dtmf", "digits": digits]) { digits.forEach { audio.tone($0) } }
    }

    private func request(_ action: CXCallAction) {
        controller.request(CXTransaction(action: action)) { [weak self] error in
            guard let error else { return }
            Task { @MainActor in self?.error = "CallKit：\(error.localizedDescription)" }
        }
    }

    private func microphone() async -> Bool {
        if await AVAudioApplication.requestRecordPermission() { return true }
        error = "未获得麦克风权限。请在系统设置中允许。"
        return false
    }

    func prepareMicrophone() async {
        _ = await AVAudioApplication.requestRecordPermission()
    }

    private func waitForIncoming(_ id: UUID) async -> Bool {
        for _ in 0..<150 {
            if connected, state == "incoming", uuid == id,
               callID == nil || callID == id.uuidString.lowercased() { return true }
            if uuid != id || state == "idle" { return false }
            try? await Task.sleep(for: .milliseconds(100))
        }
        return false
    }

    @discardableResult
    private func send(_ m: [String: String]) -> Bool {
        guard let task, connected, let d = try? JSONEncoder().encode(m) else {
            error = "未连接通话服务"
            return false
        }
        error = nil
        task.send(.string(String(decoding: d, as: UTF8.self))) { _ in }
        return true
    }
}

extension CallSession: @preconcurrency CXProviderDelegate {
    func providerDidReset(_ provider: CXProvider) {
        if inCall { send(["op": "hangup"]) }
        uuid = nil
        sessionActive = false
        audio.stop()
    }

    func provider(_ provider: CXProvider, perform action: CXStartCallAction) {
        guard state == "idle", send(["op": "dial", "number": action.handle.value]) else { return action.fail() }
        uuid = action.callUUID
        endedHere = false
        CallAudio.configure()
        provider.reportOutgoingCall(with: action.callUUID, startedConnectingAt: nil)
        action.fulfill()
    }

    func provider(_ provider: CXProvider, perform action: CXAnswerCallAction) {
        Task {
            guard await waitForIncoming(action.callUUID), await microphone() else { return action.fail() }
            var message = ["op": "answer"]
            if let callID { message["call_id"] = callID }
            guard send(message) else { return action.fail() }
            CallAudio.configure()
            action.fulfill()
        }
    }

    func provider(_ provider: CXProvider, perform action: CXEndCallAction) {
        endedHere = true
        if awaitingPush == action.callUUID, !connected {
            pendingDecline = action.callUUID
            declineSent = false
            state = "idle"
            incoming = false
            uuid = nil
        } else {
            var message = ["op": "hangup"]
            if let callID { message["call_id"] = callID }
            send(message)
        }
        action.fulfill()
    }

    func provider(_ provider: CXProvider, perform action: CXSetMutedCallAction) {
        muted = action.isMuted
        audio.muted = action.isMuted
        action.fulfill()
    }

    func provider(_ provider: CXProvider, perform action: CXPlayDTMFCallAction) {
        guard send(["op": "dtmf", "digits": action.digits]) else { return action.fail() }
        action.digits.forEach { audio.tone($0) }
        action.fulfill()
    }

    func provider(_ provider: CXProvider, perform action: CXSetHeldCallAction) {
        action.fail()   // the node has no hold
    }

    func provider(_ provider: CXProvider, didActivate audioSession: AVAudioSession) {
        sessionActive = true
        updateAudio()
    }

    func provider(_ provider: CXProvider, didDeactivate audioSession: AVAudioSession) {
        sessionActive = false
        updateAudio()
    }
}

/// Microphone and speaker for one call. The wire format is PCM S16LE, 8000 Hz, mono, 320 bytes per frame.
final class CallAudio: @unchecked Sendable {
    // Created at the first call, not at launch: the engine connects to the audio server.
    private lazy var player = AVAudioPlayerNode()
    /// Local keypad tones, on their own node so they do not wait behind the downlink queue.
    private lazy var tonePlayer = AVAudioPlayerNode()
    private lazy var engine: AVAudioEngine = {
        let e = AVAudioEngine()
        e.attach(player)
        e.attach(tonePlayer)
        // Turning voice processing on changes the I/O configuration. iOS then posts this notification and
        // stops the engine, often just after start(). Without a restart the microphone tap gets no buffers.
        NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: e, queue: .main) { [weak self] _ in
            self?.restart()
        }
        return e
    }()
    private let wire = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 8000, channels: 1, interleaved: true)!
    private let output = AVAudioFormat(standardFormatWithSampleRate: 8000, channels: 1)!
    private let lock = NSLock()
    private var running = false
    private var queued = 0
    /// Downlink buffers held after an underrun until `prebuffer` frames are there (a small jitter buffer).
    private var hold: [AVAudioPCMBuffer] = []
    private var pending = Data()
    private var converter: AVAudioConverter?
    private var sender: (@Sendable (Data) -> Void)?
    private var _muted = false

    static let frame = 320
    /// Downlink buffers waiting to play. Past this (300 ms), new frames drop to keep the delay low.
    private static let maxQueued = 15
    /// After the queue runs empty, playback starts again only with 3 frames (60 ms) in hand. With 1 frame,
    /// network jitter emptied the queue again and again, and the sound fluttered (2026-10-21).
    private static let prebuffer = 3

    var muted: Bool {
        get { lock.withLock { _muted } }
        set { lock.withLock { _muted = newValue } }
    }

    var speaker = false {
        didSet { try? AVAudioSession.sharedInstance().overrideOutputAudioPort(speaker ? .speaker : .none) }
    }

    /// Sets the session category before CallKit activates the session.
    static func configure() {
        try? AVAudioSession.sharedInstance().setCategory(.playAndRecord, mode: .voiceChat, options: [.allowBluetoothHFP])
    }

    /// Call after CallKit has activated the audio session.
    func start(send: @escaping @Sendable (Data) -> Void) throws {
        if lock.withLock({ running }) { return }
        let input = engine.inputNode
        try input.setVoiceProcessingEnabled(true)      // echo cancellation
        engine.connect(player, to: engine.mainMixerNode, format: output)
        engine.connect(tonePlayer, to: engine.mainMixerNode, format: output)
        lock.withLock {
            converter = nil
            sender = send
            pending = Data()
            queued = 0
            hold = []
            running = true
        }
        // nil: the format the engine uses at run time. Voice processing can change it after start.
        input.installTap(onBus: 0, bufferSize: 1024, format: nil) { [weak self] buf, _ in self?.captured(buf) }
        do {
            try engine.start()
        } catch {
            stop()
            throw error
        }
        player.play()
        tonePlayer.play()
    }

    /// Starts the engine again after a configuration change, while a call needs audio.
    private func restart() {
        guard lock.withLock({ running }), !engine.isRunning else { return }
        // Buffers scheduled while the engine was stopped never call their completion handlers.
        // Without this reset the queue count stays at the limit and every later downlink frame drops.
        lock.withLock { queued = 0; hold = [] }
        // The configuration change also breaks the player -> mixer -> output path: the player then reports
        // isPlaying but renders nothing (diag on 2026-10-21: 15 scheduled, 0 played). Connect it again.
        player.stop()
        tonePlayer.stop()
        engine.connect(player, to: engine.mainMixerNode, format: output)
        engine.connect(tonePlayer, to: engine.mainMixerNode, format: output)
        engine.connect(engine.mainMixerNode, to: engine.outputNode, format: nil)
        do {
            try engine.start()
            player.play()
            tonePlayer.play()
        } catch {}
    }

    func stop() {
        guard lock.withLock({ let r = running; running = false; sender = nil; return r }) else { return }
        engine.inputNode.removeTap(onBus: 0)
        player.stop()
        tonePlayer.stop()
        engine.stop()
    }

    /// Plays the keypad tone of `digit` locally (120 ms, the two DTMF frequencies), as the Phone app does.
    func tone(_ digit: Character) {
        let rows: [Character: Double] = ["1": 697, "2": 697, "3": 697, "A": 697, "4": 770, "5": 770, "6": 770, "B": 770,
                                         "7": 852, "8": 852, "9": 852, "C": 852, "*": 941, "0": 941, "#": 941, "D": 941]
        let cols: [Character: Double] = ["1": 1209, "4": 1209, "7": 1209, "*": 1209, "2": 1336, "5": 1336, "8": 1336, "0": 1336,
                                         "3": 1477, "6": 1477, "9": 1477, "#": 1477, "A": 1633, "B": 1633, "C": 1633, "D": 1633]
        guard let lo = rows[digit], let hi = cols[digit], lock.withLock({ running }) else { return }
        let n = 960                                    // 120 ms at 8 kHz
        guard let buf = AVAudioPCMBuffer(pcmFormat: output, frameCapacity: AVAudioFrameCount(n)),
              let ch = buf.floatChannelData?[0] else { return }
        buf.frameLength = AVAudioFrameCount(n)
        for i in 0..<n {
            let t = Double(i) / 8000
            let ramp = min(1, Double(min(i, n - i)) / 80)   // 10 ms fade in and out: no clicks
            ch[i] = Float(0.15 * ramp * (sin(2 * .pi * lo * t) + sin(2 * .pi * hi * t)))
        }
        tonePlayer.scheduleBuffer(buf, at: nil, options: .interrupts)
    }

    /// Microphone buffer (hardware format) -> 8 kHz frames to the node.
    private func captured(_ buf: AVAudioPCMBuffer) {
        guard let (converter, send, muted) = lock.withLock({ () -> (AVAudioConverter, @Sendable (Data) -> Void, Bool)? in
            guard running, let s = sender else { return nil }
            if converter?.inputFormat != buf.format { converter = AVAudioConverter(from: buf.format, to: wire) }
            guard let c = converter else { return nil }
            return (c, s, _muted)
        }) else { return }
        let cap = AVAudioFrameCount(Double(buf.frameLength) * wire.sampleRate / buf.format.sampleRate) + 16
        guard let out = AVAudioPCMBuffer(pcmFormat: wire, frameCapacity: cap) else { return }
        // The input block runs synchronously inside convert, once per call.
        nonisolated(unsafe) var fed = false
        nonisolated(unsafe) let input = buf
        converter.convert(to: out, error: nil) { _, status in
            if fed { status.pointee = .noDataNow; return nil }
            fed = true
            status.pointee = .haveData
            return input
        }
        guard let samples = out.int16ChannelData?[0], out.frameLength > 0 else { return }
        let bytes = muted ? Data(count: Int(out.frameLength) * 2) : Data(bytes: samples, count: Int(out.frameLength) * 2)
        pending.append(bytes)
        while pending.count >= Self.frame {
            send(pending.prefix(Self.frame))
            pending.removeFirst(Self.frame)
        }
    }

    /// 8 kHz frame from the node -> speaker.
    func play(_ d: Data) {
        let n = d.count / 2
        guard n > 0, lock.withLock({ running && queued < Self.maxQueued }),
              let buf = AVAudioPCMBuffer(pcmFormat: output, frameCapacity: AVAudioFrameCount(n)),
              let ch = buf.floatChannelData?[0] else { return }
        buf.frameLength = AVAudioFrameCount(n)
        d.withUnsafeBytes { raw in
            for i in 0..<n { ch[i] = Float(Int16(littleEndian: raw.loadUnaligned(fromByteOffset: i * 2, as: Int16.self))) / 32768 }
        }
        let ready: [AVAudioPCMBuffer] = lock.withLock {
            if queued > 0 { queued += 1; return [buf] }
            hold.append(buf)                            // queue empty: wait for a few frames first
            guard hold.count >= Self.prebuffer else { return [] }
            let h = hold
            hold = []
            queued += h.count
            return h
        }
        for b in ready {
            player.scheduleBuffer(b) { [weak self] in
                guard let self else { return }
                lock.withLock { queued = max(0, queued - 1) }
            }
        }
    }
}
