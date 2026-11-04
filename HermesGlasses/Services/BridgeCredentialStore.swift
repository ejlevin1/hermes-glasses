//
// BridgeCredentialStore.swift
//
// Keychain storage for the three bridge secrets: the bridge's own token
// (HERMES_BRIDGE_TOKEN, sent as ?token=) and the Cloudflare Access service
// token pair (sent as CF-Access-Client-Id / CF-Access-Client-Secret
// headers). Same pattern as MapCredentials: one generic-password item per
// value, AfterFirstUnlock, failures logged by OSStatus only - never the
// value, never surfaced as an alert.
//
// The host the secrets were saved for is NOT a secret and lives in
// UserDefaults (`bridge_credentials_host`). `BridgeRequestBuilder` attaches
// the secrets only to a `wss://` endpoint on that host.
//

import Foundation
import Security
import os

enum BridgeCredentialStore {
    enum Field: String, CaseIterable {
        case token = "bridge_token"
        case accessClientID = "bridge_cf_access_client_id"
        case accessClientSecret = "bridge_cf_access_client_secret"
    }

    private static let logger = Logger(
        subsystem: "me.ericlevin.hermesglasses", category: "bridge-credentials"
    )

    static let boundHostKey = "bridge_credentials_host"
    /// Set once the pre-binding migration has run, so it never re-binds
    /// saved secrets to whatever endpoint happens to be current later.
    static let boundHostMigratedKey = "bridge_credentials_host_migrated"

    /// Everything currently saved; missing items are empty strings.
    static func load() -> BridgeCredentials {
        BridgeCredentials(
            token: value(for: .token) ?? "",
            accessClientID: value(for: .accessClientID) ?? "",
            accessClientSecret: value(for: .accessClientSecret) ?? "",
            boundHost: boundHost
        )
    }

    /// The host saved secrets apply to; empty = none.
    static var boundHost: String {
        UserDefaults.standard.string(forKey: boundHostKey) ?? ""
    }

    static var hasAnySecret: Bool { Field.allCases.contains { has($0) } }

    /// Bind the saved secrets to the host of `endpoint` (call on every save).
    /// An endpoint without a host clears the binding: nothing is sent.
    static func bind(toEndpoint endpoint: String) {
        let defaults = UserDefaults.standard
        defaults.set(true, forKey: boundHostMigratedKey)
        if let host = BridgeRequestBuilder.host(of: endpoint) {
            defaults.set(host, forKey: boundHostKey)
        } else {
            defaults.removeObject(forKey: boundHostKey)
        }
    }

    /// Drop the binding once nothing is saved any more.
    static func unbindIfEmpty() {
        if !hasAnySecret { UserDefaults.standard.removeObject(forKey: boundHostKey) }
    }

    /// One-time migration for secrets saved before host binding: bind to the
    /// current endpoint's host only if it is `wss://`; otherwise leave them
    /// unbound (sent nowhere) until the user saves them again.
    static func migrateBoundHostIfNeeded(currentEndpoint: String) {
        let defaults = UserDefaults.standard
        guard !defaults.bool(forKey: boundHostMigratedKey) else { return }
        defaults.set(true, forKey: boundHostMigratedKey)
        guard defaults.string(forKey: boundHostKey) == nil, hasAnySecret,
              let host = BridgeRequestBuilder.migratedBoundHost(currentEndpoint: currentEndpoint)
        else { return }
        defaults.set(host, forKey: boundHostKey)
    }

    static func has(_ field: Field) -> Bool { value(for: field) != nil }

    /// Stores `value` trimmed; an empty value deletes the item.
    static func set(_ value: String, for field: Field) {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        let query = baseQuery(field)
        // errSecItemNotFound on the delete is the normal first-save case.
        let deleteStatus = SecItemDelete(query as CFDictionary)
        if deleteStatus != errSecSuccess, deleteStatus != errSecItemNotFound {
            logger.error("Delete \(field.rawValue, privacy: .public) failed: OSStatus \(deleteStatus, privacy: .public)")
        }
        guard !trimmed.isEmpty, let data = trimmed.data(using: .utf8) else { return }
        var attributes = query
        attributes[kSecValueData as String] = data
        attributes[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        let addStatus = SecItemAdd(attributes as CFDictionary, nil)
        if addStatus != errSecSuccess {
            logger.error("Store \(field.rawValue, privacy: .public) failed: OSStatus \(addStatus, privacy: .public)")
        }
    }

    static func clear(_ field: Field) { set("", for: field) }

    static func value(for field: Field) -> String? {
        var query = baseQuery(field)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: AnyObject?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data,
              let string = String(data: data, encoding: .utf8), !string.isEmpty
        else { return nil }
        return string
    }

    private static func baseQuery(_ field: Field) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrAccount as String: field.rawValue,
        ]
    }
}
