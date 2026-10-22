#!/usr/bin/env python3
"""
One-time helper: connect your YouTube Studio data (read-only) to the trend radar.

Run this ON YOUR OWN COMPUTER, not on GitHub:
    python3 get_youtube_token.py path/to/client_secret.json

It opens your browser, you sign in with the Google account that owns your channel and allow
read-only access to YouTube Analytics. It then prints three values to paste into GitHub secrets:
YT_OAUTH_CLIENT_ID, YT_OAUTH_CLIENT_SECRET, YT_OAUTH_REFRESH_TOKEN.

Nothing is saved to disk. Treat the printed refresh token like a password: paste it only into the
GitHub secret, never into chats, emails or screenshots. You can revoke it any time at
https://myaccount.google.com/permissions
Standard library only.
"""
import base64
import hashlib
import http.server
import json
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 get_youtube_token.py path/to/client_secret.json")
        return 1
    with open(sys.argv[1], encoding="utf-8") as f:
        raw = json.load(f)
    c = raw.get("installed") or raw.get("web")
    if not c:
        print("This doesn't look like an OAuth client file. Download it from Google Cloud → Credentials → your Desktop app client.")
        return 1
    client_id, client_secret = c["client_id"], c["client_secret"]

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] != state:
                self.send_response(400); self.end_headers(); self.wfile.write(b"State mismatch. Close this tab and try again.")
                return
            result["code"] = q.get("code", [""])[0]
            result["error"] = q.get("error", [""])[0]
            self.send_response(200); self.send_header("content-type", "text/html; charset=utf-8"); self.end_headers()
            self.wfile.write("<p style='font-family:sans-serif'>Done. You can close this tab and go back to the terminal.</p>".encode())

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)  # local only, random free port
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    print("Opening your browser to sign in. If it doesn't open, copy this link into your browser:\n\n" + url + "\n")
    threading.Thread(target=server.handle_request, daemon=True).start()
    webbrowser.open(url)
    while "code" not in result:
        threading.Event().wait(0.3)
    server.server_close()
    if result.get("error") or not result["code"]:
        print("Sign-in was cancelled or failed:", result.get("error"))
        return 1

    body = urllib.parse.urlencode({"code": result["code"], "client_id": client_id, "client_secret": client_secret,
                                   "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier}).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=body, method="POST",
                                 headers={"content-type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        tok = json.loads(r.read())
    refresh = tok.get("refresh_token")
    if not refresh:
        print("Google didn't return a refresh token. Remove the app's access at https://myaccount.google.com/permissions and run this again.")
        return 1
    print("Success. Add these three as GitHub secrets (Settings → Secrets and variables → Actions):\n")
    print("YT_OAUTH_CLIENT_ID      =", client_id)
    print("YT_OAUTH_CLIENT_SECRET  =", client_secret)
    print("YT_OAUTH_REFRESH_TOKEN  =", refresh)
    print("\nThen close this terminal. Don't save these values anywhere else.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
