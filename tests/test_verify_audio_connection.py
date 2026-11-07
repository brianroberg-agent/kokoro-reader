"""A Gemini request on a slow connection goes on; one on a connection that stops moving fails.

Acceptance dry run 2 (2026-11-05): a backup on the Mac's connection slowed the upload of
one section's audio to about 30 KB/s. The SDK uploads in 8 MB requests, and over TLS one
socket write of a whole request body must finish within the request's timeout: the third
request failed with "The write operation timed out" exactly 10 minutes after it started,
while the upload was still moving. These tests run a real HTTPS server on localhost that
reads a request body slowly, or stops reading it.
"""
import datetime
import re
import socket
import ssl
import threading
import time

import httpx
import pytest

import verify_audio as va

pytest.importorskip("cryptography")  # the test certificate; google-auth brings it in

BODY = b"x" * (4 * 1024 * 1024)
RATE = 2 * 1024 * 1024  # bytes a second: the 4 MiB body takes about 2 s
STALL_S = 0.5  # where a timeout is the expected outcome
# Where it isn't (review R5): one 64 KiB piece takes about 31 ms at RATE, so a server
# thread held up for less than a second doesn't fail the test.
SLOW_STALL_S = 1.0
# Small socket buffers on both ends, so the slow reader, not the buffers, sets the pace.
BUFFER = 64 * 1024
SMALL_SEND_BUFFER = [(socket.SOL_SOCKET, socket.SO_SNDBUF, BUFFER)]


@pytest.fixture(scope="module")
def tls(tmp_path_factory):
    """(server context, client context) for a self-signed localhost certificate."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, content_commitment=False,
                                         key_encipherment=False, data_encipherment=False, key_agreement=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(ski, critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ski), critical=False)
            .sign(key, hashes.SHA256()))
    d = tmp_path_factory.mktemp("tls")
    cert_path, key_path = d / "cert.pem", d / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert_path, key_path)
    return server, ssl.create_default_context(cafile=str(cert_path))


class SlowReader:
    """A one-request HTTPS server on localhost. It reads the request body at `rate` bytes a
    second and answers 200 (or, with answer False, never answers), or, with rate 0,
    reads none of the body."""

    def __init__(self, ctx: ssl.SSLContext, rate: int, answer: bool = True):
        self.answer = answer
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, BUFFER)  # accepted sockets inherit it
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.url = f"https://localhost:{self.listener.getsockname()[1]}/upload"
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, args=(ctx, rate), daemon=True)
        self.thread.start()

    def _serve(self, ctx, rate):
        try:
            conn, _ = self.listener.accept()
            with conn:
                self._read(ctx.wrap_socket(conn, server_side=True), rate)
        except OSError:  # the client gave up and closed the connection (ssl.SSLError is one)
            pass

    def _read(self, tls, rate):
        def recv(n):
            data = tls.recv(n)
            if not data:  # the client closed the connection
                raise ConnectionResetError
            return data

        with tls:
            head = b""
            while b"\r\n\r\n" not in head:
                head += recv(4096)
            head, _, body = head.partition(b"\r\n\r\n")
            length, got = int(re.search(rb"content-length: *(\d+)", head, re.I).group(1)), len(body)
            if rate == 0:
                self.stop.wait(30)
                return
            piece = 16 * 1024
            while got < length:
                got += len(recv(min(piece, length - got)))
                time.sleep(piece / rate)
            if not self.answer:
                self.stop.wait(30)
                return
            tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")

    def close(self):
        self.stop.set()
        self.listener.close()
        self.thread.join(5)


@pytest.fixture
def server(tls):
    made = []

    def start(rate, answer=True):
        made.append(SlowReader(tls[0], rate, answer))
        return made[-1]
    yield start
    for s in made:
        s.close()


def post(ctx, url, stall_s, **limits):
    """POST BODY through the verifier's request hook. The test's own transport only
    trusts the test certificate and keeps the send buffer small; the client's timeout
    is 60 s, so any limit the test sees comes from the hook."""
    transport = httpx.HTTPTransport(verify=ctx, socket_options=SMALL_SEND_BUFFER)
    with httpx.Client(transport=transport, event_hooks={"request": [va.StallLimit(stall_s, **limits)]},
                      timeout=60, trust_env=False) as client:
        return client.post(url, content=BODY)


def test_with_a_plain_transport_a_slow_request_body_times_out_as_a_whole(tls, server):
    # What failed in dry run 2: the body goes out in one TLS write, and the write timeout
    # is a deadline for all of it, however steadily it moves.
    slow = server(RATE)
    try:
        with httpx.Client(transport=httpx.HTTPTransport(verify=tls[1], socket_options=SMALL_SEND_BUFFER),
                          timeout=STALL_S, trust_env=False) as client:
            client.post(slow.url, content=BODY)
    except httpx.WriteTimeout:
        return
    pytest.skip("a slow TLS request body no longer times out as a whole: "
                "StallLimit's pieces may no longer be needed")


def test_a_slow_upload_that_keeps_moving_is_not_cut_off(tls, server):
    slow = server(RATE)
    started = time.monotonic()
    response = post(tls[1], slow.url, SLOW_STALL_S)
    assert response.status_code == 200 and response.text == "ok"
    assert time.monotonic() - started > SLOW_STALL_S  # longer than one stall limit: the body was slow


def test_a_check_stops_once_it_outlasts_any_session(tls, server):
    # Review R2.1: a check whose session was killed goes on in the server. Past the
    # longest a session may run, it stops, even mid-upload and while still moving.
    slow = server(RATE)
    started = time.monotonic()
    with pytest.raises(httpx.TimeoutException, match="longest a narration session may run"):
        post(tls[1], slow.url, SLOW_STALL_S, total_s=0.5)
    assert time.monotonic() - started < 1.5  # before the 2 s body was through


def test_a_request_after_the_limit_is_not_sent():
    request = httpx.Request("POST", "https://gemini.invalid/", content=b"x")
    with pytest.raises(httpx.TimeoutException, match="longest a narration session may run"):
        va.StallLimit(5, total_s=0)(request)


def test_an_answer_that_never_comes_still_fails(tls, server):
    # Review R6: the read side has the same stall limit. The body goes through, then the
    # server says nothing; the client's own timeout is 60 s.
    silent = server(RATE, answer=False)
    started = time.monotonic()
    with pytest.raises(httpx.ReadTimeout):
        post(tls[1], silent.url, STALL_S)
    assert time.monotonic() - started < 10


def test_the_stall_limit_caps_every_kind_of_wait():
    # Review R6: connect, read, write and pool, whatever the request asked for, and none
    # left unlimited. The body is unchanged, only cut into pieces.
    request = httpx.Request("POST", "https://gemini.invalid/", content=b"x" * 10,
                            extensions={"timeout": {"connect": 60, "read": None, "write": 7200, "pool": 0.1}})
    va.StallLimit(5)(request)
    assert request.extensions["timeout"] == {"connect": 5, "read": 5, "write": 5, "pool": 0.1}
    assert b"".join(request.stream) == b"x" * 10


def test_gemini_requests_follow_the_environments_proxy(monkeypatch):
    # Review R1: an httpx client given its own transport ignores HTTPS_PROXY and ALL_PROXY.
    # The SDK's client follows them, and the verifier's options must leave that alone.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    seen = []

    def accept():
        try:
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(5)
                seen.append(conn.recv(4096))
        except OSError:
            pass
    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    for name in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{listener.getsockname()[1]}")
    with httpx.Client(**va.gemini_http_options().client_args, timeout=5) as client:  # as the SDK builds it
        with pytest.raises(httpx.HTTPError):
            client.get("https://gemini.invalid/")
    thread.join(5)
    listener.close()
    assert seen and seen[0].startswith(b"CONNECT gemini.invalid:443 ")


def test_an_upload_that_stops_moving_still_fails(tls, server):
    # Review F3: a request on a dead connection must not hang the narration server's work
    # item for good. The client's own timeout here is 60 s; the stall limit ends it first.
    stalled = server(0)
    started = time.monotonic()
    with pytest.raises(httpx.WriteTimeout):
        post(tls[1], stalled.url, STALL_S)
    assert time.monotonic() - started < 10
