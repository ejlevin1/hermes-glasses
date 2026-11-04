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
let remoteHost = "glasses.example.com"
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
                                   accessClientSecret: "s3cret", boundHost: remoteHost))
expect(header(both, idH) == "id.access", "both set: client id header")
expect(header(both, secretH) == "s3cret", "both set: client secret header")
expect(both?.url?.absoluteString == remote, "CF credentials never go in the URL")
expect(!(both?.url?.absoluteString.contains("s3cret") ?? true), "secret absent from URL")
expect(!(both?.url?.absoluteString.contains("id.access") ?? true), "client id absent from URL")

// Only one half -> neither header
let onlyID = BridgeRequestBuilder.request(
    endpoint: remote, credentials: BridgeCredentials(accessClientID: "id.access", boundHost: remoteHost))
expect(header(onlyID, idH) == nil && header(onlyID, secretH) == nil,
       "client id without secret: no headers")
let onlySecret = BridgeRequestBuilder.request(
    endpoint: remote, credentials: BridgeCredentials(accessClientSecret: "s3cret", boundHost: remoteHost))
expect(header(onlySecret, idH) == nil && header(onlySecret, secretH) == nil,
       "secret without client id: no headers")
let blankID = BridgeRequestBuilder.request(
    endpoint: remote,
    credentials: BridgeCredentials(accessClientID: "  ", accessClientSecret: "s3cret",
                                   boundHost: remoteHost))
expect(header(blankID, secretH) == nil, "whitespace client id counts as missing")

// Values are trimmed (pasted with a trailing newline)
let pasted = BridgeRequestBuilder.request(
    endpoint: remote,
    credentials: BridgeCredentials(token: " tok\n", accessClientID: "id\n",
                                   accessClientSecret: "\tsec ", boundHost: " \(remoteHost)\n"))
expect(header(pasted, idH) == "id" && header(pasted, secretH) == "sec", "header values trimmed")
expect(pasted?.url?.absoluteString == "\(remote)?token=tok", "token trimmed")

// Everything together
let full = BridgeRequestBuilder.request(
    endpoint: remote,
    credentials: BridgeCredentials(token: "tok", accessClientID: "id", accessClientSecret: "sec",
                                   boundHost: remoteHost))
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


// MARK: - Host binding (secrets only go to the wss:// host they were saved for)

let allSecrets = BridgeCredentials(token: "tok", accessClientID: "id", accessClientSecret: "sec",
                                   boundHost: remoteHost)
func noSecrets(_ r: URLRequest?, _ expectedURL: String, _ label: String) {
    expect(r?.url?.absoluteString == expectedURL, "\(label): URL unchanged")
    expect(header(r, idH) == nil && header(r, secretH) == nil, "\(label): no CF headers")
    expect(r?.allHTTPHeaderFields?.isEmpty ?? true, "\(label): no headers at all")
    expect(!(r?.url?.absoluteString.contains("tok") ?? true), "\(label): no token")
}

// Different host -> nothing
let otherHost = "wss://evil.example.net/voice"
noSecrets(BridgeRequestBuilder.request(endpoint: otherHost, credentials: allSecrets),
          otherHost, "different host")
// Subdomain / suffix tricks are different hosts too
let suffixHost = "wss://glasses.example.com.evil.net/voice"
noSecrets(BridgeRequestBuilder.request(endpoint: suffixHost, credentials: allSecrets),
          suffixHost, "bound host as a prefix of another host")
let userinfoTrick = "wss://glasses.example.com@evil.net/voice"
noSecrets(BridgeRequestBuilder.request(endpoint: userinfoTrick, credentials: allSecrets),
          userinfoTrick, "bound host as userinfo")

// Same host over cleartext ws:// -> nothing
let sameHostWS = "ws://glasses.example.com/voice"
noSecrets(BridgeRequestBuilder.request(endpoint: sameHostWS, credentials: allSecrets),
          sameHostWS, "same host over ws://")
// ...and https:// is not a WebSocket scheme we send secrets on either
let sameHostHTTPS = "https://glasses.example.com/voice"
noSecrets(BridgeRequestBuilder.request(endpoint: sameHostHTTPS, credentials: allSecrets),
          sameHostHTTPS, "same host over https://")

// Same host over wss:// -> both
let sameHost = BridgeRequestBuilder.request(endpoint: remote, credentials: allSecrets)
expect(sameHost?.url?.absoluteString == "\(remote)?token=tok", "same host wss: token")
expect(header(sameHost, idH) == "id" && header(sameHost, secretH) == "sec",
       "same host wss: CF headers")
// Case-insensitive host and scheme, either side
let upperEndpoint = "WSS://Glasses.EXAMPLE.com/voice"
let upper = BridgeRequestBuilder.request(endpoint: upperEndpoint, credentials: allSecrets)
expect(header(upper, idH) == "id" && header(upper, secretH) == "sec",
       "different case endpoint: CF headers")
expect(upper?.url?.query == "token=tok", "different case endpoint: token")
var upperBound = allSecrets
upperBound.boundHost = "GLASSES.Example.COM"
let upperB = BridgeRequestBuilder.request(endpoint: remote, credentials: upperBound)
expect(header(upperB, idH) == "id" && upperB?.url?.query == "token=tok",
       "different case bound host: both")
// Port does not change the host
let withPort = "wss://glasses.example.com:8443/voice"
let ported = BridgeRequestBuilder.request(endpoint: withPort, credentials: allSecrets)
expect(header(ported, idH) == "id" && ported?.url?.query == "token=tok",
       "same host, explicit port: both")

// Bound host unset -> nothing, even on the right wss:// endpoint
var unbound = allSecrets
unbound.boundHost = ""
noSecrets(BridgeRequestBuilder.request(endpoint: remote, credentials: unbound),
          remote, "bound host unset")
unbound.boundHost = "   "
noSecrets(BridgeRequestBuilder.request(endpoint: remote, credentials: unbound),
          remote, "bound host whitespace")

// A ?token= the user typed into the URL is theirs and is kept, bound or not
let typed = "ws://192.168.1.20:8765/voice?token=mine"
let typedReq = BridgeRequestBuilder.request(endpoint: typed, credentials: allSecrets)
expect(typedReq?.url?.absoluteString == typed, "user-typed token kept (other host, ws)")
expect(header(typedReq, idH) == nil, "user-typed token, other host: no CF headers")
let typedRemote = "\(remote)?token=mine"
let typedRemoteReq = BridgeRequestBuilder.request(endpoint: typedRemote, credentials: allSecrets)
expect(typedRemoteReq?.url?.absoluteString == typedRemote,
       "user-typed token kept on bound host, Keychain token not added")
expect(header(typedRemoteReq, idH) == "id", "user-typed token on bound host: CF headers still sent")
let typedUnbound = BridgeRequestBuilder.request(endpoint: typedRemote, credentials: .empty)
expect(typedUnbound?.url?.absoluteString == typedRemote, "user-typed token kept, no credentials")

// Empty credentials -> exactly URLRequest(url:)
let emptyBound = BridgeRequestBuilder.request(endpoint: remote, credentials: .empty)
expect(emptyBound == URLRequest(url: URL(string: remote)!), "empty credentials: bare URLRequest(url:)")

// Host helpers
expect(BridgeRequestBuilder.host(of: "  WSS://Glasses.Example.com/voice\n") == remoteHost,
       "host(of:) lowercases + trims")
expect(BridgeRequestBuilder.host(of: "not a url") == nil, "host(of:) nil for garbage")
expect(BridgeRequestBuilder.migratedBoundHost(currentEndpoint: remote) == remoteHost,
       "migration binds a wss:// endpoint's host")
expect(BridgeRequestBuilder.migratedBoundHost(currentEndpoint: lan) == nil,
       "migration binds nothing for ws://")
expect(BridgeRequestBuilder.migratedBoundHost(currentEndpoint: "") == nil,
       "migration binds nothing for an empty endpoint")

print(failures == 0 ? "\nALL PASS" : "\n\(failures) FAILURES")
exit(failures == 0 ? 0 : 1)
