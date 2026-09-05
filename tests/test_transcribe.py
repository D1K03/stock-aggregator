import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import httpx
import pytest
import socket

from screener.transcribe import MAX_AUDIO_BYTES, Transcript, transcribe
from screener.transcribe.server import build_server

AUDIO = b"OggS" + b"\x00" * 200


def responder(*responses):
    """A transport replaying `responses` in order, recording every request."""
    seen: list[httpx.Request] = []
    remaining = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return remaining.pop(0) if remaining else httpx.Response(200, json={"text": "tail"})

    return httpx.MockTransport(handler), seen


@pytest.fixture
def service():
    """The real service on an ephemeral port, with a fake transcriber."""
    heard: list[bytes] = []

    def fake(audio: bytes) -> tuple[str, float]:
        heard.append(audio)
        return "what did nvidia do last month", 4.25

    server = build_server("127.0.0.1", 0, transcriber=fake)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", heard
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def post(url, body, *, length=None, content_type="audio/ogg"):
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", content_type)
    if length is not None:
        request.add_header("Content-Length", str(length))
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read()), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), exc.headers


def raw_post(url, *, declared, body, path="/transcribe"):
    """A request written straight onto the socket, headers and body separately.

    httpx and urllib both refuse to send fewer bytes than the Content-Length
    they declared, which is exactly the request these two tests are about.
    """
    host, port = url.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=10) as sock:
        sock.sendall(
            f"POST {path} HTTP/1.1\r\nHost: {host}\r\n"
            f"Content-Type: audio/ogg\r\nContent-Length: {declared}\r\n\r\n".encode()
        )
        if body:
            sock.sendall(body)
        sock.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            try:
                data = sock.recv(4096)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
    head, _, payload = b"".join(chunks).partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    return status, json.loads(payload) if payload else {}


# -- the client -------------------------------------------------------------


def test_importing_the_package_does_not_import_the_model():
    # The bot and the status service both import this to ask for a transcript.
    # If that pulled in faster_whisper, every container would carry ctranslate2,
    # onnxruntime and PyAV to make one HTTP call.
    code = "import screener.transcribe, sys; print('faster_whisper' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "False", out.stderr


def test_the_http_layer_never_reaches_for_faster_whisper():
    # The seam that lets every test below run in a CI that installs `dev` and
    # not `voice`. If the import moves to module scope this fails first.
    code = (
        "import screener.transcribe.server, sys; print('faster_whisper' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "False", out.stderr


def test_a_transcript_comes_back_as_text_and_seconds():
    transport, seen = responder(
        httpx.Response(200, json={"text": "  hello there  ", "seconds": 2.5})
    )
    with httpx.Client(transport=transport) as client:
        got = transcribe(AUDIO, client=client)
    assert got == Transcript(text="hello there", seconds=2.5)
    assert seen[0].content == AUDIO


def test_a_service_that_fails_costs_the_answer_but_not_a_traceback():
    # Never raises: this runs on the gateway's worker thread, and an exception
    # there costs the reply rather than the picture.
    transport, _ = responder(httpx.Response(502))
    with httpx.Client(transport=transport) as client:
        assert transcribe(AUDIO, client=client) is None


def test_something_that_is_not_a_transcript_is_refused():
    # A 200 carrying a sign-in page would otherwise become the question, and
    # Steven would answer somebody's login form.
    transport, _ = responder(httpx.Response(200, text="<html>sign in</html>"))
    with httpx.Client(transport=transport) as client:
        assert transcribe(AUDIO, client=client) is None


def test_silence_comes_back_empty_rather_than_missing():
    # "I could not hear that" and "you did not say anything" are different
    # things to be told, so they are different return values.
    transport, _ = responder(httpx.Response(200, json={"text": "   ", "seconds": 3.0}))
    with httpx.Client(transport=transport) as client:
        got = transcribe(AUDIO, client=client)
    assert got is not None and got.text == "" and got.seconds == 3.0


def test_audio_over_the_cap_is_refused_without_being_sent():
    transport, seen = responder(httpx.Response(200, json={"text": "no"}))
    with httpx.Client(transport=transport) as client:
        assert transcribe(b"x" * (MAX_AUDIO_BYTES + 1), client=client) is None
    assert seen == []


def test_the_service_address_is_read_fresh_so_it_can_be_pointed_elsewhere(monkeypatch):
    from screener.transcribe import transcriber_url

    monkeypatch.setenv("TRANSCRIBER_URL", "http://elsewhere:9/transcribe")
    assert transcriber_url() == "http://elsewhere:9/transcribe"


# -- the service ------------------------------------------------------------


def test_the_service_answers_with_the_text_it_heard(service):
    url, heard = service
    status, body, _ = post(f"{url}/transcribe", AUDIO)
    assert status == 200
    assert body == {"text": "what did nvidia do last month", "seconds": 4.25}
    assert heard == [AUDIO]


def test_a_body_over_the_limit_is_refused_without_being_decoded(service):
    # Refused on the Content-Length alone: the body is never sent here, and the
    # 413 comes back anyway, which is the proof that nothing was read.
    url, heard = service
    status, body = raw_post(url, declared=MAX_AUDIO_BYTES + 1, body=b"")
    assert status == 413 and "over" in body["error"]
    assert heard == []


def test_a_request_with_no_body_is_refused_rather_than_read_forever(service):
    url, heard = service
    status, body, _ = post(f"{url}/transcribe", b"")
    assert status == 400 and body["error"] == "no audio"
    assert heard == []


def test_a_truncated_upload_is_a_bad_request_not_half_a_transcript(service):
    # Half a clip decodes into half a sentence, which reads as something the
    # speaker said rather than as an upload that ended early.
    url, heard = service
    status, body = raw_post(url, declared=len(AUDIO), body=AUDIO[:50])
    assert status == 400 and body["error"] == "the upload ended early"
    assert heard == []


def test_a_decoder_that_raises_becomes_a_status_code_not_a_traceback():
    def broken(audio: bytes) -> tuple[str, float]:
        raise RuntimeError("not audio")

    server = build_server("127.0.0.1", 0, transcriber=broken)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body, _ = post(
            f"http://127.0.0.1:{server.server_address[1]}/transcribe", AUDIO
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert status == 422 and body["error"] == "could not decode the audio"


def test_a_second_request_while_one_is_running_is_told_the_service_is_busy(monkeypatch):
    # Two cores and one semaphore: the second caller is told so rather than
    # queued behind a decode it will outlive.
    from screener.transcribe import server as service_module

    monkeypatch.setattr(service_module, "BUSY_WAIT_SECONDS", 0.05)
    running = threading.Event()
    release = threading.Event()

    def slow(audio: bytes) -> tuple[str, float]:
        running.set()
        release.wait(timeout=5)
        return "slow", 1.0

    server = build_server("127.0.0.1", 0, transcriber=slow)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/transcribe"
    first: list[int] = []
    caller = threading.Thread(target=lambda: first.append(post(url, AUDIO)[0]), daemon=True)
    try:
        caller.start()
        assert running.wait(timeout=5)
        status, body, _ = post(url, AUDIO)
        assert status == 503 and body["error"] == "busy"
    finally:
        release.set()
        caller.join(timeout=5)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert first == [200]


def test_health_answers_without_loading_the_model(service):
    url, heard = service
    with urllib.request.urlopen(f"{url}/health", timeout=5) as response:
        assert json.loads(response.read()) == {"status": "ok"}
    assert heard == []


def test_every_response_carries_a_content_length(service):
    # HTTP/1.1 with keep-alive: a response with neither a length nor chunked
    # encoding leaves the client waiting for the socket to close.
    url, _ = service
    _, _, headers = post(f"{url}/transcribe", AUDIO)
    assert headers["Content-Length"] is not None
    _, _, headers = post(f"{url}/nowhere", AUDIO)
    assert headers["Content-Length"] is not None


def test_an_unknown_path_is_a_json_not_found(service):
    url, _ = service
    status, body, _ = post(f"{url}/nowhere", AUDIO)
    assert status == 404 and body["error"] == "not found"


# -- the one test that needs the model --------------------------------------


def test_the_real_decoder_reads_an_ogg_without_ffmpeg_on_the_path():
    """The genuinely risky claim: a slim image with no system ffmpeg decodes a
    real container, because PyAV ships the libraries in its wheel.

    Skipped wherever the `voice` extra is not installed, which is CI and most
    development machines. Not ASR accuracy, which is not ours to assert.
    """
    pytest.importorskip("faster_whisper")
    av = pytest.importorskip("av")
    import io

    buffer = io.BytesIO()
    with av.open(buffer, mode="w", format="ogg") as container:
        stream = container.add_stream("libopus", rate=48000)
        for frame in av.AudioFrame(format="s16", layout="mono", samples=48000), :
            frame.sample_rate = 48000
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)

    from screener.transcribe.server import load_model

    text, seconds = load_model()(buffer.getvalue())
    assert isinstance(text, str)
    assert seconds >= 0
