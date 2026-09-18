#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Actual-client schedules; independent software pixels; no device in self-test."""
import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('owned_process',HERE/'concurrent-process.py')
owned=importlib.util.module_from_spec(spec);spec.loader.exec_module(owned)
LINE=re.compile(r'^worker (\d+) frames=(\d+) MD5=([0-9a-f]{32})$',re.M)
HELD=re.compile(r'^retained (\d+) MD5=([0-9a-f]{32}) client_error=([01])$',re.M)


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def save(path,data):Path(path).write_text(json.dumps(data,indent=2)+'\n')


def run_group(commands,root,deadline=30,barrier=False,env=None):
    """Reserve group ownership through final signal before reap; no pipe EOF wait.

    Explicitly detached descendants are not owned groups. Negative fixtures use
    pidfds for their separate cleanup; real worker code does not spawn descendants.
    """
    if not 1<=len(commands)<=4 or not math.isfinite(deadline) or not 0<deadline<=120:
        raise ValueError('bounded command/deadline domain')
    root=Path(root);root.mkdir(parents=True,exist_ok=False)
    limit=time.monotonic()+deadline
    workers=[];stopped=[];reason=None;records=[]
    def stop(signum,_frame):stopped.append(signum)
    old={sig:signal.signal(sig,stop) for sig in (signal.SIGTERM,signal.SIGINT)}
    try:
        try:
            for i,cmd in enumerate(commands):
                if stopped or time.monotonic()>=limit:
                    reason='cancelled' if stopped else 'deadline';break
                output=(root/f'{i}.log').open('w+b')
                try:
                    proc=subprocess.Popen(cmd,stdin=subprocess.PIPE if barrier else subprocess.DEVNULL,
                                          stdout=output,stderr=subprocess.STDOUT,start_new_session=True,env=env)
                except (OSError,ValueError) as exc:
                    output.close();reason='launch: '+str(exc);break
                proc._group_owned=True
                workers.append((proc,output))
            for proc,_ in workers:
                if barrier:
                    try:proc.stdin.write(b'S');proc.stdin.close()
                    except (BrokenPipeError,OSError):pass
            while reason is None:
                if stopped:reason='cancelled';break
                if time.monotonic()>=limit:reason='deadline';break
                if any(os.fstat(out.fileno()).st_size>1024*1024 for _,out in workers):
                    reason='output-limit';break
                exits=[os.waitid(os.P_PID,p.pid,os.WEXITED|os.WNOHANG|os.WNOWAIT) for p,_ in workers]
                if any(e is not None and (e.si_code!=os.CLD_EXITED or e.si_status!=0) for e in exits):
                    reason='child-error';break
                if all(e is not None for e in exits):break
                time.sleep(.01)
        finally:
            for proc,_ in workers:owned.kill_process_group(proc)
            cleanup_end=time.monotonic()+5
            for i,(proc,out) in enumerate(workers):
                try:
                    code=proc.wait(timeout=max(.001,cleanup_end-time.monotonic()))
                    out.seek(0);data=out.read(1024*1024+1)
                    if len(data)>1024*1024:reason=reason or 'output-limit'
                    records.append(dict(returncode=code,text=data.decode(errors='replace'),log=f'{i}.log'))
                except subprocess.TimeoutExpired:
                    reason=reason or 'cleanup-timeout'
                finally:
                    if proc.stdin:
                        try:proc.stdin.close()
                        except (BrokenPipeError,OSError):pass
                    out.close()
    finally:
        for sig,handler in old.items():signal.signal(sig,handler)
    if stopped:reason=reason or 'cancelled'
    if len(records)!=len(commands):reason=reason or 'missing-child'
    if any(r['returncode'] for r in records):reason=reason or 'child-error'
    result=dict(ok=reason is None,reason=reason,signal=stopped[0] if stopped else None,records=records)
    save(root/'result.json',result)
    return result


def successful(cmd,root,deadline=60):
    result=run_group([list(map(str,cmd))],root,deadline)
    if not result['ok']:raise RuntimeError(str(result))
    return result['records'][0]['text']


def prepare(root):
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=False)
    clips=[]
    recipes=[('h264','libx264','yuv420p',['-preset','ultrafast','-bf','2']),
             ('hevc','libx265','yuv420p',['-preset','ultrafast','-x265-params','log-level=error:pools=2']),
             ('vp9-8','libvpx-vp9','yuv420p',['-cpu-used','8','-deadline','good','-lag-in-frames','0']),
             ('vp9-10','libvpx-vp9','yuv420p10le',['-cpu-used','8','-deadline','good','-lag-in-frames','0'])]
    for i,(name,codec,fmt,extra) in enumerate(recipes):
        width=128+i*32;height=96;count=8
        path=root/(name+'.mkv');raw=root/(name+'.yuv')
        cmd=['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',f'testsrc2=size={width}x{height}:rate=8,format={fmt}',
             '-frames:v',str(count),'-c:v',codec,*extra,'-g','4','-pix_fmt','+'+fmt,str(path)]
        if name=='vp9-10':
            # Older hosted libvpx cannot encode 10-bit. Remux eight frames from
            # the existing public, hash-pinned synthetic fixture without decoding.
            fixture=HERE/'fixtures/resource/vp9-10.webm'
            if sha(fixture)!='cfc1d27ad161696e574020b910cd952725cf67d12e0913dedec7d202aae84463':
                raise ValueError('VP9 10-bit fixture identity drift')
            width=640;height=360
            cmd=['ffmpeg','-nostdin','-v','error','-i',str(fixture),'-frames:v','8','-c','copy',str(path)]
        successful(cmd,root/(name+'-encode'))
        # Independent CLI decoding to packed raw pixels; never ask the worker for its own oracle.
        successful(['ffmpeg','-nostdin','-v','error','-i',path,'-map','0:v:0','-pix_fmt',fmt,'-f','rawvideo',raw],root/(name+'-reference'))
        data=raw.read_bytes();size=width*height*3//2*(2 if fmt.endswith('10le') else 1)
        if len(data)!=count*size:raise ValueError('software reference extent')
        clips.append(dict(name=name,path=str(path),sha256=sha(path),format=fmt,frames=count,width=width,height=height,
                          full=hashlib.md5(data).hexdigest(),prefix=hashlib.md5(data[:size*(count//2)]).hexdigest(),
                          held=hashlib.md5(data[size*(count//2-1):size*(count//2)]).hexdigest(),
                          oracle_raw_sha256=sha(raw),generation=cmd))
    manifest=dict(schema='libva-v4l2_request.concurrent-va-inputs/1',clips=clips,ffmpeg_sha256=sha(shutil.which('ffmpeg')))
    save(root/'manifest.json',manifest)
    return root/'manifest.json'


def validate(text,clips,schedule,seed):
    rows=[(int(i),int(n),h) for i,n,h in LINE.findall(text)]
    expected=[(i,c['frames']//2 if schedule!='normal' and i==0 else c['frames'],
               c['prefix'] if schedule!='normal' and i==0 else c['full']) for i,c in enumerate(clips)]
    if sorted(rows)!=expected or len(rows)!=len(expected):raise ValueError('exact worker count/identity/digest mismatch')
    heads=re.findall(r'^schedule (\w+) seed=(\d+) threads=(\d+)$',text,re.M)
    if heads!=[(schedule,str(seed),str(len(clips)))]:raise ValueError('schedule identity mismatch')
    held=HELD.findall(text)
    wanted=[] if schedule=='normal' else [('0',clips[0]['held'],'1' if schedule=='failure' else '0')]
    if held!=wanted:raise ValueError('retained-frame or expected-error evidence mismatch')
    if 'Sanitizer' in text or 'runtime error:' in text:raise ValueError('sanitizer diagnostic')
    return dict(workers=len(rows),frames=sum(n for _,n,_ in rows),retained=len(held))


def matrix(binary,manifest,out,mode='software',reps=1,deadline=30):
    if mode not in ('software','vaapi') or not 1<=reps<=10:raise ValueError('mode/repetition domain')
    if mode=='vaapi' and not os.environ.get('LIBVA_HW_GUARD_LEASE'):
        raise ValueError('explicit hardware matrix requires tests/hwguard.py')
    binary=Path(binary).resolve();manifest=Path(manifest).resolve();out=Path(out).resolve()
    data=json.loads(manifest.read_text());clips=data['clips']
    if data.get('schema')!='libva-v4l2_request.concurrent-va-inputs/1' or len(clips)!=4:
        raise ValueError('input manifest schema')
    for c,(name,width,height,fmt) in zip(clips,[('h264',128,96,'yuv420p'),('hevc',160,96,'yuv420p'),
                                                 ('vp9-8',192,96,'yuv420p'),('vp9-10',640,360,'yuv420p10le')]):
        if (c['name'],c['width'],c['height'],c['format'],c['frames'])!=(name,width,height,fmt,8):
            raise ValueError('input recipe domain')
        if sha(c['path'])!=c['sha256'] or any(not re.fullmatch('[0-9a-f]{32}',c[k]) for k in ('full','prefix','held')):
            raise ValueError('input identity/digest drift')
    driver=None
    if mode=='vaapi':
        directory=os.environ.get('LIBVA_DRIVERS_PATH','')
        if not directory or os.environ.get('LIBVA_DRIVER_NAME')!='v4l2_request':
            raise ValueError('explicit v4l2_request driver directory/name required')
        driver=dict(path=str(Path(directory).resolve()/'v4l2_request_drv_video.so'),
                    sha256=sha(Path(directory)/'v4l2_request_drv_video.so'))
    out.mkdir(parents=True,exist_ok=False)
    save(out/'identity.json',dict(worker_sha256=sha(binary),manifest_sha256=sha(manifest),mode=mode,
        guard_lease=os.environ.get('LIBVA_HW_GUARD_LEASE'),driver_directory=os.environ.get('LIBVA_DRIVERS_PATH'),
        driver=driver,source_commit=os.environ.get('V4L2R_SOURCE_COMMIT'),
        helper_sources={p.name:sha(p) for p in (Path(__file__),HERE/'concurrent-va-worker.c',HERE/'frame-check.c',HERE/'concurrent-process.py')},
        repetitions=reps,deadline_per_group=deadline))
    results=[]
    for rep in range(reps):
        for count in (1,2,4):
            chosen=[clips[(rep+i)%4] for i in range(count)]
            for schedule in ('normal','teardown','failure'):
                seed=549203187+rep*17+count
                args=['--schedule',schedule,'--seed',str(seed),'--frames','8','--wait-start']
                for style in ('threads','processes'):
                    groups=[chosen] if style=='threads' else [[c] for c in chosen]
                    commands=[[str(binary),mode,*args,'--threads',str(len(group)),
                               *[part for c in group for part in (c['path'],c['format'])]] for group in groups]
                    name=f'r{rep}-{style}-{count}-{schedule}'
                    result=run_group(commands,out/name,deadline,barrier=True)
                    if not result['ok']:raise RuntimeError(name+': '+str(result['reason']))
                    counts=[validate(record['text'],group,schedule,seed) for record,group in zip(result['records'],groups)]
                    row=dict(name=name,seed=seed,schedule=schedule,style=style,contexts=count,checks=counts)
                    results.append(row);save(out/'accepted.json',results)
    save(out/'complete.json',dict(groups=len(results),decoded_frames=sum(c['frames'] for r in results for c in r['checks']),
                                  mode=mode,hardware_executed=mode=='vaapi'))
    return results


def self_test(binary):
    with tempfile.TemporaryDirectory(prefix='concurrent-va-software-') as tmp:
        root=Path(tmp);manifest=prepare(root/'inputs')
        rows=matrix(binary,manifest,root/'matrix')
        # Explicitly remove any inherited lease: refusal happens before opening a device.
        env=os.environ.copy();env.pop('LIBVA_HW_GUARD_LEASE',None)
        clip=json.loads(manifest.read_text())['clips'][0]
        r=run_group([[str(binary),'vaapi',clip['path'],clip['format']]],root/'unguarded',5,env=env)
        if r['ok'] or r['records'][0]['returncode']!=2 or 'unguarded hardware run refused' not in r['records'][0]['text']:
            raise ValueError('missing pre-device guard refusal')
        print(f'PASS: {len(rows)} software thread/process schedules; independent mixed-codec oracle; retained frames and live EOF rejection; no device opened')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('worker',nargs='?',type=Path)
    p.add_argument('--self-test',action='store_true')
    p.add_argument('--prepare',type=Path)
    p.add_argument('--manifest',type=Path)
    p.add_argument('--output',type=Path)
    p.add_argument('--mode',choices=('software','vaapi'),default='software')
    p.add_argument('--repetitions',type=int,default=1)
    p.add_argument('--deadline',type=float,default=30)
    args=p.parse_args()
    if args.prepare:print(prepare(args.prepare));return
    if not args.worker:p.error('worker required')
    binary=args.worker.resolve()
    if args.self_test:self_test(binary);return
    if not args.manifest or not args.output:p.error('--manifest and --output required')
    matrix(binary,args.manifest,args.output,args.mode,args.repetitions,args.deadline)


if __name__=='__main__':main()
