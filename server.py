"""Bounded YouTube audio import. Run behind HTTPS; never accepts arbitrary URLs."""
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic
from urllib.parse import parse_qs, urlsplit

ORIGIN = os.environ.get('SITE_ORIGIN', 'https://maqam-listener.soundtern.chatgpt.site')
LIMIT = 40 * 1024 * 1024
SLOTS = threading.BoundedSemaphore(2)
RATE_LOCK = threading.Lock()
RATE = {}

def selection(data):
    u = urlsplit(str(data.get('url', '')))
    if u.scheme != 'https' or u.username or u.password or u.port not in (None, 443):
        raise ValueError('استخدم رابط يوتيوب يبدأ بـ https.')
    host = u.hostname
    if host == 'youtu.be':
        vid = u.path.strip('/')
    elif host in ('youtube.com', 'www.youtube.com', 'm.youtube.com'):
        if u.path == '/watch':
            vid = parse_qs(u.query).get('v', [''])[0]
        elif re.fullmatch(r'/(shorts|embed)/[A-Za-z0-9_-]{11}', u.path):
            vid = u.path.split('/')[-1]
        else:
            vid = ''
    else:
        vid = ''
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', vid):
        raise ValueError('رابط الفيديو غير صالح؛ لا ندعم روابط القنوات والقوائم.')
    start, end = float(data.get('start', 0)), float(data.get('end', 60))
    if not all(map(math.isfinite, (start, end))) or start < 0 or not 3 <= end-start <= 180 or end > 1800:
        raise ValueError('اختر جزءًا بين ٣ و١٨٠ ثانية، داخل أول ٣٠ دقيقة.')
    return f'https://www.youtube.com/watch?v={vid}', start, end

def failure_message(stderr):
    text = stderr.decode('utf-8', errors='replace').lower()
    if 'confirm you' in text and 'bot' in text:
        return 'يوتيوب طلب التحقق من أن اتصال الخادم ليس آليًا؛ تعذّر الجلب من هذه الاستضافة.'
    if 'no module named yt_dlp' in text:
        return 'أداة جلب الصوت غير مثبتة على الخادم.'
    if any(s in text for s in ('javascript runtime', 'javascript challenge', 'n challenge', 'signature extraction')):
        return 'أداة الجلب لم تتمكن من معالجة تحقق يوتيوب؛ يلزم فحص إصدارها ومحرك JavaScript.'
    if any(s in text for s in ('http error 403', '403 forbidden')):
        return 'يوتيوب رفض طلب الصوت من الخادم (403).'
    if any(s in text for s in ('private video', 'video unavailable', 'removed by', 'age-restricted', 'not available in your country')):
        return 'الفيديو غير متاح للخادم أو يحتاج صلاحية وصول لا تدعمها الخدمة.'
    if any(s in text for s in ('timed out', 'unable to download', 'name resolution', 'network is unreachable')):
        return 'تعذّر اتصال أداة الجلب بيوتيوب؛ قد يكون عطلًا في الشبكة أو قيود وصول.'
    if 'requested format is not available' in text:
        return 'لم تتوفر صيغة صوت قابلة للجلب لهذا الفيديو.'
    return 'فشلت أداة جلب الصوت؛ يلزم فحص سجل الخادم لمعرفة السبب.'

def run(args, timeout=90):
    # Kill the whole process group (including ffmpeg) on timeout.
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        out, stderr = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.communicate()
        raise ValueError('انتهت مهلة جلب الصوت؛ جرّب رفع الملف.')
    if p.returncode:
        message = failure_message(stderr)
        print('YouTube import failure: ' + message, file=sys.stderr, flush=True)
        raise ValueError(message)
    return out

def trim(source, target, start, end):
    run(['ffmpeg', '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe',
         '-ss', str(start), '-i', str(source), '-t', str(end-start), '-vn',
         '-ac', '1', '-ar', '22050', '-c:a', 'pcm_s16le', '-fs', str(LIMIT), str(target)], 30)

def import_audio(data):
    url, start, end = selection(data)
    base = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist',
            '--no-warnings', '--socket-timeout', '15', '--retries', '1', '--js-runtimes', 'node']
    meta = json.loads(run(base + ['--dump-single-json', '--skip-download', url], 45))
    duration = meta.get('duration')
    if meta.get('is_live') or not isinstance(duration, (float, int)) or not 3 <= duration <= 1800 or end > duration:
        raise ValueError('اختر فيديو مسجلًا حتى ٣٠ دقيقة، وفترة داخل مدته.')
    with tempfile.TemporaryDirectory(prefix='maqam-youtube-') as folder:
        root = Path(folder)
        run(base + ['-f', 'bestaudio[filesize<40M]/bestaudio', '--max-filesize', str(LIMIT),
                    '--no-progress', '-o', str(root / 'source.%(ext)s'), url])
        files = [p for p in root.glob('source.*') if p.suffix not in ('.part', '.ytdl')]
        if len(files) != 1 or not 0 < files[0].stat().st_size <= LIMIT:
            raise ValueError('الصوت غير متاح أو أكبر من ٤٠ ميجابايت؛ ارفع الجزء المطلوب كملف.')
        target = root / 'clip.wav'
        trim(files[0], target, start, end)
        return target.read_bytes()

class Handler(BaseHTTPRequestHandler):
    def reply(self, status, body, content_type='application/json; charset=utf-8'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Access-Control-Allow-Origin', ORIGIN)
        self.send_header('Vary', 'Origin')
        self.end_headers()
        self.wfile.write(body)

    def error(self, status, message):
        self.reply(status, json.dumps({'error': message}, ensure_ascii=False).encode())

    def do_OPTIONS(self):
        if self.headers.get('Origin') != ORIGIN:
            return self.error(403, 'Origin denied')
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', ORIGIN)
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        if self.path == '/health':
            return self.reply(200, b'{"ok":true}')
        self.error(404, 'Not found')

    def do_POST(self):
        if self.path != '/audio':
            return self.error(404, 'Not found')
        if self.headers.get('Origin') != ORIGIN:
            return self.error(403, 'Origin denied')
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 4096:
                return self.error(413, 'Request too large')
            data = json.loads(self.rfile.read(size))
            selection(data)
        except (ValueError, TypeError, AttributeError):
            return self.error(400, 'تحقق من الرابط والفترة المطلوبة.')
        now = monotonic()
        with RATE_LOCK:
            # Bound bookkeeping even under many distinct clients; rate limit by socket IP.
            for key in list(RATE):
                if not RATE[key] or now - RATE[key][-1] > 600:
                    del RATE[key]
            if len(RATE) > 10000:
                return self.error(503, 'الخدمة مشغولة؛ جرّب لاحقًا.')
            q = RATE.setdefault(self.client_address[0], deque())
            while q and now - q[0] > 600:
                q.popleft()
            if len(q) >= 6:
                return self.error(429, 'طلبات كثيرة؛ انتظر قليلًا أو ارفع الملف.')
            q.append(now)
        if not SLOTS.acquire(blocking=False):
            return self.error(503, 'الخدمة مشغولة؛ جرّب بعد قليل.')
        try:
            self.reply(200, import_audio(data), 'audio/wav')
        except ValueError as e:
            self.error(422, str(e))
        except Exception:
            self.error(502, 'تعذّر تجهيز الصوت؛ جرّب رفع الملف.')
        finally:
            SLOTS.release()

    def setup(self):
        super().setup()
        self.connection.settimeout(180)

    def log_message(self, *_):
        pass  # No video URLs, credentials or visitor addresses in logs.

if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', 8080))), Handler).serve_forever()
