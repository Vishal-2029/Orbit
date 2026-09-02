#!/usr/bin/env python3
"""Zero-dependency static file server for the Orbit web client.

Run:  python3 serve.py [port]
Serves the current directory on http://0.0.0.0:5173 (default) with
no-cache headers so edits show up on refresh without hard-reloading.
"""
import http.server
import socketserver
import sys
import os

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 5173


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        # Allow the API's CORS to work regardless of what host/IP this is served from.
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


class ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    with ReusableTCPServer(("0.0.0.0", PORT), NoCacheHandler) as httpd:
        print(f"Orbit web client: http://localhost:{PORT}  (Ctrl+C to stop)")
        print("Note: getUserMedia needs a secure context. Serving over a LAN IP")
        print("(e.g. http://192.168.x.x:5173) will NOT allow camera access in most")
        print("browsers unless you use HTTPS or a browser flag. See README notes.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping.")
