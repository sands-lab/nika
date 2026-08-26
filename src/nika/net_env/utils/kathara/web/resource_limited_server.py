import http.server
import socketserver
import threading
import time

LEAKS = []
MAX_LEAKS_MB = 256
LEAK_PER_REQ_MB = 0.5

MAX_HOG_THREADS = 4
hog_threads = 0
lock = threading.Lock()


def cpu_hog():
    global hog_threads
    try:
        x = 0
        while True:
            x = (x + 1) % 1000000
    finally:
        with lock:
            hog_threads -= 1


class BadHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global hog_threads

        if len(LEAKS) * LEAK_PER_REQ_MB < MAX_LEAKS_MB:
            LEAKS.append(bytearray(int(LEAK_PER_REQ_MB * 1024 * 1024)))

        with lock:
            if hog_threads < MAX_HOG_THREADS:
                t = threading.Thread(target=cpu_hog, daemon=True)
                t.start()
                hog_threads += 1

        time.sleep(0.05)

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Welcome! This is a test server.\n")


if __name__ == "__main__":
    HOST, PORT = "", 80
    with socketserver.ThreadingTCPServer((HOST, PORT), BadHandler) as httpd:
        print(f"Server running on port {PORT}")
        httpd.serve_forever()
