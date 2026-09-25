"""Who may talk to a localhost server.

Binding to 127.0.0.1 is necessary but not sufficient. Two attacks reach a
localhost server anyway, both launched from an ordinary browser tab:

  CSRF          any website you happen to have open can POST to
                http://127.0.0.1:3000 - the browser sends it, from your
                machine, with no user action beyond visiting the page
  DNS rebinding a hostile domain resolves to 127.0.0.1 after its first
                lookup, letting that site READ responses too, because to the
                browser it is now same-origin

The defence for both is header inspection, not a token: a browser always
stamps the true Origin on a cross-site POST and the true Host on every
request, and neither can be forged from a web page. Tools like curl send no
Origin at all - and stay welcome, because the threat model is the browser,
not the shell.

Pure functions on header strings, so every case is testable without a socket.
"""

from __future__ import annotations

from urllib.parse import urlsplit

_LOCAL = frozenset({"localhost", "127.0.0.1", "::1"})


def _hostname(value: str) -> str:
    """The bare hostname out of a Host header or URL netloc."""
    value = (value or "").strip().lower()
    if value.startswith("["):  # [::1]:3000
        return value[1:].split("]")[0]
    return value.rsplit(":", 1)[0] if ":" in value else value


def request_allowed(host: str, origin: str) -> bool:
    """May this request mutate state?

    host   the Host header - must name this machine, or a hostile domain
           rebound to 127.0.0.1 is reading us as same-origin
    origin the Origin header if the client sent one - browsers stamp it on
           every cross-site POST; absent means a non-browser client (curl,
           scripts), which the browser threat model does not cover
    """
    if _hostname(host) not in _LOCAL:
        return False
    if origin:
        parsed = urlsplit(origin.strip())
        if (parsed.hostname or "").lower() not in _LOCAL:
            return False
    return True
