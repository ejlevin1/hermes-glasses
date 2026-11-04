import asyncio
import base64
import os
import tempfile
import unittest
import unittest.mock

import hermes_bridge as hb
from hermes_bridge import (
    is_visual_query, await_photo, build_provider_request, parse_provider_reply,
)


class TestIsVisualQuery(unittest.TestCase):
    def test_visual_phrases_match(self):
        for phrase in [
            "What am I looking at?",
            "can you see this",
            "READ THIS for me",
            "what is this thing in front of me",
            "take a picture",
            "use the camera",
        ]:
            self.assertTrue(is_visual_query(phrase), phrase)

    def test_non_visual_phrases_do_not_match(self):
        for phrase in [
            "what's the weather tomorrow",
            "tell me a joke",
            "who wrote hamlet",
            # bare 'picture'/'photo' must NOT trigger - meta-questions about
            # photos were re-photographing the room. (Note: a meta-question
            # containing the literal phrase 'take a picture' still triggers;
            # keyword matching cannot tell it from the command.)
            "why was a photo captured just now",
            "do you always talk this much",
        ]:
            self.assertFalse(is_visual_query(phrase), phrase)

    def test_deictic_covers_photo_reference(self):
        from hermes_bridge import should_capture_photo
        # "describe this photo" flows through the deictic path now
        self.assertTrue(should_capture_photo("describe this photo", 0.0, 1000.0))
        # ...but meta/degree deictics never capture
        self.assertFalse(should_capture_photo("do you always talk this much", 0.0, 1000.0))
        self.assertFalse(should_capture_photo("keep it short like this answer", 0.0, 1000.0))
        # auxiliary verbs after this/that must not trigger
        self.assertFalse(should_capture_photo("hi this is a test", 0.0, 1000.0))
        self.assertFalse(should_capture_photo("that was great", 0.0, 1000.0))
        self.assertFalse(should_capture_photo("this should work now", 0.0, 1000.0))


class FakeWebSocket:
    """Minimal stand-in exposing recv()/send() like websockets."""

    def __init__(self, messages):
        self._messages = list(messages)
        self.sent = []

    async def recv(self):
        if not self._messages:
            await asyncio.sleep(30)  # simulate silence until timeout
        return self._messages.pop(0)

    async def send(self, message):
        self.sent.append(message)


class TestAwaitPhoto(unittest.TestCase):
    def test_photo_message_returns_decoded_bytes(self):
        jpeg = b"\xff\xd8\xff\xe0fakejpeg"
        ws = FakeWebSocket([
            b"\x00\x01binary-mic-audio-to-skip",
            '{"type":"debug","msg":"ignored"}',
            '{"type":"photo","data":"%s"}' % base64.b64encode(jpeg).decode(),
        ])
        result = asyncio.run(await_photo(ws, timeout=2.0))
        self.assertEqual(result, jpeg)

    def test_photo_error_returns_none(self):
        ws = FakeWebSocket(['{"type":"photo_error","message":"no camera"}'])
        result = asyncio.run(await_photo(ws, timeout=2.0))
        self.assertIsNone(result)

    def test_timeout_returns_none(self):
        ws = FakeWebSocket([])
        result = asyncio.run(await_photo(ws, timeout=0.2))
        self.assertIsNone(result)

    def test_malformed_json_frame_ignored_keeps_waiting(self):
        # A malformed text frame must not raise / abort the wait - it should
        # be logged and skipped, leaving the loop to pick up the next frame.
        jpeg = b"\xff\xd8\xff\xe0fakejpeg"
        ws = FakeWebSocket([
            "{not valid json",
            '{"type":"photo","data":"%s"}' % base64.b64encode(jpeg).decode(),
        ])
        result = asyncio.run(await_photo(ws, timeout=2.0))
        self.assertEqual(result, jpeg)

    def test_non_dict_json_frame_ignored_keeps_waiting(self):
        # Valid JSON that isn't an object (e.g. a bare array) must not raise
        # AttributeError from `.get` - it should be skipped like malformed
        # JSON.
        jpeg = b"\xff\xd8\xff\xe0fakejpeg"
        ws = FakeWebSocket([
            "[1]",
            '{"type":"photo","data":"%s"}' % base64.b64encode(jpeg).decode(),
        ])
        result = asyncio.run(await_photo(ws, timeout=2.0))
        self.assertEqual(result, jpeg)


class TestProcessQuery(unittest.TestCase):
    """process_query: text in → hermes → response + TTS out, incl. photo path."""

    def _run(self, text, ws, fake_reply="ok", fake_tts=b"\x00\x00",
             stored_session=None, bridge_tts=True):
        orig_ask, orig_tts = hb.ask_hermes, hb.synthesize_speech
        orig_bridge_tts = hb.BRIDGE_TTS
        hb.BRIDGE_TTS = bridge_tts
        # Pin the brain so an exported HERMES_BRIDGE_BRAIN can't route this
        # around the fake ask_hermes (KeyError, or a real HTTP call).
        orig_brain = hb.BRAIN
        hb.BRAIN = "hermes"
        orig_session_file = hb.SESSION_FILE
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp.close()
        os.unlink(tmp.name)  # start with no stored session
        hb.SESSION_FILE = tmp.name
        if stored_session:
            hb.store_session(stored_session)
        calls = {}

        def fake_ask(query, image_path=None, resume=None):
            calls["query"] = query
            calls["image_path"] = image_path
            calls["resume"] = resume
            return fake_reply, "20260710_000000_test"

        hb.ask_hermes = fake_ask
        hb.synthesize_speech = lambda t: fake_tts
        try:
            asyncio.run(hb.process_query(ws, text))
        finally:
            hb.ask_hermes, hb.synthesize_speech = orig_ask, orig_tts
            hb.BRIDGE_TTS = orig_bridge_tts
            hb.BRAIN = orig_brain
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            hb.SESSION_FILE = orig_session_file
        return calls

    def test_default_no_bridge_tts(self):
        ws = FakeWebSocket([])
        self._run("tell me a joke", ws, bridge_tts=False)
        sent = [m for m in ws.sent if isinstance(m, str)]
        self.assertTrue(any('"tts": false' in m for m in sent))
        self.assertFalse(any('"audio_start"' in m for m in sent))
        self.assertFalse(any('"audio_end"' in m for m in sent))

    def test_plain_query_answers_without_photo(self):
        ws = FakeWebSocket([])
        calls = self._run("tell me a joke", ws)
        # Fresh session → persona preamble prefixed
        self.assertTrue(calls["query"].endswith("tell me a joke"))
        self.assertIn("voice assistant", calls["query"])
        self.assertIsNone(calls["resume"])
        self.assertIsNone(calls["image_path"])
        sent_types = [m for m in ws.sent if isinstance(m, str)]
        self.assertFalse(any('"capture_photo"' in m for m in sent_types))
        self.assertTrue(any('"response"' in m for m in sent_types))
        self.assertTrue(any('"audio_end"' in m for m in sent_types))

    def test_resumed_session_skips_persona(self):
        ws = FakeWebSocket([])
        calls = self._run("tell me a joke", ws, stored_session="sid123")
        self.assertEqual(calls["query"], "tell me a joke")
        self.assertEqual(calls["resume"], "sid123")

    def test_visual_query_requests_photo_and_passes_image(self):
        jpeg = b"\xff\xd8\xff\xe0fakejpeg"
        ws = FakeWebSocket([
            '{"type":"photo","data":"%s"}' % base64.b64encode(jpeg).decode(),
        ])
        calls = self._run("what am I looking at", ws)
        self.assertTrue(any('"capture_photo"' in m
                            for m in ws.sent if isinstance(m, str)))
        self.assertIsNotNone(calls["image_path"])
        self.assertTrue(calls["query"].endswith("what am I looking at"))

    def test_visual_query_photo_error_falls_back_to_text(self):
        ws = FakeWebSocket(['{"type":"photo_error","message":"no camera"}'])
        calls = self._run("what am I looking at", ws)
        self.assertIsNone(calls["image_path"])
        self.assertIn("No photo could be captured", calls["query"])


class TestSessionStore(unittest.TestCase):
    def setUp(self):
        self.hb = hb
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self.orig = hb.SESSION_FILE
        hb.SESSION_FILE = self.tmp.name

    def tearDown(self):
        self.hb.SESSION_FILE = self.orig
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def test_store_and_load_same_day(self):
        self.hb.store_session("20260710_120000_abc123")
        self.assertEqual(self.hb.load_session(), "20260710_120000_abc123")

    def test_stale_session_not_loaded(self):
        import json as j
        with open(self.tmp.name, "w") as f:
            j.dump({"session_id": "old", "date": "2020-01-01"}, f)
        self.assertIsNone(self.hb.load_session())

    def test_clear(self):
        self.hb.store_session("x")
        self.hb.clear_session()
        self.assertIsNone(self.hb.load_session())


class TestSessionIdExtraction(unittest.TestCase):
    def test_extracts_from_stderr(self):
        from hermes_bridge import extract_session_id
        self.assertEqual(
            extract_session_id("\nsession_id: 20260710_162225_7c1fec\n"),
            "20260710_162225_7c1fec",
        )
        self.assertIsNone(extract_session_id("no id here"))


class TestShouldCapturePhoto(unittest.TestCase):
    def test_explicit_keyword_always_captures(self):
        from hermes_bridge import should_capture_photo
        self.assertTrue(should_capture_photo("take a picture", 100.0, 110.0))

    def test_deictic_triggers_capture(self):
        from hermes_bridge import should_capture_photo
        for phrase in ["how do I make this drink", "what's that building",
                       "is the one on the left better"]:
            self.assertTrue(should_capture_photo(phrase, 0.0, 1000.0), phrase)

    def test_deictic_suppressed_by_recent_photo(self):
        from hermes_bridge import should_capture_photo
        # photo 30s ago → deictic follow-up uses memory, no re-capture
        self.assertFalse(
            should_capture_photo("how do I make this drink", 970.0, 1000.0))

    def test_non_visual_never_captures(self):
        from hermes_bridge import should_capture_photo
        self.assertFalse(should_capture_photo("tell me a joke", 0.0, 1000.0))


# Tests for the retained legacy SDK helper (ask_claude / build_claude_user_content
# / trim_claude_history) - not on the live request path, which now goes through
# ask_provider()/build_provider_request(); kept for reference.
class TestClaudeBrain(unittest.TestCase):
    def test_user_content_text_only(self):
        from hermes_bridge import build_claude_user_content
        content = build_claude_user_content("hello there", None)
        self.assertEqual(content, [{"type": "text", "text": "hello there"}])

    def test_user_content_with_photo(self):
        from hermes_bridge import build_claude_user_content
        content = build_claude_user_content("what is this", b"\xff\xd8jpegbytes")
        self.assertEqual(content[0]["type"], "image")
        self.assertEqual(content[0]["source"]["type"], "base64")
        self.assertEqual(content[0]["source"]["media_type"], "image/jpeg")
        self.assertEqual(content[1], {"type": "text", "text": "what is this"})

    def test_history_trimmed_to_cap(self):
        from hermes_bridge import trim_claude_history
        history = [{"role": "user", "content": f"m{i}"} for i in range(100)]
        trimmed = trim_claude_history(history, max_messages=40)
        self.assertEqual(len(trimmed), 40)
        self.assertEqual(trimmed[-1]["content"], "m99")

    def test_claude_history_store_same_day(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp.close()
        os.unlink(tmp.name)
        orig = hb.CLAUDE_HISTORY_FILE
        hb.CLAUDE_HISTORY_FILE = tmp.name
        try:
            hb.store_claude_history([{"role": "user", "content": "hi"}])
            self.assertEqual(len(hb.load_claude_history()), 1)
            hb.clear_claude_history()
            self.assertEqual(hb.load_claude_history(), [])
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            hb.CLAUDE_HISTORY_FILE = orig


class ProviderRequestTests(unittest.TestCase):
    def test_anthropic_request_shape(self):
        url, headers, body = build_provider_request(
            "anthropic", "claude-opus-4-8", "https://api.anthropic.com",
            "sk-ant", "hello", None)
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(headers["x-api-key"], "sk-ant")
        self.assertEqual(body["model"], "claude-opus-4-8")

    def test_anthropic_request_has_persona_system_prompt(self):
        from hermes_bridge import CLAUDE_SYSTEM
        _, _, body = build_provider_request(
            "anthropic", "claude-opus-4-8", "https://api.anthropic.com",
            "sk-ant", "hello", None)
        self.assertIsInstance(body["system"], str)
        self.assertTrue(body["system"])
        self.assertEqual(body["system"], CLAUDE_SYSTEM)

    def test_openai_request_shape_with_image(self):
        url, headers, body = build_provider_request(
            "openai", "gpt-4o", "https://api.openai.com", "sk-oai", "look", "QUJD")
        self.assertEqual(url, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer sk-oai")
        content = body["messages"][-1]["content"]
        self.assertTrue(any(p.get("type") == "image_url" for p in content))

    def test_openai_request_has_persona_system_message_first(self):
        from hermes_bridge import CLAUDE_SYSTEM
        _, _, body = build_provider_request(
            "openai", "gpt-4o", "https://api.openai.com", "sk-oai", "look", None)
        self.assertEqual(body["messages"][0],
                          {"role": "system", "content": CLAUDE_SYSTEM})

    def test_gemini_request_shape(self):
        url, headers, body = build_provider_request(
            "gemini", "gemini-2.5-flash",
            "https://generativelanguage.googleapis.com", "g-key", "hi", None)
        self.assertTrue(url.endswith(":generateContent"))
        # The key belongs in a header, not the query string (proxy logs).
        self.assertNotIn("key=", url)
        self.assertEqual(headers["x-goog-api-key"], "g-key")

    def test_gemini_request_has_persona_system_instruction(self):
        _, _, body = build_provider_request(
            "gemini", "gemini-2.5-flash",
            "https://generativelanguage.googleapis.com", "g-key", "hi", None)
        text = body["systemInstruction"]["parts"][0]["text"]
        self.assertIsInstance(text, str)
        self.assertTrue(text)

    def test_parse_replies(self):
        self.assertEqual(
            parse_provider_reply("anthropic", 200,
                b'{"content":[{"type":"text","text":"a"}]}'), "a")
        self.assertEqual(
            parse_provider_reply("openai", 200,
                b'{"choices":[{"message":{"content":"b"}}]}'), "b")
        self.assertEqual(
            parse_provider_reply("gemini", 200,
                b'{"candidates":[{"content":{"parts":[{"text":"c"}]}}]}'), "c")

    def test_claude_is_alias_for_anthropic(self):
        url, _, _ = build_provider_request(
            "claude", "claude-opus-4-8", "https://api.anthropic.com", "k", "hi", None)
        self.assertTrue(url.endswith("/v1/messages"))


class AskHermesChildProcessTests(unittest.TestCase):
    """The argv and the environment handed to the hermes CLI.

    Both of these are invisible from the outside: a leaked token produces a
    perfectly normal answer, and a query eaten by argparse produces a usage
    error that looks like the agent said something odd. So they are asserted
    on the call itself.
    """

    def setUp(self):
        self.calls = []

        def fake_run(cmd, **kwargs):
            self.calls.append((cmd, kwargs))
            return unittest.mock.Mock(stdout="ok", stderr="", returncode=0)

        patcher = unittest.mock.patch.object(hb.subprocess, "run", fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)

    @property
    def argv(self):
        return self.calls[0][0]

    @property
    def env(self):
        return self.calls[0][1]["env"]

    def test_query_passed_as_single_equals_argument(self):
        hb.ask_hermes("what is the weather")
        self.assertIn("--query=what is the weather", self.argv)
        # the old two-token form must be gone, or a leading "-" is an option
        self.assertNotIn("-q", self.argv)

    def test_query_starting_with_dash_is_not_parsed_as_an_option(self):
        # "-q -foo" made argparse fail with "expected one argument"; every
        # one of these reaches the CLI as a single token now.
        for text in ["-foo", "--help", "-ish, maybe", "--image /etc/passwd"]:
            self.calls.clear()
            hb.ask_hermes(text)
            self.assertIn(f"--query={text}", self.argv, text)
            # exactly one argv entry carries the user's text, so the CLI
            # cannot read part of it as a flag of its own
            carriers = [a for a in self.argv if text in a]
            self.assertEqual(carriers, [f"--query={text}"], text)

    def test_image_and_resume_still_passed(self):
        hb.ask_hermes("look", image_path="/tmp/x.jpg", resume="sess-1")
        self.assertIn("--image", self.argv)
        self.assertIn("/tmp/x.jpg", self.argv)
        self.assertIn("--resume", self.argv)
        self.assertIn("sess-1", self.argv)

    def test_bridge_token_is_not_in_the_child_environment(self):
        with unittest.mock.patch.dict(
            os.environ,
            {"HERMES_BRIDGE_TOKEN": "s3cret-token", "ANTHROPIC_API_KEY": "k"},
        ):
            hb.ask_hermes("hi")
        self.assertNotIn("HERMES_BRIDGE_TOKEN", self.env)
        self.assertNotIn("s3cret-token", self.env.values())

    def test_everything_else_is_still_inherited(self):
        # The hermes CLI needs its own provider keys, PATH and HOME; only the
        # one variable is stripped.
        with unittest.mock.patch.dict(
            os.environ,
            {"HERMES_BRIDGE_TOKEN": "s3cret", "ANTHROPIC_API_KEY": "k",
             "HOME": "/Users/test", "HERMES_BRIDGE_PORT": "8766"},
        ):
            hb.ask_hermes("hi")
        self.assertEqual(self.env["ANTHROPIC_API_KEY"], "k")
        self.assertEqual(self.env["HOME"], "/Users/test")
        # a *different* HERMES_BRIDGE_* name must survive - the strip is by
        # exact name, not by prefix
        self.assertEqual(self.env["HERMES_BRIDGE_PORT"], "8766")
        self.assertEqual(self.env["HERMES_NO_COLOR"], "1")

    def test_env_builder_is_a_plain_copy_when_no_token_is_set(self):
        env_without = dict(os.environ)
        env_without.pop("HERMES_BRIDGE_TOKEN", None)
        with unittest.mock.patch.dict(os.environ, env_without, clear=True):
            built = hb.hermes_child_env()
        self.assertEqual(
            {k: v for k, v in built.items() if k != "HERMES_NO_COLOR"},
            env_without,
        )

# ── Hermes gateway API brain (BRAIN="hermes-api") ──────────────────────────
import contextlib
import http.server
import io
import json
import re
import threading
import time
import urllib.request

FAKE_KEY = "sk-test-SECRET-key-0123456789"


class FakeGateway:
    """Local OpenAI-compatible fake on 127.0.0.1:<free port>.

    `responses` is a queue of (status, body_dict, delay_s[, headers]); the last entry
    repeats once the queue is drained. Every request is recorded.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        gateway = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                gateway.requests.append({
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": json.loads(raw.decode()),
                })
                if len(gateway.responses) > 1:
                    entry = gateway.responses.pop(0)
                else:
                    entry = gateway.responses[0]
                status, body, delay = entry[:3]
                extra_headers = entry[3] if len(entry) > 3 else {}
                if delay:
                    time.sleep(delay)
                payload = json.dumps(body).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    for name, value in extra_headers.items():
                        self.send_header(name, value)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass  # client gave up (timeout test)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d/p/hostbot/v1/chat/completions" % self.port

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class QuerySocket(FakeWebSocket):
    """Drives handle_connection: yields each queued message, then closes."""
    remote_address = ("127.0.0.1", 1)
    request = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class _FakeResponse:
    def __init__(self, status=200, body=b"", read_error=None):
        self.status, self._body, self._read_error = status, body, read_error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        if self._read_error:
            raise self._read_error
        return self._body


def ok_reply(text):
    return (200, {"choices": [{"message": {"role": "assistant",
                                           "content": text}}]}, 0)


class HermesApiBrainTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmpdir, True))
        saved = {name: getattr(hb, name) for name in (
            "BRAIN", "BRIDGE_TTS", "SESSION_FILE", "API_SESSION_FILE",
            "HERMES_API_URL", "HERMES_API_TIMEOUT")}

        def restore():
            for name, value in saved.items():
                setattr(hb, name, value)
        self.addCleanup(restore)
        hb.BRAIN = "hermes-api"
        hb.BRIDGE_TTS = False
        hb.SESSION_FILE = os.path.join(self.tmpdir, "cli_session.json")
        hb.API_SESSION_FILE = os.path.join(self.tmpdir, "api_session.json")
        hb.HERMES_API_TIMEOUT = 5.0
        env = unittest.mock.patch.dict(os.environ, {"HERMES_API_KEY": FAKE_KEY})
        env.start()
        self.addCleanup(env.stop)
        self.log = io.StringIO()

    def gateway(self, responses):
        gw = FakeGateway(responses)
        self.addCleanup(gw.close)
        hb.HERMES_API_URL = gw.url
        return gw

    def ask(self, text, ws=None):
        """Run one process_query turn; returns the response frame dict."""
        ws = ws or FakeWebSocket([])
        with contextlib.redirect_stdout(self.log):
            asyncio.run(hb.process_query(ws, text))
        frames = [json.loads(m) for m in ws.sent if isinstance(m, str)]
        responses = [f for f in frames if f.get("type") == "response"]
        self.assertEqual(len(responses), 1)
        return responses[0]

    def assertKeyNowhere(self, *texts):
        for t in list(texts) + [self.log.getvalue()]:
            self.assertNotIn(FAKE_KEY, t)
            self.assertNotIn("SECRET", t)

    # ── request shape ──
    def test_request_path_auth_and_single_message(self):
        gw = self.gateway([ok_reply("Hi there.")])
        reply = self.ask("tell me a joke")
        self.assertEqual(reply["text"], "Hi there.")
        self.assertEqual(len(gw.requests), 1)
        req = gw.requests[0]
        self.assertEqual(req["path"], "/p/hostbot/v1/chat/completions")
        self.assertEqual(req["headers"]["authorization"], "Bearer " + FAKE_KEY)
        self.assertEqual(req["headers"]["content-type"], "application/json")
        body = req["body"]
        self.assertEqual(body["model"], "hermes")
        self.assertIs(body["stream"], False)
        self.assertEqual(len(body["messages"]), 1)
        self.assertEqual(body["messages"][0]["role"], "user")
        # First turn carries the voice persona, like the CLI brain
        self.assertIn("voice assistant", body["messages"][0]["content"])
        self.assertTrue(body["messages"][0]["content"].endswith("tell me a joke"))
        self.assertKeyNowhere(json.dumps(reply))

    def test_session_header_on_first_and_second_turn_and_stable(self):
        gw = self.gateway([ok_reply("One."), ok_reply("Two.")])
        self.ask("first question")
        self.ask("second question")
        ids = [r["headers"].get("x-hermes-session-id") for r in gw.requests]
        self.assertEqual(len(ids), 2)
        self.assertIsNotNone(ids[0])
        self.assertRegex(ids[0], r"^glasses-\d{8}-[0-9a-f]{12}$")
        self.assertEqual(ids[0], ids[1])
        # Second turn: only the new message, no persona, no history
        second = gw.requests[1]["body"]["messages"]
        self.assertEqual(second, [{"role": "user", "content": "second question"}])
        self.assertIn("[Bridge] Asking Hermes API... (session: %s)" % ids[0],
                      self.log.getvalue())
        self.assertRegex(self.log.getvalue(), r"\[Bridge\] Hermes \(\d+\.\ds\): ")

    def test_image_part_shape(self):
        gw = self.gateway([ok_reply("A mug.")])
        jpeg = b"\xff\xd8\xff\xe0fakejpeg"
        ws = FakeWebSocket([
            '{"type":"photo","data":"%s"}' % base64.b64encode(jpeg).decode()])
        self.ask("what am I looking at", ws)
        content = gw.requests[0]["body"]["messages"][0]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(len(content), 2)
        self.assertEqual(content[0]["type"], "text")
        self.assertTrue(content[0]["text"].endswith("what am I looking at"))
        self.assertEqual(content[1], {"type": "image_url", "image_url": {
            "url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}})
        self.assertIsNotNone(gw.requests[0]["headers"].get("x-hermes-session-id"))

    # ── errors ──
    def test_409_retries_once_with_fresh_id_and_stores_it(self):
        hb.store_api_session("glasses-20260101-aaaaaaaaaaaa")
        gw = self.gateway([
            (409, {"error": {"message": "session busy"}}, 0),
            ok_reply("Recovered."),
        ])
        reply = self.ask("hello")
        self.assertEqual(reply["text"], "Recovered.")
        self.assertEqual(len(gw.requests), 2)
        first = gw.requests[0]["headers"]["x-hermes-session-id"]
        second = gw.requests[1]["headers"]["x-hermes-session-id"]
        self.assertEqual(first, "glasses-20260101-aaaaaaaaaaaa")
        self.assertNotEqual(first, second)
        self.assertRegex(second, r"^glasses-\d{8}-[0-9a-f]{12}$")
        self.assertEqual(hb.load_api_session(), second)
        # Fresh server session → persona again
        self.assertIn("voice assistant",
                      gw.requests[1]["body"]["messages"][0]["content"])

    def test_409_twice_retries_only_once(self):
        gw = self.gateway([(409, {"error": {"message": "busy"}}, 0)])
        reply = self.ask("hello")
        self.assertEqual(len(gw.requests), 2)
        self.assertEqual(reply["text"],
                         "Hermes is unavailable right now (HTTP 409).")

    def test_500_returns_friendly_text_and_logs_message(self):
        long_msg = "boom " + "x" * 500
        self.gateway([(500, {"error": {"message": long_msg}}, 0)])
        reply = self.ask("hello")
        self.assertEqual(reply["text"],
                         "Hermes is unavailable right now (HTTP 500).")
        log = self.log.getvalue()
        self.assertIn("HTTP 500: boom", log)
        line = next(l for l in log.splitlines() if "HTTP 500:" in l)
        self.assertLessEqual(len(line.split("HTTP 500: ", 1)[1]), 200)
        self.assertNotIn("x" * 201, log)
        self.assertKeyNowhere(json.dumps(reply))
        # Failed turn stores nothing
        self.assertIsNone(hb.load_api_session())

    def test_server_error_echoing_key_is_redacted(self):
        self.gateway([(401, {"error": {"message": "bad key " + FAKE_KEY}}, 0)])
        reply = self.ask("hello")
        self.assertEqual(reply["text"],
                         "Hermes is unavailable right now (HTTP 401).")
        self.assertKeyNowhere(json.dumps(reply))

    def test_timeout_returns_friendly_text(self):
        hb.HERMES_API_TIMEOUT = 0.3
        self.gateway([(200, {"choices": []}, 1.5)])
        reply = self.ask("hello")
        self.assertEqual(reply["text"], "Sorry, Hermes took too long to respond.")
        self.assertKeyNowhere(json.dumps(reply))

    def test_connection_refused_returns_friendly_text(self):
        gw = self.gateway([ok_reply("never")])
        gw.close()
        reply = self.ask("hello")
        self.assertEqual(reply["text"], "Hermes is unavailable right now.")
        self.assertKeyNowhere(json.dumps(reply))

    def test_empty_content_returns_no_reply_text(self):
        self.gateway([ok_reply("")])
        reply = self.ask("hello")
        self.assertEqual(reply["text"], "Hermes didn't return a reply.")

    def test_missing_key_is_a_startup_error_and_a_spoken_error(self):
        with unittest.mock.patch.dict(os.environ, {"HERMES_API_KEY": ""}):
            self.assertIn("HERMES_API_KEY", hb.check_brain_config("hermes-api"))
            reply, stored = hb.ask_hermes_api("hi", None, "glasses-x", True)
        self.assertIn("HERMES_API_KEY", reply)
        self.assertIsNone(stored)
        self.assertIsNone(hb.check_brain_config("hermes-api"))
        self.assertIsNone(hb.check_brain_config("hermes"))

    def test_main_exits_without_key(self):
        hb.BRAIN = "hermes-api"
        with unittest.mock.patch.dict(os.environ, {"HERMES_API_KEY": ""}):
            with contextlib.redirect_stdout(self.log):
                with self.assertRaises(SystemExit):
                    asyncio.run(hb.main())
        self.assertIn("HERMES_API_KEY", self.log.getvalue())

    # ── session store ──
    def test_new_session_rotates_the_id(self):
        gw = self.gateway([ok_reply("One."), ok_reply("Two.")])
        self.ask("first")

        ws = QuerySocket(['{"type":"new_session"}'])
        with contextlib.redirect_stdout(self.log):
            asyncio.run(hb.handle_connection(ws))
        self.assertIsNone(hb.load_api_session())
        self.ask("second")
        ids = [r["headers"]["x-hermes-session-id"] for r in gw.requests]
        self.assertNotEqual(ids[0], ids[1])
        self.assertIn("voice assistant",
                      gw.requests[1]["body"]["messages"][0]["content"])

    def test_stale_api_session_not_reused(self):
        with open(hb.API_SESSION_FILE, "w") as f:
            json.dump({"session_id": "glasses-20000101-000000000000",
                       "date": "2000-01-01"}, f)
        self.assertIsNone(hb.load_api_session())

    def test_cli_session_store_untouched_and_never_sent(self):
        hb.store_session("local-20260710_000000_cli")
        with open(hb.SESSION_FILE) as f:
            before = f.read()
        gw = self.gateway([ok_reply("One."), ok_reply("Two.")])
        self.ask("first")
        self.ask("second")
        with open(hb.SESSION_FILE) as f:
            self.assertEqual(f.read(), before)
        for r in gw.requests:
            self.assertTrue(
                r["headers"]["x-hermes-session-id"].startswith("glasses-"))
        self.assertNotEqual(hb.SESSION_FILE, hb.API_SESSION_FILE)

    # ── fix round: R1 (never raise), R2 (no redirects / proxies), R3 ──
    def test_malformed_url_returns_friendly_text(self):
        for bad in ("not a url", "ftp//127.0.0.1/x", "http://[::1"):
            hb.HERMES_API_URL = bad
            reply = self.ask("hello")
            self.assertEqual(reply["text"], "Hermes is unavailable right now.")
        self.assertKeyNowhere(json.dumps(reply))
        self.assertIsNone(hb.load_api_session())

    def test_incomplete_read_returns_friendly_text(self):
        import http.client
        err = http.client.IncompleteRead(b"partial " + FAKE_KEY.encode(), 99)
        with unittest.mock.patch.object(
                hb._HERMES_API_OPENER, "open",
                return_value=_FakeResponse(read_error=err)):
            reply = self.ask("hello")
        self.assertEqual(reply["text"], "Hermes is unavailable right now.")
        self.assertIn("[Hermes API] IncompleteRead", self.log.getvalue())
        self.assertKeyNowhere(json.dumps(reply))

    def test_ws_loop_keeps_running_after_unexpected_error(self):
        import http.client
        ok = json.dumps({"choices": [{"message": {"content": "Second."}}]})
        with unittest.mock.patch.object(
                hb._HERMES_API_OPENER, "open", side_effect=[
                    _FakeResponse(read_error=http.client.IncompleteRead(b"", 5)),
                    _FakeResponse(body=ok.encode())]):
            ws = QuerySocket(['{"type":"query","text":"first"}',
                              '{"type":"query","text":"second"}'])
            with contextlib.redirect_stdout(self.log):
                asyncio.run(hb.handle_connection(ws))
        texts = [json.loads(m).get("text") for m in ws.sent
                 if isinstance(m, str) and json.loads(m).get("type") == "response"]
        self.assertEqual(texts, ["Hermes is unavailable right now.", "Second."])

    def test_check_brain_config_rejects_non_http_url(self):
        for bad in ("127.0.0.1:8642/v1/chat/completions", "ftp://x/y", ""):
            hb.HERMES_API_URL = bad
            self.assertIn("HERMES_API_URL", hb.check_brain_config("hermes-api"))
        for good in ("http://127.0.0.1:8642/v1/chat/completions",
                     "https://gw.example/p/hostbot/v1/chat/completions"):
            hb.HERMES_API_URL = good
            self.assertIsNone(hb.check_brain_config("hermes-api"))
        hb.HERMES_API_URL = "ftp://x/y"
        self.assertIsNone(hb.check_brain_config("hermes"))

    def test_main_exits_on_bad_url(self):
        hb.HERMES_API_URL = "127.0.0.1:8642/v1/chat/completions"
        with contextlib.redirect_stdout(self.log):
            with self.assertRaises(SystemExit):
                asyncio.run(hb.main())
        self.assertIn("FATAL: HERMES_API_URL", self.log.getvalue())
        self.assertKeyNowhere()

    def test_redirect_is_refused_and_key_not_resent(self):
        target = FakeGateway([ok_reply("should never be asked")])
        self.addCleanup(target.close)
        self.gateway([(302, {}, 0, {"Location": target.url})])
        reply = self.ask("hello")
        self.assertEqual(reply["text"],
                         "Hermes is unavailable right now (HTTP 302).")
        self.assertEqual(target.requests, [])
        self.assertIsNone(hb.load_api_session())
        self.assertKeyNowhere(json.dumps(reply))

    def test_opener_ignores_proxy_environment(self):
        proxy = FakeGateway([ok_reply("via proxy")])
        self.addCleanup(proxy.close)
        gw = self.gateway([ok_reply("Direct.")])
        env = {k: "" for k in ("no_proxy", "NO_PROXY")}
        env.update({"http_proxy": "http://127.0.0.1:%d" % proxy.port,
                    "HTTP_PROXY": "http://127.0.0.1:%d" % proxy.port})
        with unittest.mock.patch.dict(os.environ, env):
            reply = self.ask("hello")
        self.assertEqual(reply["text"], "Direct.")
        self.assertEqual(proxy.requests, [])
        self.assertEqual(len(gw.requests), 1)

    def test_409_twice_clears_stored_session(self):
        hb.store_api_session("glasses-20260101-aaaaaaaaaaaa")
        gw = self.gateway([(409, {"error": {"message": "busy"}}, 0)])
        self.ask("hello")
        self.assertEqual(len(gw.requests), 2)
        self.assertIsNone(hb.load_api_session())
        # Next turn starts a fresh session (persona again), not the stale id
        gw.responses = [ok_reply("Back.")]
        self.ask("again")
        third = gw.requests[2]
        self.assertNotEqual(third["headers"]["x-hermes-session-id"],
                            "glasses-20260101-aaaaaaaaaaaa")
        self.assertIn("voice assistant", third["body"]["messages"][0]["content"])

    def test_session_id_format(self):
        sid = hb.new_api_session_id()
        today = __import__("datetime").date.today().strftime("%Y%m%d")
        self.assertTrue(re.fullmatch(r"glasses-%s-[0-9a-f]{12}" % today, sid))
        self.assertNotEqual(sid, hb.new_api_session_id())


if __name__ == "__main__":
    unittest.main()
