"""Disposable HTTPS login/storage origin and exact-origin proxy, with no outbound access."""

from __future__ import annotations

import argparse
import json
import secrets
import select
import socket
import socketserver
import ssl
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGE = b"""<!doctype html><title>Persistence acceptance fixture</title>
<meta charset="utf-8"><h1>Disposable HTTPS login fixture</h1>
<script>
window.fixtureReady = true;
window.fixturePut = async (value) => {
  localStorage.setItem('persistent-marker', value);
  const db = await new Promise((resolve, reject) => {
    const request = indexedDB.open('persistence-acceptance', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('values');
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  await new Promise((resolve, reject) => {
    const transaction = db.transaction('values', 'readwrite');
    transaction.objectStore('values').put(value, 'marker');
    transaction.oncomplete = resolve;
    transaction.onerror = () => reject(transaction.error);
  });
  db.close();
  return true;
};
window.fixtureRead = async () => {
  const db = await new Promise((resolve, reject) => {
    const request = indexedDB.open('persistence-acceptance', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('values');
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  const marker = await new Promise((resolve, reject) => {
    const request = db.transaction('values').objectStore('values').get('marker');
    request.onsuccess = () => resolve(request.result ?? null);
    request.onerror = () => reject(request.error);
  });
  db.close();
  const auth = await (await fetch('/whoami')).json();
  return {local: localStorage.getItem('persistent-marker'), indexedDB: marker,
          cookieVisibleToJS: document.cookie, secureContext: window.isSecureContext,
          authenticated: auth.authenticated, persistentAuthenticated: auth.persistent,
          sessionAuthenticated: auth.session};
};
</script>"""


class LoginServer(ThreadingHTTPServer):
    """The fixture session secret exists only in this disposable process."""

    auth_cookie: str
    session_cookie: str


class LoginHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        """Never log request headers or fixture authentication state."""

    def reply(self, body: bytes, *, cookies: tuple[str, ...] = (), html: bool = False) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html" if html else "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/":
            self.reply(PAGE, html=True)
            return
        if self.path == "/whoami":
            if not isinstance(self.server, LoginServer):
                raise RuntimeError("Fixture server type mismatch")
            parsed = SimpleCookie()
            parsed.load(self.headers.get("Cookie", ""))
            found = parsed.get("fixture_auth")
            authenticated = found is not None and secrets.compare_digest(
                found.value, self.server.auth_cookie
            )
            session = parsed.get("fixture_session")
            session_authenticated = session is not None and secrets.compare_digest(
                session.value, self.server.session_cookie
            )
            self.reply(
                json.dumps(
                    {
                        "authenticated": authenticated and session_authenticated,
                        "persistent": authenticated,
                        "session": session_authenticated,
                    }
                ).encode()
            )
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if not isinstance(self.server, LoginServer):
            raise RuntimeError("Fixture server type mismatch")
        if self.path == "/login":
            persistent = (
                "fixture_auth="
                + self.server.auth_cookie
                + "; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=3600"
            )
            session = (
                "fixture_session="
                + self.server.session_cookie
                + "; Path=/; Secure; HttpOnly; SameSite=Strict"
            )
        elif self.path == "/logout":
            persistent = "fixture_auth=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0"
            session = "fixture_session=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0"
        else:
            self.send_error(404)
            return
        self.reply(b'{"ok":true}', cookies=(persistent, session))


class FixtureProxy(socketserver.StreamRequestHandler):
    """Permit CONNECT only to this container's own HTTPS fixture; deny everything else."""

    def handle(self) -> None:
        self.connection.settimeout(5)
        request = self.rfile.readline(4096)
        if request != b"CONNECT fixture.internal:4443 HTTP/1.1\r\n":
            self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            return
        total = len(request)
        while True:
            line = self.rfile.readline(4096)
            total += len(line)
            if total > 16384 or not line:
                return
            if line == b"\r\n":
                break
        with socket.create_connection(("127.0.0.1", 4443), timeout=3) as upstream:
            self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.wfile.flush()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                readable, _, _ = select.select([self.connection, upstream], [], [], 1)
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    destination = upstream if source is self.connection else self.connection
                    destination.sendall(data)


class ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--certificate", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    args = parser.parse_args()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.certificate, args.private_key)
    with LoginServer(("0.0.0.0", 4443), LoginHandler) as https:
        https.auth_cookie = secrets.token_urlsafe(32)
        https.session_cookie = secrets.token_urlsafe(32)
        https.socket = context.wrap_socket(https.socket, server_side=True)
        thread = threading.Thread(target=https.serve_forever, daemon=True)
        thread.start()
        with ProxyServer(("0.0.0.0", 3128), FixtureProxy) as proxy:
            print("Disposable HTTPS fixture and exact-origin proxy ready", flush=True)
            proxy.serve_forever()


if __name__ == "__main__":
    main()
