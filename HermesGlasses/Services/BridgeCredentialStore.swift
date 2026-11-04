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
        subsystem: "com.flowsxr.hermesglasses", category: "bridge-credentials"
    )

    /// Everything currently saved; missing items are empty strings.
    static func load() -> BridgeCredentials {
        BridgeCredentials(
            token: value(for: .token) ?? "",
            accessClientID: value(for: .accessClientID) ?? "",
            accessClientSecret: value(for: .accessClientSecret) ?? ""
        )
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
