"""Lossless segments, image identity and bounded asynchronous video ownership."""
import json
import shutil
import subprocess
import threading
import time

import numpy as np
import pytest


def frame(value, shape=(7, 9, 3)):
    pixels = np.full(shape, value, np.uint8)
    return {'image': pixels, 'valid': True, 'sequence': value, 'timestamp': float(value)}


def decode(path, width=9, height=7):
    data = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path),
        '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'])
    return np.frombuffer(data, np.uint8).reshape(-1, height, width, 3)


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required')
def test_exact_rgb_150_frame_segments_and_short_tail(tmp_path):
    from robot_control.recording_media import VideoStore
    store = VideoStore(tmp_path)
    refs = []
    try:
        for index in range(153):
            images, pending = store.sample({'head': frame(index % 255)}, index)
            refs.append(images['head'])
            for future in pending: future.result(5)
        store.close(time.monotonic() + 10)
        assert refs[149]['frame_index'] == 149
        assert refs[150]['frame_index'] == 0
        assert refs[149]['file'] != refs[150]['file']
        first, tail = decode(tmp_path / refs[0]['file']), decode(tmp_path / refs[-1]['file'])
        assert len(first) == 150 and len(tail) == 3
        for ref,duration in ((refs[0],5.),(refs[-1],.1)):
            probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_entries',
                'format=duration:stream=codec_name,r_frame_rate','-of','json',str(tmp_path/ref['file'])]))
            assert float(probe['format']['duration'])==duration
            assert probe['streams'][0]['r_frame_rate']=='30/1'
            assert probe['streams'][0]['codec_name']=='ffv1'
        np.testing.assert_array_equal(np.concatenate((first, tail)),
            np.stack([frame(index % 255)['image'] for index in range(153)]))
        manifest = [json.loads(x) for x in (tmp_path / 'video_segments.jsonl').read_text().splitlines()]
        assert [x['frames'] for x in manifest if x.get('event') == 'video.segment'] == [150, 3]
        assert not list(tmp_path.rglob('*.part'))
    finally:
        store.cancel()


def test_missing_frames_black_then_hold_and_observation_pixel_identity(tmp_path):
    from robot_control.recording_media import VideoStore
    store = VideoStore(tmp_path)
    try:
        missing, _ = store.sample({}, 0)
        real, pending = store.sample({'head': frame(23)}, 1)
        repeated, _ = store.sample({}, 2)
        same, duplicate = store.observation({'head': frame(23)})
        leased, _ = store.observation({'head':{'image':frame(23)['image'], 'valid':True,
            'timestamp':999., 'pool_id':'policy-pool','pool_frame_id':'actual-lease'}})
        assert leased['head']['file']==real['head']['file']
        assert leased['head']['timestamp']==999. and leased['head']['pool_frame_id']=='actual-lease'
        assert 'sequence' not in leased['head'] and 'received' not in leased['head']
        extra, extra_pending = store.observation({'head': frame(99)})
        assert same['head']['file'] == real['head']['file']
        assert same['head']['frame_index'] == real['head']['frame_index']
        assert extra['head']['file'] != real['head']['file']
        assert missing['head']['placeholder'] == 'black'
        assert repeated['head']['placeholder'] == 'hold'
        assert missing['head']['missing'] and repeated['head']['missing']
        for future in pending + duplicate + extra_pending: future.result(5)
        store.close(time.monotonic() + 10)
        result = decode(tmp_path / real['head']['file'])
        assert len(result) == 3
        assert np.all(result[0] == 0) and np.all(result[1:] == 23)
        assert np.all(decode(tmp_path / extra['head']['file']) == 99)
    finally:
        store.cancel()


def test_video_pipe_block_does_not_block_text_or_release_credit(tmp_path, monkeypatch):
    from robot_control.recording_media import VideoStream
    from robot_control.recording_writer import SessionFiles
    from test_recording_writer import sample, rows
    entered, release = threading.Event(), threading.Event()
    original = VideoStream._write_pixels

    def blocked(self, pixels):
        entered.set()
        assert release.wait(5)
        return original(self, pixels)

    monkeypatch.setattr(VideoStream, '_write_pixels', blocked)
    session = SessionFiles(tmp_path / 'record_test', 's', {})
    try:
        payload = sample(); payload['frames'] = {'head': frame(7)}
        pending = session.sample(payload)
        assert entered.wait(2)
        assert not all(future.done() for future in pending)
        other = sample(1); other['frames'] = {'head': frame(8)}
        session.sample(other)
        assert len(rows(session.root / 'samples.jsonl')) == 2
        assert len((session.root / 'qpos.csv').read_text().splitlines()) == 3
        assert not list(session.root.rglob('*.mkv'))
    finally:
        release.set()
        session.close()


def test_active_recording_holds_exclusive_session_lock(tmp_path):
    from robot_control.recording_media import SessionLock
    from robot_control.recording_writer import SessionFiles
    session = SessionFiles(tmp_path / 'record_test', 's', {})
    try:
        with pytest.raises(BlockingIOError):
            with SessionLock(session.root): pass
    finally:
        session.close()
    with SessionLock(session.root): pass


def test_resolution_change_and_encoder_crash_never_seal_tail(tmp_path):
    from robot_control.recording_media import VideoStore
    for scenario in ('resize','crash'):
        root = tmp_path/scenario; root.mkdir()
        store = VideoStore(root)
        try:
            _, pending = store.sample({'head':frame(1)},0)
            for future in pending: future.result(5)
            stream = store.streams[('samples','head')]
            if scenario == 'crash':
                stream._process.kill(); stream._process.wait()
                bad = frame(2)
            else: bad = frame(2,(8,9,3))
            _, pending = store.sample({'head':bad},1)
            with pytest.raises(Exception):
                for future in pending: future.result(5)
            with pytest.raises(Exception): store.close(time.monotonic()+2)
            assert not list(root.rglob('*.mkv'))
        finally: store.cancel()


def test_bounded_video_queue_reports_error_and_retains_credits(tmp_path, monkeypatch):
    from robot_control.recording_media import VideoStore, VideoStream
    release, entered = threading.Event(), threading.Event()
    original = VideoStream._write_pixels
    def blocked(self,pixels):
        entered.set(); release.wait(5)
        return original(self,pixels)
    monkeypatch.setattr(VideoStream,'_write_pixels',blocked)
    store=VideoStore(tmp_path)
    try:
        store.sample({'head':frame(1)},0)
        assert entered.wait(2)
        with pytest.raises(RuntimeError,match='queue is full'):
            for index in range(1,140): store.sample({'head':frame(1)},index)
        assert store.stats()['video_pending_bytes'] >= 128*7*9*3
    finally:
        release.set();store.cancel()


def test_video_fsync_failure_does_not_publish_complete_segment(tmp_path,monkeypatch):
    from robot_control import recording_media as media
    store=media.VideoStore(tmp_path)
    try:
        _, pending=store.sample({'head':frame(1)},0)
        for future in pending: future.result(5)
        def failed(fd): raise OSError('disk full')
        monkeypatch.setattr(media.os,'fsync',failed)
        with pytest.raises(OSError,match='disk full'): store.close(time.monotonic()+2)
        assert not list(tmp_path.rglob('*.mkv'))
        index=tmp_path/'video_segments.jsonl'
        assert not index.exists() or not any(json.loads(line)['event']=='video.segment' for line in index.read_text().splitlines())
    finally: store.cancel()


def test_delayed_dimensions_backfill_previous_complete_segment(tmp_path):
    from robot_control.recording_media import VideoStore,segments,decode_segment
    store=VideoStore(tmp_path)
    try:
        for index in range(152):
            _, pending=store.sample({'head':frame(9)} if index==151 else {},index)
            for future in pending: future.result(5)
        store.close(time.monotonic()+5)
        entries=[e for e in segments(tmp_path) if e['camera']=='head']
        assert [e['frames'] for e in entries]==[150,2]
        frames=[rgb for entry in entries for rgb in decode_segment(tmp_path,entry)]
        assert all(rgb==bytes(7*9*3) for rgb in frames[:151])
        assert frames[-1]==bytes([9])*7*9*3
    finally: store.cancel()


def test_three_cameras_odd_rgb_pixels_and_independent_observation_hashes(tmp_path):
    from robot_control.recording_media import VideoStore,segments,decode_segment,pixel_hash
    store=VideoStore(tmp_path,cache_limit=2)
    rng=np.random.default_rng(42)
    originals={camera:rng.integers(0,256,(17,19,3),np.uint8) for camera in ('head','left_wrist','right_wrist')}
    try:
        for index in range(151):
            _,pending=store.sample({name:{'image':pixels,'valid':True} for name,pixels in originals.items()},index)
            for future in pending: future.result(5)
        assert len(store.cache)==2
        store.close(time.monotonic()+5)
        entries=segments(tmp_path)
        for camera,pixels in originals.items():
            rows=[entry for entry in entries if entry['camera']==camera]
            assert [row['frames'] for row in rows]==[150,1]
            for row in rows:
                for rgb in decode_segment(tmp_path,row): assert rgb==pixels.tobytes()
    finally: store.cancel()


def test_missing_ffmpeg_rejects_recording_start_and_removes_partial_session(tmp_path,monkeypatch):
    from robot_control import recording_media
    from robot_control.recording_writer import SessionFiles
    monkeypatch.setattr(recording_media.shutil,'which',lambda executable:None)
    with pytest.raises(RuntimeError,match='requires ffmpeg'):
        SessionFiles(tmp_path/'record_missing','s',{})
    assert not (tmp_path/'record_missing').exists()


def test_crash_preserves_previous_sealed_segment(tmp_path):
    from robot_control.recording_media import VideoStore,segments,decode_segment
    store=VideoStore(tmp_path)
    try:
        for index in range(151):
            _,pending=store.sample({'head':frame(index)},index)
            for future in pending: future.result(5)
        before=segments(tmp_path)
        assert len(before)==1 and before[0]['frames']==150
        stream=store.streams[('samples','head')]
        stream._process.kill();stream._process.wait()
        with pytest.raises(Exception): store.close(time.monotonic()+2)
        assert segments(tmp_path)==before
        assert len(list(decode_segment(tmp_path,before[0])))==150
        assert len(list(tmp_path.rglob('*.mkv')))==1
    finally: store.cancel()


def test_observation_auxiliary_stream_segments_by_image_count(tmp_path):
    from robot_control.recording_media import VideoStore,segments,decode_segment
    store=VideoStore(tmp_path)
    try:
        for index in range(153):
            refs,pending=store.observation({'head':frame(index)})
            assert refs['head']['frame_index']==index%150
            assert refs['head']['timestamp']==float(index)
            for future in pending: future.result(5)
        store.close(time.monotonic()+5)
        rows=segments(tmp_path)
        assert [row['frames'] for row in rows]==[150,3]
        assert all(row['stream']=='observations' for row in rows)
        assert [rgb[0] for row in rows for rgb in decode_segment(tmp_path,row)]==list(range(153))
    finally: store.cancel()


def test_video_index_sync_does_not_block_admission_or_statistics(tmp_path,monkeypatch):
    from robot_control import recording_media as media
    store=media.VideoStore(tmp_path)
    entered,release,admitted=threading.Event(),threading.Event(),threading.Event()
    original=media.os.fsync
    def gated(fd):
        if store._index is not None and fd==store._index.fileno():
            entered.set();assert release.wait(5)
        return original(fd)
    producer=None
    try:
        for index in range(149):
            _,pending=store.observation({'head':frame(index)})
            for future in pending: future.result(5)
        monkeypatch.setattr(media.os,'fsync',gated)
        store.observation({'head':frame(149)})
        assert entered.wait(2)
        def produce():
            store.sample({'head':frame(9)},0)
            store.stats()
            admitted.set()
        producer=threading.Thread(target=produce)
        producer.start()
        assert admitted.wait(.5), 'video fsync blocked text-writer admission/statistics'
    finally:
        release.set()
        if producer is not None: producer.join(5)
        store.close(time.monotonic()+5)
