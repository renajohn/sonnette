"""Tests for the read-only static file server that serves the photo archive.

Spins up a real (single-threaded) http.server.HTTPServer on an OS-assigned
port and drives it with a real HTTP client: this handler subclasses
http.server directly, so exercising it end to end through a socket is more
convincing than reimplementing its dispatch logic in a mock.

server.handle_request() processes exactly one connection and returns only
once that connection has been fully handled -- including any log_message
call -- which is what lets the logging tests below read capsys right after
the call without racing a background thread.
"""
import http.client
import threading

from http.server import HTTPServer

from doorbell.webserve import _serve_once, make_handler


def _one_request(handler_cls, method, path):
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    result = {}

    def client():
        conn = http.client.HTTPConnection(
            "127.0.0.1", srv.server_address[1], timeout=2)
        conn.request(method, path)
        resp = conn.getresponse()
        result["status"] = resp.status
        result["body"] = resp.read()
        conn.close()

    t = threading.Thread(target=client)
    t.start()
    srv.handle_request()
    t.join(timeout=2)
    srv.server_close()
    return result["status"], result["body"]


def test_serves_a_file_under_the_root(tmp_path):
    (tmp_path / "2026-09").mkdir()
    (tmp_path / "2026-09" / "pic.jpg").write_bytes(b"\xff\xd8jpeg")
    status, body = _one_request(make_handler(tmp_path), "GET", "/2026-09/pic.jpg")
    assert status == 200
    assert body == b"\xff\xd8jpeg"


def test_head_is_allowed_and_returns_no_body(tmp_path):
    (tmp_path / "pic.jpg").write_bytes(b"data")
    status, body = _one_request(make_handler(tmp_path), "HEAD", "/pic.jpg")
    assert status == 200
    assert body == b""


def test_post_is_rejected_with_405():
    status, _ = _one_request(make_handler("/tmp"), "POST", "/")
    assert status == 405


def test_put_is_rejected_with_405():
    status, _ = _one_request(make_handler("/tmp"), "PUT", "/anything")
    assert status == 405


def test_delete_is_rejected_with_405():
    status, _ = _one_request(make_handler("/tmp"), "DELETE", "/anything")
    assert status == 405


def test_a_missing_file_is_a_plain_404_not_a_crash(tmp_path):
    status, _ = _one_request(make_handler(tmp_path), "GET", "/nope.jpg")
    assert status == 404


def test_a_symlink_escaping_the_root_is_refused(tmp_path):
    """The archive itself never contains symlinks; closing this door still
    matters because a link that did appear (or was planted) must not let a
    request read anything outside photos_dir."""
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("do not serve this")
    root = tmp_path / "photos"
    root.mkdir()
    (root / "escape").symlink_to(outside)
    status, body = _one_request(make_handler(root), "GET", "/escape")
    assert status != 200
    assert b"do not serve this" not in body


def test_directory_traversal_is_blocked(tmp_path):
    (tmp_path.parent / "outside-secret2.txt").write_text("also do not serve this")
    status, body = _one_request(
        make_handler(tmp_path), "GET", "/../outside-secret2.txt")
    assert status != 200
    assert b"also do not serve this" not in body


def test_successful_requests_are_not_logged(tmp_path, capsys):
    (tmp_path / "pic.jpg").write_bytes(b"data")
    _one_request(make_handler(tmp_path), "GET", "/pic.jpg")
    assert capsys.readouterr().err == ""


def test_missing_files_are_logged_to_stderr(tmp_path, capsys):
    _one_request(make_handler(tmp_path), "GET", "/nope.jpg")
    assert "404" in capsys.readouterr().err


# --- Review round 1, follow-up: directory listing disabled ------------------

def test_directory_listing_of_the_root_is_disabled(tmp_path):
    """Follow-up to the review: the dashboard never needs to browse the
    archive -- part C already carries the exact relative path into every
    event that owns a photo, so a consumer always knows what to ask for.
    Listing served no one and let any LAN visitor enumerate the whole visit
    history (filenames encode date, time, kind and a hash)."""
    (tmp_path / "2026-09").mkdir()
    (tmp_path / "2026-09" / "pic.jpg").write_bytes(b"data")
    status, _ = _one_request(make_handler(tmp_path), "GET", "/")
    assert status == 404


def test_directory_listing_of_a_subfolder_is_disabled(tmp_path):
    (tmp_path / "2026-09").mkdir()
    (tmp_path / "2026-09" / "pic.jpg").write_bytes(b"data")
    status, _ = _one_request(make_handler(tmp_path), "GET", "/2026-09/")
    assert status == 404


def test_a_named_file_is_still_served_when_listing_is_disabled(tmp_path):
    (tmp_path / "2026-09").mkdir()
    (tmp_path / "2026-09" / "pic.jpg").write_bytes(b"data")
    status, body = _one_request(make_handler(tmp_path), "GET", "/2026-09/pic.jpg")
    assert status == 200
    assert body == b"data"


# --- Review round 1, I-4 ----------------------------------------------------

def test_serve_once_closes_the_socket_even_when_serve_forever_raises(tmp_path):
    """I-4: the reviewer proved that without server_close(), an exception
    escaping serve_forever() leaves the listening socket bound, so every
    retry in serve_forever()'s guarded loop fails forever with EADDRINUSE
    while printing a traceback in a tight loop -- the server ends up dead
    for good while the container still reports healthy. _serve_once must
    release the port no matter how serve_forever() exits."""
    srv = HTTPServer(("127.0.0.1", 0), make_handler(tmp_path))
    port = srv.server_address[1]

    def _boom():
        raise RuntimeError("simulated failure inside serve_forever")
    srv.serve_forever = _boom

    try:
        _serve_once(srv)
    except RuntimeError:
        pass

    # If the port was truly released, a fresh server can bind to it right
    # away; if server_close() was skipped, this raises OSError: Address
    # already in use.
    replacement = HTTPServer(("127.0.0.1", port), make_handler(tmp_path))
    replacement.server_close()


def test_serve_once_closes_the_socket_on_a_normal_return_too(tmp_path):
    srv = HTTPServer(("127.0.0.1", 0), make_handler(tmp_path))
    port = srv.server_address[1]
    srv.serve_forever = lambda: None   # returns normally, no exception

    _serve_once(srv)

    replacement = HTTPServer(("127.0.0.1", port), make_handler(tmp_path))
    replacement.server_close()
