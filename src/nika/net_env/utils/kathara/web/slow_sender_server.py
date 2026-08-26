import time
from http.server import BaseHTTPRequestHandler, HTTPServer

CHUNK = b"x" * 1024
DELAY = 0.1


class SlowSenderHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()

        for _ in range(5 * 1024):
            time.sleep(DELAY)
            self.wfile.write(CHUNK)


server = HTTPServer(("", 80), SlowSenderHandler)
server.serve_forever()
