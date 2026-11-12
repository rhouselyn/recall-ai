from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from recall.web import RecallServer


class WebTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = RecallServer(0, str(Path(self.tmp.name) / "recall.db"))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.stopping.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, path, data=None, **headers):
        if data is not None:
            headers.setdefault("Content-Type", "application/json")
            data = json.dumps(data).encode()
        request = Request(self.base + path, data=data, headers=headers)
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def test_import_search_chat_and_origin(self):
        token = self.request("/api/session")["token"]
        headers = {"X-Recall-Token": token, "Origin": self.base}
        self.request("/api/add", {"url": "https://example.com/japan", "title": "日本市场研究"}, **headers)
        results = self.request("/api/items?query=%E6%97%A5%E6%9C%AC")
        self.assertEqual(results["items"][0]["title"], "日本市场研究")
        detail = self.request("/api/items/" + results["items"][0]["item_id"])
        self.assertEqual(detail["item"]["url"], "https://example.com/japan")
        with patch("recall.web.answer", return_value="找到收藏"):
            chat = self.request("/api/chat", {"text": "日本市场"}, **headers)
        self.assertEqual(chat["items"][0]["title"], "日本市场研究")
        with self.assertRaises(HTTPError) as forbidden:
            self.request("/api/add", {"url": "https://example.com/"}, **{
                "X-Recall-Token": token, "Origin": "https://attacker.example"
            })
        self.assertEqual(forbidden.exception.code, 403)


class MediaRegressionTest(unittest.TestCase):
    def test_small_decoder_frames_are_combined_before_asr(self):
        import sys
        import types
        import numpy as np
        from recall.media.engines import default_asr

        seen = []

        class Stream:
            result = types.SimpleNamespace(text="识别文字")

            def accept_waveform(self, rate, samples):
                seen.append((rate, len(samples)))

        class Recognizer:
            def create_stream(self):
                return Stream()

            def decode_stream(self, stream):
                pass

        module = types.SimpleNamespace(
            OfflineRecognizer=types.SimpleNamespace(from_paraformer=lambda **kwargs: Recognizer())
        )
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict("sys.modules", {"sherpa_onnx": module}), \
             patch.dict("os.environ", {"RECALL_ASR_MODEL_DIR": directory}), \
             patch("recall.media.engines.decode_audio_16k", return_value=[np.zeros(800)] * 4):
            segments = default_asr("sample.wav")
        self.assertEqual(seen, [(16000, 3200)])
        self.assertEqual(segments[0]["locator"]["end_seconds"], 0.2)


@unittest.skipUnless(os.name == "nt", "Windows DPAPI")
class CredentialTest(unittest.TestCase):
    def test_encrypted_credential_round_trip(self):
        from recall.secrets import load_secret, save_secret
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential.dpapi"
            with patch("recall.secrets._secret_path", return_value=path):
                self.assertTrue(save_secret("github", "test-only-secret"))
                self.assertNotIn(b"test-only-secret", path.read_bytes())
                self.assertEqual(load_secret("github"), "test-only-secret")


if __name__ == "__main__":
    unittest.main()
