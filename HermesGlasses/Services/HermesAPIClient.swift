//
// HermesAPIClient.swift
//
// WebSocket client for communicating with Hermes Agent's voice endpoint.
// Handles bidirectional audio streaming: sends captured audio from glasses,
// receives STT transcripts, agent text responses, and TTS audio.
//

import Foundation
import os

/// WebSocket-based client for Hermes Agent voice API
final class HermesAPIClient: NSObject {
    // MARK: - Callbacks

    var onTranscript: ((String) -> Void)?
    /// (text, bridgeWillSendAudio) - when the second value is false, the
    /// app speaks the text itself with on-device TTS
    var onResponse: ((String, Bool) -> Void)?
    var onAudioResponse: ((Data) -> Void)?
    var onPlaybackComplete: (() -> Void)?
    var onError: ((String) -> Void)?
    /// Called when the WebSocket disconnects
    var onDisconnected: (() -> Void)?
    /// Bridge asks the app to take a photo with the glasses
    var onCapturePhotoRequested: (() -> Void)?
    /// Bridge confirmed the conversation was reset
    var onSessionReset: (() -> Void)?

    // MARK: - Private

    private let endpoint: String
    /// Bridge token + Cloudflare Access service token. Only ever put into
    /// the request (query item / headers) by `BridgeRequestBuilder`; never
    /// logged, never included in an error message.
    private let credentials: BridgeCredentials
    private var webSocket: URLSessionWebSocketTask?
    private var session: URLSession?
    /// Behind a lock because the receive loop clears it from a background
    /// executor while the view model reads it on the main actor to decide
    /// whether an utterance can be sent. A plain `Bool` was a data race; an
    /// actor hop would have made `disconnect()` land after callers had
    /// already read the stale value.
    private let connectedFlag = OSAllocatedUnfairLock(initialState: false)
    var isConnected: Bool { connectedFlag.withLock { $0 } }
    private var receiveTask: Task<Void, Never>?
    private var isFinalized: Bool = false
    /// TTS audio accumulated between audio_start and audio_end
    private var ttsBuffer = Data()

    init(endpoint: String, credentials: BridgeCredentials = .empty) {
        self.endpoint = endpoint
        self.credentials = credentials
        super.init()
    }

    // MARK: - Public API

    /// Connect to the Hermes bridge and wait for its welcome message.
    /// Returns true once the bridge has confirmed the connection.
    @discardableResult
    func connect() async -> Bool {
        // A URLRequest, not a bare URL: the Cloudflare Access headers can
        // only travel on a request. The message names the endpoint as
        // typed, never the built URL - that one carries the bridge token.
        guard let request = BridgeRequestBuilder.request(
            endpoint: endpoint, credentials: credentials
        ) else {
            await reportError("Invalid Hermes endpoint URL: \(endpoint)")
            return false
        }

        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 10
        // Redirects are refused: the Cloudflare Access headers and the
        // bridge token must never follow a 3xx to another host.
        let urlSession = URLSession(
            configuration: config, delegate: BridgeNoRedirectDelegate(), delegateQueue: nil)
        session = urlSession

        let ws = urlSession.webSocketTask(with: request)
        webSocket = ws
        ws.resume()

        do {
            // The bridge sends {"type":"welcome"} immediately on connect
            let first = try await ws.receive()
            setConnected(true)
            await handleMessage(first)

            receiveTask = Task { [weak self] in
                await self?.receiveLoop()
            }
            return true
        } catch {
            // URLSession reports a refused upgrade as "bad server response"
            // and a bridge-side 4401 as a generic close - neither says which
            // lock was wrong, so name it from the status / close code.
            let status = (ws.response as? HTTPURLResponse)?.statusCode
            let closeCode = ws.closeCode == .invalid ? nil : ws.closeCode.rawValue
            let hint = BridgeRequestBuilder.failureHint(httpStatus: status, closeCode: closeCode)
            let detail = hint ?? error.localizedDescription
            await reportError("Failed to connect to Hermes: \(detail)")
            disconnect()
            return false
        }
    }

    /// Disconnect from Hermes
    func disconnect() {
        receiveTask?.cancel()
        receiveTask = nil
        webSocket?.cancel(with: .normalClosure, reason: nil)
        webSocket = nil
        session?.invalidateAndCancel()
        session = nil
        setConnected(false)
        isFinalized = false
        Task { @MainActor in
            onDisconnected?()
        }
    }

    func sendAudioChunk(_ data: Data) {
        guard isConnected, let ws = webSocket, !isFinalized else { return }

        ws.send(.data(data)) { [weak self] error in
            if let error {
                Task { @MainActor in
                    self?.onError?("Send error: \(error.localizedDescription)")
                }
            }
        }
    }

    /// Send a diagnostic message that the bridge prints to its log
    func sendDebug(_ message: String) {
        guard isConnected, let ws = webSocket else { return }
        let payload: [String: String] = ["type": "debug", "msg": message]
        guard let data = try? JSONSerialization.data(withJSONObject: payload),
              let text = String(data: data, encoding: .utf8) else { return }
        ws.send(.string(text)) { _ in }
    }

    /// Ask the bridge to forget the conversation (same-day memory reset)
    func sendNewSession() {
        guard isConnected, let ws = webSocket else { return }
        ws.send(.string(#"{"type":"new_session"}"#)) { _ in }
    }

    /// Send an on-device-transcribed query. bridgeTTS asks the bridge to
    /// synthesize the reply voice (edge-tts); false = app speaks locally.
    func sendQuery(_ text: String, bridgeTTS: Bool) {
        guard isConnected, let ws = webSocket else { return }
        let payload: [String: Any] = ["type": "query", "text": text,
                                      "tts": bridgeTTS]
        guard let data = try? JSONSerialization.data(withJSONObject: payload),
              let json = String(data: data, encoding: .utf8) else { return }
        ws.send(.string(json)) { [weak self] error in
            if let error {
                Task { @MainActor in
                    self?.onError?("Query send error: \(error.localizedDescription)")
                }
            }
        }
    }

    /// Send a captured JPEG as base64 JSON (binary frames are mic audio only)
    func sendPhoto(_ data: Data) {
        guard isConnected, let ws = webSocket else { return }
        let payload: [String: String] = [
            "type": "photo",
            "data": data.base64EncodedString(),
        ]
        guard let json = try? JSONSerialization.data(withJSONObject: payload),
              let text = String(data: json, encoding: .utf8) else { return }
        ws.send(.string(text)) { [weak self] error in
            if let error {
                Task { @MainActor in
                    self?.onError?("Photo send error: \(error.localizedDescription)")
                }
            }
        }
    }

    func sendPhotoError(_ message: String) {
        guard isConnected, let ws = webSocket else { return }
        let payload: [String: String] = ["type": "photo_error", "message": message]
        guard let json = try? JSONSerialization.data(withJSONObject: payload),
              let text = String(data: json, encoding: .utf8) else { return }
        ws.send(.string(text)) { _ in }
    }

    func finalizeAudio() async {
        guard isConnected, let ws = webSocket, !isFinalized else { return }
        isFinalized = true

        let endMarker = #"{"type":"end_of_audio"}"#
        ws.send(.string(endMarker)) { [weak self] error in
            if let error {
                Task { @MainActor in
                    self?.onError?("Finalize error: \(error.localizedDescription)")
                }
            }
        }
    }

    // MARK: - Private

    private func setConnected(_ value: Bool) {
        connectedFlag.withLock { $0 = value }
    }

    private func receiveLoop() async {
        guard let ws = webSocket else { return }

        while !Task.isCancelled, ws.closeCode == .invalid {
            do {
                let message = try await ws.receive()
                await handleMessage(message)
            } catch {
                setConnected(false)
                if !Task.isCancelled {
                    await reportError("Connection lost: \(error.localizedDescription)")
                    await MainActor.run { [weak self] in
                        self?.onDisconnected?()
                    }
                }
                break
            }
        }
    }

    private func handleMessage(_ message: URLSessionWebSocketTask.Message) async {
        switch message {
        case .string(let text):
            await handleTextMessage(text)
        case .data(let data):
            // Binary from the bridge is a TTS chunk; play only when complete
            ttsBuffer.append(data)
        @unknown default:
            break
        }
    }

    private func handleTextMessage(_ text: String) async {
        guard let data = text.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = json["type"] as? String else {
            return
        }

        await MainActor.run { [weak self] in
            switch type {
            case "welcome":
                break
            case "transcript":
                if let transcript = json["text"] as? String {
                    self?.onTranscript?(transcript)
                }
            case "response":
                if let response = json["text"] as? String {
                    // Absent "tts" field = old bridge that always streams
                    // audio afterwards
                    let bridgeAudio = (json["tts"] as? Bool) ?? true
                    self?.onResponse?(response, bridgeAudio)
                }
            case "audio_start":
                self?.ttsBuffer.removeAll()
            case "audio_end":
                if let self, !self.ttsBuffer.isEmpty {
                    let audio = self.ttsBuffer
                    self.ttsBuffer.removeAll()
                    self.onAudioResponse?(audio)
                } else {
                    self?.onPlaybackComplete?()
                }
                self?.isFinalized = false
            case "error":
                if let msg = json["message"] as? String {
                    self?.onError?("Hermes: \(msg)")
                }
            case "capture_photo":
                self?.onCapturePhotoRequested?()
            case "session_reset":
                self?.onSessionReset?()
            default:
                break
            }
        }
    }

    private func reportError(_ message: String) async {
        await MainActor.run { [weak self] in
            self?.onError?(message)
        }
    }
}

/// Refuses every HTTP redirect on the bridge socket's session, so the
/// credentials on the original request can never be replayed elsewhere.
/// A separate object rather than the client itself: URLSession retains its
/// delegate strongly until invalidated.
final class BridgeNoRedirectDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        completionHandler(nil)
    }
}
