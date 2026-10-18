"""URL / SSRF guard for Y Download.

Pure helpers (no Flask and no app state) so they can be unit-tested on their own.
A host is refused when it is not a normal public DNS name, or when *any* address
it resolves to is not globally routable (private, loopback, link-local, CGNAT,
reserved, multicast, unspecified). DNS failures fail CLOSED.

There are three integration points, because yt-dlp otherwise happily fetches
anything reachable from the host, including cloud metadata endpoints:

* ``validate_url()`` - the user-supplied link, before any fetch happens.
* ``install_urllib_redirect_guard()`` - every redirect hop taken by yt-dlp's
  urllib handler.
* ``install_ydl_redirect_guard()`` - every redirect hop taken by yt-dlp's
  curl_cffi handler (used for browser impersonation on the generic extractor).

The last two exist because a public host can answer with a ``302`` that points
at ``169.254.169.254``; validating only the first URL would not stop it.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import urllib.error
from urllib.parse import urljoin, urlparse

INTERNAL_HOST_SUFFIXES = (
    ".internal", ".local", ".localhost", ".home.arpa",
    ".lan", ".corp", ".intranet", ".cluster.local",
)

_REDIRECT_STATUSES = (301, 302, 303, 307, 308)


def resolve_host(host: str) -> list[str] | None:
    try:
        return [info[4][0] for info in socket.getaddrinfo(host, None)]
    except (socket.gaierror, OSError):
        return None


def host_error(host: str) -> str | None:
    """
    Return a refusal reason for a host yt-dlp must not fetch, else None.

    Without this, /download is a server-side request forgery hole: yt-dlp's
    generic extractor will happily fetch http://169.254.169.254/ (cloud metadata)
    or any service reachable from inside the host's private network.

    Fails CLOSED. An earlier version allowed the request through when DNS
    resolution failed, which let http://metadata.google.internal/ reach the
    extractor.
    """
    if not host:
        return "That link has no host."
    host = host.strip().rstrip(".").lower()
    if not host:
        return "That link has no host."
    if host == "localhost" or host.endswith(INTERNAL_HOST_SUFFIXES):
        return "That host is not allowed."
    # A single-label name is never a public DNS name (also catches "[::1]").
    if "." not in host:
        return "That host is not allowed."

    box: list[list[str] | None] = []
    worker = threading.Thread(target=lambda: box.append(resolve_host(host)), daemon=True)
    worker.start()
    worker.join(timeout=4)
    if not box:
        return "Could not resolve that host (timed out)."
    addresses = box[0]
    if not addresses:
        return "Could not resolve that host."

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%")[0])
        except ValueError:
            return "That host is not allowed."
        # is_global is False for private, loopback, link-local, reserved,
        # multicast, unspecified and shared (CGNAT) ranges.
        if not ip.is_global:
            return "That host is not allowed."
    return None


def validate_url(url: str) -> str | None:
    """Return an error message, or None when the url looks usable."""
    if not url:
        return "Please paste a video link."
    if len(url) > 2048:
        return "That link is too long."
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "Only http:// and https:// links are supported."
    if not parsed.netloc:
        return "That doesn't look like a valid link."
    return host_error(parsed.hostname or "")


def install_urllib_redirect_guard() -> bool:
    """
    Make yt-dlp's urllib handler validate every redirect Location.

    ``UrllibRH._create_instance`` builds an opener using the module-global
    ``RedirectHandler``, so swapping that global is enough. Returns True when the
    guard is (already) installed.
    """
    try:
        from yt_dlp.networking import _urllib as yt_urllib
    except Exception:  # pragma: no cover - yt-dlp missing/broken
        return False

    base = getattr(yt_urllib, "RedirectHandler", None)
    if base is None:
        return False
    if getattr(base, "_ydl_guarded", False):
        return True

    class GuardedRedirectHandler(base):
        _ydl_guarded = True

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            error = host_error(urlparse(newurl).hostname or "")
            if error:
                raise urllib.error.HTTPError(
                    newurl, code, f"Redirect blocked: {error}", headers, fp)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    yt_urllib.RedirectHandler = GuardedRedirectHandler
    return True


def install_ydl_redirect_guard(ydl) -> bool:
    """
    Replace the curl_cffi handler on a ``YoutubeDL`` instance with one that
    validates every redirect hop.

    libcurl follows redirects inside C with no Python hook, so the trick is to
    force ``max_redirects=0`` on the handler's sessions and replay the chain here,
    checking each Location before following it.

    Returns True only when the guard is genuinely in place, and False when it
    could not be installed or there is nothing to guard (curl_cffi not installed
    / handler absent). The caller decides how to react - see ``app.make_ydl``,
    which fails closed by dropping the unguarded handler.
    """
    try:
        handler = ydl._request_director.handlers.get("CurlCFFI")
    except Exception:  # pragma: no cover - yt-dlp internals changed
        return False
    if handler is None:
        return False
    if getattr(handler, "_ydl_redirect_guarded", False):
        return True

    # Resolve the internals this guard depends on up front, so a yt-dlp change is
    # reported here (the caller warns / refuses to start) instead of surfacing
    # per request or, worse, silently leaving redirects unguarded.
    try:
        from yt_dlp.networking._helper import get_redirect_method  # noqa: F401
        from yt_dlp.networking.common import Request  # noqa: F401
        from yt_dlp.networking.exceptions import HTTPError, RequestError  # noqa: F401

        original_get_instance = handler._get_instance
        original_send = handler._send
        if not callable(original_get_instance) or not callable(original_send):
            return False
    except Exception:  # pragma: no cover - yt-dlp internals changed
        return False

    def get_instance(*args, **kwargs):
        session = original_get_instance(*args, **kwargs)
        if not getattr(session, "_ydl_no_redirect", False):
            original_request = session.request

            def request_no_redirect(*rargs, **rkwargs):
                rkwargs["max_redirects"] = 0
                return original_request(*rargs, **rkwargs)

            session.request = request_no_redirect
            session._ydl_no_redirect = True
        return session

    def send(request):
        return _guarded_curl_send(request, original_send)

    try:
        handler._get_instance = get_instance
        handler._send = send
    except Exception:  # pragma: no cover - handler is not writable
        return False

    # Set the idempotency flag only after the patch actually took effect, so a
    # later call cannot mistake a half-installed handler for a guarded one.
    handler._ydl_redirect_guarded = True
    return True


def _guarded_curl_send(request, original_send, max_hops: int = 10):
    from yt_dlp.networking.common import Request
    from yt_dlp.networking.exceptions import HTTPError as YDLHTTPError
    from yt_dlp.networking.exceptions import RequestError

    current = request
    for _ in range(max_hops + 1):
        try:
            return original_send(current)
        except YDLHTTPError as exc:
            response = exc.response
            status = getattr(response, "status", None)
            if status not in _REDIRECT_STATUSES:
                raise
            location = response.headers.get("Location")
            if not location:
                raise
            new_url = urljoin(current.url, location)
            error = host_error(urlparse(new_url).hostname or "")
            if error:
                raise RequestError(f"Redirect blocked: {error}") from exc
            current = _redirected_request(Request, current, status, new_url)
    raise RequestError("Too many redirects.")


def _redirected_request(Request, request, status: int, new_url: str):
    from yt_dlp.networking._helper import get_redirect_method

    new_method = get_redirect_method(request.method, status)
    headers = {k: v for k, v in request.headers.items() if k.lower() != "cookie"}
    data = request.data
    if new_method != request.method:
        data = None
        for name in ("Content-Length", "Content-Type"):
            headers.pop(name, None)
    return Request(
        new_url,
        data=data,
        headers=headers,
        proxies=request.proxies,
        method=new_method,
        extensions=dict(request.extensions),
    )
