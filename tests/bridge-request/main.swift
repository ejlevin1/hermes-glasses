//
// Bridge WebSocket request building (Cloudflare Access headers + bridge
// token). Pure Foundation:
//
// swiftc HermesGlasses/Services/BridgeRequestBuilder.swift \
//        tests/bridge-request/main.swift -o /tmp/bridge-request-tests && /tmp/bridge-request-tests
//
import Foundation

var failures = 0
func expect(_ c: Bool, _ label: String) {
    if c { print("PASS \(label)") } else { failures += 1; print("FAIL \(label)") }
}

let remote = "wss://glasses.example.com/voice"
let lan = "ws://192.168.1.20:8765/voice"

// MARK: - URL building

// Empty token = the endpoint exactly as typed (LAN users see no change).
let lanURL = BridgeRequestBuilder.url(endpoint: lan, token: "")
expect(lanURL?.absoluteString == lan, "LAN ws:// with no token is unchanged")
expect(BridgeRequestBuilder.url(endpoint: remote, token: "   ")?.absoluteString == remote,
       "whitespace-only token counts as empty")
expect(BridgeRequestBuilder.url(endpoint: "  \(lan)\n", token: "")?.absoluteString == lan,
       "endpoint is trimmed")

// Token appended as ?token=
let withToken = BridgeRequestBuilder.url(endpoint: remote, token: "abc123")
expect(withToken?.absoluteString == "\(remote)?token=abc123", "token appended as ?token=")

// Token appended after an existing query with &
let withQuery = BridgeRequestBuilder.url(endpoint: "\(remote)?client=ios", token: "abc")
expect(withQuery?.absoluteString == "\(remote)?client=ios&token=abc",
       "token appended after an existing query item")

// Percent-encoding: characters that would change meaning in a query
let tricky = "a+b&c=d e/f?g#h%i"
let trickyURL = BridgeRequestBuilder.url(endpoint: remote, token: tricky)
let trickyQuery = trickyURL?.query ?? ""
expect(trickyQuery == "token=a%2Bb%26c%3Dd%20e%2Ff%3Fg%23h%25i",
       "token is percent-encoded (+ & = space / ? # %): \(trickyQuery)")
let decoded = URLComponents(url: trickyURL!, resolvingAgainstBaseURL: false)?
    .queryItems?.first { $0.name == "token" }?.value
expect(decoded == tricky, "encoded token decodes back to the original")
expect(!(trickyQuery.contains("+")), "no raw + (a form decoder would read a space)")

// Existing token (pre-Keychain endpoints) is never duplicated or replaced
let legacy = "\(remote)?token=saved"
let legacyURL = BridgeRequestBuilder.url(endpoint: legacy, token: "keychain")
expect(legacyURL?.absoluteString == legacy, "existing ?token= kept, not duplicated")
let legacyCount = (legacyURL?.absoluteString.components(separatedBy: "token=").count ?? 0) - 1
expect(legacyCount == 1, "exactly one token item")
// ...even when it is not the first item
let legacyMid = "\(remote)?a=1&token=saved&b=2"
expect(BridgeRequestBuilder.url(endpoint: legacyMid, token: "new")?.absoluteString == legacyMid,
       "existing token in the middle of the query is left alone")
// A different item that merely contains "token" is not mistaken for it
let lookalike = BridgeRequestBuilder.url(endpoint: "\(remote)?mytoken=x", token: "t")
expect(lookalike?.absoluteString == "\(remote)?mytoken=x&token=t",
       "an item named mytoken does not count as token")

// Invalid / empty endpoints
expect(BridgeRequestBuilder.url(endpoint: "", token: "t") == nil, "empty endpoint -> nil")
expect(BridgeRequestBuilder.url(endpoint: "not a url", token: "") == nil, "garbage endpoint -> nil")

// MARK: - Request + headers

func header(_ r: URLRequest?, _ name: String) -> String? { r?.value(forHTTPHeaderField: name) }
let idH = BridgeRequestBuilder.accessClientIDHeader
let secretH = BridgeRequestBuilder.accessClientSecretHeader
expect(idH == "CF-Access-Client-Id", "client id header name")
expect(secretH == "CF-Access-Client-Secret", "client secret header name")

// Nothing configured = bare request, no headers
let bare = BridgeRequestBuilder.request(endpoint: lan, credentials: .empty)
expect(bare?.url?.absoluteString == lan, "no credentials: URL unchanged")
expect(bare?.allHTTPHeaderFields?.isEmpty ?? true, "no credentials: no headers")

// Both set -> both headers
let both = BridgeRequestBuilder.request(
    endpoint: remote,
    credentials: BridgeCredentials(token: "", accessClientID: "id.access",
                                   accessClientSecret: "s3cret"))
expect(header(both, idH) == "id.access", "both set: client id header")
expect(header(both, secretH) == "s3cret", "both set: client secret header")
expect(both?.url?.absoluteString == remote, "CF credentials never go in the URL")
expect(!(both?.url?.absoluteString.contains("s3cret") ?? true), "secret absent from URL")
expect(!(both?.url?.absoluteString.contains("id.access") ?? true), "client id absent from URL")

// Only one half -> neither header
let onlyID = BridgeRequestBuilder.request(
    endpoint: remote, credentials: BridgeCredentials(accessClientID: "id.access"))
expect(header(onlyID, idH) == nil && header(onlyID, secretH) == nil,
       "client id without secret: no headers")
let onlySecret = BridgeRequestBuilder.request(
    endpoint: remote, credentials: BridgeCredentials(accessClientSecret: "s3cret"))
expect(header(onlySecret, idH) == nil && header(onlySecret, secretH) == nil,
       "secret without client id: no headers")
let blankID = BridgeRequestBuilder.request(
    endpoint: remote,
    credentials: BridgeCredentials(accessClientID: "  ", accessClientSecret: "s3cret"))
expect(header(blankID, secretH) == nil, "whitespace client id counts as missing")

// Values are trimmed (pasted with a trailing newline)
let pasted = BridgeRequestBuilder.request(
    endpoint: remote,
    credentials: BridgeCredentials(token: " tok\n", accessClientID: "id\n",
                                   accessClientSecret: "\tsec "))
expect(header(pasted, idH) == "id" && header(pasted, secretH) == "sec", "header values trimmed")
expect(pasted?.url?.absoluteString == "\(remote)?token=tok", "token trimmed")

// Everything together
let full = BridgeRequestBuilder.request(
    endpoint: remote,
    credentials: BridgeCredentials(token: "tok", accessClientID: "id", accessClientSecret: "sec"))
expect(full?.url?.absoluteString == "\(remote)?token=tok", "full: token in URL")
expect(header(full, idH) == "id" && header(full, secretH) == "sec", "full: both headers")
expect(full?.allHTTPHeaderFields?.count == 2, "full: no other headers added")

expect(BridgeRequestBuilder.request(endpoint: "", credentials: .empty) == nil,
       "invalid endpoint -> nil request")

// MARK: - Failure hints

let h403 = BridgeRequestBuilder.failureHint(httpStatus: 403, closeCode: nil) ?? ""
expect(h403.contains("Cloudflare Access"), "403 names Cloudflare Access")
let h4401 = BridgeRequestBuilder.failureHint(httpStatus: 101, closeCode: 4401) ?? ""
expect(h4401.contains("token"), "close 4401 names the bridge token")
expect(BridgeRequestBuilder.failureHint(httpStatus: 404, closeCode: nil)?.contains("404") == true,
       "404 names the path")
expect(BridgeRequestBuilder.failureHint(httpStatus: 502, closeCode: nil)?.contains("502") == true,
       "5xx names the status")
expect(BridgeRequestBuilder.failureHint(httpStatus: nil, closeCode: nil) == nil,
       "no status, no close code: no hint (transport error is used)")
expect(BridgeRequestBuilder.failureHint(httpStatus: 101, closeCode: 1000) == nil,
       "normal close: no hint")

print(failures == 0 ? "\nALL PASS" : "\n\(failures) FAILURES")
exit(failures == 0 ? 0 : 1)
