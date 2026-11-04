#!/usr/bin/env python3
"""
Hermes Glasses Bridge - WebSocket server connecting the Hermes Glasses iOS
app to a Hermes Agent. STT happens on the phone; this bridge handles text
queries, photo requests, conversation memory, and TTS.

Protocol (JSON text frames):
  app → bridge: {"type":"query","text":...}      transcribed utterance
  app → bridge: {"type":"new_session"}           forget the conversation
  app → bridge: {"type":"photo","data":<b64>}    reply to capture_photo
  app → bridge: {"type":"photo_error", ...}
  bridge → app: {"type":"welcome"} / {"type":"capture_photo"} /
                {"type":"response","text":...} / {"type":"session_reset"} /
                audio_start + binary PCM16 mono 24kHz TTS + audio_end
"""

import asyncio
import base64
import datetime
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import wave
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

try:
    import websockets
except ImportError:
    print("Install websockets: pip install websockets")
    sys.exit(1)

# ── Configuration ──────────────────────────────────────────────────────────
HOST = os.environ.get("HERMES_BRIDGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("HERMES_BRIDGE_PORT", "8765"))
HERMES_BIN = os.environ.get(
    "HERMES_BIN",
    os.path.expanduser("~/.hermes/hermes-agent/venv/bin/hermes"),
)

# Shared-secret auth. When set (HERMES_BRIDGE_TOKEN env), clients must
# connect with ws://host:8765/voice?token=<value>. REQUIRED on any bridge
# reachable from the internet - hermes has tool access, so an open bridge
# is remote code execution for anyone who finds the port.
AUTH_TOKEN = os.environ.get("HERMES_BRIDGE_TOKEN", "")

# Bridge-side TTS (edge-tts/say → PCM streaming). Default OFF: the app
# speaks replies on-device with AVSpeechSynthesizer. Set HERMES_BRIDGE_TTS=1
# to fall back to server TTS for clients that can't speak locally.
BRIDGE_TTS = os.environ.get("HERMES_BRIDGE_TTS", "") == "1"

# Which brain answers queries:
#   "hermes" (default) - spawn the hermes CLI per query (agent with tools;
#                        slower: pays agent boot every question)
#   "claude"           - call the Claude API directly (fast conversational
#                        answers + vision; needs ANTHROPIC_API_KEY)
#   "anthropic" / "openai" / "gemini" - call the provider's HTTP API
#                        directly (fast conversational answers + vision;
#                        needs the matching *_API_KEY). "claude" is kept as
#                        an alias for "anthropic" (back-compat).
BRAIN = os.environ.get("HERMES_BRIDGE_BRAIN", "hermes")
CLAUDE_MODEL = os.environ.get("HERMES_BRIDGE_MODEL", "claude-opus-4-8")

# ── Provider brains (direct API, BRAIN="anthropic"|"openai"|"gemini") ──────
BASE_URLS = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
    "gemini": "https://generativelanguage.googleapis.com",
}
PROVIDER_API_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


def _canon_brain(brain: str) -> str:
    return "anthropic" if brain == "claude" else brain


def build_provider_request(
    brain: str, model: str, base_url: str, api_key: str, prompt: str,
    image_b64: str | None, system: str | None = None,
) -> tuple[str, dict, dict]:
    """Return (url, headers, body_dict) for a one-shot chat+vision request.

    `system` defaults to CLAUDE_SYSTEM (the voice persona: 1-3 spoken
    sentences) so provider-brain answers stay voice-optimized, same as the
    legacy ask_claude() path.
    """
    system = system if system is not None else CLAUDE_SYSTEM
    brain = _canon_brain(brain)
    headers = {"Content-Type": "application/json"}
    if brain == "anthropic":
        content = []
        if image_b64:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg", "data": image_b64}})
        content.append({"type": "text", "text": prompt})
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
        body = {"model": model, "max_tokens": 1024, "system": system,
                "messages": [{"role": "user", "content": content}]}
        return base_url + "/v1/messages", headers, body
    if brain == "openai":
        if image_b64:
            content = [{"type": "text", "text": prompt},
                       {"type": "image_url", "image_url": {
                           "url": "data:image/jpeg;base64," + image_b64}}]
        else:
            content = prompt
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        body = {"model": model, "max_tokens": 1024,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": content}]}
        return base_url + "/v1/chat/completions", headers, body
    if brain == "gemini":
        parts = []
        if image_b64:
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": image_b64}})
        parts.append({"text": prompt})
        body = {"contents": [{"role": "user", "parts": parts}],
                "systemInstruction": {"parts": [{"text": system}]}}
        # The key travels in a header, never in the URL: query strings end up
        # in proxy logs, crash reports and error messages.
        if api_key:
            headers["x-goog-api-key"] = api_key
        url = "%s/v1beta/models/%s:generateContent" % (base_url, model)
        return url, headers, body
    raise ValueError("unknown brain: %s" % brain)


def parse_provider_reply(brain: str, status: int, body_bytes: bytes) -> str:
    brain = _canon_brain(brain)
    data = json.loads(body_bytes.decode("utf-8"))
    if status != 200:
        raise RuntimeError(data.get("error", {}).get("message", "API error %s" % status))
    if brain == "anthropic":
        return next(b["text"] for b in data["content"] if b.get("type") == "text")
    if brain == "openai":
        return data["choices"][0]["message"]["content"]
    if brain == "gemini":
        return data["candidates"][0]["content"]["parts"][0]["text"]
    raise ValueError("unknown brain: %s" % brain)


def ask_provider(brain: str, text: str, photo: bytes | None = None) -> str:
    """Answer via a direct provider HTTP API (anthropic/openai/gemini).

    Stdlib-only (urllib), stateless single-turn request built with
    build_provider_request() and parsed with parse_provider_reply(). Unlike
    the legacy SDK-based ask_claude() below, this path carries no cross-turn
    history - each query is answered independently.
    """
    brain = _canon_brain(brain)
    base_url = os.environ.get("HERMES_BRIDGE_BASE_URL", BASE_URLS[brain])
    key_env = PROVIDER_API_KEY_ENV[brain]
    api_key = os.environ.get(key_env, "")
    if not api_key:
        return f"The {brain} brain needs an API key. Set {key_env} for the bridge."

    image_b64 = base64.b64encode(photo).decode() if photo else None
    url, headers, body = build_provider_request(
        brain, CLAUDE_MODEL, base_url, api_key, text, image_b64)

    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            status, body_bytes = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, body_bytes = e.code, e.read()
    except urllib.error.URLError as e:
        print(f"[{brain}] Connection error: {e}")
        return f"Sorry, I couldn't reach the {brain} API."

    try:
        return parse_provider_reply(brain, status, body_bytes)
    except Exception as e:
        print(f"[{brain}] API error ({status}): {e}")
        return f"Sorry, the {brain} API returned an error."


# Utterances containing any of these ask about something the user sees;
# the bridge then requests a photo from the glasses. Phrases, not bare
# words: "picture" alone would fire on "why did you take a picture?"
VISUAL_KEYWORDS = [
    "look at", "looking at", "see this", "what am i seeing",
    "what is this", "what's this", "read this", "in front of me",
    "take a picture", "take a photo", "snap a photo", "use the camera",
    "through the camera",
]


def is_visual_query(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in VISUAL_KEYWORDS)


# "this drink", "that building", "the one on the left" - references to
# something the user is (probably) looking at
DEICTIC_RE = re.compile(r"\b(this|that|these|those)\s+([a-z]+)|\bthe one\b",
                        re.IGNORECASE)


# "this morning", "that question" - deictic in grammar but not about
# anything visible. Nouns here never trigger a photo.
DEICTIC_STOP_NOUNS = {
    "morning", "afternoon", "evening", "night", "time", "day", "week",
    "month", "year", "moment", "question", "answer", "idea", "point",
    "case", "way", "reason", "sense", "stuff", "conversation", "chat",
    "session", "app", "voice", "sound", "response", "reply", "much",
    "long", "short", "many", "far", "fast", "slow", "bit", "lot", "kind",
    "sort", "type",
    # Auxiliary/copular verbs: "this is a test", "that was fun" are not
    # about anything visible
    "is", "was", "are", "were", "be", "being", "been", "isn", "wasn",
    "will", "would", "can", "could", "should", "might", "may", "must",
    "does", "did", "done", "has", "had", "have", "gets", "got", "goes",
    "went", "seems", "means", "works", "sounds", "feels", "happened",
}


# Deictic follow-up captures only when no photo was taken within this many
# seconds - within the window, conversation memory already knows what the
# user is looking at.
PHOTO_MEMORY_WINDOW_S = 120.0


def should_capture_photo(text: str, last_photo_at: float, now: float) -> bool:
    """Decide whether a query needs a fresh photo.

    Explicit visual keywords always capture. Deictic references ("this X")
    capture only for plausibly-visible nouns, and only when no photo was
    taken recently - within the window, conversation memory already knows
    what the user is looking at.
    """
    if is_visual_query(text):
        return True
    for match in DEICTIC_RE.finditer(text):
        noun = (match.group(2) or "").lower()
        if match.group(0).lower() == "the one" or (
            noun and noun not in DEICTIC_STOP_NOUNS
        ):
            return (now - last_photo_at) > PHOTO_MEMORY_WINDOW_S
    return False


# ── Conversation memory (same-day hermes session) ──────────────────────────
SESSION_FILE = os.path.expanduser("~/.hermes_glasses_bridge_session.json")

VOICE_PERSONA = (
    "(You are a voice assistant running on smart glasses. Your answers are "
    "spoken aloud - keep them to 1-3 conversational sentences unless the "
    "user asks for detail. The user may reference things they see; photos "
    "may be attached to queries. Do not mention codebases or files unless "
    "asked.) "
)


def _today() -> str:
    return datetime.date.today().isoformat()


def load_session() -> str | None:
    """Stored hermes session ID, if it is from today."""
    try:
        with open(SESSION_FILE) as f:
            data = json.load(f)
        if data.get("date") == _today() and data.get("session_id"):
            return data["session_id"]
    except (OSError, ValueError):
        pass
    return None


def store_session(session_id: str):
    try:
        with open(SESSION_FILE, "w") as f:
            json.dump({"session_id": session_id, "date": _today()}, f)
    except OSError as e:
        print(f"[Bridge] Could not persist session: {e}")


def clear_session():
    try:
        os.unlink(SESSION_FILE)
    except OSError:
        pass


def extract_session_id(stderr_text: str) -> str | None:
    match = re.search(r"session_id:\s*(\S+)", stderr_text)
    return match.group(1) if match else None


# ── Legacy Claude SDK brain (unused by the request path below, which now
# routes anthropic/openai/gemini through ask_provider(); kept for its
# tested same-day history helpers) ──────────────────────────────────────────
CLAUDE_HISTORY_FILE = os.path.expanduser("~/.hermes_glasses_claude_history.json")
CLAUDE_MAX_HISTORY = 40  # messages kept (20 turns)

CLAUDE_SYSTEM = (
    "You are a voice assistant running on the user's smart glasses. Your "
    "answers are spoken aloud: keep them to 1-3 conversational sentences "
    "unless the user asks for detail. The user may reference things they "
    "see; photos from the glasses camera may be attached to queries. Be "
    "direct, natural, and helpful."
)


def load_claude_history() -> list:
    """Same-day conversation history for the Claude brain."""
    try:
        with open(CLAUDE_HISTORY_FILE) as f:
            data = json.load(f)
        if data.get("date") == _today():
            return data.get("messages", [])
    except (OSError, ValueError):
        pass
    return []


def store_claude_history(messages: list):
    try:
        with open(CLAUDE_HISTORY_FILE, "w") as f:
            json.dump({"date": _today(), "messages": messages}, f)
    except OSError as e:
        print(f"[Bridge] Could not persist Claude history: {e}")


def clear_claude_history():
    try:
        os.unlink(CLAUDE_HISTORY_FILE)
    except OSError:
        pass


def trim_claude_history(messages: list, max_messages: int = CLAUDE_MAX_HISTORY) -> list:
    return messages[-max_messages:] if len(messages) > max_messages else messages


def build_claude_user_content(text: str, photo: bytes | None) -> list:
    """User content blocks: optional glasses photo first, then the words."""
    content = []
    if photo:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.b64encode(photo).decode(),
            },
        })
    content.append({"type": "text", "text": text})
    return content


# Retained legacy SDK helper - no longer on the request path (provider
# brains now go through ask_provider()/build_provider_request()); kept for
# reference and its tested same-day history helpers.
def ask_claude(text: str, photo: bytes | None = None) -> str | None:
    """Answer via the Claude API with same-day conversation memory.

    Fast path: no process spawn, no agent boot - typically 2-4s for a
    spoken-length reply. Photos go in as native image blocks.
    """
    try:
        import anthropic
    except ImportError:
        return "The Claude brain needs the anthropic package: pip install anthropic"

    history = load_claude_history()
    user_message = {"role": "user", "content": build_claude_user_content(text, photo)}

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            # Stable prefix cached; history rides behind it
            system=[{
                "type": "text",
                "text": CLAUDE_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=trim_claude_history(history + [user_message]),
        )
        reply = next(
            (b.text for b in response.content if b.type == "text"), None
        )
        if reply:
            # Persist text-only user turn (images would bloat the file and
            # re-bill on every later request)
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": reply})
            store_claude_history(trim_claude_history(history))
        return reply
    except anthropic.AuthenticationError:
        return ("The Claude API key is missing or invalid. Set "
                "ANTHROPIC_API_KEY for the bridge.")
    except anthropic.APIStatusError as e:
        print(f"[Claude] API error {e.status_code}: {e.message}")
        return "Sorry, the Claude API returned an error."
    except anthropic.APIConnectionError:
        return "Sorry, I couldn't reach the Claude API."
    except Exception as e:
        print(f"[Claude] Error: {e}")
        return f"Error: {e}"

# ── Hermes Agent ────────────────────────────────────────────────────────────

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
BOX_CHARS = set("╭╮╰╯│─═╞╡┌┐└┘┤├")


def extract_hermes_reply(raw: str) -> str | None:
    """Pull just the assistant's reply out of the hermes CLI output.

    The CLI prints: a Query echo, spinner lines, then the reply inside a
    box whose top border contains 'Hermes', then a session footer.
    """
    text = ANSI_RE.sub("", raw).replace("\r", "\n")
    lines = text.split("\n")

    reply_lines = []
    in_box = False
    for line in lines:
        stripped = line.strip()
        if not in_box:
            if "Hermes" in stripped and BOX_CHARS & set(stripped):
                in_box = True
            continue
        # Bottom border (or any pure box-drawing line) ends the reply
        if stripped and set(stripped) <= (BOX_CHARS | set(" ")):
            break
        cleaned = stripped.strip("│").strip()
        reply_lines.append(cleaned)

    reply = "\n".join(reply_lines).strip()
    return reply or None


def hermes_child_env() -> dict[str, str]:
    """The environment for the hermes child process.

    HERMES_BRIDGE_TOKEN is stripped. The child is an agent WITH TOOLS - it
    can read its own environment and report what it finds - so passing the
    bridge's shared secret down to it hands the key to the lock to anything
    that can get a question answered. Nothing else is removed: the hermes
    CLI needs its own provider keys, PATH, HOME and the rest to work at all.
    """
    env = {k: v for k, v in os.environ.items() if k != "HERMES_BRIDGE_TOKEN"}
    env["HERMES_NO_COLOR"] = "1"
    return env


def ask_hermes(
    text: str,
    image_path: str | None = None,
    resume: str | None = None,
) -> tuple[str | None, str | None]:
    """Send text (and optionally an image) to Hermes Agent.

    Returns (reply, session_id). session_id enables conversation memory
    via --resume on the next query; either value may be None.
    """
    # --query=<text>, not ["-q", text]: argparse reads a value beginning with
    # "-" as the next option, so an utterance transcribed as "-ish, maybe"
    # or "--help me out" made the hermes CLI exit with a usage error instead
    # of answering. The "=" form is unambiguous for any text at all.
    cmd = [HERMES_BIN, "chat", f"--query={text}", "-Q", "--cli"]
    if image_path:
        cmd += ["--image", image_path]
    if resume:
        cmd += ["--resume", resume]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120 if image_path else 60,
            env=hermes_child_env(),
        )
        session_id = extract_session_id(result.stderr)
        output = result.stdout.strip()
        if output:
            # -Q should print only the reply; if box UI sneaks in, unwrap it
            if BOX_CHARS & set(output):
                reply = extract_hermes_reply(output)
                if reply:
                    return reply, session_id
            return output, session_id
        if result.stderr.strip():
            # No reply on stdout - return the error text sans session line
            err = "\n".join(
                line for line in result.stderr.strip().splitlines()
                if not line.startswith("session_id:")
            ).strip()
            return (err or None), session_id
        return None, session_id
    except subprocess.TimeoutExpired:
        return "Sorry, Hermes took too long to respond.", None
    except Exception as e:
        print(f"[Hermes] Error: {e}")
        return f"Error: {e}", None


# ── Hermes gateway API brain (BRAIN="hermes-api") ──────────────────────────
# The always-running Hermes gateway serves an OpenAI-compatible chat API on
# the same Mac. Calling it skips the 1.6-2.5 s CLI boot `ask_hermes` pays on
# every utterance. Conversation memory lives server-side: the gateway loads
# the history for the X-Hermes-Session-Id header, so each request carries
# ONLY the new user message.
HERMES_API_BRAIN = "hermes-api"
HERMES_API_URL = os.environ.get(
    "HERMES_API_URL", "http://127.0.0.1:8642/v1/chat/completions")
HERMES_API_TIMEOUT = float(os.environ.get("HERMES_API_TIMEOUT", "180"))

# A separate file from SESSION_FILE on purpose: the CLI brain stores
# `local-…`/timestamp ids the gateway does not own (it answers 409), so
# switching brains must never hand one store's id to the other brain.
API_SESSION_FILE = os.path.expanduser(
    "~/.hermes_glasses_bridge_api_session.json")

HERMES_API_UNAVAILABLE_TOO_SLOW = "Sorry, Hermes took too long to respond."
HERMES_API_EMPTY = "Hermes didn't return a reply."


def hermes_api_key() -> str:
    """Read at call time so the value never sits in a module global."""
    return os.environ.get("HERMES_API_KEY", "")


def check_brain_config(brain: str) -> str | None:
    """Startup check: an error message when the brain can't work, else None.

    Never includes secret values.
    """
    if brain != HERMES_API_BRAIN:
        return None
    if not hermes_api_key():
        return ("HERMES_BRIDGE_BRAIN=hermes-api needs HERMES_API_KEY (the "
                "Hermes gateway API bearer key). Set it in the bridge "
                "environment and restart.")
    if not HERMES_API_URL.lower().startswith(("http://", "https://")):
        return ("HERMES_API_URL must start with http:// or https:// "
                "(e.g. http://127.0.0.1:8642/v1/chat/completions).")
    return None


def new_api_session_id() -> str:
    """glasses-<YYYYMMDD>-<12 hex>: bridge-generated, so the very first turn
    already carries a session header (without one the gateway derives an id
    from the first message's text and merges unrelated conversations)."""
    return "glasses-%s-%s" % (datetime.date.today().strftime("%Y%m%d"),
                              uuid.uuid4().hex[:12])


def load_api_session() -> str | None:
    """Stored gateway session id, if it is from today (same rules as
    load_session for the CLI brain)."""
    try:
        with open(API_SESSION_FILE) as f:
            data = json.load(f)
        if data.get("date") == _today() and data.get("session_id"):
            return data["session_id"]
    except (OSError, ValueError):
        pass
    return None


def store_api_session(session_id: str):
    try:
        with open(API_SESSION_FILE, "w") as f:
            json.dump({"session_id": session_id, "date": _today()}, f)
    except OSError as e:
        print(f"[Bridge] Could not persist API session: {e}")


def clear_api_session():
    try:
        os.unlink(API_SESSION_FILE)
    except OSError:
        pass


def build_hermes_api_request(
    url: str, api_key: str, session_id: str, text: str,
    photo: bytes | None = None,
) -> tuple[str, dict, dict]:
    """Return (url, headers, body) for one gateway chat turn."""
    if photo:
        content = [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {
                "url": "data:image/jpeg;base64,"
                       + base64.b64encode(photo).decode()}},
        ]
    else:
        content = text
    headers = {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
        "X-Hermes-Session-Id": session_id,
    }
    body = {"model": "hermes", "stream": False,
            "messages": [{"role": "user", "content": content}]}
    return url, headers, body


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def _hermes_api_error_message(body_bytes: bytes) -> str:
    try:
        data = json.loads(body_bytes.decode("utf-8", "replace"))
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            return str(err.get("message", ""))
        if isinstance(err, str):
            return err
    except (ValueError, AttributeError):
        pass
    return body_bytes.decode("utf-8", "replace")


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """A 3xx is an error, never followed: following one would re-send the
    Bearer key to wherever the Location header points."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises HTTPError(code) for the 3xx


# No proxies (an env HTTP(S)_PROXY must never see the Bearer key or the
# conversation) and no redirects.
_HERMES_API_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _RefuseRedirects())


def _post_hermes_api(api_key: str, session_id: str, text: str,
                     photo: bytes | None) -> tuple[int | None, str]:
    """One POST. Returns (status, reply_or_spoken_error).

    status None = transport failure (timeout / refused / anything else
    unexpected); the second value is then already the spoken message.
    Never raises.
    """
    try:
        url, headers, body = build_hermes_api_request(
            HERMES_API_URL, api_key, session_id, text, photo)
        request = urllib.request.Request(
            url, data=json.dumps(body).encode(), headers=headers,
            method="POST")
        with _HERMES_API_OPENER.open(request,
                                     timeout=HERMES_API_TIMEOUT) as resp:
            status, body_bytes = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, body_bytes = e.code, e.read()
    except (TimeoutError, socket.timeout):
        print(f"[Hermes API] Timed out after {HERMES_API_TIMEOUT:g}s")
        return None, HERMES_API_UNAVAILABLE_TOO_SLOW
    except urllib.error.URLError as e:
        if isinstance(e.reason, (TimeoutError, socket.timeout)):
            print(f"[Hermes API] Timed out after {HERMES_API_TIMEOUT:g}s")
            return None, HERMES_API_UNAVAILABLE_TOO_SLOW
        print("[Hermes API] Connection error: "
              + _redact(str(e.reason), api_key)[:200])
        return None, "Hermes is unavailable right now."
    except OSError as e:
        print("[Hermes API] Connection error: " + _redact(str(e), api_key)[:200])
        return None, "Hermes is unavailable right now."
    except Exception as e:  # malformed URL, IncompleteRead, ... - never crash
        print(f"[Hermes API] {type(e).__name__}: "
              + _redact(str(e), api_key)[:200])
        return None, "Hermes is unavailable right now."

    if status != 200:
        message = _redact(_hermes_api_error_message(body_bytes), api_key)
        print(f"[Hermes API] HTTP {status}: {message[:200]}")
        return status, f"Hermes is unavailable right now (HTTP {status})."
    try:
        data = json.loads(body_bytes.decode("utf-8"))
        reply = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        print(f"[Hermes API] Unparseable reply: {type(e).__name__}")
        return 200, HERMES_API_EMPTY
    if not isinstance(reply, str) or not reply.strip():
        return 200, HERMES_API_EMPTY
    return 200, reply.strip()


def ask_hermes_api(
    text: str,
    photo: bytes | None,
    session_id: str,
    new_session: bool,
) -> tuple[str, str | None]:
    """Ask the Hermes gateway API. Returns (reply_text, session_to_store).

    `text` is the query without persona; the voice persona is prefixed when
    `new_session` (same rule as the CLI brain). On HTTP 409 (session busy /
    owned elsewhere) retries ONCE with a fresh session id. session_to_store
    is the id to persist (None when nothing should be stored). The reply is
    always speakable text and never contains the key.
    """
    api_key = hermes_api_key()
    if not api_key:
        return ("The Hermes API brain needs HERMES_API_KEY. Set it for "
                "the bridge."), None

    query = (VOICE_PERSONA + text) if new_session else text
    status, reply = _post_hermes_api(api_key, session_id, query, photo)
    if status == 409:
        session_id = new_api_session_id()
        print(f"[Bridge] Hermes API session busy (409) - retrying with a "
              f"fresh session ({session_id})")
        status, reply = _post_hermes_api(
            api_key, session_id, VOICE_PERSONA + text, photo)
        if status == 409:
            # Still busy with a brand-new id: forget the stored one so the
            # next turn starts clean instead of hitting the same 409.
            clear_api_session()
    if status == 200:
        return reply, session_id
    return reply, None


# ── Text-to-Speech ─────────────────────────────────────────────────────────

def synthesize_speech(text: str) -> bytes | None:
    """Convert text to speech. Always returns raw PCM16 mono 24kHz -
    the only format the app can play."""
    # Try Edge TTS first (better quality)
    try:
        import edge_tts

        mp3_tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        mp3_tmp.close()
        wav_tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        wav_tmp.close()
        try:
            async def _synth():
                communicate = edge_tts.Communicate(text, "en-US-AriaNeural")
                await communicate.save(mp3_tmp.name)

            asyncio.run(_synth())

            # Decode MP3 → PCM16 mono 24kHz WAV. afconvert on macOS,
            # ffmpeg elsewhere (Linux).
            if shutil.which("afconvert"):
                cmd = ["afconvert", "-f", "WAVE", "-d", "LEI16@24000", "-c", "1",
                       mp3_tmp.name, wav_tmp.name]
            elif shutil.which("ffmpeg"):
                cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", mp3_tmp.name,
                       "-ar", "24000", "-ac", "1", "-sample_fmt", "s16",
                       wav_tmp.name]
            else:
                cmd = None
                print("[TTS] Neither afconvert nor ffmpeg found")

            if cmd:
                result = subprocess.run(cmd, capture_output=True, timeout=30)
                if result.returncode == 0:
                    with wave.open(wav_tmp.name, "rb") as wf:
                        return wf.readframes(wf.getnframes())
                print(f"[TTS] decode failed: {result.stderr.decode()[:200]}")
        finally:
            # NamedTemporaryFile(delete=False) - always clean up, whether
            # synthesis/decode succeeded, failed, or raised.
            for path in (mp3_tmp.name, wav_tmp.name):
                try:
                    os.unlink(path)
                except OSError:
                    pass
    except ImportError:
        pass
    except Exception as e:
        print(f"[TTS] Edge TTS error: {e}")

    # Fallback: macOS say command, directly as PCM16 24kHz WAV
    if not shutil.which("say"):
        print("[TTS] No 'say' fallback available on this platform")
        return None
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    try:
        result = subprocess.run(
            ["say", "-o", tmp.name,
             "--file-format=WAVE", "--data-format=LEI16@24000", text],
            capture_output=True, timeout=30,
        )
        if result.returncode != 0:
            print(f"[TTS] 'say' failed: {result.stderr.decode()[:200]}")
            return None
        with wave.open(tmp.name, "rb") as wf:
            return wf.readframes(wf.getnframes())
    except Exception as e:
        print(f"[TTS] Error: {e}")
        return None
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# ── WebSocket Handler ──────────────────────────────────────────────────────

async def await_photo(websocket, timeout: float = 25.0) -> bytes | None:
    """Wait for the app to answer a capture_photo request.

    Discards mic-audio (binary) frames that arrive meanwhile. Returns the
    decoded JPEG bytes, or None on photo_error or timeout.
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print("[Bridge] Photo wait timed out")
            return None
        try:
            message = await asyncio.wait_for(websocket.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            print("[Bridge] Photo wait timed out")
            return None
        if isinstance(message, bytes):
            continue  # stray binary frame while waiting - drop it
        try:
            data = json.loads(message)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"[Bridge] Malformed JSON while awaiting photo, ignored: {e}")
            continue
        if not isinstance(data, dict):
            print(f"[Bridge] Non-object JSON while awaiting photo, ignored: {data!r}")
            continue
        msg_type = data.get("type")
        if msg_type == "photo":
            try:
                return base64.b64decode(data.get("data", ""))
            except Exception as e:
                print(f"[Bridge] Bad photo payload: {e}")
                return None
        if msg_type == "photo_error":
            print(f"[Bridge] Photo error from app: {data.get('message')}")
            return None
        if msg_type == "debug":
            print(f"[App] {data.get('msg')}")
        # any other message type: keep waiting


async def process_query(websocket, text: str, conn_state: dict | None = None,
                        want_tts: bool | None = None):
    """Answer a text query: photo capture if visual, Hermes, TTS reply.

    The app transcribes on-device and sends {"type":"query"} text.
    conn_state carries per-connection context: {"last_photo_at": float}.
    want_tts: app's per-query choice of bridge TTS; None falls back to the
    HERMES_BRIDGE_TTS env default.
    """
    bridge_tts = BRIDGE_TTS if want_tts is None else bool(want_tts)
    if conn_state is None:
        conn_state = {"last_photo_at": 0.0}

    # ── Capture a photo for visual/deictic queries ──
    photo = None
    query_text = text
    if should_capture_photo(text, conn_state.get("last_photo_at", 0.0),
                            time.monotonic()):
        print("[Bridge] Visual query - requesting photo from glasses")
        await websocket.send(json.dumps({"type": "capture_photo"}))
        photo = await await_photo(websocket)
        if photo:
            conn_state["last_photo_at"] = time.monotonic()
            print(f"[Bridge] Photo received: {len(photo)} bytes")
        else:
            print("[Bridge] No photo - answering text-only")
            query_text = ("(No photo could be captured from the glasses.) "
                          + text)

    canon_brain = _canon_brain(BRAIN)
    started = time.monotonic()
    if canon_brain in ("anthropic", "openai", "gemini"):
        # ── Ask the provider API directly (fast path) ──
        print(f"[Bridge] Asking {canon_brain} ({CLAUDE_MODEL})...")
        response = await asyncio.to_thread(ask_provider, canon_brain, query_text, photo)
    elif canon_brain == HERMES_API_BRAIN:
        # ── Ask the always-running Hermes gateway (no CLI boot) ──
        session_id = load_api_session()
        new_session = session_id is None
        if new_session:
            session_id = new_api_session_id()
        print(f"[Bridge] Asking Hermes API... (session: {session_id})")
        response, stored_id = await asyncio.to_thread(
            ask_hermes_api, query_text, photo, session_id, new_session)
        if stored_id:
            store_api_session(stored_id)
    else:
        # ── Ask Hermes (with same-day conversation memory) ──
        image_path = None
        if photo:
            img_tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            img_tmp.write(photo)
            img_tmp.close()
            image_path = img_tmp.name
        resume = load_session()
        if not resume:
            # Fresh conversation: teach Hermes it is a glasses voice assistant
            query_text = VOICE_PERSONA + query_text
        print(f"[Bridge] Asking Hermes... (session: {resume or 'new'})")
        try:
            response, session_id = await asyncio.to_thread(
                ask_hermes, query_text, image_path, resume
            )
            if resume and not response:
                # Stored session may have been pruned - retry fresh once
                print("[Bridge] Resume failed - retrying with a fresh session")
                clear_session()
                response, session_id = await asyncio.to_thread(
                    ask_hermes, VOICE_PERSONA + query_text, image_path, None
                )
            if session_id:
                store_session(session_id)
        finally:
            if image_path:
                os.unlink(image_path)
    response_text = response or "I'm not sure what to say."

    if canon_brain in ("anthropic", "openai", "gemini"):
        print(f"[Bridge] Hermes: {response_text[:100]}...")
    else:
        elapsed = time.monotonic() - started
        print(f"[Bridge] Hermes ({elapsed:.1f}s): {response_text[:100]}...")

    # "tts": whether PCM audio follows. False → the app speaks the text
    # itself with on-device synthesis.
    await websocket.send(json.dumps({
        "type": "response",
        "text": response_text,
        "tts": bridge_tts,
    }))

    if bridge_tts:
        # ── Server-side TTS (legacy fallback, HERMES_BRIDGE_TTS=1) ──
        print("[Bridge] Generating speech...")
        await websocket.send(json.dumps({"type": "audio_start"}))

        audio_data = await asyncio.to_thread(synthesize_speech, response_text)
        if audio_data:
            # Send in chunks to avoid frame size limits
            chunk_size = 16384
            for i in range(0, len(audio_data), chunk_size):
                await websocket.send(audio_data[i:i+chunk_size])
                await asyncio.sleep(0.01)

        await websocket.send(json.dumps({"type": "audio_end"}))
    print("[Bridge] Response complete.")


def is_authorized(websocket) -> bool:
    """When AUTH_TOKEN is set, require ?token=<AUTH_TOKEN> in the WS path."""
    if not AUTH_TOKEN:
        return True
    try:
        path = websocket.request.path if websocket.request else ""
        query = parse_qs(urlparse(path).query)
        return hmac.compare_digest(query.get("token", [""])[0], AUTH_TOKEN)
    except Exception:
        return False


async def handle_connection(websocket):
    """Handle a single glasses connection."""
    if not is_authorized(websocket):
        print(f"[Bridge] REJECTED unauthorized connection from "
              f"{websocket.remote_address}")
        await websocket.close(code=4401, reason="unauthorized")
        return

    print(f"[Bridge] Glasses connected from {websocket.remote_address}")

    # Send welcome to confirm connection
    await websocket.send(json.dumps({"type": "welcome"}))
    # Per-connection context for photo-recency suppression
    conn_state = {"last_photo_at": 0.0}

    try:
        async for message in websocket:
            if isinstance(message, bytes):
                # Binary frames were the legacy mic-audio path; the app
                # transcribes on-device now. Ignore them.
                continue

            try:
                data = json.loads(message)
            except (json.JSONDecodeError, ValueError) as e:
                print(f"[Bridge] Malformed JSON frame ignored: {e}")
                continue
            if not isinstance(data, dict):
                print(f"[Bridge] Non-object JSON frame ignored: {data!r}")
                continue
            msg_type = data.get("type")

            if msg_type == "query":
                # App transcribed on-device and sends text directly
                text = (data.get("text") or "").strip()
                if text:
                    print(f"[Bridge] Query: {text}")
                    await process_query(websocket, text, conn_state,
                                        data.get("tts"))
                else:
                    await websocket.send(json.dumps({
                        "type": "error",
                        "message": "Empty query."
                    }))

            elif msg_type == "new_session":
                # Forget the conversation; next query starts fresh
                clear_session()
                clear_api_session()
                clear_claude_history()
                conn_state["last_photo_at"] = 0.0
                print("[Bridge] Conversation reset by app")
                await websocket.send(json.dumps({"type": "session_reset"}))

            elif msg_type == "debug":
                print(f"[App] {data.get('msg')}")

            elif msg_type == "ping":
                await websocket.send(json.dumps({"type": "pong"}))

    except websockets.exceptions.ConnectionClosed:
        print("[Bridge] Glasses disconnected")
    except Exception as e:
        print(f"[Bridge] Error: {e}")
    finally:
        print("[Bridge] Connection closed")


def local_ip() -> str:
    """Best-effort LAN IP for the connection hint."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "<your-mac-ip>"


def brain_label() -> str:
    canon = _canon_brain(BRAIN)
    if canon in ("anthropic", "openai", "gemini"):
        return f"({CLAUDE_MODEL})"
    if canon == HERMES_API_BRAIN:
        return "(gateway API)"
    return "(CLI agent)"


async def main():
    config_error = check_brain_config(_canon_brain(BRAIN))
    if config_error:
        print(f"[Bridge] FATAL: {config_error}")
        sys.exit(1)
    if not AUTH_TOKEN:
        print(f"""
!!! WARNING: no HERMES_BRIDGE_TOKEN set - this bridge is UNAUTHENTICATED.
!!! It is listening on ws://{HOST}:{PORT}/voice - anyone who can reach
!!! that address on the network can drive the agent (tool access
!!! included) with no credentials. Set HERMES_BRIDGE_TOKEN before
!!! exposing this beyond localhost.
""")
    print(f"""
╔══════════════════════════════════════════════════════════╗
║              Hermes Glasses Bridge Server                ║
║                                                          ║
║  Listening on ws://{HOST}:{PORT}/voice                       ║
║  STT: on the phone - the app sends text queries          ║
║  Brain: {BRAIN} {brain_label():<30}  ║
║                                                          ║
║  Connect your glasses app to:                            ║
║  ws://{local_ip()}:{PORT}/voice                             ║
╚══════════════════════════════════════════════════════════╝
""")
    # Glasses photos arrive as a single large base64-encoded JSON text frame
    # (a 1-3 MB raw JPEG becomes an even bigger base64 string), so raise
    # max_size well above the websockets default of 1 MiB to avoid a 1009
    # close before the frame is fully received.
    async with websockets.serve(handle_connection, HOST, PORT, max_size=16 * 1024 * 1024):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Bridge] Shutting down.")
