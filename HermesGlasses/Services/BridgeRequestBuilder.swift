//
// BridgeRequestBuilder.swift
//
// Builds the URLRequest the bridge WebSocket is opened with. Pure
// Foundation (no Security, no os, no SwiftUI) so tests/bridge-request/ can
// compile it with swiftc alone.
//
// Two independent locks sit in front of a remote bridge:
//
//   * Cloudflare Access (service token) - the CF-Access-Client-Id /
//     CF-Access-Client-Secret HEADERS. Cloudflare answers 403 before the
//     bridge ever sees the request when they are missing or wrong.
//   * The bridge's own HERMES_BRIDGE_TOKEN - the `token` QUERY item, which is
//     what hermes_bridge.py checks.
//
// Every field is optional and empty means "not configured": with nothing
// set the request is exactly `URLRequest(url: URL(string: endpoint))`, so a
// LAN `ws://host:8765/voice` keeps working as it always did.
//

import Foundation

/// What the user typed in Settings -> Assistant -> Bridge. Held in the
/// Keychain (`BridgeCredentialStore`), never in UserDefaults.
struct BridgeCredentials: Equatable {
    var token: String = ""
    var accessClientID: String = ""
    var accessClientSecret: String = ""

    static let empty = BridgeCredentials()

    /// Trimmed copy - a pasted value with a trailing newline would otherwise
    /// produce a header Cloudflare rejects with a bare 403.
    var trimmed: BridgeCredentials {
        BridgeCredentials(
            token: token.trimmingCharacters(in: .whitespacesAndNewlines),
            accessClientID: accessClientID.trimmingCharacters(in: .whitespacesAndNewlines),
            accessClientSecret: accessClientSecret.trimmingCharacters(in: .whitespacesAndNewlines)
        )
    }

    /// Cloudflare Access needs BOTH halves. One without the other is a
    /// half-typed form, and sending it would only trade a clear "not
    /// configured" for an opaque 403 - so neither header is sent.
    var hasAccessToken: Bool {
        let t = trimmed
        return !t.accessClientID.isEmpty && !t.accessClientSecret.isEmpty
    }
}

enum BridgeRequestBuilder {
    static let accessClientIDHeader = "CF-Access-Client-Id"
    static let accessClientSecretHeader = "CF-Access-Client-Secret"
    static let tokenQueryItem = "token"
    private static let unreserved = CharacterSet(
        charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")

    /// The endpoint with `token=<bridge token>` appended, percent-encoded.
    /// A URL that already carries a `token` item (the pre-Keychain way of
    /// configuring it, still saved in some users' endpoints) is left as it
    /// is: the item is never duplicated and the saved one wins.
    /// Returns nil for an endpoint that is not a URL.
    static func url(endpoint: String, token: String) -> URL? {
        let trimmedEndpoint = endpoint.trimmingCharacters(in: .whitespacesAndNewlines)
        // A scheme and a host, or it is not something a socket can open -
        // newer Foundation happily turns "not a url" into a relative URL.
        guard !trimmedEndpoint.isEmpty, let base = URL(string: trimmedEndpoint),
              base.scheme != nil, !(base.host ?? "").isEmpty
        else { return nil }

        let trimmedToken = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedToken.isEmpty,
              var components = URLComponents(url: base, resolvingAgainstBaseURL: false)
        else { return base }

        if (components.queryItems ?? []).contains(where: { $0.name == tokenQueryItem }) {
            return base
        }
        // Encoded by hand against the RFC 3986 unreserved set rather than via
        // `queryItems`: URLComponents leaves "+", "&" and "=" in a value as
        // they are, and the bridge (parse_qs) reads "+" as a space and "&" as
        // the start of the next item - so a token containing any of them
        // would arrive changed and be rejected.
        let encodedToken = trimmedToken.addingPercentEncoding(
            withAllowedCharacters: unreserved) ?? trimmedToken
        let item = "\(tokenQueryItem)=\(encodedToken)"
        if let existing = components.percentEncodedQuery, !existing.isEmpty {
            components.percentEncodedQuery = existing + "&" + item
        } else {
            components.percentEncodedQuery = item
        }
        return components.url ?? base
    }

    /// A human reason for a failed connect, from the handshake's HTTP status
    /// or the socket's close code. nil = nothing specific to say (use the
    /// transport error). Never includes a credential.
    static func failureHint(httpStatus: Int?, closeCode: Int?) -> String? {
        if closeCode == 4401 {
            return "the bridge rejected the token (check Bridge token in Settings → Assistant)"
        }
        switch httpStatus {
        case 403?:
            return "HTTP 403 - Cloudflare Access refused the request (check CF Access Client ID and Secret)"
        case 401?:
            return "HTTP 401 - not authorised"
        case 404?:
            return "HTTP 404 - nothing serves this path (check the endpoint URL)"
        case let code? where code >= 500:
            return "HTTP \(code) - the server or tunnel in front of the bridge failed"
        default:
            return nil
        }
    }

    /// The request to open the WebSocket with. Secrets go in headers (CF)
    /// and the query (bridge token) only - nothing else is added.
    static func request(endpoint: String, credentials: BridgeCredentials) -> URLRequest? {
        let creds = credentials.trimmed
        guard let url = url(endpoint: endpoint, token: creds.token) else { return nil }
        var request = URLRequest(url: url)
        if creds.hasAccessToken {
            request.setValue(creds.accessClientID, forHTTPHeaderField: accessClientIDHeader)
            request.setValue(creds.accessClientSecret, forHTTPHeaderField: accessClientSecretHeader)
        }
        return request
    }
}
