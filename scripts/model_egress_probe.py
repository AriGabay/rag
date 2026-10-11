"""Layered check of the path from a container to the model provider. Run by scripts/check-model-egress.sh.

Prints, for the provider host and a control host behind the same CDN: the resolved addresses, a TCP connect to
each, a verified TLS handshake (protocol and certificate issuer), the proxy variables that are set (names only),
and an HTTPS request to the models endpoint without and with the configured key. Only status codes are printed;
the key and response bodies never are. Finally the app's own connection test (a synthetic prompt) runs.
"""

from __future__ import annotations

import os
import socket
import ssl
import time

HOSTS = ("api.openai.com", "cloudflare.com")
TIMEOUT = 6


def tcp(ip: str) -> str:
    t0 = time.monotonic()
    try:
        socket.create_connection((ip, 443), timeout=TIMEOUT).close()
        return f"ok ({time.monotonic() - t0:.2f}s)"
    except OSError as exc:
        return f"FAIL {type(exc).__name__} ({time.monotonic() - t0:.1f}s)"


def tls(ip: str, sni: str) -> str:
    ctx = ssl.create_default_context()  # verification stays on
    try:
        with socket.create_connection((ip, 443), timeout=TIMEOUT) as raw, \
                ctx.wrap_socket(raw, server_hostname=sni) as s:
            issuer = dict(x[0] for x in s.getpeercert()["issuer"])
            return f"ok {s.version()} issuer={issuer.get('organizationName')}/{issuer.get('commonName')}"
    except (OSError, ssl.SSLError) as exc:
        return f"FAIL {type(exc).__name__}"


def http_status(with_key: bool) -> str:
    import httpx

    headers = {}
    if with_key:
        from app.config import get_settings

        key = get_settings().openai_key.get_secret_value()  # the key the app itself resolves
        if not key:
            return "skipped (no key configured)"
        headers["Authorization"] = f"Bearer {key}"
    t0 = time.monotonic()
    try:
        r = httpx.get("https://api.openai.com/v1/models", headers=headers, timeout=20)
        return f"{r.status_code} ({time.monotonic() - t0:.2f}s)"
    except httpx.HTTPError as exc:
        return f"FAIL {type(exc).__name__} ({time.monotonic() - t0:.1f}s)"


def main() -> None:
    proxies = sorted(k for k, v in os.environ.items() if "proxy" in k.lower() and v)
    print("proxy variables set:", proxies or "none")
    for host in HOSTS:
        try:
            addrs = sorted({a[4][0] for a in socket.getaddrinfo(host, 443)})
        except OSError as exc:
            print(f"{host}: DNS FAIL {type(exc).__name__}")
            continue
        print(f"{host}: DNS {addrs}")
        for ip in addrs:
            result = tcp(ip)
            print(f"  tcp {ip}: {result}")
            if result.startswith("ok"):
                print(f"  tls {ip}: {tls(ip, host)}")
    print("https models endpoint without key (401 expected):", http_status(False))
    print("https models endpoint with the configured key (200 expected):", http_status(True))
    try:
        from app.providers.status import run_connection_test

        outcome = run_connection_test()
        print("app connection test:", "ok" if outcome.ok else f"FAIL {outcome.status}")
    except Exception as exc:  # noqa: BLE001 - a diagnostic reports every failure kind
        print("app connection test: FAIL", type(exc).__name__)


if __name__ == "__main__":
    main()
