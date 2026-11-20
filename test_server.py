import io
import math
import tempfile
import unittest
import wave
import threading
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from pathlib import Path
from server import (selection, trim, Handler, ORIGINS, failure_message, import_audio,
                    AudioSource, AudioSourceCache, ProviderFormatError, ProviderRateError)

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
            req = urllib.request.Request(url, payload, {'Origin': ORIGINS[0], 'Content-Type': 'application/json'})
            with patch('server.import_audio', return_value=(b'RIFF-test', 3.0)) as extract:
                with urllib.request.urlopen(req) as r:
                    self.assertEqual(r.read(), b'RIFF-test')
                    self.assertEqual(r.headers['Access-Control-Allow-Origin'], ORIGINS[0])
                    self.assertEqual(r.headers['Content-Type'], 'audio/wav')
                    self.assertEqual(r.headers['X-Maqam-Clip-Seconds'], '3.0')
                extract.assert_called_once()
            req = urllib.request.Request(url, payload, {'Origin': 'https://evil.test'})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(req)
            self.assertEqual(caught.exception.code, 403)
        finally:
            server.shutdown(); server.server_close(); t.join()

    def test_combined_mp4_fallback_and_short_video(self):
        combined = {'formatId': '18', 'ext': 'mp4', 'hasAudio': True, 'hasVideo': True}
        standalone = {'formatId': '140', 'ext': 'm4a', 'hasAudio': True, 'hasVideo': False}
        for expected_formats, formats, prefer_video in (
            (['video'], [combined], False),
            (['video'], [standalone, combined], True),
            (['audio', 'video'], [standalone, combined], False),
        ):
            with self.subTest(formats=formats, prefer_video=prefer_video):
                seen = []

                def fetch(path, payload, target=None, media_type='audio/'):
                    if path.endswith('/info'):
                        return {'success': True, 'data': {'durationSeconds': 8, 'formats': formats}}
                    seen.append(payload['format'])
                    if payload['format'] == 'audio':
                        raise ProviderFormatError('MP3 failed')
                    self.assertEqual((payload['formatId'], media_type), ('18', 'video/mp4'))
                    self.assertIs(payload['convert'], False)
                    with wave.open(str(target), 'wb') as wav:
                        wav.setparams((1, 2, 22050, 0, 'NONE', 'not compressed'))
                        wav.writeframes(b'\0\0' * (22050 * 8))

                with patch('server.provider_request', side_effect=fetch), patch('server.AUDIO_SOURCES', AudioSourceCache()):
                    audio, seconds = import_audio({'url': 'https://youtu.be/abcdefghijk',
                                                   'start': 2, 'end': 120, 'preferVideo': prefer_video})
                self.assertEqual(seen, expected_formats)
                self.assertEqual(seconds, 6)
                with wave.open(io.BytesIO(audio)) as wav:
                    self.assertEqual(wav.getnframes(), 22050 * 6)

    def test_known_format_skips_metadata_and_reuses_source_for_new_range(self):
        first = {'url': 'https://youtu.be/abcdefghijk', 'start': 1, 'end': 6, 'videoFormat18': True}
        second = {**first, 'url': 'https://www.youtube.com/watch?v=abcdefghijk', 'start': 3, 'end': 30}
        def fetch(path, payload, target=None, media_type='audio/'):
            self.assertTrue(path.startswith('/api/v1/download'))
            self.assertEqual((payload['formatId'], media_type), ('18', 'video/mp4'))
            self.assertEqual(payload['url'], 'https://www.youtube.com/watch?v=abcdefghijk')
            with wave.open(str(target), 'wb') as wav:
                wav.setparams((1, 2, 22050, 0, 'NONE', 'not compressed'))
                wav.writeframes(b'\0\0' * (22050 * 8))
        with patch('server.provider_request', side_effect=fetch) as provider, patch('server.AUDIO_SOURCES', AudioSourceCache()):
            timing_a, timing_b = {}, {}
            _, seconds_a = import_audio(first, timing_a)
            _, seconds_b = import_audio(second, timing_b)
            self.assertEqual((seconds_a, seconds_b), (5, 5))
            provider.assert_called_once()
        self.assertEqual((timing_a['cache'], timing_b['cache']), ('miss', 'hit'))
        self.assertGreater(timing_a['sourceBytes'], 0)
        self.assertEqual(timing_b['sourceBytes'], 0)
        self.assertNotIn('metadataMs', timing_a)
        self.assertIn('probeMs', timing_a)

    def test_hint_does_not_retry_upstream_429(self):
        request = {'url': 'https://youtu.be/abcdefghijk', 'start': 1, 'end': 6, 'videoFormat18': True}
        with patch('server.provider_request', side_effect=ProviderRateError('slow down')) as provider:
            with self.assertRaises(ProviderRateError):
                import_audio(request)
            provider.assert_called_once()

    def test_source_cache_coalesces_and_evicts(self):
        owner_started, shared_started, release = threading.Event(), threading.Event(), threading.Event()
        now = 0
        cache = AudioSourceCache(max_bytes=6, ttl=1, clock=lambda: now)
        class TrackedFuture(Future):
            def result(self, timeout=None):
                shared_started.set()
                return super().result(timeout)
        def fetch():
            owner_started.set()
            if not release.wait(2):
                raise RuntimeError('test did not release fetch')
            return AudioSource(b'abc', 6)
        def unnecessary_fetch():
            self.fail('duplicate media fetch')
        with patch('server.Future', TrackedFuture), ThreadPoolExecutor(max_workers=2) as pool:
            owner = pool.submit(cache.get, 'video-a', fetch)
            self.assertTrue(owner_started.wait(2))
            shared = pool.submit(cache.get, 'video-a', unnecessary_fetch)
            self.assertTrue(shared_started.wait(2))
            release.set()
            self.assertEqual(owner.result()[1], 'miss')
            self.assertEqual(shared.result()[1], 'shared')
        self.assertEqual(cache.get('video-a', unnecessary_fetch)[1], 'hit')
        cache.get('video-b', lambda: AudioSource(b'012345', 6))
        self.assertNotIn('video-a', cache.entries)
        self.assertEqual(cache.bytes, 6)
        now = 2
        self.assertEqual(cache.get('video-b', lambda: AudioSource(b'xy', 6))[1], 'miss')
        self.assertEqual(cache.bytes, 2)

    def test_invalid_media_not_persisted(self):
        cache = AudioSourceCache()
        def reject():
            raise ValueError('corrupt media')
        with self.assertRaises(ValueError):
            cache.get('id', reject)
        self.assertFalse(cache.entries)
        self.assertFalse(cache.inflight)
        self.assertEqual(cache.get('id', lambda: AudioSource(b'clip', 8))[1], 'miss')

    def test_preflight_accepts_both_site_origins(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/audio'
            for origin in ORIGINS:
                request = urllib.request.Request(url, method='OPTIONS', headers={'Origin': origin})
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(response.status, 204)
                    self.assertEqual(response.headers['Access-Control-Allow-Origin'], origin)
                    self.assertEqual(response.headers.get_all('Vary'), ['Origin'])
        finally:
            server.shutdown()
            server.server_close()
            t.join()


if __name__ == '__main__': unittest.main()
