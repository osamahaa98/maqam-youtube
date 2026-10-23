"""Run ON THE TARGET HOST. This times source import only, not browser analysis.
Exit nonzero on a failed source or missed latency gate; never count failure as fast.
"""
import argparse,json,time,math,os
from pathlib import Path
from fast_audio import Service

def percentile(values,p):return sorted(values)[max(0,math.ceil(len(values)*p)-1)] if values else None

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--manifest',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--seconds',type=float,default=30);parser.add_argument('--cold-p95-seconds',type=float,default=5);parser.add_argument('--credit-budget',type=int,default=0,help='Owner-approved live test budget, in credits; no automatic paid probes.');args=parser.parse_args()
    records=json.loads(args.manifest.read_text())['records']
    if os.environ.get('MAQAM_AUDIO_RESOLVER')=='tunelio' and args.credit_budget<10*len(records):parser.error('No requests sent: set an explicit credit budget covering up to 10 credits per source.')
    service=Service();results=[]
    for record in records:
        video=record['video_id'];start=float(record.get('start',0));begin=time.monotonic()
        try:
            body,timings=service.clip(video,start,start+args.seconds)
            cold=time.monotonic()-begin
            begin=time.monotonic();_,warm=service.clip(video,start,start+args.seconds)
            result={'video_id':video,'ok':True,'cold_seconds':cold,'repeat_seconds':time.monotonic()-begin,'audio_bytes':len(body),'cold':timings,'repeat':warm}
        except Exception as e:result={'video_id':video,'ok':False,'elapsed_seconds':time.monotonic()-begin,'error':str(e)[:300]}
        results.append(result);print(json.dumps(result,ensure_ascii=False),flush=True)
    times=[r['cold_seconds'] for r in results if r['ok']]
    output={'scope':'source import on target host only; does not include browser/network/analysis','target':{'p95_seconds':args.cold_p95_seconds,'minimum_videos':10,'all_requests_must_succeed':True},'results':results,'summary':{'requested':len(results),'succeeded':len(times),'cold_p50':percentile(times,.5),'cold_p95':percentile(times,.95)},'gatePassed':len(results)>=10 and len(times)==len(results) and percentile(times,.95)<=args.cold_p95_seconds}
    args.output.write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
    raise SystemExit(0 if output['gatePassed'] else 1)
if __name__=='__main__':main()
