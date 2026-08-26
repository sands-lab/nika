import random
import string
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse


def random_paragraph(min_len=80, max_len=200):
    length = random.randint(min_len, max_len)
    chars = string.ascii_letters + string.digits + "     "
    text = "".join(random.choice(chars) for _ in range(length))
    return f"<p>{text}</p>\n"


def generate_html_body(target_kb=256):
    target_bytes = target_kb * 1024
    parts = []
    current_size = 0

    header = """
    <h1>Mock Page</h1>
    <p>This is a mock page used for network / browser experiments.</p>
    <hr/>
    """
    parts.append(header)
    current_size += len(header.encode("utf-8"))

    while current_size < target_bytes:
        p = random_paragraph()
        parts.append(p)
        current_size += len(p.encode("utf-8"))

    return "".join(parts)


class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        size_kb = int(qs.get("size_kb", [256])[0])
        delay_ms = int(qs.get("delay_ms", [50])[0])
        chunks = int(qs.get("chunks", [16])[0])

        body = generate_html_body(size_kb)
        full_html = f"""
        <html>
            <head>
                <title>Mock Big Page</title>
                <meta charset="utf-8" />
            </head>
            <body>
                <div>
                    <p><strong>Path:</strong> {parsed.path}</p>
                    <p><strong>Size:</strong> ~{size_kb} KB</p>
                    <p><strong>Delay per chunk:</strong> {delay_ms} ms</p>
                    <p><strong>Chunks:</strong> {chunks}</p>
                    <p>Try change query: ?size_kb=1024&delay_ms=200&chunks=32</p>
                </div>
                <hr/>
                {body}
            </body>
        </html>
        """

        data = full_html.encode("utf-8")
        total_len = len(data)

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(total_len))
        self.end_headers()

        chunk_size = max(1, total_len // chunks)
        for i in range(0, total_len, chunk_size):
            self.wfile.write(data[i : i + chunk_size])
            self.wfile.flush()
            time.sleep(delay_ms / 1000.0)


if __name__ == "__main__":
    host = "0.0.0.0"
    port = 80

    httpd = HTTPServer((host, port), SimpleHandler)
    print(f"Serving on http://{host}:{port}")
    httpd.serve_forever()
