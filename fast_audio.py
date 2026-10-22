"""Opt-in native AAC range path. Never falls back to a whole-video conversion queue.
Only the resolver talks to YouTube metadata; only signed Googlevideo URLs are read.
Signed URLs remain in bounded process memory and are never returned to the browser.
"""
import bisect
import concurrent.futures
import signal
import io
import json
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

MAX_METADATA=4*1024*1024
MAX_SOURCE=40*1024*1024
MAX_TRANSFER=12*1024*1024
CHUNK=128*1024
RATES=[96000,88200,64000,48000,44100,32000,24000,22050,16000,12000,11025,8000,7350]

class FastAudioError(ValueError):pass

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*_):return None

def media_url(url):
    u=urlsplit(url)
    if u.scheme!='https' or u.username or u.password or u.port not in (None,443) or not u.hostname or not u.hostname.endswith('.googlevideo.com'):
        raise FastAudioError('مصدر الصوت المباشر غير مدعوم.')
    return url

class Remote:
    def __init__(self,url,length,headers=None):
        self.url=media_url(url)
        if not isinstance(length,int) or not 0<length<=MAX_SOURCE:raise FastAudioError('حجم مصدر الصوت غير مدعوم.')
        self.length=length;self.bytes=0;self.headers=headers or {};self.deadline=time.monotonic()+20
        self.opener=build_opener(NoRedirect());self.transfer_lock=threading.Lock()
    def read(self,offset,size):
        if offset<0 or size<0 or offset+size>self.length or size>MAX_METADATA:raise FastAudioError('نطاق صوت غير صالح.')
        if self.bytes+size>MAX_TRANSFER:raise FastAudioError('تجاوز جلب الفترة الحد المحدد.')
        def fetch(pos):
            if time.monotonic()>self.deadline:raise FastAudioError('انتهت مهلة جلب الفترة المباشرة.')
            count=min(CHUNK,offset+size-pos)
            headers={k:v for k,v in self.headers.items() if k.lower() in ('user-agent','accept','accept-language')}
            headers['Range']=f'bytes={pos}-{pos+count-1}'
            with self.opener.open(Request(self.url,headers=headers),timeout=min(8,max(.1,self.deadline-time.monotonic()))) as r:
                expected=f'bytes {pos}-{pos+count-1}/{self.length}'
                if r.status!=206 or r.headers.get('Content-Range')!=expected:raise FastAudioError('المصدر لم يحترم نطاق الصوت؛ أوقفنا الطلب.')
                data=r.read(count+1)
                if len(data)!=count:raise FastAudioError('وصلت فترة صوت غير مكتملة.')
                with self.transfer_lock:self.bytes+=len(data)
                return data
        positions=range(offset,offset+size,CHUNK)
        if size<=CHUNK:return b''.join(fetch(p) for p in positions)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:return b''.join(pool.map(fetch,positions))

def boxes(data,start=0,end=None):
    end=len(data) if end is None else end;pos=start
    while pos<end:
        if pos+8>end:raise FastAudioError('حاوية صوت غير مكتملة.')
        size,kind=struct.unpack_from('>I4s',data,pos);header=8
        if size==1:
            if pos+16>end:raise FastAudioError('حاوية صوت غير مكتملة.')
            size=struct.unpack_from('>Q',data,pos+8)[0];header=16
        if size==0:size=end-pos
        if size<header or pos+size>end:raise FastAudioError('حجم حاوية صوت غير صالح.')
        yield kind,data[pos+header:pos+size]
        pos+=size

def child(data,kind):
    for k,payload in boxes(data):
        if k==kind:return payload
    raise FastAudioError('بيانات توقيت الصوت غير مكتملة.')

def descriptors(data):
    pos=0
    while pos<len(data):
        tag=data[pos];pos+=1;length=0
        for i in range(4):
            if pos>=len(data):raise FastAudioError('وصف AAC غير صالح.')
            n=data[pos];pos+=1;length=(length<<7)|(n&127)
            if not n&128:break
        else:raise FastAudioError('وصف AAC غير صالح.')
        if pos+length>len(data):raise FastAudioError('وصف AAC غير مكتمل.')
        payload=data[pos:pos+length];pos+=length
        if tag==5:return payload
        if tag==3:
            if len(payload)<3:raise FastAudioError('وصف AAC غير صالح.')
            flags=payload[2];skip=3+(2 if flags&128 else 0)
            if flags&64:
                if skip>=len(payload):raise FastAudioError('وصف AAC غير صالح.')
                skip+=1+payload[skip]
            if flags&32:skip+=2
            found=descriptors(payload[skip:])
            if found:return found
        elif tag==4:
            if len(payload)<13 or payload[0]!=0x40:raise FastAudioError('ترميز غير مدعوم للمسار السريع.')
            found=descriptors(payload[13:])
            if found:return found
    return None

def u32(data,offset):
    if offset+4>len(data):raise FastAudioError('جدول توقيت غير مكتمل.')
    return struct.unpack_from('>I',data,offset)[0]

def table(data,width):
    count=u32(data,4)
    if count>200000 or 8+count*width!=len(data):raise FastAudioError('جدول صوت غير صالح.')
    return [data[p:p+width] for p in range(8,len(data),width)]

class M4A:
    def __init__(self,source):
        self.source=source;pos=0;moov=None
        for _ in range(64):
            if pos+8>source.length:break
            head=source.read(pos,8);size,kind=struct.unpack('>I4s',head);header=8
            if size==1:size=struct.unpack('>Q',source.read(pos+8,8))[0];header=16
            if not size:size=source.length-pos
            if size<header or pos+size>source.length:raise FastAudioError('حاوية مصدر غير صالحة.')
            if kind==b'moov':
                if size>MAX_METADATA:raise FastAudioError('بيانات توقيت المصدر كبيرة.')
                moov=source.read(pos+header,size-header);break
            pos+=size
        if moov is None:raise FastAudioError('لم نجد توقيت الصوت في المصدر.')
        track=None
        for kind,tr in boxes(moov):
            if kind!=b'trak':continue
            mdia=child(tr,b'mdia');handler=child(mdia,b'hdlr')
            if len(handler)>=12 and handler[8:12]==b'soun':track=tr;break
        if track is None:raise FastAudioError('لا يوجد مسار صوتي.')
        mdia=child(track,b'mdia');mdhd=child(mdia,b'mdhd');v=mdhd[0]
        self.scale=u32(mdhd,12 if v==0 else 20)
        if self.scale not in RATES:raise FastAudioError('معدل الصوت غير مدعوم.')
        stbl=child(child(mdia,b'minf'),b'stbl');stsd=child(stbl,b'stsd')
        entries=list(boxes(stsd,8))
        if u32(stsd,4)!=1 or len(entries)!=1 or entries[0][0]!=b'mp4a':raise FastAudioError('صيغة المسار السريع تتطلب AAC.')
        sample=entries[0][1]
        if len(sample)<28 or sample[8:10]!=b'\0\0':raise FastAudioError('إصدار AAC غير مدعوم.')
        esds=child(sample[28:],b'esds');asc=descriptors(esds[4:])
        if not asc or len(asc)<2:raise FastAudioError('وصف AAC غير مكتمل.')
        bits=int.from_bytes(asc[:2],'big');aot=bits>>11;self.rate_index=(bits>>7)&15;self.channels=(bits>>3)&15
        if aot!=2 or self.rate_index>=len(RATES) or RATES[self.rate_index]!=self.scale or not 1<=self.channels<=2:
            raise FastAudioError('المسار السريع يدعم AAC-LC أحاديًا أو ستيريو فقط.')
        stsz=child(stbl,b'stsz');fixed=u32(stsz,4);count=u32(stsz,8)
        if not 1<=count<=200000 or len(stsz)!=(12 if fixed else 12+count*4):raise FastAudioError('عدد عينات غير صالح.')
        self.sizes=[fixed]*count if fixed else [u32(stsz,12+i*4) for i in range(count)]
        if any(not 0<n<=8184 for n in self.sizes):raise FastAudioError('حجم عينة AAC غير صالح.')
        self.times=[0]
        for row in table(child(stbl,b'stts'),8):
            n,delta=struct.unpack('>II',row)
            final_partial=n==1 and len(self.times)+n==count+1 and 0<delta<1024
            if (delta!=1024 and not final_partial) or n<1 or len(self.times)+n>count+1:raise FastAudioError('توقيت AAC غير مدعوم.')
            for _ in range(n):self.times.append(self.times[-1]+delta)
        if len(self.times)!=count+1:raise FastAudioError('توقيت AAC غير مكتمل.')
        try:offsets=[struct.unpack('>I',r)[0] for r in table(child(stbl,b'stco'),4)]
        except FastAudioError:offsets=[struct.unpack('>Q',r)[0] for r in table(child(stbl,b'co64'),8)]
        mapping=[struct.unpack('>III',r) for r in table(child(stbl,b'stsc'),12)]
        if not mapping or mapping[0][0]!=1 or any(c<1 or d!=1 for _,c,d in mapping) or any(mapping[i][0]>=mapping[i+1][0] for i in range(len(mapping)-1)):
            raise FastAudioError('توزيع عينات غير صالح.')
        self.offsets=[];m=0;s=0
        for chunk,offset in enumerate(offsets,1):
            while m+1<len(mapping) and mapping[m+1][0]<=chunk:m+=1
            for _ in range(mapping[m][1]):
                if s>=count:raise FastAudioError('توزيع عينات زائد.')
                if offset<0 or offset+self.sizes[s]>source.length:raise FastAudioError('عنوان عينة خارج المصدر.')
                self.offsets.append(offset);offset+=self.sizes[s];s+=1
        if s!=count:raise FastAudioError('توزيع عينات غير مكتمل.')
        self.media_start=0
        try:
            elst=child(child(track,b'edts'),b'elst')
            if u32(elst,4)!=1 or elst[0]!=0 or len(elst)!=20:raise FastAudioError('تحرير توقيت غير مدعوم.')
            self.media_start=struct.unpack_from('>i',elst,12)[0]
            if not 0<=self.media_start<=4096:raise FastAudioError('بداية صوت غير مدعومة.')
        except FastAudioError as e:
            if 'تحرير' in str(e) or 'بداية' in str(e):raise
        self.duration=(self.times[-1]-self.media_start)/self.scale
    def cut(self,start,end,target):
        if not all(map(math.isfinite,(start,end))) or start<0 or not 3<=end-start<=180:raise FastAudioError('فترة غير صالحة.')
        end=min(end,self.duration)
        if end-start<3:raise FastAudioError('الفترة خارج المصدر.')
        first=max(0,bisect.bisect_right(self.times,self.media_start+start*self.scale)-3)
        last=min(len(self.sizes),bisect.bisect_left(self.times,self.media_start+end*self.scale)+1)
        # Coalesce consecutive samples into bounded reads. Never read the full mdat.
        with open(target,'wb') as out:
            i=first
            while i<last:
                j=i+1;size=self.sizes[i]
                while j<last and self.offsets[j]==self.offsets[j-1]+self.sizes[j-1] and size+self.sizes[j]<=8*CHUNK:
                    size+=self.sizes[j];j+=1
                data=self.source.read(self.offsets[i],size);p=0
                for k in range(i,j):
                    n=self.sizes[k]+7;ch=self.channels;fi=self.rate_index
                    out.write(bytes([255,241,(1<<6)|(fi<<2)|(ch>>2),((ch&3)<<6)|(n>>11),(n>>3)&255,((n&7)<<5)|31,252]));out.write(data[p:p+self.sizes[k]]);p+=self.sizes[k]
                i=j
        offset=(self.media_start+start*self.scale-self.times[first])/self.scale
        return offset,end-start

class Resolver:
    def __init__(self):self.lock=threading.Lock();self.cache=OrderedDict();self.jobs=[threading.Lock() for _ in range(16)]
    def resolve(self,video):
        with self.jobs[hash(video)%len(self.jobs)]:return self._resolve(video)
    def _resolve(self,video):
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}',video):raise FastAudioError('رابط فيديو غير صالح.')
        with self.lock:
            item=self.cache.get(video)
            if item and item[0]>time.monotonic():self.cache.move_to_end(video);return item[1],True
        args=[sys.executable,'-m','yt_dlp','--no-playlist','--no-warnings','--skip-download','--dump-single-json','--socket-timeout','5','--retries','0','--extractor-retries','0','--js-runtimes','node','-f','bestaudio[ext=m4a][acodec=mp4a.40.2][protocol=https]','--','https://www.youtube.com/watch?v='+video]
        p=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        try:out,stderr=p.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid,signal.SIGKILL);p.communicate()
            raise FastAudioError('انتهت مهلة استخراج رابط الصوت.') from None
        if p.returncode:
            detail=stderr.decode('utf-8',errors='replace').lower()
            reason='تعذّر استخراج مصدر الصوت مباشرة من هذا الخادم.'
            if 'confirm you' in detail and 'bot' in detail:reason='يوتيوب طلب التحقق من اتصال الخادم؛ الجلب المباشر غير متاح عليه.'
            elif '403' in detail:reason='يوتيوب رفض استخراج مصدر الصوت من هذا الخادم (403).'
            elif any(term in detail for term in ('javascript challenge','signature extraction','n challenge')):reason='تعذّر معالجة تحقق JavaScript لاستخراج الصوت.'
            elif 'requested format is not available' in detail:reason='صيغة AAC المباشرة غير متاحة لهذا الفيديو.'
            print('Direct resolver: '+reason,file=sys.stderr,flush=True)
            raise FastAudioError(reason)
        if len(out)>4*1024*1024:raise FastAudioError('بيانات مصدر كبيرة.')
        try:
            j=json.loads(out);duration=j.get('duration');size=j.get('filesize');url=media_url(j['url'])
            if j.get('id')!=video or j.get('is_live') or not isinstance(duration,(int,float)) or not math.isfinite(duration) or not 3<=duration<=1800 or not isinstance(size,int) or not 0<size<=MAX_SOURCE:raise ValueError()
            result={'url':url,'length':size,'duration':duration,'headers':j.get('http_headers',{})}
        except (KeyError,TypeError,ValueError):raise FastAudioError('بيانات مصدر غير مدعومة.') from None
        with self.lock:
            self.cache[video]=(time.monotonic()+300,result)
            while len(self.cache)>64:self.cache.popitem(last=False)
        return result,False

class Service:
    def __init__(self):self.resolver=Resolver();self.lock=threading.Lock();self.cache=OrderedDict();self.bytes=0;self.jobs=[threading.Lock() for _ in range(16)]
    def clip(self,video,start,end):
        queued=time.monotonic()
        with self.jobs[hash((video,start,end))%len(self.jobs)]:
            acquired=time.monotonic();body,timings=self._clip(video,start,end)
            timings['waitMs']=round((acquired-queued)*1000)
            timings['totalMs']=round((time.monotonic()-queued)*1000)
            return body,timings
    def _clip(self,video,start,end):
        key=(video,start,end);started=time.monotonic()
        with self.lock:
            saved=self.cache.get(key)
            if saved and saved[0]>started:self.cache.move_to_end(key);return saved[1],{'cache':'hit','totalMs':round((time.monotonic()-started)*1000),'sourceBytes':0}
        meta,resolver_hit=self.resolver.resolve(video);resolved=time.monotonic();remote=Remote(meta['url'],meta['length'],meta['headers']);index=M4A(remote)
        with tempfile.TemporaryDirectory(prefix='maqam-fast-') as folder:
            source=Path(folder)/'clip.aac';target=Path(folder)/'clip.wav';offset,duration=index.cut(start,end,source);fetched=time.monotonic()
            args=['ffmpeg','-nostdin','-v','error','-protocol_whitelist','file,pipe','-ss',str(offset),'-i',str(source),'-t',str(duration),'-vn','-ac','1','-ar','22050','-c:a','pcm_s16le',str(target)]
            try:p=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=8)
            except subprocess.TimeoutExpired:raise FastAudioError('انتهت مهلة فك الفترة الصوتية.') from None
            if p.returncode or not target.is_file():raise FastAudioError('تعذّر فك الفترة الصوتية.')
            body=target.read_bytes()
            with wave.open(io.BytesIO(body)) as w:
                if abs(w.getnframes()/w.getframerate()-duration)>.08:raise FastAudioError('الفترة الصوتية غير مكتملة.')
        with self.lock:
            now=time.monotonic()
            for k in list(self.cache):
                if self.cache[k][0]<=now:self.bytes-=len(self.cache.pop(k)[1])
            while self.cache and (self.bytes+len(body)>32*1024*1024 or len(self.cache)>=32):self.bytes-=len(self.cache.popitem(last=False)[1][1])
            if key in self.cache:self.bytes-=len(self.cache.pop(key)[1])
            self.cache[key]=(now+600,body);self.bytes+=len(body)
        return body,{'cache':'miss','resolverCache':resolver_hit,'resolveMs':round((resolved-started)*1000),'rangeMs':round((fetched-resolved)*1000),'decodeMs':round((time.monotonic()-fetched)*1000),'totalMs':round((time.monotonic()-started)*1000),'sourceBytes':remote.bytes}
