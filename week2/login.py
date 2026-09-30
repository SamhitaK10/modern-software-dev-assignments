"""One-time interactive Linear login. Run this in a terminal, NOT from the MCP server.

    export LINEAR_CLIENT_ID=... LINEAR_CLIENT_SECRET=...
    python week2/login.py

Opens the browser, catches the redirect on localhost, exchanges the code (PKCE + state),
and writes the token cache (default ~/.config/linear-mcp/token.json, mode 0600).
"""

from __future__ import annotations

import asyncio
import http.server
import secrets
import sys
import threading
import urllib.parse
import webbrowser

import auth


def _wait_for_callback(expected_state: str, timeout: float = 300) -> str:
    parsed = urllib.parse.urlparse(auth.REDIRECT_URI)
    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if urllib.parse.urlparse(self.path).path != parsed.path:
                self.send_response(404)
                self.end_headers()
                return
            if query.get("state", [""])[0] != expected_state:
                result["error"] = "state mismatch (possible CSRF); aborting"
            elif "error" in query:
                result["error"] = query["error"][0]
            else:
                result["code"] = query.get("code", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Linear login finished. You can close this tab.")
            done.set()

        def log_message(self, *args):  # silence request logging
            pass

    server = http.server.HTTPServer((parsed.hostname or "localhost", parsed.port or 8765), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        if not done.wait(timeout):
            raise TimeoutError("Timed out waiting for the browser redirect")
    finally:
        server.shutdown()
    if "error" in result:
        raise RuntimeError(result["error"])
    return result["code"]


async def main() -> int:
    client_id, _ = auth.client_credentials()
    verifier, challenge = auth.make_pkce_pair()
    state = secrets.token_urlsafe(24)
    url = auth.build_authorize_url(client_id, state, challenge)
    print(f"Opening browser for Linear consent (scopes: {', '.join(auth.SCOPES)}).")
    print(f"If it does not open, visit:\n{url}\n")
    webbrowser.open(url)
    code = await asyncio.to_thread(_wait_for_callback, state)
    store = auth.TokenStore()
    await store.exchange_code(code, verifier)
    print(f"Signed in. Token cached at {store.path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except (auth.OAuthConfigError, auth.AuthRequired, RuntimeError, TimeoutError) as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        sys.exit(1)
