#!/usr/bin/env python3
"""Same-disk PNG6003d3a/MKV benchmark, with complete decoding after timed drain.

Run with the project Python: scripts/bench_recording.py --output outputs/recording-bench
No robot, ROS, policy runtime or networking is used. Evidence and media are kept.
"""
import argparse
from collections import Counter
import csv
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'),str(ROOT/'packages/policy-sdk/src'),str(ROOT/'packages/connector-sdk/src')]
# Spawned benchmark writers inherit this immutable baseline selection.
import robot_control
if os.environ.get('RC_BENCH_SOURCE'):
    robot_control.__path__.insert(0,os.environ['RC_BENCH_SOURCE'])


def instrumented_writer(receiver, responder, directory, run_id):
    from robot_control.recording_writer import writer_main
    original = os.fsync
    counts = Counter()
    guard = threading.Lock()
    def counted(fd):
        with guard: counts[threading.current_thread().name] += 1
        return original(fd)
    os.fsync = counted
    try: writer_main(receiver,responder,directory,run_id)
    finally:
        own, children = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
        Path(directory,'writer-metrics.json').write_text(json.dumps({'fsync_calls':sum(counts.values()),
            'fsync_by_thread':dict(counts),'cpu_s':own.ru_utime+own.ru_stime+children.ru_utime+children.ru_stime}))


def make_frames(index, pattern):
    import numpy as np
    rng = np.random.default_rng(4000+index)
    cameras = {}
    y, x = np.indices((480,640), dtype=np.uint16)
    for offset, name in enumerate(('head','left_wrist','right_wrist')):
        if pattern == 'noise': pixels = rng.integers(0,256,(480,640,3),dtype=np.uint8)
        else:
            pixels = np.empty((480,640,3),np.uint8)
            pixels[:,:,0] = (x+index*3+offset*40)%256
            pixels[:,:,1] = (y+index*2+offset*30)%256
            pixels[:,:,2] = ((x//8+y//8+index)*9)%256
            pixels = np.add(pixels,rng.integers(0,16,pixels.shape,dtype=np.uint8),dtype=np.uint8)
        pixels.flags.writeable = False
        cameras[name] = {'image':pixels,'valid':True,'sequence':index,'timestamp':index/30}
    return cameras


def digest(pixels):
    value = hashlib.sha256(str((pixels.shape,str(pixels.dtype))).encode())
    value.update(memoryview(pixels).cast('B'))
    return value.hexdigest()


def monitor_tree(stop, measurements):
    while not stop.is_set():
        result = subprocess.run(['ps','-axo','pid=,ppid=,rss='],capture_output=True,text=True)
        rows = [tuple(map(int,line.split())) for line in result.stdout.splitlines() if line.strip()]
        selected = {os.getpid()}
        for _ in range(5): selected.update(pid for pid,ppid,rss in rows if ppid in selected)
        measurements.append({'t':time.monotonic(),'rss_bytes':sum(rss*1024 for pid,ppid,rss in rows if pid in selected)})
        stop.wait(.25)


def verify(folder, expected, observations):
    from PIL import Image
    import numpy as np
    from robot_control.recording_media import segments, decode_segment
    decoded, hashes, png_inodes = 0, {}, {}
    if (folder/'video_segments.jsonl').exists():
        for entry in segments(folder):
            for index,rgb in enumerate(decode_segment(folder,entry)):
                hashes[(entry['file'],index)] = entry['sha256'][index]
                decoded += 1
    def actual(ref):
        if ref.get('storage'):
            return hashes[(ref['file'],ref['frame_index'])]
        if ref['file'] not in hashes:
            path=folder/ref['file']; stat=path.stat(); identity=(stat.st_dev,stat.st_ino)
            if identity not in png_inodes:
                with Image.open(path) as image: png_inodes[identity]=digest(np.asarray(image))
            hashes[ref['file']]=png_inodes[identity]
        return hashes[ref['file']]
    commits = [json.loads(line) for line in (folder/'samples.jsonl').read_text().splitlines()]
    valid = 0
    for row in commits:
        for camera,ref in row['images'].items():
            if ref.get('missing') or ref.get('placeholder'): continue
            assert actual(ref) == expected[str(row['sample_id'])][camera] == ref['sha256']
            valid += 1
    policy = [json.loads(line) for line in (folder/'policy.jsonl').read_text().splitlines()]
    observed = 0
    for row in policy:
        if row.get('event') != 'policy.observation': continue
        for camera,ref in row['image_files'].items():
            assert actual(ref) == observations[row['observation_id']][camera] == ref['sha256']
            observed += 1
    for name in ('epos','qpos','control_epos','control_qpos'):
        with (folder/(name+'.csv')).open() as source: rows=list(csv.reader(source))
        assert rows[0] == ['sample_id','timestamp_ns','monotonic_ns'] + [f'{arm}_{i}' for arm in ('left','right') for i in range(7)]
        assert len(rows)-1 == len(commits)
        assert [int(row[0]) for row in rows[1:]] == list(range(len(commits)))
        for index,row in enumerate(rows[1:]):
            values = ([str(index*.001)]*14 if name=='qpos' else [str(index*.002)]*14 if name=='epos'
                      else [str(index*.002)]*7+['']*7 if name=='control_epos' else ['']*14)
            assert row[3:] == values
    controls = [json.loads(line) for line in (folder/'control.jsonl').read_text().splitlines()]
    published = {row['attempt_id'] for row in controls if row.get('event')=='control.published'}
    assert all(str(row['sample_id']) in published for row in commits)
    return {'text_samples':len(commits),'verified_valid_sample_frames':valid,'verified_observation_frames':observed,
            'decoded_video_frames':decoded,'unique_png_assets':len(set(hashes.values())) if not decoded else 0}


def worker(args):
    import numpy as np
    from robot_control import recording
    recording.writer_main = instrumented_writer
    class ManualRecorder(recording.RecordingService):
        def _sample_loop(self): self._quit.wait()
    root = args.output.resolve(); root.mkdir(parents=True,exist_ok=False)
    service = ManualRecorder(root,'bench',lambda:None,stop_timeout=60)
    service.start(); service.command('on',{'benchmark':True}).result(15)
    expected, observations, latency, raw = {}, {}, [], []
    failed, stop = None, threading.Event()
    observation_rejections = []
    sampler = threading.Thread(target=monitor_tree,args=(stop,raw),daemon=True);sampler.start()
    total = args.samples if args.mode == 'throughput' else round(args.seconds*30)
    began = time.perf_counter(); cpu_start = time.process_time()
    input_end = began
    try:
        for index in range(total):
            frames = make_frames(index,args.pattern)
            observed = dict(frames)
            if index % 31 == 0:
                special = frames['head']['image'].copy(); special[0,0] ^= 255;special.flags.writeable=False
                observed['head'] = {**frames['head'],'image':special}
            expected[str(index)] = {name:digest(frame['image']) for name,frame in frames.items()}
            observations[str(index)] = {name:digest(frame['image']) for name,frame in observed.items()}
            state = {'qpos':[index*.001]*14,'eef':[index*.002]*14,'raw':{'padding':'x'*256}}
            payload = {'sample_id':index,'timestamp_ns':index*33333333,'monotonic_ns':index*33333333,
                       'scheduled_ns':index*33333333,'state':state,'state_error':None,'frames':frames,
                       'controls':{'left':{'mode':'epos','values':[index*.002]*7,'attempt_id':str(index)}},'missed':False}
            if args.mode == 'throughput':
                # Saturate within the same bounded credits, never bypass limits.
                sample_size = recording.estimated_bytes(payload)+recording.estimated_bytes(None)
                event_size = recording.estimated_bytes(observed)+recording.estimated_bytes({'state':state})+16384
                while service.active:
                    with service._lock:
                        ready = (service._bytes['sample']+sample_size <= service.sample_budget
                            and service._counts['sample'] < service.max_samples
                            and service._bytes['extra']+event_size <= service.extra_budget*.8)
                    if ready: break
                    time.sleep(.001)
            else:
                delay = began+index/30-time.perf_counter()
                if delay > 0: time.sleep(delay)
            started = time.perf_counter_ns()
            accepted = service._enqueue('sample',payload)
            latency.append((time.perf_counter_ns()-started)/1e6)
            if not accepted:
                failed = {'at_s':time.perf_counter()-began,'sample':index,'reason':service.status()['error'] or 'sample rejected'}
                break
            if not service.event('policy',{'event':'policy.observation','observation_id':str(index),'state':state},frames=observed):
                rejection = {'at_s':time.perf_counter()-began,'sample':index,'reason':'policy observation rejected'}
                observation_rejections.append(rejection)
                failed = failed or rejection
            service.event('control',{'event':'control.published','attempt_id':str(index),'arm':'left',
                'values':[index*.002]*7,'monotonic_ns':index*33333333},priority=True)
        if args.mode == 'realtime' and service.active:
            time.sleep(max(0,began+args.seconds-time.perf_counter()))
        input_end = time.perf_counter()
        status = service.command('off').result(65)
        if status['error'] and failed is None: failed={'at_s':input_end-began,'reason':status['error']}
    except Exception as exc:
        input_end = time.perf_counter();status=service.status()
        failed = failed or {'at_s':input_end-began,'reason':repr(exc)}
    finally:
        finished = time.perf_counter()
        forced = service.shutdown(time.monotonic()+10)
        stop.set();sampler.join(2)
    status = service.status()
    folder = Path(status['path'])
    parent_cpu = time.process_time()-cpu_start
    writer = json.loads((root/'writer-metrics.json').read_text()) if (root/'writer-metrics.json').exists() else {}
    verified = {}
    try: verified = verify(folder,expected,observations)
    except Exception as exc: verified={'verification_error':repr(exc)}
    files=[path for path in folder.rglob('*') if path.is_file()]
    # Count hard-linked PNG payloads only once for physical byte volume.
    unique={(p.stat().st_dev,p.stat().st_ino):p.stat().st_size for p in files}
    elapsed=finished-began
    result={'implementation':args.implementation,'mode':args.mode,'pattern':args.pattern,'requested_samples':total,
        'elapsed_s':elapsed,'input_s':input_end-began,'stop_drain_s':finished-input_end,
        'end_to_end_samples_s':verified.get('text_samples',0)/elapsed,'failed':failed,'forced':forced,
        'enqueue_ms':dict(zip(('p50','p95','p99'),map(float,np.percentile(latency,[50,95,99])))),
        'file_count':len(files),'unique_file_count':len(unique),'file_bytes':sum(unique.values()),
        'peak_tree_rss_bytes':max((row['rss_bytes'] for row in raw),default=0),
        'parent_cpu_s':parent_cpu,'cpu_s':parent_cpu+writer.get('cpu_s',0),
        'status':status,'observation_rejections':observation_rejections,'verification':verified,**writer}
    result['cpu_s']=parent_cpu+writer.get('cpu_s',0)
    result['success']=(failed is None and not forced and verified.get('text_samples')==total
        and verified.get('verified_valid_sample_frames')==total*3 and verified.get('verified_observation_frames')==total*3)
    (root/'raw.json').write_text(json.dumps({'enqueue_ms':latency,'memory':raw,'expected_sample_hashes':expected,
        'expected_observation_hashes':observations},indent=2))
    (root/'result.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result),flush=True)


def summarize(root, results):
    """Keep failed runs visible and only compare complete throughput runs."""
    aggregates={}
    for impl in ('png','mkv'):
        rows=[row for row in results if row.get('mode')=='throughput' and row.get('implementation')==impl and row.get('success')]
        if rows:
            aggregates[impl]={'successful_rounds':len(rows),**{key:statistics.mean(row[key] for row in rows)
                for key in ('elapsed_s','end_to_end_samples_s','stop_drain_s','file_count','file_bytes','fsync_calls','cpu_s','peak_tree_rss_bytes')},
                'enqueue_ms':{key:statistics.mean(row['enqueue_ms'][key] for row in rows) for key in ('p50','p95','p99')}}
    ratios={}
    if set(aggregates)=={'png','mkv'}:
        png,mkv=aggregates['png'],aggregates['mkv']
        ratios={'throughput_factor':mkv['end_to_end_samples_s']/png['end_to_end_samples_s'],
                'elapsed_reduction':1-mkv['elapsed_s']/png['elapsed_s'],
                'file_count_reduction':1-mkv['file_count']/png['file_count'],
                'fsync_reduction':1-mkv['fsync_calls']/png['fsync_calls'],
                'byte_reduction':1-mkv['file_bytes']/png['file_bytes']}
    comparison={'throughput_means':aggregates,'ratios':ratios,
                'failures':[{'name':row['name'],'failure':row.get('failed'),'verification':row.get('verification')}
                            for row in results if not row.get('success')]}
    (root/'comparison.json').write_text(json.dumps(comparison,indent=2))
    env=json.loads((root/'environment.json').read_text())
    lines=['# 本机录制 PNG / MKV 对照结果','',
           f"环境：{env['cpu_model']}，{env['cpu_count']} 核，{env['platform']}。三路 640×480、30 Hz；基线 `6003d3a`。",'',
           '吞吐轮采用相同确定性输入和文本负载，每轮 450 样本；三轮交错运行。计时包含停止排空及尾段封口。失败轮不纳入速度比较。', '',
           '| 吞吐轮 | 完整验证 | 耗时 s | 样本/s | 排空 ms | 策略观察拒绝 |',
           '| --- | ---: | ---: | ---: | ---: | ---: |']
    for row in results:
        if row.get('mode')=='throughput':
            lines.append(f"| {row['name']} | {row['success']} | {row['elapsed_s']:.3f} | {row['end_to_end_samples_s']:.2f} | {row['stop_drain_s']*1000:.2f} | {row['status']['extra_dropped']} |")
    if ratios:
        lines += ['',f"完整吞吐轮均值：吞吐 {ratios['throughput_factor']:.3f} 倍（+{(ratios['throughput_factor']-1)*100:.1f}%），总耗时减少 {ratios['elapsed_reduction']*100:.1f}%。",'',
                  '| 均值指标 | PNG | MKV |','| --- | ---: | ---: |']
        for label,key,scale in [('文件数','file_count',1),('实际文件体积 MiB（硬链接去重）','file_bytes',1/2**20),
                                ('Python writer fsync 次数','fsync_calls',1),('CPU 秒（父进程+writer+编码器）','cpu_s',1),
                                ('进程树峰值 RSS MiB（250ms 抽样）','peak_tree_rss_bytes',1/2**20)]:
            lines.append(f"| {label} | {aggregates['png'][key]*scale:.2f} | {aggregates['mkv'][key]*scale:.2f} |")
        for key in ('p50','p95','p99'):
            lines.append(f"| 入队 {key} ms | {aggregates['png']['enqueue_ms'][key]:.4f} | {aggregates['mkv']['enqueue_ms'][key]:.4f} |")
    lines += ['','| 60 秒实时场景 | 文本样本 | 有效采样图像核验 | 策略图像核验 | 观察拒绝次数 | 首次拒绝 s | 排空 ms |',
              '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for row in results:
        if row.get('mode')=='realtime':
            checked=row['verification'];failure=row.get('failed') or {}
            lines.append(f"| {row['name']} | {checked.get('text_samples',0)} | {checked.get('verified_valid_sample_frames',0)} | {checked.get('verified_observation_frames',0)} | {len(row.get('observation_rejections',[]))} | {failure.get('at_s',0):.3f} | {row['stop_drain_s']*1000:.2f} |")
    lines += ['', '实时目标为每轮 1800 个文本样本、5400 张采样图像、5400 张策略观察图像。附加队列沿用 64 MiB，采样队列 512 MiB。任何观察拒绝都使该轮完整性结果为失败；它不会提前结束必需采样，也不被视作速度优势。各轮首个失败时间、具体原因、积压峰值、CPU/内存、文件数量/体积和 fsync 次数见 `summary.json`。', '',
              '有效采样帧及已接受的策略观察均在计时后完整解码核对像素哈希；CSV 列及样本顺序独立检查。FFmpeg 内部同步调用不计入 Python fsync 统计。原始逐次入队延迟、RSS 抽样与预期像素哈希在各轮 `raw.json`；完整结果在 `result.json`。', '',
              '源码快照为 `baseline-source/` 与 `mkv-source/`，基准脚本快照、源码 SHA256、FFmpeg/Python/磁盘信息在同目录及 `environment.json`。本结果仅代表本机，未运行服务器部署或真机录制验收。','']
    (root/'report.md').write_text('\n'.join(lines))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--worker',action='store_true')
    parser.add_argument('--implementation',choices=['png','mkv'],default='mkv')
    parser.add_argument('--mode',choices=['throughput','realtime'],default='throughput')
    parser.add_argument('--pattern',choices=['moving','noise'],default='moving')
    parser.add_argument('--samples',type=int,default=450)
    parser.add_argument('--seconds',type=float,default=60)
    parser.add_argument('--rounds',type=int,default=3)
    parser.add_argument('--skip-realtime',action='store_true')
    args=parser.parse_args()
    if args.worker: return worker(args)
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=True)
    baseline=root/'baseline-source';baseline.mkdir(exist_ok=True)
    for name in ('recording.py','recording_writer.py'):
        (baseline/name).write_bytes(subprocess.check_output(['git','show',f'6003d3a:src/robot_control/{name}'],cwd=ROOT))
    frozen=root/'mkv-source';frozen.mkdir(exist_ok=True)
    for name in ('recording.py','recording_writer.py','recording_media.py'):
        (frozen/name).write_bytes((ROOT/'src/robot_control'/name).read_bytes())
    sources={str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest()
             for path in [ROOT/'src/robot_control'/name for name in ('recording.py','recording_writer.py','recording_media.py')]}
    environment={'platform':platform.platform(),'python':sys.version,'cpu_count':os.cpu_count(),'cpu_model':subprocess.check_output(['sysctl','-n','machdep.cpu.brand_string'],text=True).strip() if sys.platform=='darwin' else platform.processor(),
        'numpy':importlib.metadata.version('numpy'),'Pillow':importlib.metadata.version('Pillow'),
        'ffmpeg':subprocess.check_output(['ffmpeg','-version'],text=True),'source_sha256':sources,'baseline':'6003d3a','benchmark_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'disk':subprocess.check_output(['df','-h',str(root)],text=True),'width':640,'height':480,'fps':30,
        'sample_budget':512*1024*1024,'extra_budget':64*1024*1024,'encoder_threads':2,'timestamp':time.time()}
    (root/'bench_recording.py').write_bytes(Path(__file__).read_bytes())
    (root/'environment.json').write_text(json.dumps(environment,indent=2))
    jobs=[]
    for repeat in range(args.rounds):
        for impl in (('png','mkv') if repeat%2==0 else ('mkv','png')):
            jobs.append((f'throughput-{repeat+1}-{impl}',impl,'throughput','moving'))
    if not args.skip_realtime:
        for pattern in ('moving','noise'):
            for impl in ('png','mkv'): jobs.append((f'realtime-{pattern}-{impl}',impl,'realtime',pattern))
    results=[]
    for name,impl,mode,pattern in jobs:
        env=dict(os.environ)
        env['RC_BENCH_SOURCE']=str(baseline if impl=='png' else frozen)
        command=[sys.executable,str(Path(__file__).resolve()),'--worker','--output',str(root/name),
                 '--implementation',impl,'--mode',mode,'--pattern',pattern,'--samples',str(args.samples),'--seconds',str(args.seconds)]
        print(f'RUN {name}',flush=True)
        with (root/(name+'.log')).open('w') as log: process=subprocess.run(command,env=env,stdout=log,stderr=subprocess.STDOUT)
        result=json.loads((root/name/'result.json').read_text()) if (root/name/'result.json').exists() else {'failed':f'worker exit {process.returncode}'}
        results.append({'name':name,**result});(root/'summary.json').write_text(json.dumps(results,indent=2))
        print(f'DONE {name} success={result.get("success")} elapsed={result.get("elapsed_s")} failure={result.get("failed")}',flush=True)
    summarize(root,results)
    return 0


if __name__=='__main__': raise SystemExit(main())
