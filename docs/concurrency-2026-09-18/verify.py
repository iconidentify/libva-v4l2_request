#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify archived pixels, every worker, guard and identities without runner code."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tarfile

HERE=Path(__file__).resolve().parent

def check(value,reason):
    if not value:raise ValueError(reason)

def digest(data):return hashlib.sha256(data).hexdigest()
def md5(data):return hashlib.md5(data).hexdigest()
def decode(files,name):return json.loads(files[name])

def load():
    index=json.loads((HERE/'archive-index.json').read_text());path=HERE/'evidence.tar.xz'
    check(digest(path.read_bytes())==index['archive_sha256'],'archive identity')
    files={}
    with tarfile.open(path,'r:xz') as archive:
        total=0
        for member in archive:
            name=member.name;total+=member.size
            check(member.isfile() and not PurePosixPath(name).is_absolute() and '..' not in PurePosixPath(name).parts,'unsafe member')
            check(name not in files and 0<=member.size<=8*1024*1024 and total<=32*1024*1024,'member bounds/duplicate')
            data=archive.extractfile(member).read()
            check(index['members'].get(name)==dict(size=len(data),sha256=digest(data)),'member identity '+name)
            files[name]=data
    check(set(files)==set(index['members']),'member inventory')
    return files,index

def verify(files,index):
    plan=json.loads((HERE/'identities.json').read_text())
    check(decode(files,'identities.json')==plan,'plan identity')
    manifest=decode(files,'inputs/manifest.json')
    check(manifest==json.loads((HERE/'inputs.json').read_text()),'pre-run input manifest')
    clips=manifest['clips'];check(len(clips)==4,'four codecs')
    expected=[]
    for c in clips:
        media=files['inputs/'+c['path']];raw=files['inputs/'+Path(c['path']).stem+'.yuv']
        size=c['width']*c['height']*3//2*(2 if c['format']=='yuv420p10le' else 1)
        check(c['frames']==8 and len(raw)==8*size,'reference extent')
        check(digest(media)==c['sha256'] and digest(raw)==c['oracle_raw_sha256'],'media/reference identity')
        hashes=[md5(raw[i*size:(i+1)*size]) for i in range(8)]
        check((md5(raw),md5(raw[:4*size]),hashes[3])==(c['full'],c['prefix'],c['held']),'independent raw-pixel oracle')
        expected.append(hashes)
    guard=[json.loads(line) for line in files['guard.jsonl'].splitlines()]
    check([g['event'] for g in guard]==['preflight','start','final'],'complete guard event sequence')
    lease=guard[0]['run_id'];check(all(g['run_id']==lease for g in guard),'guard lease identity')
    check(guard[0]['idle'] and guard[0]['module_loaded'] and guard[0]['userspace']['source_commit']==index['execution_head'],'guard preflight')
    check(all(guard[-1].get(k)==v for k,v in dict(status='ok',returncode=0,timed_out=False,wedged=False,abort_reason=None,idle=True,holders=[]).items()),'guard final health')
    control=decode(files,'controller.log')
    check(control==dict(status='ok',returncode=0,timed_out=False,wedged=False,abort_reason=None,busy=False,owner=None),'controller success')
    final=decode(files,'final-state.json')
    for label,snapshot in [('before',decode(files,'preflight.json')),('after',final)]:
        state=snapshot['state']
        check(state['module_loaded'] and state['video_node']=='/dev/video0' and state['media_node']=='/dev/media0','device presence '+label)
        check(not state['holders'] and not state['stuck_tasks'] and not state['faults'],'idle whole-boot health '+label)
        check(snapshot['boot_id']==plan['boot_id'],'boot identity '+label)
    check(final['module_sha256']==plan['original_module_sha256'] and final['loaded_note_hex']==plan['loaded_module_note_hex'],'unchanged original module')
    summary={}
    for mode in ('software','hardware'):
        identity=decode(files,mode+'/identity.json')
        check(identity['mode']==('vaapi' if mode=='hardware' else mode),'execution mode')
        check(identity['worker_sha256']==plan['worker_sha256'] and identity['manifest_sha256']==plan['input_manifest_sha256'],'executed binary/input identity')
        check(identity['source_commit']==plan['source_commit'],'compiled source identity')
        check(identity['helper_sources']=={Path(p).name:h for p,h in plan['helpers'].items() if Path(p).name!='hwguard.py'},'helper identities')
        check(identity['repetitions']==10 and identity['deadline_per_group']==30,'finite run domain')
        if mode=='hardware':
            check(identity['guard_lease']==lease and identity['driver']['sha256']==plan['driver_sha256'],'selected driver/lease')
        rows=[];groups=set();frames=held_count=processes=0
        for repetition in range(10):
            for count in (1,2,4):
                selected=[(repetition+i)%4 for i in range(count)]
                seed=549203187+repetition*17+count
                for schedule in ('normal','teardown','failure'):
                    for style in ('threads','processes'):
                        name=f'r{repetition}-{style}-{count}-{schedule}';groups.add(name)
                        prefix=mode+'/'+name;result=decode(files,prefix+'/result.json')
                        check(result['ok'] is True and result['reason'] is None and result['signal'] is None,'group success '+prefix)
                        grouping=[selected] if style=='threads' else [[c] for c in selected]
                        check(len(result['records'])==len(grouping),'process count '+prefix)
                        counts=[]
                        for pid,(record,group) in enumerate(zip(result['records'],grouping)):
                            check(record['returncode']==0 and record['log']==f'{pid}.log','worker completion '+prefix)
                            text=files[prefix+'/'+record['log']].decode();check(text==record['text'],'raw/result log binding '+prefix)
                            check('Sanitizer' not in text and 'runtime error:' not in text and 'software frame rejected' not in text,'worker diagnostics')
                            if mode=='hardware':check(text.count('via /dev/video0 [avd] (media /dev/media0)')==len(group),'actual AVD contexts '+prefix)
                            check(re.findall(r'^schedule (\w+) seed=(\d+) threads=(\d+)$',text,re.M)==[(schedule,str(seed),str(len(group)))],'schedule identity '+prefix)
                            expected_rows=[];expected_held=[];per_frames=0
                            pixel_rows=re.findall(r'^worker-frame (\d+) frame (\d+) (\d+)x(\d+) (\w+) ([a-f0-9]{32})$',text,re.M)
                            for wid,clip_id in enumerate(group):
                                c=clips[clip_id];victim=schedule!='normal' and wid==0;n=4 if victim else 8
                                expected_rows.append((str(wid),str(n),c['prefix'] if victim else c['full']))
                                pixels=[(str(i),str(c['width']),str(c['height']),c['format'],expected[clip_id][i]) for i in range(n)]
                                if victim:
                                    pixels += [('0',str(c['width']),str(c['height']),c['format'],expected[clip_id][3])]*2
                                    expected_held.append((str(wid),c['held'],'1' if schedule=='failure' else '0'))
                                observed=[row[1:] for row in pixel_rows if row[0]==str(wid)]
                                check(observed==pixels,'every decoded/retained pixel hash '+prefix)
                                per_frames+=n
                            check(set(r[0] for r in pixel_rows)==set(map(str,range(len(group)))),'pixel stream association')
                            check(re.findall(r'^worker (\d+) frames=(\d+) MD5=([a-f0-9]{32})$',text,re.M)==expected_rows,'every stream count/digest '+prefix)
                            check(re.findall(r'^retained (\d+) MD5=([a-f0-9]{32}) client_error=([01])$',text,re.M)==expected_held,'retained/client-error evidence '+prefix)
                            counts.append(dict(workers=len(group),frames=per_frames,retained=len(expected_held)))
                            frames+=per_frames;held_count+=len(expected_held);processes+=1
                        rows.append(dict(name=name,seed=seed,schedule=schedule,style=style,contexts=count,checks=counts))
        found={PurePosixPath(n).parts[1] for n in files if n.startswith(mode+'/') and n.endswith('/result.json')}
        check(found==groups,'exact group set '+mode)
        check(decode(files,mode+'/accepted.json')==rows,'accepted log derived independently '+mode)
        check(decode(files,mode+'/complete.json')==dict(groups=180,decoded_frames=2560,mode='vaapi' if mode=='hardware' else mode,hardware_executed=mode=='hardware'),'completion counters '+mode)
        check((frames,held_count,processes)==(2560,200,300),'full denominators '+mode)
        summary[mode]=dict(groups=180,processes=processes,decoded_frame_hashes=frames,retained_frames=held_count,additional_retained_readback_hashes=2*held_count)
    return summary

def mutate_tests(files,index):
    def replace_json(name,change):
        changed=dict(files);data=decode(files,name);change(data);changed[name]=(json.dumps(data)+'\n').encode();return changed
    variants={
        'missing-group':{n:d for n,d in files.items() if not n.startswith('hardware/r9-processes-4-failure/')},
        'guard-fault':dict(files,**{'guard.jsonl':files['guard.jsonl'].replace(b'"wedged": false',b'"wedged": true')}),
        'holder':replace_json('final-state.json',lambda d:d['state']['holders'].append({'pid':1})),
        'module':replace_json('final-state.json',lambda d:d.update(module_sha256='0'*64)),
        'source':replace_json('hardware/identity.json',lambda d:d.update(source_commit='0'*40)),
        'driver':replace_json('hardware/identity.json',lambda d:d['driver'].update(sha256='0'*64)),
        'lease':replace_json('hardware/identity.json',lambda d:d.update(guard_lease='unrelated')),
        'claimed-complete':replace_json('hardware/complete.json',lambda d:d.update(decoded_frames=9999)),
    }
    prefix='hardware/r0-threads-4-teardown/'
    text=files[prefix+'0.log'].decode()
    # Change both copies of each log, bypassing a mere duplicate-text/hash check.
    for label,pattern,replacement in [
        ('wrong-count',r'worker 1 frames=8','worker 1 frames=7'),
        ('wrong-stream-hash',r'(worker 1 frames=8 MD5=)[a-f0-9]{32}',r'\g<1>'+'0'*32),
        ('wrong-frame-hash',r'(worker-frame 2 frame 1 \d+x\d+ \w+ )[a-f0-9]{32}',r'\g<1>'+'0'*32),
        ('wrong-retained',r'(retained 0 MD5=)[a-f0-9]{32}',r'\g<1>'+'0'*32),
        ('wrong-seed','seed=549203191','seed=0'),
        ('missing-worker',r'^worker 3 frames=8 MD5=[a-f0-9]{32}\n',''),
        ('duplicate-worker',r'^(worker 1 frames=8 MD5=[a-f0-9]{32}\n)',r'\1\1')]:
        bad,n=re.subn(pattern,replacement,text,count=1,flags=re.M);check(n==1,'mutation drift '+label)
        changed=replace_json(prefix+'result.json',lambda d:d['records'][0].update(text=bad));changed[prefix+'0.log']=bad.encode();variants[label]=changed
    raw='inputs/h264.yuv';bad=bytearray(files[raw]);bad[0]^=1;variants['raw-reference']=dict(files,**{raw:bytes(bad)})
    for label,changed in variants.items():
        try:verify(changed,index)
        except (ValueError,KeyError):continue
        raise AssertionError('semantic mutation accepted: '+label)
    return len(variants)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--self-test',action='store_true');args=parser.parse_args()
    files,index=load();print(json.dumps(verify(files,index),indent=2))
    if args.self_test:print('PASS:',mutate_tests(files,index),'semantic evidence mutations rejected')
