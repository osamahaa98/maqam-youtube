import concurrent.futures
from contextlib import redirect_stderr
import io
import json
import math
import re
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
import wave
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from fast_audio import M4A,Remote,Service,FastAudioError,media_url,CHUNK,TunelioResolver,configured_resolver,Resolver

class FileSource:
    def __init__(self,data):self.data=data;self.length=len(data);self.bytes=0;self.reads=[]
    def read(self,offset,size):
        self.bytes+=size;self.reads.append((offset,size));return self.data[offset:offset+size]

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.folder=Path(cls.tmp.name)
        cls.fast=cls.folder/'fast.m4a';cls.tail=cls.folder/'tail.m4a';cls.fragmented=cls.folder/'fragmented.m4a'
        command=['ffmpeg','-v','error','-f','lavfi','-i','aevalsrc=if(lt(t\\,590)\\,0.3*sin(2*PI*220*t)\\,0.3*sin(2*PI*440*t)):s=44100:d=600','-c:a','aac','-b:a','128k','-movflags','+faststart',str(cls.fast)]
        subprocess.run(command,check=True,timeout=30)
        subprocess.run(['ffmpeg','-v','error','-i',str(cls.fast),'-t','10','-c:a','copy',str(cls.tail)],check=True,timeout=10)
        subprocess.run(['ffmpeg','-v','error','-i',str(cls.fast),'-c:a','copy','-movflags','+dash+global_sidx','-frag_duration','5000000',str(cls.fragmented)],check=True,timeout=10)
        cls.data=cls.fast.read_bytes()
    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()
    def test_late_clip_reads_only_selected_samples_and_correct_time(self):
        source=FileSource(self.data);m=M4A(source);self.assertAlmostEqual(m.duration,600,delta=.06)
        target=self.folder/'late.aac';offset,duration=m.cut(589,592,target)
        pcm=subprocess.check_output(['ffmpeg','-v','error','-ss',str(offset),'-i',str(target),'-t',str(duration),'-ar','22050','-ac','1','-f','f32le','-'],timeout=8)
        self.assertAlmostEqual(len(pcm)/4/22050,3,delta=.04)
        values=struct.unpack('<'+'f'*(len(pcm)//4),pcm)
        for start,end,hz in ((.2,.8,220),(1.2,1.8,440)):
            lo=int(start*22050);hi=int(end*22050);crossings=sum(values[i-1]<=0<values[i] for i in range(lo,hi))
            self.assertAlmostEqual(crossings/(end-start),hz,delta=4)
        self.assertLess(source.bytes,len(self.data)*.03)
        print('Local 10-minute AAC fixture: source=%d bytes, late3s read=%d bytes; timing/pitch checked.'%(len(self.data),source.bytes),flush=True)
    def test_moov_at_end_skips_media_and_rejects_bad_tables(self):
        source=FileSource(self.tail.read_bytes());m=M4A(source);self.assertAlmostEqual(m.duration,10,delta=.1);self.assertLess(source.bytes,source.length/4)
        with self.assertRaises(FastAudioError):M4A(FileSource(b'not m4a'))
        for url in ('http://x.googlevideo.com/audio','https://googlevideo.com.evil.test/audio','https://u:p@x.googlevideo.com/audio','https://x.googlevideo.com:444/audio','https://localhost/audio'):
            with self.assertRaises(FastAudioError):media_url(url)
    def test_fragmented_aac_late_fractional_clip_matches_full_decode(self):
        source=FileSource(self.fragmented.read_bytes());m=M4A(source)
        self.assertTrue(m.fragmented);self.assertAlmostEqual(m.duration,600,delta=.06)
        target=self.folder/'fragment-cut.aac';offset,duration=m.cut(589.125,592.625,target)
        remux=self.folder/'fractional.m4a'
        subprocess.run(['ffmpeg','-v','error','-ss',str(offset),'-i',str(target),'-t',str(duration),'-c:a','copy','-movflags','+faststart',str(remux)],check=True)
        command=['ffmpeg','-v','error','-ss','0','-i',str(remux),'-t',str(duration),'-ar','44100','-ac','1','-f','f32le','-']
        pcm=subprocess.check_output(command,timeout=8)
        ref=subprocess.check_output(['ffmpeg','-v','error','-ss','589.125','-i',str(self.fragmented),'-t','3.5','-ar','44100','-ac','1','-f','f32le','-'],timeout=8)
        self.assertAlmostEqual(len(pcm)/4/44100,3.5,delta=.04)
        a=struct.unpack('<'+'f'*(len(pcm)//4),pcm);b=struct.unpack('<'+'f'*(len(ref)//4),ref)
        lo=int(.15*44100);hi=min(len(a),len(b))-int(.1*44100)
        # FFmpeg quantizes -ss to microseconds: a fractional sample can round
        # differently for a long source versus a short ADTS preroll. Allow 1
        # native sample (23 microseconds), then require matching waveforms.
        denominator=sum(b[i]**2 for i in range(lo,hi))
        normalized_error=min(sum((a[i+lag]-b[i])**2 for i in range(lo,hi))/denominator for lag in (-1,0,1))
        self.assertLess(normalized_error,.000001)
        self.assertLess(source.bytes,source.length*.05)
        print('Fragmented AAC late fractional clip: source=%d, read=%d bytes, normalized waveform error=%.8f'%(source.length,source.bytes,normalized_error),flush=True)
    def test_fragment_index_rejects_hierarchy_and_invalid_sample_offsets(self):
        data=bytearray(self.fragmented.read_bytes());i=data.index(b'sidx')+4
        version=data[i];p=i+12+(16 if version else 8)+4
        bad=bytearray(data);struct.pack_into('>I',bad,p,0x80000010)
        with self.assertRaises(FastAudioError):M4A(FileSource(bytes(bad)))
        bad=bytearray(data);bad[i-4:i]=b'free'
        with self.assertRaises(FastAudioError):M4A(FileSource(bytes(bad)))
        bad=bytearray(data);p=bad.index(b'trun')+4;flags=struct.unpack_from('>I',bad,p)[0]&0xffffff
        self.assertTrue(flags&1);struct.pack_into('>I',bad,p+8,0x7fffffff)
        m=M4A(FileSource(bytes(bad)))
        with self.assertRaises(FastAudioError):m.cut(0,3,self.folder/'invalid.aac')
    def test_real_http_bounded_parallel_ranges_and_ignored_range_rejection(self):
        data=self.data;requests=[];auth=[]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                request=self.headers.get('Range','');requests.append(request);auth.append(self.headers.get('Authorization'));a,b=map(int,re.fullmatch(r'bytes=(\d+)-(\d+)',request).groups());body=data[a:b+1]
                if self.path=='/ignored':self.send_response(200)
                else:self.send_response(206)
                self.send_header('Content-Range',f'bytes {a}-{b}/{len(data)}');self.send_header('Content-Length',str(len(body)));self.end_headers()
                try:self.wfile.write(body)
                except (BrokenPipeError,ConnectionResetError):pass
            def log_message(self,*_):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        class Opener:
            path='/audio'
            def open(self,request,timeout):return urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{server.server_port}'+self.path,headers=dict(request.header_items())),timeout=timeout)
        try:
            for url in ('https://test.googlevideo.com/audio','https://tunelio.dev/tunnel?sig=test'):
                requests.clear();auth.clear()
                source=Remote(url,len(data),{'Authorization':'Bearer tnl_test_only'});source.opener=Opener();n=CHUNK*2+100
                self.assertEqual(source.read(7,n),data[7:7+n]);self.assertEqual(source.bytes,n);self.assertEqual(len(requests),3);self.assertEqual(auth,[None]*3)
            requests.clear()
            metadata=Remote('https://tunelio.dev/tunnel?sig=test',len(data));metadata.opener=Opener()
            self.assertEqual(metadata.read(0,8),data[:8]);self.assertEqual(metadata.read(28,8),data[28:36]);self.assertEqual(metadata.read(100,2000),data[100:2100])
            self.assertEqual(len(requests),1);self.assertEqual(metadata.bytes,65536)
            source.opener.path='/ignored'
            with self.assertRaises(FastAudioError):source.read(0,20)
        finally:server.shutdown();server.server_close();thread.join()
    def test_service_cache_and_concurrent_dedup(self):
        sources=[];data=self.data
        class Resolver:
            calls=0
            def resolve(self,video):self.calls+=1;return {'url':'https://test.googlevideo.com/audio','length':len(data),'duration':600,'headers':{}},False
        def factory(*_):s=FileSource(data);sources.append(s);return s
        service=Service();service.resolver=Resolver()
        with patch('fast_audio.Remote',side_effect=factory):
            started=time.monotonic()
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda _:service.clip('abcdefghijk',589,592),range(2)))
            self.assertEqual(service.resolver.calls,1);self.assertEqual(len(sources),1)
            self.assertCountEqual([r[1]['cache'] for r in results],['miss','hit']);self.assertEqual(results[0][0],results[1][0])
            self.assertEqual(results[0][1]['contentType'],'audio/mp4');self.assertEqual(results[1][1]['clipSeconds'],3)
            file=self.folder/'service.m4a';file.write_bytes(results[0][0])
            pcm=subprocess.check_output(['ffmpeg','-v','error','-i',str(file),'-t','3','-ar','22050','-ac','1','-f','s16le','-'])
            self.assertAlmostEqual(len(pcm)/2/22050,3,delta=.04)
            print('Local clip service pair (AAC remux, no re-encoding): %.3fs; cache hit reads0source bytes.'%(time.monotonic()-started),flush=True)
    def test_http_opt_in_and_timings(self):
        import server as module
        server=ThreadingHTTPServer(('127.0.0.1',0),module.Handler);t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
        url=f'http://127.0.0.1:{server.server_port}/clip';req=urllib.request.Request(url,b'{"url":"https://youtu.be/abcdefghijk","start":0,"end":3}',{'Content-Type':'application/json','Origin':module.ORIGIN})
        try:
            with patch('server.DIRECT_ENABLED',False):
                with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(req)
                self.assertEqual(caught.exception.code,503)
            with patch('server.DIRECT_ENABLED',True),patch('server.DIRECT_SERVICE.clip',return_value=(b'RIFF-test',{'cache':'miss','sourceBytes':123,'totalMs':10,'clipSeconds':3})):
                with urllib.request.urlopen(req) as r:self.assertEqual(r.read(),b'RIFF-test');self.assertEqual(r.headers['X-Maqam-Source-Bytes'],'123');self.assertEqual(r.headers['X-Maqam-Clip-Seconds'],'3');self.assertIn('total;dur=10',r.headers['Server-Timing'])
        finally:server.shutdown();server.server_close();t.join()
class TunelioTests(unittest.TestCase):
    def test_range_network_errors_are_actionable_without_signed_url_leaks(self):
        for error in (urllib.error.HTTPError('https://tunelio.dev/tunnel?sig=private',403,'private',{},io.BytesIO(b'private')),TimeoutError('private')):
            source=Remote('https://tunelio.dev/tunnel?sig=private',1234)
            log=io.StringIO()
            with patch.object(source.opener,'open',side_effect=error),redirect_stderr(log):
                with self.assertRaises(FastAudioError) as caught:source.read(0,8)
            self.assertNotIn('private',log.getvalue());self.assertNotIn('private',str(caught.exception))
            self.assertEqual(source.bytes,0)
    def resolver(self,change=None,body=None):
        data={'status':'ok','quality':'m4a','mode':'audio','file_size':123456,'expires':int(time.time())+3600,'url':'https://tunelio.dev/tunnel?exp=123&sig=test'}
        data.update(change or {})
        raw=body if body is not None else json.dumps(data).encode()
        class Response(io.BytesIO):status=200
        class Opener:
            calls=[]
            def open(self,request,timeout):
                self.calls.append((request,timeout));return Response(raw)
        r=TunelioResolver('tnl_test_only');r.opener=Opener();r.opener.calls=[]
        return r
    def test_one_m4a_resolution_concurrent_cache_and_secret_scope(self):
        r=self.resolver()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:r.resolve('abcdefghijk'),range(2)))
        self.assertEqual(len(r.opener.calls),1)
        req,timeout=r.opener.calls[0]
        query=urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)
        self.assertEqual(query['quality'],['m4a']);self.assertEqual(timeout,8)
        self.assertEqual(req.get_header('Authorization'),'Bearer tnl_test_only')
        self.assertNotIn('tnl_test_only',req.full_url)
        self.assertCountEqual([hit for _,hit in results],[False,True])
        self.assertEqual(results[0][0]['headers'],{'User-Agent':'MaqamYouTube/1.0','Accept':'*/*'})
        self.assertNotIn('tnl_test_only',json.dumps(results))
    def test_tunnel_allowlist_rejects_credentials_redirect_hosts_and_nonmedia_paths(self):
        self.assertTrue(media_url('https://tunelio.dev/tunnel?sig=example'))
        for url in ('https://tunelio.dev.evil.test/tunnel?sig=x','http://tunelio.dev/tunnel?sig=x','https://user:pass@tunelio.dev/tunnel?sig=x','https://tunelio.dev/create?url=x','https://tunelio.dev/tunnel','https://tunelio.dev:444/tunnel?sig=x','https://tunelio.dev/tunnel?sig=x#fragment',None):
            with self.subTest(url=url),self.assertRaises(FastAudioError):media_url(url)
    def test_reject_bad_contract_or_missing_key_without_fallback(self):
        for change in ({'quality':'mp3'},{'mode':'video'},{'status':'failed'},{'expires':1},{'file_size':True},{'file_size':50*1024*1024},{'url':'https://evil.test/tunnel?sig=x'},{'url':'https://x.googlevideo.com/audio'}):
            r=self.resolver(change)
            with self.subTest(change=change),self.assertRaises(FastAudioError):r.resolve('abcdefghijk')
        for raw in (b'[]',b'not-json',b' '*65537):
            with self.subTest(raw=raw[:10]),self.assertRaises(FastAudioError):self.resolver(body=raw).resolve('abcdefghijk')
        for key in ('','key\r\nInjected: value'):
            r=self.resolver();r.key=key
            with self.assertRaises(FastAudioError):r.resolve('abcdefghijk')
            self.assertEqual(len(r.opener.calls),0)
    def test_http_errors_never_expose_vendor_body_credentials_or_signed_urls(self):
        for status in (401,402,404,429,500):
            r=self.resolver()
            with patch.object(r.opener,'open',side_effect=urllib.error.HTTPError('https://tunelio.dev/create',status,'secret-detail',{},io.BytesIO(b'tnl_test_only'))):
                with self.assertRaises(FastAudioError) as caught:r.resolve('abcdefghijk')
            self.assertNotIn('tnl_test_only',str(caught.exception));self.assertNotIn('secret-detail',str(caught.exception))
            self.assertEqual(len(r.cache),0)
    def test_short_link_expiry_controls_cache_ttl(self):
        r=self.resolver({'expires':int(time.time())+60})
        r.resolve('abcdefghijk')
        self.assertLessEqual(r.cache['abcdefghijk'][0]-time.monotonic(),30)
        with patch('fast_audio.time.monotonic',return_value=time.monotonic()+31):r.resolve('abcdefghijk')
        self.assertEqual(len(r.opener.calls),2)
    def test_explicit_provider_configuration(self):
        with patch.dict('os.environ',{'MAQAM_AUDIO_RESOLVER':'tunelio','TUNELIO_API_KEY':'tnl_test_only'}):
            self.assertIsInstance(configured_resolver(),TunelioResolver)
            self.assertIsInstance(Service().resolver,TunelioResolver)
        with patch.dict('os.environ',{'MAQAM_AUDIO_RESOLVER':'yt-dlp'}):self.assertIsInstance(configured_resolver(),Resolver)
        with patch.dict('os.environ',{'MAQAM_AUDIO_RESOLVER':'unknown'}):
            with self.assertRaises(FastAudioError):configured_resolver()

if __name__=='__main__':unittest.main()
