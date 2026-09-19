"""Read-only static file server for the photo archive.

The service runs five threads in total: the MQTT thread only calls
incoming.put(); a single background thread owns the pipeline, the ledger,
the assembler and the state machine (which is why nothing in this whole
service needs a lock); the heartbeat thread probes the classifier on a
timer; the retention thread purges old photos once a day; and this module
is the fifth, serving photos read-only. This module never imports ledger,
pipeline or machine, and never touches any of the objects they own -- it
only reads files from photos_dir. If a future change here ever seems to
need one of those, the design has drifted: that is a signal to raise, not
a reason to add a lock.

Standard library only, matching the Dockerfile, which installs nothing but
paho-mqtt and PyYAML: http.server.HTTPServer/ThreadingHTTPServer with a
handler derived from SimpleHTTPRequestHandler is enough for read-only
static files on a local network, behind Traefik.
"""
import sys
import time
import traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def make_handler(directory):
    """Build a request handler class rooted at `directory`.

    A fresh class per call (rather than a module-level one) is what lets
    the root be baked in via a closure instead of reconstructed from
    request state -- the standard library's own recommended way to bind
    `directory` when the handler is not instantiated directly by the
    caller.
    """
    root = Path(directory).resolve()

    class _Handler(SimpleHTTPRequestHandler):
        """Read-only file server for the photo archive.

        Never touches the ledger, the pipeline or the state machine: the
        whole service runs without a single lock because exactly one
        thread owns that state, and this one only reads files.
        """

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def __getattr__(self, name):
            # BaseHTTPRequestHandler.handle_one_request() dispatches to
            # do_<VERB> via hasattr/getattr and answers 501 Unsupported
            # when that attribute is missing. GET and HEAD are the only
            # verbs SimpleHTTPRequestHandler defines, so this catches
            # every other one -- POST, PUT, DELETE, or anything else --
            # and turns it into an explicit 405, a guarantee, not merely
            # whatever the base class happens to fall back to.
            if name.startswith("do_"):
                def _method_not_allowed():
                    self.send_error(405, "Method not allowed: read-only file server")
                return _method_not_allowed
            raise AttributeError(name)

        def translate_path(self, path):
            # The base implementation already strips ".." components while
            # joining the URL path onto `directory` (see http.server's own
            # translate_path): reimplementing that logic here would be
            # exactly the way to introduce the traversal bug it already
            # avoids. The one thing it does NOT catch is a symlink that
            # resolves outside the root -- the archive never contains one,
            # but refusing anything that resolves outside root after the
            # fact closes that door for good.
            translated = super().translate_path(path)
            try:
                resolved = Path(translated).resolve()
                resolved.relative_to(root)
            except ValueError:
                # Escapes photos_dir, necessarily through a symlink since
                # ".." was already neutralised above. Point at a path that
                # cannot exist so the normal 404 machinery below handles
                # it -- no new error path to get wrong. (A null byte would
                # do this too, but open() rejects those with a ValueError
                # instead of a clean ENOENT, which would leak as a 500.)
                return str(root / ".webserve-path-refused")
            return str(resolved)

        def list_directory(self, path):
            # Reviewed decision: directory listing served no one and
            # exposed everything. The dashboard never needs it -- part C
            # already carries the exact relative path into every event
            # that owns a photo, so a consumer always knows what to ask
            # for -- while GET / or GET /2026-09/ would otherwise let any
            # LAN visitor enumerate the entire visit history: filenames
            # alone encode the date, the time, the kind (mouvement/ding)
            # and a hash. 404, not 403: a 403 would still confirm the
            # directory exists, and there is nothing to gain by saying
            # even that much.
            self.send_error(404, "File not found")
            return None

        def log_message(self, fmt, *args):
            # Silent by default: a dashboard card polling this server every
            # few seconds would otherwise flood `docker logs`. Codes >= 400
            # are the useful signal -- something asked for could not be
            # served -- and are still printed, in the same shape
            # BaseHTTPRequestHandler's own default log_message would have
            # used.
            try:
                code = int(args[1])
            except (IndexError, ValueError):
                return
            if code >= 400:
                sys.stderr.write("%s - - [%s] %s\n" % (
                    self.address_string(), self.log_date_time_string(),
                    fmt % args))

    return _Handler


def _serve_once(server):
    """Run one server until serve_forever() stops, then always release its
    socket.

    Reviewed finding (I-4): without server_close(), an exception escaping
    serve_forever() left the listening socket bound; every retry in the
    guarded loop below then failed forever with EADDRINUSE, printing a
    traceback in a tight one-second loop -- 86,400 tracebacks a day, enough
    to push every other signal (including the purge's own stderr output)
    out of a log capped at 10 MB x 3 files. The server was then dead for
    good while the container kept reporting healthy: exactly the failure
    mode this whole service is built to avoid. `finally` guarantees the
    close whether serve_forever() raised or returned normally.
    """
    try:
        server.serve_forever()
    finally:
        server.server_close()


def serve_forever(cfg):
    """Run the photo archive's read-only file server forever.

    Guarded like background_loop and heartbeat, and for the same reason: an
    unguarded exception here would silently kill the server while the
    container still reports healthy -- a card in Home Assistant would just
    stop loading photos with no signal why. Print and carry on.

    Retries after a few seconds, not immediately: a persistent failure (the
    port still held by an old container during a redeploy, for instance)
    must not turn into a tight loop of tracebacks -- see _serve_once's
    docstring for the measured cost of that.
    """
    handler_cls = make_handler(cfg.photos_dir)
    while True:
        try:
            _serve_once(ThreadingHTTPServer(("", cfg.serve_port), handler_cls))
        except Exception:
            traceback.print_exc()
        time.sleep(5.0)
