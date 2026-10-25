import BackgroundTasks
import Foundation
import UserNotifications

// New SMS notifications and the background check.
enum SMSWatch {
    static let taskID = "net.janlay.SIMAnywhere.smscheck"
    private static let foreground = Foreground()

    /// Call once at launch.
    static func register() {
        UNUserNotificationCenter.current().delegate = foreground
        BGTaskScheduler.shared.register(forTaskWithIdentifier: taskID, using: nil) { task in
            guard let task = task as? BGAppRefreshTask else { return }
            schedule()
            nonisolated(unsafe) let done = task
            let work = Task { @MainActor in
                let host = UserDefaults.standard.string(forKey: "host") ?? Store.defaultHost
                if let api = DongleAPI(host: host), let list = try? await api.sms() {
                    await check(list, host: host)
                }
                done.setTaskCompleted(success: true)
            }
            task.expirationHandler = { work.cancel() }
        }
    }

    /// Asks iOS for the next background check, 15 min at the earliest.
    static func schedule() {
        let request = BGAppRefreshTaskRequest(identifier: taskID)
        request.earliestBeginDate = Date(timeIntervalSinceNow: 15 * 60)
        try? BGTaskScheduler.shared.submit(request)
    }

    /// Notifies for each received SMS that the previous check did not see.
    static func check(_ list: SMSList, host: String) async {
        let key = "knownIncoming." + host
        let defaults = UserDefaults.standard
        let incoming = list.messages.filter(\.incoming)
        guard let knownIDs = defaults.stringArray(forKey: key) else {
            defaults.set(incoming.map(\.id), forKey: key)
            return  // First run: store the baseline only.
        }
        let known = Set(knownIDs)
        let new = incoming.filter { !known.contains($0.id) }
        defaults.set(knownIDs + new.map(\.id), forKey: key)
        guard !new.isEmpty else { return }

        let center = UNUserNotificationCenter.current()
        guard (try? await center.requestAuthorization(options: [.alert, .sound])) == true else { return }
        for m in new {
            let content = UNMutableNotificationContent()
            content.title = m.number
            content.body = m.text
            content.sound = .default
            try? await center.add(UNNotificationRequest(identifier: m.id, content: content, trigger: nil))
        }
    }
}

// Shows banners while the app is open. Without it, iOS hides notifications in the foreground.
private final class Foreground: NSObject, UNUserNotificationCenterDelegate, Sendable {
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        [.banner, .sound]
    }
}

// Links and verification codes in SMS text.
enum SMSText {
    static let linksKey = "smsLinks"
    static let codesKey = "smsCodes"

    /// The text with each URL set as a link.
    static func linked(_ text: String) -> AttributedString {
        var result = AttributedString(text)
        guard let detector = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue) else { return result }
        for match in detector.matches(in: text, range: NSRange(text.startIndex..., in: text)) {
            if let url = match.url, let range = Range(match.range, in: result) {
                result[range].link = url
            }
        }
        return result
    }

    /// The first 4–8 digit number that stands next to a code keyword.
    static func code(in text: String) -> String? {
        let after = /(?:验证码|校验码|动态码|确认码|验证代码|code|otp)[^\d]{0,12}(\d{4,8})(?!\d)/.ignoresCase()
        let before = /(?:^|[^\d])(\d{4,8})(?!\d)[^\d]{0,12}(?:验证码|校验码|动态码|确认码|验证代码|code|otp)/.ignoresCase()
        return (text.firstMatch(of: after) ?? text.firstMatch(of: before)).map { String($0.output.1) }
    }
}
