// Distribute an uploaded build to a TestFlight group after processing completes.
// Usage: swift Scripts/testflight.swift <bundleId> <version> <build> <group> <whatToTestFile>
// Authentication: ASC_KEY_ID and ASC_ISSUER_ID, plus a local API key under ~/.appstoreconnect/private_keys/.
import CryptoKit
import Foundation

struct ASCError: Error, CustomStringConvertible {
    let description: String
}

let args = CommandLine.arguments
guard args.count == 6 else {
    print("usage: testflight.swift <bundleId> <version> <build> <group> <whatToTestFile>")
    exit(2)
}
let (bundleId, version, buildNumber, groupName) = (args[1], args[2], args[3], args[4])
let whatToTest = try String(contentsOfFile: args[5], encoding: .utf8)
    .trimmingCharacters(in: .whitespacesAndNewlines)

let env = ProcessInfo.processInfo.environment
guard let keyId = env["ASC_KEY_ID"], let issuerId = env["ASC_ISSUER_ID"] else {
    print("Set ASC_KEY_ID and ASC_ISSUER_ID.")
    exit(1)
}

func base64URL(_ data: Data) -> String {
    data.base64EncodedString()
        .replacingOccurrences(of: "+", with: "-")
        .replacingOccurrences(of: "/", with: "_")
        .replacingOccurrences(of: "=", with: "")
}

/// Sign a new ES256 JWT for each request. Tokens expire after 20 minutes.
func token() throws -> String {
    let path = NSString(string: "~/.appstoreconnect/private_keys/AuthKey_\(keyId).p8").expandingTildeInPath
    let key = try P256.Signing.PrivateKey(pemRepresentation: String(contentsOfFile: path, encoding: .utf8))
    let now = Int(Date().timeIntervalSince1970)
    let header = base64URL(try JSONSerialization.data(withJSONObject: ["alg": "ES256", "kid": keyId, "typ": "JWT"]))
    let payload = base64URL(try JSONSerialization.data(withJSONObject: [
        "iss": issuerId, "iat": now, "exp": now + 1200, "aud": "appstoreconnect-v1",
    ]))
    let signature = try key.signature(for: Data("\(header).\(payload)".utf8)).rawRepresentation
    return "\(header).\(payload).\(base64URL(signature))"
}

@discardableResult
func api(_ method: String, _ path: String, _ body: [String: Any]? = nil) async throws -> [String: Any] {
    var request = URLRequest(url: URL(string: "https://api.appstoreconnect.apple.com" + path)!)
    request.httpMethod = method
    request.setValue("Bearer \(try token())", forHTTPHeaderField: "Authorization")
    if let body {
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
    }
    let (data, response) = try await URLSession.shared.data(for: request)
    let status = (response as! HTTPURLResponse).statusCode
    guard (200..<300).contains(status) else {
        throw ASCError(description: "\(method) \(path) → \(status): \(String(decoding: data, as: UTF8.self))")
    }
    guard !data.isEmpty else { return [:] }
    return try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:]
}

func items(_ response: [String: Any]) -> [[String: Any]] {
    response["data"] as? [[String: Any]] ?? []
}

func attributes(_ item: [String: Any]) -> [String: Any] {
    item["attributes"] as? [String: Any] ?? [:]
}

guard let app = items(try await api("GET", "/v1/apps?filter[bundleId]=\(bundleId)")).first,
      let appId = app["id"] as? String else {
    throw ASCError(description: "App not found: \(bundleId)")
}

// App Store Connect may take several minutes to list and process an uploaded build.
// Stop after 10 minutes if the build never appears.
let buildQuery = "/v1/builds?filter[app]=\(appId)&filter[version]=\(buildNumber)"
    + "&filter[preReleaseVersion.version]=\(version)&limit=1"
let startedAt = Date()
let deadline = startedAt.addingTimeInterval(60 * 60)
var buildId: String?
while buildId == nil {
    let build = items(try await api("GET", buildQuery)).first
    let state = build.map { attributes($0)["processingState"] as? String ?? "?" } ?? "NOT_FOUND"
    switch state {
    case "VALID":
        buildId = build?["id"] as? String
    case "FAILED", "INVALID":
        throw ASCError(description: "Build \(version) (\(buildNumber)) processing failed: \(state)")
    case "NOT_FOUND" where Date().timeIntervalSince(startedAt) > 10 * 60:
        throw ASCError(description: "Build \(version) (\(buildNumber)) was not found after 10 minutes. Check make upload.")
    default:
        guard Date() < deadline else { throw ASCError(description: "Build processing timed out: \(state)") }
        print("Build \(version) (\(buildNumber)): \(state). Retrying in 30 seconds...")
        try await Task.sleep(for: .seconds(30))
    }
}
let build = buildId!
print("Build \(version) (\(buildNumber)) is ready.")

let localizations = items(try await api("GET", "/v1/builds/\(build)/betaBuildLocalizations"))
if let existing = localizations.first(where: { attributes($0)["locale"] as? String == "en-US" }),
   let id = existing["id"] as? String {
    try await api("PATCH", "/v1/betaBuildLocalizations/\(id)", ["data": [
        "type": "betaBuildLocalizations", "id": id, "attributes": ["whatsNew": whatToTest],
    ]])
} else {
    try await api("POST", "/v1/betaBuildLocalizations", ["data": [
        "type": "betaBuildLocalizations",
        "attributes": ["locale": "en-US", "whatsNew": whatToTest],
        "relationships": ["build": ["data": ["type": "builds", "id": build]]],
    ]])
}
print("What to Test was updated.")

guard let group = items(try await api("GET", "/v1/apps/\(appId)/betaGroups"))
    .first(where: { attributes($0)["name"] as? String == groupName }),
    let groupId = group["id"] as? String else {
    throw ASCError(description: "TestFlight group not found: \(groupName)")
}
let groupAttributes = attributes(group)
let isInternalGroup = groupAttributes["isInternalGroup"] as? Bool ?? false
if isInternalGroup, groupAttributes["hasAccessToAllBuilds"] as? Bool == true {
    print("Internal group \(groupName) already has access to all builds.")
} else {
    try await api("POST", "/v1/betaGroups/\(groupId)/relationships/builds", [
        "data": [["type": "builds", "id": build]],
    ])
    print("Build added to group \(groupName).")
}

// Only external groups need Beta App Review.
guard !isInternalGroup else {
    print("Internal group: Beta App Review is not required.")
    exit(0)
}

let submission = try await api("GET", "/v1/builds/\(build)/betaAppReviewSubmission")
if let state = (submission["data"] as? [String: Any]).map({ attributes($0)["betaReviewState"] as? String ?? "?" }) {
    print("Beta App Review already exists: \(state)")
} else {
    let created = try await api("POST", "/v1/betaAppReviewSubmissions", ["data": [
        "type": "betaAppReviewSubmissions",
        "relationships": ["build": ["data": ["type": "builds", "id": build]]],
    ]])
    let state = (created["data"] as? [String: Any]).map { attributes($0)["betaReviewState"] as? String ?? "?" } ?? "?"
    print("Beta App Review submitted: \(state)")
}
