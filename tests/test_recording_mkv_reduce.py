"""Real codec migration, lock and deletion boundaries."""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

from robot_control.recording_writer import SessionFiles
from test_recording_writer import sample
from test_recording_media import frame

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/recording_videos.py'


def reduce(root, *args):
    return subprocess.run([sys.executable, str(SCRIPT), str(root), *args], capture_output=True, text=True, timeout=30)


def make(root, count=3):
    session = SessionFiles(root / 'record_mkv', 's', {})
    for index in range(count):
        payload = sample(index)
        payload['frames'] = {} if index == 0 else {camera:frame(index+10) for camera in ('head','left_wrist','right_wrist')}
        for future in session.sample(payload): future.result(5)
    session.frames({'head':frame(99)})
    session.close()
    return session.root


def test_reduce_mkv_keeps_timeline_first_valid_pixels_and_is_idempotent(tmp_path):
    root = make(tmp_path)
    before = {str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    dry = reduce(root, '--dry-run')
    assert dry.returncode == 0, dry.stderr
    assert before == {str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    result = reduce(root)
    assert result.returncode == 0, result.stderr
    assert len(list((root/'videos').glob('*.mp4'))) == 3
    np.testing.assert_array_equal(np.asarray(Image.open(root/'initial.png')), frame(11)['image'])
    for video in (root/'videos').glob('*.mp4'):
        probe = subprocess.check_output(['ffprobe','-v','error','-count_frames','-show_entries','stream=nb_read_frames','-of','json',str(video)])
        assert json.loads(probe)['streams'][0]['nb_read_frames'] == '3'
    assert not list((root/'video_raw').rglob('*.mkv'))
    assert (root/'video_segments.jsonl').exists() and (root/'samples.jsonl').exists()
    again = reduce(root)
    assert again.returncode == 0 and '[SKIP]' in again.stdout


def test_active_lock_skipped_but_abnormal_static_recording_processed(tmp_path):
    session = SessionFiles(tmp_path/'record_active', 's', {})
    try:
        result = reduce(session.root)
        assert result.returncode == 0 and '[SKIP]' in result.stdout, result.stderr
    finally: session.close()
    root = make(tmp_path)
    (root/'session.jsonl').write_text('{"event":"record.started"}\n')
    assert reduce(root).returncode == 0


def test_corrupt_segment_preserves_all_raw_material(tmp_path):
    root = make(tmp_path)
    video = next(root.rglob('*.mkv'))
    video.write_bytes(video.read_bytes()[:50])
    before = {str(p):p.read_bytes() for p in root.rglob('*.mkv')}
    result = reduce(root)
    assert result.returncode == 1
    assert before == {str(p):p.read_bytes() for p in root.rglob('*.mkv')}
    assert not (root/'initial.png').exists()


def test_unsealed_tail_is_reported_and_preserved(tmp_path):
    root = make(tmp_path)
    tail = root/'video_raw/samples/head/000001.mkv.part'
    tail.write_bytes(b'crashed encoder')
    result = reduce(root)
    assert result.returncode == 0, result.stderr
    assert 'unsealed' in result.stdout and tail.read_bytes() == b'crashed encoder'


def test_resume_cleanup_after_cancelled_unlink_verifies_outputs(tmp_path,monkeypatch):
    from argparse import Namespace
    from robot_control.recording_videos import process_session
    import shutil
    root=make(tmp_path)
    original=Path.unlink
    count=0
    def interrupted(self,*args,**kwargs):
        nonlocal count
        if self.suffix=='.mkv':
            count+=1
            if count==2: raise KeyboardInterrupt
        return original(self,*args,**kwargs)
    monkeypatch.setattr(Path,'unlink',interrupted)
    args=Namespace(fps=30,overwrite=False,dry_run=False,crf=28,preset='medium')
    import pytest
    with pytest.raises(KeyboardInterrupt): process_session(root,args,shutil.which('ffmpeg'),shutil.which('ffprobe'))
    monkeypatch.setattr(Path,'unlink',original)
    assert list(root.rglob('*.mkv'))
    receipt=root/'video_reduction.json'
    original_receipt=receipt.read_text()
    broken=json.loads(original_receipt);broken['outputs']={}
    receipt.write_text(json.dumps(broken))
    assert reduce(root).returncode==1
    assert list(root.rglob('*.mkv'))
    receipt.write_text(original_receipt)
    assert reduce(root).returncode==0
    assert not list(root.rglob('*.mkv'))
    (root/'initial.png').write_bytes(b'corrupt published image')
    assert reduce(root).returncode==1
