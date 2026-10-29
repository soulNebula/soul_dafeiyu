# -*- coding: utf-8 -*-
"""模型下载：断点续传 + sha256。"""
from __future__ import annotations

import hashlib
import http.server
import threading

from tools import fetch_model

PAYLOAD = b"GGUF" + b"x" * 1024 * 300


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        data = PAYLOAD
        rng = self.headers.get("Range")
        start = 0
        if rng and rng.startswith("bytes="):
            try:
                start = int(rng.split("=", 1)[1].split("-", 1)[0])
            except ValueError:
                start = 0
        body = data[start:]
        self.send_response(206 if start else 200)
        self.send_header("Content-Length", str(len(body)))
        if start:
            self.send_header("Content-Range", f"bytes {start}-{len(data)-1}/{len(data)}")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_download_and_resume(tmp_path):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/model.gguf"
        out = tmp_path / "model.gguf"
        digest = hashlib.sha256(PAYLOAD).hexdigest()
        result = fetch_model.download(url, out, sha256=digest, progress=lambda *_: None)
        assert result["ok"] is True and out.read_bytes() == PAYLOAD
        out.unlink()
        part = out.with_name(out.name + ".part")
        part.write_bytes(PAYLOAD[:5000])
        result2 = fetch_model.download(url, out, sha256=digest, progress=lambda *_: None)
        assert result2["ok"] is True and result2["resumed"] is True
        assert out.read_bytes() == PAYLOAD
    finally:
        server.shutdown()


def test_sha_mismatch_keeps_part(tmp_path):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/model.gguf"
        result = fetch_model.download(url, tmp_path / "x.gguf", sha256="0" * 64,
                                      progress=lambda *_: None)
        assert result["ok"] is False and result["error"] == "sha256_mismatch"
        assert (tmp_path / "x.gguf.part").is_file()
    finally:
        server.shutdown()
