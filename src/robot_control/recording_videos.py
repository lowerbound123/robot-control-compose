#!/usr/bin/env python3
"""Convert static RC PNG/MKV recordings to MP4, then remove verified sources.

Requires ffmpeg (with libx264) and ffprobe on PATH; Python uses only stdlib.
Videos, a valid first-head-frame PNG, and structured logs/indexes are retained.
Legacy PNG gaps stay unpadded; MKV preserves the complete 30 Hz slot timeline.
"""
from __future__ import annotations

import argparse
from contextlib import suppress
import filecmp
import hashlib
from fractions import Fraction
import json
import os
from pathlib import Path
import shutil
import signal
import struct
import subprocess
import sys
import tempfile

if __package__:
    from .recording_media import SessionLock, INDEX, STORAGE, segments, decode_segment, safe_video_path, pixel_hash
else:
    from recording_media import SessionLock, INDEX, STORAGE, segments, decode_segment, safe_video_path, pixel_hash


DEFAULT_LOGS = Path(__file__).resolve().parents[2] / 'outputs' / 'logs'


def real_directory(path: Path, *, optional=False):
    if path.is_symlink():
        raise ValueError(f'拒绝符号链接目录：{path}')
    if optional and not path.exists():
        return
    if not path.is_dir():
        raise ValueError(f'目录不存在或不是目录：{path}')


def sessions(root: Path):
    """Do not descend into recording contents or follow directory symlinks."""
    def walk_error(error):
        raise error

    for current, names, _ in os.walk(root, followlinks=False, onerror=walk_error):
        path = Path(current)
        if (path.name.startswith('record_')
                and any((path / name).exists() or (path / name).is_symlink()
                        for name in ('session.jsonl', 'images', 'assets'))):
            names[:] = []
            yield path
        else:
            names[:] = sorted(name for name in names if not (path / name).is_symlink())


def camera_images(session: Path):
    images = session / 'images'
    real_directory(images)
    cameras = {}
    for camera in sorted(images.iterdir()):
        if not camera.is_symlink() and not camera.is_dir():
            continue
        real_directory(camera)
        frames = sorted(camera.glob('*.png'), key=lambda p: int(p.stem))
        if not frames:
            raise ValueError(f'相机没有 PNG，保留 assets 和 images：{camera}')
        numbers = [int(frame.stem) for frame in frames]
        if min(numbers) < 0 or len(set(numbers)) != len(numbers):
            raise ValueError(f'采样编号重复或为负数：{camera}')
        dimensions = None
        for frame in frames:
            if frame.is_symlink() or not frame.is_file():
                raise ValueError(f'拒绝符号链接或非普通 PNG 文件：{frame}')
            with frame.open('rb') as source:
                header = source.read(24)
            if (len(header) != 24 or header[:8] != b'\x89PNG\r\n\x1a\n'
                    or header[8:16] != b'\x00\x00\x00\rIHDR'):
                raise ValueError(f'无效 PNG：{frame}')
            size = struct.unpack('>II', header[16:24])
            if min(size) <= 0 or (dimensions is not None and dimensions != size):
                raise ValueError(f'同一路图像尺寸无效或发生变化：{frame}')
            dimensions = size
        cameras[camera.name] = (frames, dimensions)
    if not cameras:
        raise ValueError(f'没有相机图像，保留 assets 和 images：{images}')
    return cameras


def encode(frames, destination, fps, ffmpeg, *, crf=28, preset='medium'):
    command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-xerror',
               '-err_detect', 'explode', '-f', 'image2pipe', '-framerate', str(fps),
               '-vcodec', 'png', '-i', 'pipe:0', '-an',
               '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2',
               '-c:v', 'libx264', '-preset', preset, '-crf', str(crf),
               '-pix_fmt', 'yuv420p', '-threads', '2', '-movflags', '+faststart',
               '-n', str(destination)]
    # A file avoids stderr pipe deadlocks while stdin carries large recordings.
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=errors)
        try:
            try:
                for frame in frames:
                    with frame.open('rb') as source:
                        shutil.copyfileobj(source, process.stdin, length=1024 * 1024)
            except BrokenPipeError:
                pass
            finally:
                with suppress(BrokenPipeError):
                    process.stdin.close()
            returncode = process.wait()
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            raise
        if returncode:
            errors.seek(max(0, errors.tell() - 4000))
            detail = errors.read().decode('utf-8', errors='replace').strip()
            raise RuntimeError(f'FFmpeg 转换失败：{destination.name}\n{detail}')


def verify_video(path, count, dimensions, fps, ffprobe):
    result = subprocess.run(
        [ffprobe, '-v', 'error', '-select_streams', 'v:0', '-count_frames',
         '-show_entries', 'stream=codec_name,nb_read_frames,width,height,avg_frame_rate',
         '-of', 'json', str(path)], capture_output=True, text=True)
    if result.returncode or result.stderr.strip():
        raise RuntimeError(f'视频解码检查失败：{path.name}\n{result.stderr.strip()}')
    streams = json.loads(result.stdout).get('streams', [])
    expected_size = tuple(value + value % 2 for value in dimensions)
    if (len(streams) != 1 or streams[0].get('codec_name') != 'h264'
            or int(streams[0].get('nb_read_frames', 0)) != count
            or (streams[0].get('width'), streams[0].get('height')) != expected_size
            or Fraction(streams[0].get('avg_frame_rate', '0')) != fps):
        raise RuntimeError(f'视频帧数、尺寸或帧率不匹配：{path.name}；保留 assets 和 images')


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def process_session(session, args, ffmpeg, ffprobe):
    real_directory(session)
    try:
        lock = SessionLock(session, create=not args.dry_run)
    except BlockingIOError:
        print(f'[SKIP] {session}：活动 writer 持有会话锁', flush=True)
        return 'skipped'
    with lock:
        if (session / INDEX).exists() or (session / 'video_raw').exists():
            return process_mkv(session, args, ffmpeg, ffprobe)
        return process_png(session, args, ffmpeg, ffprobe)


def process_png(session, args, ffmpeg, ffprobe):
    real_directory(session)
    assets, images, videos = session / 'assets', session / 'images', session / 'videos'
    initial = session / 'initial.png'
    real_directory(assets, optional=True)
    real_directory(images, optional=True)
    real_directory(videos, optional=True)
    if initial.is_symlink() or (initial.exists() and not initial.is_file()):
        raise ValueError(f'拒绝非普通初始状态图片目标：{initial}')
    if not images.exists() and not assets.exists() and any(videos.glob('*.mp4')):
        detail = '保留已有初始状态、视频和日志' if initial.exists() else '无法补存无损初始状态图片'
        print(f'[SKIP] {session}：原图目录已删除，{detail}', flush=True)
        return 'skipped'
    cameras = camera_images(session)
    if 'head' not in cameras:
        raise ValueError(f'缺少 head 相机，无法保存初始状态；保留 assets 和 images：{session}')
    first_head = cameras['head'][0][0]
    targets = [videos / (name + '.mp4') for name in cameras]
    for target in targets:
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError(f'拒绝非普通视频目标：{target}')
    if not args.overwrite and any(path.exists() for path in [*targets, initial]):
        print(f'[SKIP] {session}：视频或初始状态图片已存在；使用 --overwrite 重做，本次不删除 assets 和 images', flush=True)
        return 'skipped'
    print(f'[PLAN] {first_head} → {initial}：独立复制首张 head 原始 PNG', flush=True)
    for name, (frames, _) in cameras.items():
        gaps = int(frames[-1].stem) - int(frames[0].stem) + 1 - len(frames)
        print(f'[PLAN] {videos / (name + ".mp4")}：{len(frames)} 帧，'
              f'{args.fps} FPS，{len(frames) / args.fps:.2f} 秒，编号缺口 {gaps}（不补帧）',
              flush=True)
    print(f'[PLAN] 初始状态保存、所有视频验证成功后删除 {assets} 和 {images}；保留初始状态、视频和其他日志', flush=True)
    if args.dry_run:
        return 'planned'
    with tempfile.TemporaryDirectory(prefix='.recording-videos-', dir=session) as temporary:
        staging = Path(temporary)
        staged_initial = staging / initial.name
        shutil.copyfile(first_head, staged_initial)
        if not filecmp.cmp(first_head, staged_initial, shallow=False):
            raise RuntimeError('初始状态图片复制校验失败；保留 assets 和 images')
        with staged_initial.open('rb') as stream:
            os.fsync(stream.fileno())
        for name, (frames, dimensions) in cameras.items():
            destination = staging / (name + '.mp4')
            print(f'[ENCODE] {session.name}/{name}', flush=True)
            encode(frames, destination, args.fps, ffmpeg, crf=args.crf, preset=args.preset)
            verify_video(destination, len(frames), dimensions, args.fps, ffprobe)
            with destination.open('rb') as stream:
                os.fsync(stream.fileno())
        # Publish the independent PNG and every verified video before deletion.
        real_directory(videos, optional=True)
        videos.mkdir(exist_ok=True)
        for target in [initial, *targets]:
            source = staging / target.name
            if args.overwrite:
                os.replace(source, target)
            else:
                os.link(source, target)  # Refuse a concurrently created target.
        sync_directory(videos)
        sync_directory(session)
        real_directory(assets, optional=True)
        real_directory(images)
        if assets.exists():
            shutil.rmtree(assets)
        shutil.rmtree(images)
    print(f'[OK] {session}：initial.png 和视频已保存，assets 和 images 已删除，其他日志已保留', flush=True)
    return 'converted'


def encode_rgb(frames, destination, dimensions, fps, ffmpeg, *, crf=28, preset='medium'):
    command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-xerror',
               '-f', 'rawvideo', '-pixel_format', 'rgb24', '-video_size', f'{dimensions[0]}x{dimensions[1]}',
               '-framerate', str(fps), '-i', 'pipe:0', '-an', '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2',
               '-c:v', 'libx264', '-preset', preset, '-crf', str(crf), '-pix_fmt', 'yuv420p',
               '-threads', '2', '-movflags', '+faststart', '-n', str(destination)]
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors)
        try:
            for frame in frames: process.stdin.write(frame)
            process.stdin.close()
            if process.wait():
                errors.seek(0)
                raise RuntimeError(f'FFmpeg encode failed: {errors.read(4000)!r}')
        finally:
            if process.poll() is None: process.kill()
            process.wait()
            with suppress(BrokenPipeError): process.stdin.close()
            if hasattr(frames, 'close'): frames.close()


def process_mkv(session, args, ffmpeg, ffprobe):
    if args.fps != 30:
        raise ValueError('MKV 主视频固定 30 FPS，不能改变采样时间线')
    raw, videos, initial = session/'video_raw', session/'videos', session/'initial.png'
    real_directory(raw, optional=True); real_directory(videos, optional=True)
    entries = segments(session)
    if not entries: raise ValueError('没有已封口视频片段，保留原始素材')
    paths = {entry['file']:safe_video_path(session,entry['file']) for entry in entries}
    receipt = session/'video_reduction.json'
    for target in (initial,receipt):
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError(f'拒绝非普通输出文件：{target}')
    index_digest = hashlib.sha256(json.dumps(entries,sort_keys=True).encode()).hexdigest()
    if receipt.is_file() and not all(path.exists() for path in paths.values()):
        # A cancellation may interrupt unlinking after outputs became durable.
        # Resume only from an exact receipt and independently intact outputs.
        completed = json.loads(receipt.read_text())
        if completed.get('index_sha256') != index_digest or completed.get('files') != list(paths):
            raise ValueError('转换回执不匹配完成索引；保留剩余素材')
        expected_outputs = {'initial.png'} | {f"videos/{entry['camera']}.mp4" for entry in entries
                                              if entry['stream']=='samples'}
        outputs = completed.get('outputs')
        if not isinstance(outputs,dict) or set(outputs) != expected_outputs:
            raise ValueError('转换回执缺少输出验证记录；保留剩余素材')
        for relative, expected in outputs.items():
            if relative != 'initial.png' and (not relative.startswith('videos/') or len(Path(relative).parts)!=2):
                raise ValueError('无效转换回执输出路径')
            output = session/relative
            if output.is_symlink() or not output.is_file() or file_hash(output) != expected:
                raise ValueError(f'已发布输出缺失或损坏：{output}；保留剩余素材')
        remaining = [entry for entry in entries if paths[entry['file']].exists()]
        if args.dry_run:
            print(f'[PLAN] {session}：输出验证通过，可清理剩余 {len(remaining)} 个已转换片段',flush=True)
            return 'planned' if remaining else 'skipped'
        for entry in remaining:
            for _ in decode_segment(session,entry,ffmpeg=ffmpeg): pass
        for entry in remaining: paths[entry['file']].unlink()
        for folder in {paths[entry['file']].parent for entry in remaining}: sync_directory(folder)
        print(f'[SKIP] {session}：转换回执与输出验证通过，原始片段已清理；未封口素材保留', flush=True)
        return 'skipped'
    for path in paths.values():
        if not path.is_file(): raise ValueError(f'missing sealed video segment: {path}')
    unknown = sorted(path for path in raw.rglob('*') if path.is_file() and str(path.relative_to(session)) not in paths)
    for path in unknown:
        print(f'[WARN] unsealed 素材保留，不参与转换：{path}', flush=True)
    cameras = {}
    for entry in entries:
        if entry['stream'] not in ('samples','observations'):
            raise ValueError('未知视频流类型')
        if entry['stream'] == 'samples': cameras.setdefault(entry['camera'], []).append(entry)
    if 'head' not in cameras: raise ValueError('没有有效 head 视频；保留所有原始素材')
    for name, rows in cameras.items():
        expected = 0
        dimensions = (rows[0]['width'],rows[0]['height'])
        for row in rows:
            if row['first_frame'] != expected or (row['width'],row['height']) != dimensions:
                raise ValueError(f'缺失片段或尺寸变化：{name}')
            expected += row['frames']
    head_reference = None
    with (session/'samples.jsonl').open() as source:
        for line in source:
            try: sample = json.loads(line)
            except ValueError:
                if not line.endswith('\n'): break
                raise
            ref = sample.get('images',{}).get('head',{})
            if ref.get('file') in paths and not ref.get('placeholder') and not ref.get('missing') and ref.get('valid',True):
                head_reference = ref; break
    if head_reference is None: raise ValueError('没有已封口的有效非占位 head 帧；保留所有原始素材')
    targets = [videos/(name+'.mp4') for name in cameras]
    for target in [initial,*targets,receipt]:
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError(f'拒绝非普通输出文件：{target}')
    if not args.overwrite and any(path.exists() for path in [initial,*targets]):
        print(f'[SKIP] {session}：已有输出，使用 --overwrite 重做；保留原始素材', flush=True)
        return 'skipped'
    for name, rows in cameras.items():
        print(f'[PLAN] {name}.mp4：{sum(row["frames"] for row in rows)} 帧，30 FPS，包含占位帧', flush=True)
    print(f'[PLAN] {initial}：首个有效 head 帧；全部验证后删除 {len(entries)} 个已封口片段', flush=True)
    if args.dry_run: return 'planned'
    with tempfile.TemporaryDirectory(prefix='.recording-videos-', dir=session) as temporary:
        staging = Path(temporary)
        first_rgb = None
        first_size = None
        def pixels(rows):
            nonlocal first_rgb, first_size
            for row in rows:
                for index, rgb in enumerate(decode_segment(session,row,ffmpeg=ffmpeg)):
                    if row['file'] == head_reference['file'] and index == head_reference['frame_index']:
                        if pixel_hash(rgb,row['width'],row['height']) != head_reference['sha256']:
                            raise ValueError('head 观察引用像素哈希不匹配')
                        first_rgb, first_size = rgb, (row['width'],row['height'])
                    yield rgb
        for name, rows in cameras.items():
            dimensions = (rows[0]['width'],rows[0]['height'])
            output = staging/(name+'.mp4')
            encode_rgb(pixels(rows),output,dimensions,30,ffmpeg,crf=args.crf,preset=args.preset)
            verify_video(output,sum(row['frames'] for row in rows),dimensions,30,ffprobe)
            with output.open('rb') as stream: os.fsync(stream.fileno())
        # Auxiliary policy inputs are also verified before deleting their only
        # lossless source, but never inserted into the 30 Hz sample timeline.
        for entry in entries:
            if entry['stream'] == 'observations':
                for _ in decode_segment(session,entry,ffmpeg=ffmpeg): pass
        if first_rgb is None: raise ValueError('有效 head 引用不在已封口帧范围内')
        staged_initial = staging/'initial.png'
        result = subprocess.run([ffmpeg,'-v','error','-f','rawvideo','-pix_fmt','rgb24',
            '-s',f'{first_size[0]}x{first_size[1]}','-i','pipe:0','-frames:v','1','-threads','1',
            '-update','1',str(staged_initial)], input=first_rgb, capture_output=True)
        if result.returncode: raise RuntimeError(f'initial.png encode failed: {result.stderr!r}')
        decoded = subprocess.run([ffmpeg,'-v','error','-i',str(staged_initial),'-f','rawvideo',
            '-pix_fmt','rgb24','pipe:1'],capture_output=True,check=True).stdout
        if decoded != first_rgb: raise ValueError('initial.png 无损像素验证失败')
        with staged_initial.open('rb') as stream: os.fsync(stream.fileno())
        videos.mkdir(exist_ok=True)
        for target in [initial,*targets]:
            source = staging/target.name
            if args.overwrite: os.replace(source,target)
            else: os.link(source,target)
        sync_directory(videos); sync_directory(session)
        staged_receipt = staging/receipt.name
        with staged_receipt.open('w') as stream:
            json.dump({'storage':STORAGE,'files':list(paths),'index_sha256':index_digest,
                       'outputs':{str(path.relative_to(session)):file_hash(path) for path in [initial,*targets]},
                       'initial_sha256':head_reference['sha256']},stream)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(staged_receipt,receipt); sync_directory(session)
        for path in paths.values(): path.unlink()
        for folder in {path.parent for path in paths.values()}: sync_directory(folder)
    print(f'[OK] {session}：视频与 initial.png 验证成功，已删除对应已封口原始片段', flush=True)
    return 'converted'


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024), b''): digest.update(block)
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=(
        '批量将静态 RC PNG/MKV 录制转为 MP4，跳过活动 writer，保存有效 head 首帧为 initial.png。'
        '验证后删除 assets 和 images，保留初始状态、视频和其他日志。'))
    parser.add_argument('logs', nargs='?', type=Path, default=DEFAULT_LOGS,
                        help='日志根目录或单个 record_* 会话目录（默认项目 outputs/logs）')
    parser.add_argument('--dry-run', action='store_true', help='仅预览，不写视频或删除文件')
    parser.add_argument('--overwrite', action='store_true', help='重新生成并替换已有同名视频和初始状态图片')
    parser.add_argument('--fps', type=int, default=30, help='视频帧率，默认 30，范围 1–240')
    parser.add_argument('--crf', type=int, default=28, help='H.264 质量因子，默认 28，范围 0–51；越大体积越小')
    parser.add_argument('--preset', choices=('ultrafast', 'superfast', 'veryfast', 'faster', 'fast',
                                           'medium', 'slow', 'slower', 'veryslow'), default='medium',
                        help='编码预设，默认 medium；更慢的预设偏向压缩效率')
    parser.add_argument('--json-summary', action='store_true', help='额外输出机器可读汇总')
    args = parser.parse_args(argv)
    if not 1 <= args.fps <= 240:
        parser.error('--fps 必须在 1–240 之间')
    if not 0 <= args.crf <= 51:
        parser.error('--crf 必须在 0–51 之间')
    try:
        real_directory(args.logs.expanduser())
        root = args.logs.expanduser().resolve()
        ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
        if not args.dry_run and (not ffmpeg or not ffprobe):
            raise ValueError('需要 PATH 中的 ffmpeg 和 ffprobe（FFmpeg 需支持 libx264）')
        counts = dict(converted=0, planned=0, skipped=0, failed=0)
        print(f'日志根目录：{root}', flush=True)
        print(f'编码配置：H.264 CRF {args.crf}，preset {args.preset}，{args.fps} FPS', flush=True)
        for session in sessions(root):
            try:
                counts[process_session(session, args, ffmpeg, ffprobe)] += 1
            except (OSError, ValueError, RuntimeError, ZeroDivisionError) as exc:
                counts['failed'] += 1
                print(f'[ERROR] {session}：{exc}', file=sys.stderr, flush=True)
        print('汇总：' + ', '.join(f'{key}={value}' for key, value in counts.items()), flush=True)
        if args.json_summary:
            print(json.dumps({'type': 'record.reduce.summary', **counts}), flush=True)
        return 1 if counts['failed'] else 0
    except (OSError, ValueError) as exc:
        print(f'[ERROR] {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\n已中断；未完成转换的会话保留 assets 和 images。', file=sys.stderr)
        return 130


def entrypoint():
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    return main()


if __name__ == '__main__':
    raise SystemExit(entrypoint())
