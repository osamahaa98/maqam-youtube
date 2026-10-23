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
import traceback
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from fast_audio import Service as DirectService

ORIGIN = os.environ.get('SITE_ORIGIN', 'https://maqam-listener.soundtern.chatgpt.site')
LIMIT = 40 * 1024 * 1024
SLOTS = threading.BoundedSemaphore(2)
RATE_LOCK = threading.Lock()
RATE = {}
DIRECT_ENABLED = os.environ.get('MAQAM_DIRECT_AUDIO') == '1'
DIRECT_SERVICE = DirectService()

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
        detail = stderr.decode('utf-8', errors='replace')
        detail = re.sub(r'https?://[^\s]+', '[URL]', detail)
        detail = re.sub(r'(?i)(authorization|cookie|token|password|secret)[^\n]*', '[REDACTED]', detail)
        detail = re.sub(r'/tmp/[^\s]+', '[TEMP_FILE]', detail)
        lines = [line.strip() for line in detail.splitlines() if line.strip()]
        print('YouTube diagnostic log: ' + '\n'.join(lines)[-16000:], file=sys.stderr, flush=True)
        relevant = [line for line in lines if any(term in line.lower() for term in ('warning', 'error', 'timed out', 'http error'))]
        detail = ' | '.join((relevant or lines)[-8:])[-3000:]
        message += ' Details: ' + (detail or 'No stderr; exit=' + str(p.returncode))
        print('YouTube import failure: ' + message, file=sys.stderr, flush=True)
        raise ValueError(message)
    return out

def trim(source, target, start, end):
    run(['ffmpeg', '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe',
         '-ss', str(start), '-i', str(source), '-t', str(end-start), '-vn',
         '-ac', '1', '-ar', '22050', '-c:a', 'pcm_s16le', '-fs', str(LIMIT), str(target)], 30)

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

PROVIDER = 'https://save.brianrianrehan.com'

def provider_request(path, payload, target=None):
    request = Request(PROVIDER + path, json.dumps(payload).encode(),
                      {'Content-Type': 'application/json', 'Accept': '*/*', 'User-Agent': 'MaqamYouTube/1.0'}, method='POST')
    try:
        with build_opener(NoRedirect()).open(request, timeout=95 if target else 25) as response:
            if response.status != 200:
                raise ValueError('خدمة جلب الصوت لم تكمل الطلب؛ جرّب لاحقًا.')
            cap = LIMIT if target else 1024 * 1024
            length = response.headers.get('Content-Length')
            if length and int(length) > cap:
                raise ValueError('الصوت أكبر من الحد المتاح؛ ارفع الجزء المطلوب كملف.')
            if target and not response.headers.get('Content-Type', '').startswith('audio/'):
                raise ValueError('خدمة الجلب لم تُرجع ملف صوت صالحًا.')
            total = 0
            deadline = monotonic() + (100 if target else 25)
            chunks = []
            with open(target, 'wb') if target else tempfile.TemporaryFile() as output:
                while True:
                    if monotonic() > deadline:
                        raise ValueError('انتهت مهلة خدمة جلب الصوت.')
                    chunk = response.read(min(65536, cap - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > cap:
                        raise ValueError('الملف تجاوز الحجم المتاح.')
                    if target:
                        output.write(chunk)
                    else:
                        chunks.append(chunk)
            if not total:
                raise ValueError('خدمة الجلب أرجعت ملفًا فارغًا.')
            return None if target else json.loads(b''.join(chunks))
    except HTTPError as error:
        if error.code == 429:
            raise ValueError('خدمة الجلب وصلت حد الطلبات؛ جرّب لاحقًا أو ارفع الملف.') from None
        raise ValueError('خدمة جلب الصوت غير متاحة الآن؛ جرّب لاحقًا أو ارفع الملف.') from None
    except (URLError, TimeoutError):
        raise ValueError('تعذّر الاتصال بخدمة جلب الصوت؛ جرّب لاحقًا.') from None

def import_audio(data):
    url, start, end = selection(data)
    info = provider_request('/api/v1/info', {'url': url})
    meta = info.get('data', {})
    duration = meta.get('durationSeconds')
    if not info.get('success') or meta.get('isLive') or not isinstance(duration, (float, int)) or not math.isfinite(duration) or not 3 <= duration <= 1800 or end > duration:
        raise ValueError('اختر فيديو متاحًا ومسجلًا حتى ٣٠ دقيقة، وفترة داخل مدته.')
    with tempfile.TemporaryDirectory(prefix='maqam-youtube-') as folder:
        source = Path(folder) / 'source.mp3'
        provider_request('/api/v1/download?wait=true&download=true',
                         {'url': url, 'format': 'audio', 'quality': 'best', 'outputFormat': 'mp3'}, source)
        target = Path(folder) / 'clip.wav'
        trim(source, target, start, end)
        return target.read_bytes()

class Handler(BaseHTTPRequestHandler):
    def reply(self, status, body, content_type='application/json; charset=utf-8', timings=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Access-Control-Allow-Origin', ORIGIN)
        self.send_header('Vary', 'Origin')
        if timings is not None:
            self.send_header('X-Maqam-Cache', timings['cache'])
            self.send_header('X-Maqam-Source-Bytes', str(timings['sourceBytes']))
            self.send_header('Server-Timing', ', '.join(k[:-2]+';dur='+str(v) for k,v in timings.items() if k.endswith('Ms')))
            self.send_header('Access-Control-Expose-Headers', 'Server-Timing, X-Maqam-Cache, X-Maqam-Source-Bytes')
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
            return self.reply(200, json.dumps({'ok':True,'directAudio':DIRECT_ENABLED}).encode())
        self.error(404, 'Not found')

    def do_POST(self):
        if self.path not in ('/audio', '/clip'):
            return self.error(404, 'Not found')
        if self.headers.get('Origin') != ORIGIN:
            return self.error(403, 'Origin denied')
        if self.path == '/clip' and not DIRECT_ENABLED:
            return self.error(503, 'المسار المباشر لم يُفعّل على هذا الخادم.')
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
            # Bounded native clips are much cheaper than whole-video provider jobs.
            if len(q) >= (30 if self.path == '/clip' else 6):
                return self.error(429, 'طلبات كثيرة؛ انتظر قليلًا أو ارفع الملف.')
            q.append(now)
        if not SLOTS.acquire(blocking=False):
            return self.error(503, 'الخدمة مشغولة؛ جرّب بعد قليل.')
        try:
            if self.path == '/clip':
                url,start,end=selection(data)
                video=parse_qs(urlsplit(url).query)['v'][0]
                audio,timings=DIRECT_SERVICE.clip(video,start,end)
                self.reply(200,audio,'audio/wav',timings)
            else:
                self.reply(200, import_audio(data), 'audio/wav')
        except ValueError as e:
            self.error(422, str(e))
        except Exception as error:
            frames=' > '.join(Path(frame.filename).name+':'+str(frame.lineno)+':'+frame.name for frame in traceback.extract_tb(error.__traceback__) if Path(frame.filename).name in ('server.py','fast_audio.py'))
            print('Audio import unexpected exception type='+type(error).__name__+' frames='+frames,file=sys.stderr,flush=True)
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
