"""The sentiment service and the client that reaches it.

No model anywhere in here. CI installs the `dev` extra and not `sentiment`, so
neither `onnxruntime` nor `tokenizers` is importable — every test below passes a
plain Python function through `build_server`'s seam, which is the same
arrangement `tests/test_transcribe.py` uses and for the same reason. What these
defend is the seam, the caps and the label mapping; what the model does is
checked at image build time by `deploy/finbert_export.py`, where torch is
installed and the checkpoint is there to compare against.
"""

import json
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import httpx
import pytest

from screener.sentiment import LABELS, MAX_CHARS, MAX_TEXTS, Sentiment, score
from screener.sentiment.server import (
    INFERENCE_BATCH,
    MAX_BATCH_TOKENS,
    MAX_BODY_BYTES,
    MAX_TOKENS,
    build_server,
    plan_chunks,
    read_labels,
)

# FinBERT's own column order, which is not alphabetical. Written out here so a
# test that assumed alphabetical would fail rather than pass by luck.
FINBERT = ("positive", "negative", "neutral")


def reading(positive, negative, neutral):
    return Sentiment(positive=positive, negative=negative, neutral=neutral)


def responder(*responses):
    """A transport replaying `responses` in order, recording every request."""
    seen: list[httpx.Request] = []
    remaining = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if remaining:
            return remaining.pop(0)
        return httpx.Response(200, json={"labels": list(FINBERT), "scores": []})

    return httpx.MockTransport(handler), seen


def answer(rows, labels=FINBERT):
    """A well-formed service response carrying `rows` in `labels` order."""
    return httpx.Response(200, json={"labels": list(labels), "scores": rows})


@pytest.fixture
def service():
    """The real service on an ephemeral port, with a fake scorer."""
    seen: list[list[str]] = []

    def fake(texts):
        seen.append(list(texts))
        return [reading(0.7, 0.2, 0.1) for _ in texts]

    server = build_server("127.0.0.1", 0, scorer=fake, labels=FINBERT)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def post(url, payload, *, path="/score", raw=None):
    body = raw if raw is not None else json.dumps(payload).encode()
    request = urllib.request.Request(url + path, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def raw_post(url, *, declared, body, path="/score"):
    """A request written straight onto the socket, headers and body separately.

    urllib will not send fewer bytes than the Content-Length it declared, which
    is exactly the request this exists to make.
    """
    host, port = url.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=10) as sock:
        sock.sendall(
            f"POST {path} HTTP/1.1\r\nHost: {host}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {declared}\r\n\r\n".encode()
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
    return int(head.split(b" ")[1]), (json.loads(payload) if payload else {})


# -- the dependency line ----------------------------------------------------


def test_importing_the_package_does_not_import_the_model():
    # The nightly pipeline and anything else wanting a reading imports this. If
    # that pulled in onnxruntime, every container would carry it — and a 440 MB
    # graph would have to live somewhere other than the one image that needs it.
    code = (
        "import screener.sentiment, sys; "
        "print(any(m in sys.modules for m in ('onnxruntime', 'tokenizers', 'numpy')))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "False", out.stderr


def test_the_http_layer_never_reaches_for_the_model():
    # The seam that lets every test below run in a CI that installs `dev` and
    # not `sentiment`. If an import moves to module scope this fails first.
    code = (
        "import screener.sentiment.server, sys; "
        "print(any(m in sys.modules for m in ('onnxruntime', 'tokenizers', 'numpy')))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "False", out.stderr


# -- the reading itself -----------------------------------------------------


def test_the_score_is_positive_minus_negative():
    assert reading(0.8, 0.1, 0.1).score == pytest.approx(0.7)
    assert reading(0.1, 0.8, 0.1).score == pytest.approx(-0.7)


def test_confident_neutral_and_torn_both_score_near_zero_but_are_not_equal():
    # The reason the three probabilities are kept rather than reduced on the way
    # in: these are different readings and only the distribution says so.
    calm = reading(0.05, 0.05, 0.90)
    torn = reading(0.48, 0.48, 0.04)
    assert calm.score == pytest.approx(torn.score)
    assert calm.label == "neutral"
    assert torn.label in {"positive", "negative"}
    assert calm != torn


# -- the client -------------------------------------------------------------


def test_a_batch_comes_back_in_order():
    transport, seen = responder(answer([[0.9, 0.05, 0.05], [0.1, 0.8, 0.1]]))
    with httpx.Client(transport=transport) as client:
        got = score(["beat expectations", "guidance cut"], client=client)
    assert got == (reading(0.9, 0.05, 0.05), reading(0.1, 0.8, 0.1))
    assert json.loads(seen[0].content)["texts"] == ["beat expectations", "guidance cut"]


def test_the_columns_are_read_from_the_response_not_assumed():
    # The failure this exists for. A response whose columns are in a different
    # order carries the same numbers and a different meaning, and mapping by
    # position would read every score backwards while looking perfectly healthy.
    rows = [[0.05, 0.05, 0.9]]
    positive_first = score(
        ["x"], client=httpx.Client(transport=responder(answer(rows, FINBERT))[0])
    )
    negative_first = score(
        ["x"],
        client=httpx.Client(
            transport=responder(answer(rows, ("negative", "neutral", "positive")))[0]
        ),
    )
    assert positive_first is not None and negative_first is not None
    read_as_positive_first, read_as_negative_first = positive_first[0], negative_first[0]
    assert read_as_positive_first is not None and read_as_negative_first is not None
    assert read_as_positive_first.score == pytest.approx(0.0)
    assert read_as_negative_first.score == pytest.approx(0.85)


def test_a_response_with_unknown_columns_is_not_a_reading():
    transport, _ = responder(answer([[0.9, 0.05, 0.05]], ("up", "down", "flat")))
    with httpx.Client(transport=transport) as client:
        assert score(["x"], client=client) is None


def test_a_short_batch_is_refused_rather_than_lined_up_wrong():
    # Two texts, one reading. Zipping them would attribute a reading to the
    # wrong text, which is worse than no reading at all.
    transport, _ = responder(answer([[0.9, 0.05, 0.05]]))
    with httpx.Client(transport=transport) as client:
        assert score(["one", "two"], client=client) is None


def test_a_sign_in_page_is_not_a_reading():
    transport, _ = responder(httpx.Response(200, text="<html>sign in</html>"))
    with httpx.Client(transport=transport) as client:
        assert score(["x"], client=client) is None


def test_a_failure_is_none_and_never_an_exception():
    def explode(request):
        raise httpx.ConnectError("no route", request=request)

    with httpx.Client(transport=httpx.MockTransport(explode)) as client:
        assert score(["x"], client=client) is None


def test_no_texts_is_an_empty_answer_and_costs_no_request():
    transport, seen = responder()
    with httpx.Client(transport=transport) as client:
        assert score([], client=client) == ()
    assert seen == []


def test_blank_texts_are_absent_rather_than_neutral():
    # The distinction the whole return type exists for. A blank string put
    # through a classifier still produces three confident numbers, and calling
    # that "neutral sentiment" would put an invented reading in the corpus.
    transport, seen = responder(answer([[0.9, 0.05, 0.05]]))
    with httpx.Client(transport=transport) as client:
        got = score(["  ", "beat expectations", ""], client=client)
    assert got == (None, reading(0.9, 0.05, 0.05), None)
    # And only the real one was sent, so the cap and the CPU are spent on text.
    assert json.loads(seen[0].content)["texts"] == ["beat expectations"]


def test_a_batch_of_nothing_but_blanks_never_leaves():
    transport, seen = responder()
    with httpx.Client(transport=transport) as client:
        assert score(["", "   ", "\n"], client=client) == (None, None, None)
    assert seen == []


def test_the_service_being_down_is_not_the_same_as_a_blank_text():
    def explode(request):
        raise httpx.ConnectError("no route", request=request)

    with httpx.Client(transport=httpx.MockTransport(explode)) as client:
        down = score(["real text"], client=client)
    transport, _ = responder()
    with httpx.Client(transport=transport) as client:
        blank = score([""], client=client)
    assert down is None
    assert blank == (None,)


def test_too_many_texts_are_refused_before_a_request_is_made():
    transport, seen = responder()
    with httpx.Client(transport=transport) as client:
        assert score(["x"] * (MAX_TEXTS + 1), client=client) is None
    assert seen == []


def test_an_overlong_text_is_cut_before_it_is_sent():
    transport, seen = responder(answer([[0.3, 0.3, 0.4]]))
    with httpx.Client(transport=transport) as client:
        score(["x" * (MAX_CHARS * 3)], client=client)
    assert len(json.loads(seen[0].content)["texts"][0]) == MAX_CHARS


# -- the service ------------------------------------------------------------


def test_health_answers_with_the_column_order_it_loaded():
    server = build_server("127.0.0.1", 0, scorer=lambda texts: [], labels=FINBERT)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/health"
        with urllib.request.urlopen(url, timeout=10) as response:
            body = json.loads(response.read())
        # Not decoration: it is how an operator sees which way round the
        # container it is actually running reads its own output.
        assert body == {"status": "ok", "labels": list(FINBERT)}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_service_answers_in_its_own_column_order(service):
    url, seen = service
    status, body = post(url, {"texts": ["beat expectations"]})
    assert status == 200
    assert body["labels"] == list(FINBERT)
    assert body["scores"] == [[0.7, 0.2, 0.1]]
    assert seen[-1] == ["beat expectations"]


def test_the_client_reaches_the_real_service_through_its_url(service, monkeypatch):
    url, _ = service
    monkeypatch.setenv("SENTIMENT_URL", url + "/score")
    got = score(["beat expectations", ""])
    assert got == (reading(0.7, 0.2, 0.1), None)


def test_an_unknown_route_is_not_found(service):
    url, _ = service
    status, body = post(url, {"texts": ["x"]}, path="/classify")
    assert status == 404
    assert body == {"error": "not found"}


def test_an_empty_body_is_refused(service):
    url, _ = service
    status, body = raw_post(url, declared=0, body=b"")
    assert status == 400
    assert body["error"] == "no texts"


def test_a_body_that_is_not_json_is_refused(service):
    url, _ = service
    status, body = post(url, None, raw=b"not json at all")
    assert status == 400
    assert body["error"] == "the body is not JSON"


def test_a_body_with_no_texts_is_refused(service):
    url, _ = service
    status, body = post(url, {"texts": []})
    assert status == 400
    assert body["error"] == "no texts"


def test_a_blank_text_reaching_the_service_is_refused_not_scored(service):
    # The client drops blanks, so one arriving here is a caller doing something
    # it did not mean to — and answering it would put a number on nothing.
    url, seen = service
    before = len(seen)
    status, body = post(url, {"texts": ["real", "   "]})
    assert status == 400
    assert body["error"] == "every text must be non-empty"
    assert len(seen) == before


def test_a_non_string_text_is_refused(service):
    url, _ = service
    status, _ = post(url, {"texts": [{"body": "nice try"}]})
    assert status == 400


def test_too_many_texts_are_refused_by_the_service_too(service):
    url, seen = service
    before = len(seen)
    status, body = post(url, {"texts": ["x"] * (MAX_TEXTS + 1)})
    assert status == 413
    assert len(seen) == before


def test_an_oversized_body_is_refused_on_the_header(service):
    # Refused before a byte is read, so a caller cannot make this process hold a
    # payload it has already decided not to look at.
    url, seen = service
    before = len(seen)
    status, body = raw_post(url, declared=MAX_BODY_BYTES + 1, body=b"")
    assert status == 413
    assert len(seen) == before


def test_a_truncated_upload_is_not_scored(service):
    # Half a JSON body is not half a batch; it is a parse error that would
    # otherwise be read as the next request on a keep-alive connection.
    url, seen = service
    before = len(seen)
    status, _ = raw_post(url, declared=500, body=b'{"texts": ["hal')
    assert status == 400
    assert len(seen) == before


def test_a_model_that_raises_is_a_refusal_rather_than_a_dead_service():
    def explode(texts):
        raise RuntimeError("the graph fell over")

    server = build_server("127.0.0.1", 0, scorer=explode, labels=FINBERT)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        status, body = post(url, {"texts": ["x"]})
        assert status == 422
        assert body["error"] == "could not score the texts"
        # And it still answers afterwards, so the semaphore was released.
        with urllib.request.urlopen(url + "/health", timeout=10) as response:
            assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_short_batch_from_the_model_is_an_error_not_a_misaligned_answer():
    def short(texts):
        return [reading(0.7, 0.2, 0.1)]

    server = build_server("127.0.0.1", 0, scorer=short, labels=FINBERT)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        status, body = post(url, {"texts": ["one", "two"]})
        assert status == 500
        assert body["error"] == "the batch came back short"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# -- the column order, as it comes out of the image -------------------------


def test_the_labels_file_is_read_rather_than_assumed(tmp_path):
    tmp_path.joinpath("labels.json").write_text(json.dumps(["negative", "neutral", "positive"]))
    assert read_labels(tmp_path) == ("negative", "neutral", "positive")


def test_a_labels_file_naming_something_else_refuses_to_load(tmp_path):
    # A container that cannot name its own output columns must not serve. The
    # alternative is a default, and a default here is a silently sign-flipped
    # score for every text the system ever reads.
    tmp_path.joinpath("labels.json").write_text(json.dumps(["bullish", "bearish", "flat"]))
    with pytest.raises(RuntimeError):
        read_labels(tmp_path)


def test_a_labels_file_with_a_duplicate_column_refuses_to_load(tmp_path):
    tmp_path.joinpath("labels.json").write_text(json.dumps(["positive", "positive", "neutral"]))
    with pytest.raises(RuntimeError):
        read_labels(tmp_path)


def test_the_clients_presentation_order_covers_every_column():
    assert sorted(LABELS) == sorted(FINBERT)


def test_loading_the_model_cannot_be_separated_from_its_column_order():
    # The regression this exists for. `build_server` used to let `scorer`
    # default to `load_model()` while `labels` defaulted to the constant in
    # `client` — so the one code path that loads a model without `serve` would
    # have announced a column order it had not read, which is precisely the
    # failure the rest of this module is built to prevent.
    from screener.sentiment import server as module

    def fake_load():
        return (lambda texts: [reading(0.7, 0.2, 0.1) for _ in texts]), (
            "negative",
            "neutral",
            "positive",
        )

    real = module.load_model
    module.load_model = fake_load
    server = None
    try:
        server = module.build_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(url + "/health", timeout=10) as response:
            announced = json.loads(response.read())["labels"]
        # What the model said, not what `client.LABELS` happens to hold.
        assert announced == ["negative", "neutral", "positive"]
        # And the wire agrees, so a client mapping by name reads it correctly
        # even though the columns are in an order nothing here defaults to.
        status, body = post(url, {"texts": ["x"]})
        assert status == 200
        assert body["labels"] == ["negative", "neutral", "positive"]
        assert body["scores"] == [[0.2, 0.1, 0.7]]
    finally:
        module.load_model = real
        if server is not None:
            server.shutdown()
            server.server_close()


# -- chunking, which is what keeps the container alive ----------------------


def widest(chunk, lengths):
    """What a chunk actually costs: every row padded to its longest member."""
    return len(chunk) * max(lengths[i] for i in chunk)


def test_a_full_batch_of_maximum_length_texts_is_split():
    # The regression. Thirty-two rows at the 512 token cap went through the
    # graph as one chunk, reached 2,091 MB and was killed by the cgroup. Every
    # cap in the client allowed it.
    lengths = [MAX_TOKENS] * 32
    chunks = plan_chunks(lengths)
    assert len(chunks) > 1
    assert all(widest(c, lengths) <= MAX_BATCH_TOKENS for c in chunks)


def test_short_texts_still_fill_a_whole_chunk():
    # The budget must not cost throughput on the common case: headlines are
    # bounded by the row count, not by tokens.
    lengths = [14] * (INFERENCE_BATCH * 2)
    assert [len(c) for c in plan_chunks(lengths)] == [INFERENCE_BATCH, INFERENCE_BATCH]


def test_one_long_text_does_not_drag_short_ones_into_its_chunk():
    # What the length sort is for. Before sorting, a 512 token comment sitting
    # among one-line headlines padded every row in its chunk to 512.
    lengths = [MAX_TOKENS, 9, MAX_TOKENS, 11, 8, MAX_TOKENS]
    chunks = plan_chunks(lengths)
    short = {1, 3, 4}
    assert any(short.issubset(set(c)) for c in chunks), "the short rows should share a chunk"
    assert all(widest(c, lengths) <= MAX_BATCH_TOKENS for c in chunks)


def test_no_chunk_exceeds_the_budget_for_any_mix():
    # Property over the shapes a real request takes: headlines, comments,
    # articles and everything between.
    import random

    rng = random.Random(0)
    for _ in range(200):
        lengths = [rng.choice([8, 14, 40, 120, 300, MAX_TOKENS]) for _ in range(rng.randint(1, 32))]
        for chunk in plan_chunks(lengths):
            assert len(chunk) <= INFERENCE_BATCH
            assert widest(chunk, lengths) <= MAX_BATCH_TOKENS


def test_every_row_lands_in_exactly_one_chunk():
    # Positions are restored by index, so a dropped or duplicated one would
    # attribute a reading to the wrong text rather than fail.
    import random

    rng = random.Random(1)
    for _ in range(200):
        lengths = [rng.randint(3, MAX_TOKENS) for _ in range(rng.randint(1, 32))]
        seen = sorted(i for chunk in plan_chunks(lengths) for i in chunk)
        assert seen == list(range(len(lengths)))


def test_a_single_text_at_the_cap_is_one_chunk():
    assert plan_chunks([MAX_TOKENS]) == [[0]]


def test_nothing_to_score_is_no_chunks():
    assert plan_chunks([]) == []
