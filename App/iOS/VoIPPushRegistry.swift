import Foundation
import PushKit
import UIKit

/// Registers the device's VoIP token and hands wake-up pushes to CallKit through CallSession.
@MainActor
final class VoIPPushRegistry: NSObject, @preconcurrency PKPushRegistryDelegate {
    private weak var calls: CallSession?
    private let registry: PKPushRegistry
    private let deviceID: String
    private var token: String?
    private var host: String

    static let nameKey = "phoneName"

    var statusDeviceID: String { deviceID }
    var statusEnvironment: String { environment }

    init(calls: CallSession) {
        self.calls = calls
        let defaults = UserDefaults.standard
        deviceID = defaults.string(forKey: "voipDeviceID") ?? UUID().uuidString.lowercased()
        defaults.set(deviceID, forKey: "voipDeviceID")
        token = defaults.string(forKey: "voipToken")
        host = defaults.string(forKey: "host") ?? ""
        registry = PKPushRegistry(queue: .main)
        super.init()
        registry.delegate = self
        registry.desiredPushTypes = [.voIP]
        sync(host: host)
    }

    func sync(host newHost: String) {
        if host != newHost {
            if let old = DongleAPI(host: host) {
                Task { try? await old.unregisterVoIPDevice(deviceID) }
            }
            host = newHost
        }
        guard let token, let api = DongleAPI(host: host) else { return }
        Task {
            let info = Bundle.main.infoDictionary ?? [:]
            try? await api.registerVoIPDevice(deviceID, token: token, environment: environment, metadata: [
                "bundle_id": Bundle.main.bundleIdentifier ?? "",
                "team_id": info["SigningTeamID"] as? String ?? "",
                "name": Self.displayName,
                "model": UIDevice.current.model,
                "os_version": UIDevice.current.systemVersion,
                "app_version": info["CFBundleShortVersionString"] as? String ?? "",
                "app_build": info["CFBundleVersion"] as? String ?? "",
            ])
            await calls?.refreshVoIPStatus(host: host)
        }
    }

    func pushRegistry(_ registry: PKPushRegistry, didUpdate pushCredentials: PKPushCredentials, for type: PKPushType) {
        guard type == .voIP else { return }
        token = pushCredentials.token.map { String($0, radix: 16).leftPadded() }.joined()
        UserDefaults.standard.set(token, forKey: "voipToken")
        sync(host: host)
    }

    func pushRegistry(_ registry: PKPushRegistry, didInvalidatePushTokenFor type: PKPushType) {
        guard type == .voIP else { return }
        token = nil
        UserDefaults.standard.removeObject(forKey: "voipToken")
        if let api = DongleAPI(host: host) {
            Task { try? await api.unregisterVoIPDevice(deviceID) }
        }
    }

    func pushRegistry(_ registry: PKPushRegistry, didReceiveIncomingPushWith payload: PKPushPayload,
                      for type: PKPushType, completion: @escaping () -> Void) {
        guard type == .voIP, let text = payload.dictionaryPayload["call_id"] as? String,
              let id = UUID(uuidString: text) else { completion(); return }
        guard let calls else { completion(); return }
        calls.receiveVoIPPush(id: id, completion: completion)
    }

    /// The name set in Settings. iOS 16+ returns a generic name from UIDevice.name without an entitlement.
    static var displayName: String {
        let name = UserDefaults.standard.string(forKey: nameKey)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return name.isEmpty ? UIDevice.current.model : String(name.prefix(64))
    }

    private var environment: String {
        Bundle.main.object(forInfoDictionaryKey: "PushEnvironment") as? String ?? ""
    }
}

private extension String {
    func leftPadded() -> String { count == 1 ? "0" + self : self }
}
