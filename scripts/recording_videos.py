#!/usr/bin/env python3
"""Convert stopped RC recordings to MP4, then remove assets and images.

Requires ffmpeg (with libx264) and ffprobe on PATH; Python uses only stdlib.
Videos, a separate first-head-frame PNG, and structured logs are retained. PNGs are packed in
numeric sample order at a fixed frame rate; missing samples are not padded.
"""
from __future__ import annotations

import argparse
from contextlib import suppress
import filecmp
from fractions import Fraction
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile


DEFAULT_LOGS = Path(__file__).resolve().parents[1] / 'outputs' / 'logs'


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


def is_stopped(session: Path):
    path = session / 'session.jsonl'
    if path.is_symlink():
        raise ValueError(f'拒绝符号链接文件：{path}')
    if not path.is_file():
        return False
    last = None
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            if line.strip():
                last = json.loads(line)
    return (isinstance(last, dict) and last.get('event') == 'record.stopped'
            and not last.get('error'))


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


def encode(frames, destination, fps, ffmpeg):
    command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-xerror',
               '-err_detect', 'explode', '-f', 'image2pipe', '-framerate', str(fps),
               '-vcodec', 'png', '-i', 'pipe:0', '-an',
               '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2',
               '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
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
    if not is_stopped(session):
        print(f'[SKIP] {session}：没有正常停止标记，可能仍在录制或异常退出', flush=True)
        return 'skipped'
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
            encode(frames, destination, args.fps, ffmpeg)
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=(
        '批量将已正常停止的 RC 录制转为 MP4，独立保存 head 首帧为initial.png。'
        '验证后删除 assets 和 images，保留初始状态、视频和其他日志。'))
    parser.add_argument('logs', nargs='?', type=Path, default=DEFAULT_LOGS,
                        help='日志根目录或单个 record_* 会话目录（默认项目 outputs/logs）')
    parser.add_argument('--dry-run', action='store_true', help='仅预览，不写视频或删除文件')
    parser.add_argument('--overwrite', action='store_true', help='重新生成并替换已有同名视频和初始状态图片')
    parser.add_argument('--fps', type=int, default=30, help='视频帧率，默认 30，范围 1–240')
    args = parser.parse_args(argv)
    if not 1 <= args.fps <= 240:
        parser.error('--fps 必须在 1–240 之间')
    try:
        real_directory(args.logs.expanduser())
        root = args.logs.expanduser().resolve()
        ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
        if not args.dry_run and (not ffmpeg or not ffprobe):
            raise ValueError('需要 PATH 中的 ffmpeg 和 ffprobe（FFmpeg 需支持 libx264）')
        counts = dict(converted=0, planned=0, skipped=0, failed=0)
        print(f'日志根目录：{root}', flush=True)
        for session in sessions(root):
            try:
                counts[process_session(session, args, ffmpeg, ffprobe)] += 1
            except (OSError, ValueError, RuntimeError, ZeroDivisionError) as exc:
                counts['failed'] += 1
                print(f'[ERROR] {session}：{exc}', file=sys.stderr, flush=True)
        print('汇总：' + ', '.join(f'{key}={value}' for key, value in counts.items()), flush=True)
        return 1 if counts['failed'] else 0
    except (OSError, ValueError) as exc:
        print(f'[ERROR] {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\n已中断；未完成转换的会话保留 assets 和 images。', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
