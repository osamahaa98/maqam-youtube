"""Opt-in native AAC range path. Never falls back to a whole-video conversion queue.
Only the resolver talks to metadata; signed Googlevideo or Tunelio tunnel URLs are read.
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
from urllib.parse import urlsplit, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

MAX_METADATA=4*1024*1024
MAX_SOURCE=40*1024*1024
MAX_TRANSFER=12*1024*1024
CHUNK=1024*1024
PREFIX=64*1024
RATES=[96000,88200,64000,48000,44100,32000,24000,22050,16000,12000,11025,8000,7350]

class FastAudioError(ValueError):pass

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*_):return None

def failure_markers(error):
    # Classify a bounded vendor error without logging its text or signed URL.
    try:text=error.read(4096).decode('utf-8',errors='ignore').lower()
    except Exception:return 'unreadable'
    finally:error.close()
    terms=('cloudflare','invalid signature','expired','upstream','youtube','unauthorized','forbidden','access denied')
    return ','.join(term.replace(' ','_') for term in terms if term in text) or 'unclassified'

def media_url(url):
    if not isinstance(url,str):raise FastAudioError('مصدر الصوت المباشر غير مدعوم.')
    try:u=urlsplit(url);port=u.port
    except ValueError:raise FastAudioError('مصدر الصوت المباشر غير مدعوم.') from None
    allowed=u.hostname and (u.hostname.endswith('.googlevideo.com') or (u.hostname=='tunelio.dev' and u.path=='/tunnel' and bool(u.query)))
    if u.scheme!='https' or u.username or u.password or port not in (None,443) or not allowed or u.fragment or len(url)>8192:
        raise FastAudioError('مصدر الصوت المباشر غير مدعوم.')
    return url

class Remote:
    def __init__(self,url,length,headers=None):
        self.url=media_url(url)
        if not isinstance(length,int) or not 0<length<=MAX_SOURCE:raise FastAudioError('حجم مصدر الصوت غير مدعوم.')
        self.length=length;self.bytes=0;self.reserved=0;self.headers=headers or {};self.deadline=time.monotonic()+20
        self.opener=build_opener(NoRedirect());self.transfer_lock=threading.Lock()
        self.prefix=None;self.prefix_lock=threading.Lock()
    def read(self,offset,size):
        if offset<0 or size<0 or offset+size>self.length or size>MAX_METADATA:raise FastAudioError('نطاق صوت غير صالح.')
        if size==0:return b''
        # One bounded prefix covers ftyp/moov/sidx in ordinary YouTube AAC.
        # Avoid a new TLS round trip for every 8-byte box header.
        if offset+size<=min(PREFIX,self.length):
            with self.prefix_lock:
                if self.prefix is None:self.prefix=self._read_remote(0,min(PREFIX,self.length))
                return self.prefix[offset:offset+size]
        return self._read_remote(offset,size)
    def _read_remote(self,offset,size):
        with self.transfer_lock:
            if self.reserved+size>MAX_TRANSFER:raise FastAudioError('تجاوز جلب الفترة الحد المحدد.')
            self.reserved+=size
        def fetch(pos):
            if time.monotonic()>self.deadline:raise FastAudioError('انتهت مهلة جلب الفترة المباشرة.')
            count=min(CHUNK,offset+size-pos)
            headers={k:v for k,v in self.headers.items() if k.lower() in ('user-agent','accept','accept-language')}
            headers['Range']=f'bytes={pos}-{pos+count-1}'
            try:
                with self.opener.open(Request(self.url,headers=headers),timeout=min(12,max(.1,self.deadline-time.monotonic()))) as r:
                    expected=f'bytes {pos}-{pos+count-1}/{self.length}'
                    if r.status!=206 or r.headers.get('Content-Range')!=expected:raise FastAudioError('المصدر لم يحترم نطاق الصوت؛ أوقفنا الطلب.')
                    data=r.read(count+1)
                    if len(data)!=count:raise FastAudioError('وصلت فترة صوت غير مكتملة.')
                    with self.transfer_lock:self.bytes+=len(data)
                    return data
            except HTTPError as error:
                print('Audio range HTTP status='+str(error.code)+' markers='+failure_markers(error),file=sys.stderr,flush=True)
                raise FastAudioError('مصدر الصوت رفض جلب الفترة ('+str(error.code)+').') from None
            except (URLError,TimeoutError) as error:
                print('Audio range network failure type='+type(error).__name__,file=sys.stderr,flush=True)
                raise FastAudioError('انتهت مهلة اتصال مصدر الصوت أثناء جلب الفترة.') from None
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

def located_boxes(data):
    pos=0
    for kind,payload in boxes(data):
        header=16 if u32(data,pos)==1 else 8
        yield pos,kind,payload,header
        pos+=len(payload)+header

def adts(out,data,rate_index,channels):
    n=len(data)+7
    if not 7<n<=8191:raise FastAudioError('حجم عينة AAC غير صالح.')
    out.write(bytes([255,241,64|(rate_index<<2)|(channels>>2),((channels&3)<<6)|(n>>11),(n>>3)&255,((n&7)<<5)|31,252]));out.write(data)

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
        self.media_start=0
        try:
            elst=child(child(track,b'edts'),b'elst')
            if u32(elst,4)!=1 or elst[0]!=0 or len(elst)!=20:raise FastAudioError('تحرير توقيت غير مدعوم.')
            self.media_start=struct.unpack_from('>i',elst,12)[0]
            if not 0<=self.media_start<=4096:raise FastAudioError('بداية صوت غير مدعومة.')
        except FastAudioError as e:
            if 'تحرير' in str(e) or 'بداية' in str(e):raise
        self.fragmented=False
        stsz=child(stbl,b'stsz');fixed=u32(stsz,4);count=u32(stsz,8)
        if count==0 and len(stsz)==12:
            self._fragment_index(moov,track);self.fragmented=True;return
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
        self.duration=(self.times[-1]-self.media_start)/self.scale
    def _fragment_index(self,moov,track):
        tkhd=child(track,b'tkhd');self.track_id=u32(tkhd,12 if tkhd[0]==0 else 20)
        self.defaults=None
        for kind,data in boxes(child(moov,b'mvex')):
            if kind==b'trex' and len(data)==24 and u32(data,4)==self.track_id:
                if u32(data,8)!=1:raise FastAudioError('وصف مسار مجزأ غير مدعوم.')
                self.defaults=(u32(data,12),u32(data,16))
        if self.defaults is None:raise FastAudioError('لا توجد إعدادات الصوت المجزأ.')
        pos=0;self.fragments=[]
        for _ in range(64):
            if pos+8>self.source.length:break
            size,kind=struct.unpack('>I4s',self.source.read(pos,8));header=8
            if size==1:size=struct.unpack('>Q',self.source.read(pos+8,8))[0];header=16
            if size<header or pos+size>self.source.length:raise FastAudioError('فهرس مجزأ غير صالح.')
            if kind==b'sidx':
                if size>MAX_METADATA:raise FastAudioError('فهرس مجزأ كبير.')
                data=self.source.read(pos+header,size-header)
                if len(data)<24:raise FastAudioError('فهرس مجزأ غير مكتمل.')
                version=data[0]
                if version==1 and len(data)<32:raise FastAudioError('فهرس مجزأ غير مكتمل.')
                if version not in (0,1) or u32(data,4)!=self.track_id or u32(data,8)!=self.scale:raise FastAudioError('فهرس مجزأ غير مدعوم.')
                width=8 if version==1 else 4;p=12
                timestamp=int.from_bytes(data[p:p+width],'big');p+=width
                offset=pos+size+int.from_bytes(data[p:p+width],'big');p+=width
                if p+4>len(data):raise FastAudioError('فهرس مجزأ غير مكتمل.')
                count=struct.unpack_from('>H',data,p+2)[0];p+=4
                if not 1<=count<=4096 or len(data)!=p+12*count:raise FastAudioError('عدد أجزاء غير صالح.')
                for _ in range(count):
                    length,duration,_sap=struct.unpack_from('>III',data,p);p+=12
                    if length>>31 or not 8<=length<=MAX_METADATA or not duration or offset+length>self.source.length:raise FastAudioError('عنوان جزء غير صالح.')
                    self.fragments.append((offset,length,timestamp,timestamp+duration));offset+=length;timestamp+=duration
                self.duration=(timestamp-self.media_start)/self.scale
                if not 3<=self.duration<=1800:raise FastAudioError('مدة الصوت المجزأ غير مدعومة.')
                return
            pos+=size
        raise FastAudioError('الصوت المجزأ يحتاج فهرسًا مباشرًا للفترات.')
    def _fragment_samples(self,entry,data=None):
        offset,length,_,_=entry
        if data is None:data=self.source.read(offset,length)
        records=list(located_boxes(data));media=[(p+h,p+h+len(b)) for p,k,b,h in records if k==b'mdat'];samples=[]
        for moof_pos,kind,moof,_header in records:
            if kind!=b'moof':continue
            trafs=[b for k,b in boxes(moof) if k==b'traf']
            for traf in trafs:
                tfhd=child(traf,b'tfhd')
                if u32(tfhd,4)!=self.track_id:continue
                flags=u32(tfhd,0)&0xffffff;p=8;base=moof_pos
                if flags&1:
                    if p+8>len(tfhd):raise FastAudioError('عنوان جزء غير مكتمل.')
                    base=int.from_bytes(tfhd[p:p+8],'big')-offset;p+=8
                elif not flags&0x020000 and len(trafs)!=1:raise FastAudioError('عنونة الأجزاء غير مدعومة.')
                if flags&2:
                    if u32(tfhd,p)!=1:raise FastAudioError('وصف جزء غير مدعوم.')
                    p+=4
                duration,size=self.defaults
                if flags&8:duration=u32(tfhd,p);p+=4
                if flags&16:size=u32(tfhd,p);p+=4
                if flags&32:p+=4
                if p!=len(tfhd) or flags&0x010000:raise FastAudioError('إعداد جزء غير صالح.')
                tfdt=child(traf,b'tfdt')
                if not tfdt or tfdt[0] not in (0,1) or len(tfdt)!=(12 if tfdt[0] else 8):raise FastAudioError('توقيت جزء غير صالح.')
                timestamp=int.from_bytes(tfdt[4:],'big');cursor=None
                for k,trun in boxes(traf):
                    if k!=b'trun':continue
                    flags=u32(trun,0)&0xffffff;count=u32(trun,4);p=8
                    if not 1<=count<=200000:raise FastAudioError('عينات جزء غير صالحة.')
                    if flags&1:
                        value=u32(trun,p);cursor=base+(value if value<2**31 else value-2**32);p+=4
                    if flags&4:p+=4
                    if cursor is None:raise FastAudioError('عنوان عينات الجزء مفقود.')
                    for _ in range(count):
                        delta=duration;n=size
                        if flags&0x100:delta=u32(trun,p);p+=4
                        if flags&0x200:n=u32(trun,p);p+=4
                        if flags&0x400:p+=4
                        if flags&0x800:
                            if u32(trun,p)!=0:raise FastAudioError('إزاحة صوت مجزأ غير مدعومة.')
                            p+=4
                        if not 0<delta<=1024 or not 0<n<=8184 or not any(lo<=cursor and cursor+n<=hi for lo,hi in media):raise FastAudioError('عينة جزء خارج البيانات.')
                        samples.append((timestamp,delta,data[cursor:cursor+n]));timestamp+=delta;cursor+=n
                    if p!=len(trun):raise FastAudioError('جدول جزء غير صالح.')
        if not samples:raise FastAudioError('لا توجد عينات في الجزء.')
        return samples
    def _fragment_cut(self,start,end,target):
        wanted=self.media_start+start*self.scale;finish=self.media_start+end*self.scale
        entries=[e for e in self.fragments if e[3]>max(0,wanted-2048) and e[2]<finish+1024]
        if not entries:raise FastAudioError('الفترة خارج الأجزاء المتاحة.')
        first=None;previous=None;last=0
        groups=[]
        for entry in entries:
            if groups and groups[-1][-1][0]+groups[-1][-1][1]==entry[0] and entry[0]+entry[1]-groups[-1][0][0]<=MAX_METADATA:groups[-1].append(entry)
            else:groups.append([entry])
        with open(target,'wb') as out:
            for group in groups:
                base=group[0][0];data=self.source.read(base,group[-1][0]+group[-1][1]-base)
                for entry in group:
                    part=data[entry[0]-base:entry[0]-base+entry[1]]
                    for timestamp,delta,sample in self._fragment_samples(entry,part):
                        if timestamp+delta<=max(0,wanted-2048) or timestamp>=finish+1024:continue
                        if previous is not None and timestamp!=previous:raise FastAudioError('انقطاع في توقيت أجزاء الصوت.')
                        if first is None:first=timestamp
                        adts(out,sample,self.rate_index,self.channels);previous=timestamp+delta;last=previous
        if first is None or first>wanted or last<finish:raise FastAudioError('أجزاء الفترة غير مكتملة.')
        return (wanted-first)/self.scale,end-start
    def cut(self,start,end,target):
        if not all(map(math.isfinite,(start,end))) or start<0 or not 3<=end-start<=180:raise FastAudioError('فترة غير صالحة.')
        end=min(end,self.duration)
        if end-start<3:raise FastAudioError('الفترة خارج المصدر.')
        if self.fragmented:return self._fragment_cut(start,end,target)
        first=max(0,bisect.bisect_right(self.times,self.media_start+start*self.scale)-3)
        last=min(len(self.sizes),bisect.bisect_left(self.times,self.media_start+end*self.scale)+1)
        # Coalesce consecutive samples into bounded reads. Never read the full mdat.
        with open(target,'wb') as out:
            i=first
            while i<last:
                j=i+1;size=self.sizes[i]
                while j<last and self.offsets[j]==self.offsets[j-1]+self.sizes[j-1] and size+self.sizes[j]<=min(8*CHUNK,MAX_METADATA):
                    size+=self.sizes[j];j+=1
                data=self.source.read(self.offsets[i],size);p=0
                for k in range(i,j):
                    n=self.sizes[k]+7;ch=self.channels;fi=self.rate_index
                    out.write(bytes([255,241,(1<<6)|(fi<<2)|(ch>>2),((ch&3)<<6)|(n>>11),(n>>3)&255,((n&7)<<5)|31,252]));out.write(data[p:p+self.sizes[k]]);p+=self.sizes[k]
                i=j
        offset=(self.media_start+start*self.scale-self.times[first])/self.scale
        return offset,end-start

class Resolver:
    cache_seconds=300
    def __init__(self):self.lock=threading.Lock();self.cache=OrderedDict();self.jobs=[threading.Lock() for _ in range(16)]
    def resolve(self,video):
        with self.jobs[hash(video)%len(self.jobs)]:return self._resolve(video)
    def _resolve(self,video):
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}',video):raise FastAudioError('رابط فيديو غير صالح.')
        with self.lock:
            item=self.cache.get(video)
            if item and item[0]>time.monotonic():self.cache.move_to_end(video);return item[1],True
        result=self._uncached(video)
        ttl=min(self.cache_seconds,result.get('expires',time.time()+self.cache_seconds+30)-time.time()-30)
        if ttl<=0:raise FastAudioError('انتهت صلاحية مصدر الصوت.')
        with self.lock:
            self.cache[video]=(time.monotonic()+ttl,result)
            while len(self.cache)>64:self.cache.popitem(last=False)
        return result,False
    def _uncached(self,video):
        args=[sys.executable,'-m','yt_dlp','--no-playlist','--skip-download','--extractor-args','youtube:player_client=web;player_skip=initial_data','--dump-single-json','--socket-timeout','5','--retries','0','--extractor-retries','0','--js-runtimes','node','-f','bestaudio[ext=m4a][acodec=mp4a.40.2][protocol=https]','--','https://www.youtube.com/watch?v='+video]
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
            diagnostic=re.sub(r'https?://[^\s]+','[URL]',stderr.decode('utf-8',errors='replace'))
            diagnostic=re.sub(r'(?i)(authorization|cookie|token|password|secret)[^\n]*','[REDACTED]',diagnostic)
            print('Direct resolver diagnostic: '+diagnostic[-6000:],file=sys.stderr,flush=True)
            raise FastAudioError(reason)
        if len(out)>4*1024*1024:raise FastAudioError('بيانات مصدر كبيرة.')
        try:
            j=json.loads(out);duration=j.get('duration');size=j.get('filesize');url=media_url(j['url'])
            if j.get('id')!=video or j.get('is_live') or not isinstance(duration,(int,float)) or not math.isfinite(duration) or not 3<=duration<=1800 or not isinstance(size,int) or not 0<size<=MAX_SOURCE:raise ValueError()
            result={'url':url,'length':size,'duration':duration,'headers':j.get('http_headers',{})}
        except (KeyError,TypeError,ValueError):raise FastAudioError('بيانات مصدر غير مدعومة.') from None
        return result

class TunelioResolver(Resolver):
    """One paid/trial resolution, then bounded native AAC reads. No conversion jobs.
    Check the free balance endpoint before spending; preserve an operator reserve.
    The API credential is used only on /credits and /create, never on media reads.
    """
    cache_seconds=6*60*60
    def __init__(self,key=None,credit_reserve=None):
        super().__init__();self.key=key if key is not None else os.environ.get('TUNELIO_API_KEY','')
        self.credit_reserve=os.environ.get('TUNELIO_CREDIT_RESERVE','20') if credit_reserve is None else credit_reserve
        self.spend_lock=threading.Lock();self.opener=build_opener(NoRedirect())
    def _request_json(self,path,query=''):
        request=Request('https://tunelio.dev/'+path+('?' + query if query else ''),headers={'Authorization':'Bearer '+self.key,'Accept':'application/json','User-Agent':'MaqamYouTube/1.0'})
        try:
            with self.opener.open(request,timeout=8) as response:
                if response.status!=200:raise FastAudioError('خدمة الصوت المباشر لم تكمل الطلب.')
                raw=response.read(65537)
        except HTTPError as error:
            print('Tunelio '+path+' HTTP status='+str(error.code),file=sys.stderr,flush=True)
            messages={401:'مفتاح خدمة الصوت المباشر غير مقبول.',402:'انتهى رصيد خدمة الصوت المباشر.',404:'الفيديو غير متاح لخدمة الصوت.',429:'خدمة الصوت وصلت حد الطلبات؛ جرّب لاحقًا.'}
            error.close()
            raise FastAudioError(messages.get(error.code,'خدمة الصوت المباشر غير متاحة الآن.')) from None
        except (URLError,TimeoutError):raise FastAudioError('انتهت مهلة اتصال خدمة الصوت المباشر.') from None
        try:
            if len(raw)>65536:raise ValueError()
            data=json.loads(raw)
            if not isinstance(data,dict):raise ValueError()
        except (ValueError,TypeError):raise FastAudioError('بيانات خدمة الصوت المباشر غير صالحة.') from None
        return data
    def _uncached(self,video):
        if not self.key or len(self.key)>512 or not self.key.isascii() or any(ord(c)<=32 or ord(c)==127 for c in self.key):raise FastAudioError('مفتاح خدمة الصوت المباشر غير مضبوط على الخادم.')
        reserve=self.credit_reserve
        if isinstance(reserve,str) and re.fullmatch(r'[0-9]{1,10}',reserve):reserve=int(reserve)
        if type(reserve) is not int or not 0<=reserve<=1000000000:raise FastAudioError('حد حماية رصيد خدمة الصوت غير مضبوط.')
        # Serialize balance+spend across videos in this process. A fresh account
        # balance is checked after every restart, rather than resetting a counter.
        with self.spend_lock:
            balance=self._request_json('credits').get('credits')
            if type(balance) is not int or balance<0:raise FastAudioError('تعذّر التحقق من الرصيد؛ أوقفنا الطلب لحمايته.')
            if balance-10<reserve:raise FastAudioError('توقف الجلب المباشر لحماية رصيد الخدمة؛ جرّب المصدر البديل.')
            query=urlencode({'url':'https://www.youtube.com/watch?v='+video,'quality':'m4a'})
            data=self._request_json('create',query)
        try:
            size=data['file_size'];expires=data['expires'];url=media_url(data['url'])
            if data.get('status')!='ok' or data.get('quality')!='m4a' or data.get('mode')!='audio' or type(size) is not int or not 0<size<=MAX_SOURCE or type(expires) is not int or expires<=time.time()+30:raise ValueError()
            if urlsplit(url).hostname!='tunelio.dev':raise ValueError()
        except (ValueError,TypeError,KeyError):raise FastAudioError('بيانات خدمة الصوت المباشر غير صالحة.') from None
        print('Tunelio create validated: native AAC source ready',file=sys.stderr,flush=True)
        return {'url':url,'length':size,'headers':{'User-Agent':'MaqamYouTube/1.0','Accept':'*/*'},'expires':expires}

def configured_resolver():
    name=os.environ.get('MAQAM_AUDIO_RESOLVER','yt-dlp')
    if name=='tunelio':return TunelioResolver()
    if name=='yt-dlp':return Resolver()
    raise FastAudioError('إعداد مزوّد الصوت غير مدعوم.')

class Service:
    def __init__(self,resolver=None):self.resolver=resolver if resolver is not None else configured_resolver();self.lock=threading.Lock();self.cache=OrderedDict();self.bytes=0;self.jobs=[threading.Lock() for _ in range(16)]
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
            if saved and saved[0]>started:self.cache.move_to_end(key);return saved[1],{'cache':'hit','totalMs':round((time.monotonic()-started)*1000),'sourceBytes':0,'clipSeconds':saved[2],'contentType':'audio/mp4'}
        meta,resolver_hit=self.resolver.resolve(video);resolved=time.monotonic();remote=Remote(meta['url'],meta['length'],meta['headers']);index=M4A(remote)
        if not 3<=index.duration<=1800:raise FastAudioError('اختر فيديو مسجلًا حتى ٣٠ دقيقة.')
        with tempfile.TemporaryDirectory(prefix='maqam-fast-') as folder:
            source=Path(folder)/'clip.aac';target=Path(folder)/'clip.m4a';offset,duration=index.cut(start,end,source);fetched=time.monotonic()
            args=['ffmpeg','-nostdin','-v','error','-protocol_whitelist','file,pipe','-ss',str(offset),'-i',str(source),'-t',str(duration),'-vn','-c:a','copy','-movflags','+faststart',str(target)]
            try:p=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=8)
            except subprocess.TimeoutExpired:raise FastAudioError('انتهت مهلة تجهيز الفترة الصوتية.') from None
            if p.returncode or not target.is_file():raise FastAudioError('تعذّر تجهيز الفترة الصوتية.')
            body=target.read_bytes()
            class Memory:
                length=len(body)
                def read(self,offset,size):return body[offset:offset+size]
            if abs(M4A(Memory()).duration-duration)>.08:raise FastAudioError('الفترة الصوتية غير مكتملة.')
        with self.lock:
            now=time.monotonic()
            for k in list(self.cache):
                if self.cache[k][0]<=now:self.bytes-=len(self.cache.pop(k)[1])
            while self.cache and (self.bytes+len(body)>32*1024*1024 or len(self.cache)>=32):self.bytes-=len(self.cache.popitem(last=False)[1][1])
            if key in self.cache:self.bytes-=len(self.cache.pop(key)[1])
            self.cache[key]=(now+600,body,duration);self.bytes+=len(body)
        return body,{'cache':'miss','resolverCache':resolver_hit,'resolveMs':round((resolved-started)*1000),'rangeMs':round((fetched-resolved)*1000),'muxMs':round((time.monotonic()-fetched)*1000),'totalMs':round((time.monotonic()-started)*1000),'sourceBytes':remote.bytes,'clipSeconds':duration,'contentType':'audio/mp4'}
