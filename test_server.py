import io
import math
import tempfile
import unittest
import wave
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from pathlib import Path
from server import selection, trim, Handler, ORIGIN, failure_message

class Tests(unittest.TestCase):
    def test_safe_error_diagnosis(self):
        self.assertIn('آلي', failure_message(b"Sign in to confirm you're not a bot"))
        self.assertIn('403', failure_message(b'HTTP Error 403: Forbidden url=secret'))
        self.assertNotIn('secret', failure_message(b'unknown secret url=secret'))

    def test_urls(self):
        for url in ('https://youtu.be/abcdefghijk?t=20', 'https://www.youtube.com/watch?v=abcdefghijk&list=ignored', 'https://m.youtube.com/shorts/abcdefghijk'):
            self.assertEqual(selection({'url': url, 'start': 20, 'end': 50}), ('https://www.youtube.com/watch?v=abcdefghijk', 20, 50))
        for url in ('https://youtube.com.evil.test/watch?v=abcdefghijk', 'https://localhost/x', 'file:///tmp/x', 'https://www.youtube.com@localhost/watch?v=abcdefghijk', 'https://youtube.com:8000/watch?v=abcdefghijk', 'https://youtube.com/playlist?list=x'):
            with self.assertRaises(ValueError): selection({'url': url})

    def test_range(self):
        for start, end in ((0, 2), (-1, 60), (0, 181), (1790, 1801), (math.nan, 60), (0, math.inf)):
            with self.assertRaises(ValueError): selection({'url': 'https://youtu.be/abcdefghijk', 'start': start, 'end': end})

    def test_real_ffmpeg_trim(self):
        with tempfile.TemporaryDirectory() as d:
            source, target = Path(d)/'in.wav', Path(d)/'out.wav'
            with wave.open(str(source), 'wb') as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
                w.writeframes(b'\0\0' * (22050 * 5))
            trim(source, target, 1, 4)
            with wave.open(io.BytesIO(target.read_bytes())) as w:
                self.assertEqual(w.getframerate(), 22050)
                self.assertEqual(w.getnchannels(), 1)
                self.assertEqual(w.getnframes(), 22050 * 3)

    def test_http_import_and_cors(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        t = threading.Thread(target=server.serve_forever, daemon=True); t.start()
        url = f'http://127.0.0.1:{server.server_port}/audio'
        try:
            payload = b'{"url":"https://youtu.be/abcdefghijk","start":0,"end":3}'
            req = urllib.request.Request(url, payload, {'Origin': ORIGIN, 'Content-Type': 'application/json'})
            with patch('server.import_audio', return_value=b'RIFF-test') as extract:
                with urllib.request.urlopen(req) as r:
                    self.assertEqual(r.read(), b'RIFF-test')
                    self.assertEqual(r.headers['Access-Control-Allow-Origin'], ORIGIN)
                    self.assertEqual(r.headers['Content-Type'], 'audio/wav')
                extract.assert_called_once()
            req = urllib.request.Request(url, payload, {'Origin': 'https://evil.test'})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(req)
            self.assertEqual(caught.exception.code, 403)
        finally:
            server.shutdown(); server.server_close(); t.join()

if __name__ == '__main__': unittest.main()
