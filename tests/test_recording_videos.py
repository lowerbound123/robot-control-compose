"""Exercise the offline archive script against disposable logs and real FFmpeg."""
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/recording_videos.py'


def png(color, width=8, height=6):
    def chunk(kind, data):
        return (struct.pack('>I', len(data)) + kind + data
                + struct.pack('>I', zlib.crc32(kind + data)))
    pixels = b''.join(b'\0' + bytes(color) * width for _ in range(height))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b''))


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class RecordingVideosTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="record videos ' ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def session(self, name='record_one', stopped=True, cameras=('head',)):
        root = self.root / name
        root.mkdir(parents=True)
        events = [{'event': 'record.started'}]
        if stopped:
            events.append({'event': 'record.stopped', 'error': None})
        (root / 'session.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))
        (root / 'state.jsonl').write_text('{"state":"keep"}\n')
        for camera in cameras:
            assets, images = root / 'assets' / camera, root / 'images' / camera
            assets.mkdir(parents=True)
            images.mkdir(parents=True)
            for index, color in ((10, (0, 240, 0)), (2, (240, 0, 0))):
                asset = assets / f'{index}.png'
                asset.write_bytes(png(color))
                os.link(asset, images / f'{index}.png')
            (assets / 'policy-only.png').write_bytes(png((0, 0, 240)))
        return root

    def run_script(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), str(self.root), *args],
                              capture_output=True, text=True, timeout=45)

    def probe(self, path):
        result = subprocess.run(['ffprobe', '-v', 'error', '-count_frames',
                                 '-show_entries', 'stream=nb_read_frames,width,height,avg_frame_rate',
                                 '-of', 'json', str(path)], check=True, capture_output=True, text=True)
        return json.loads(result.stdout)['streams'][0]

    def test_dry_run_does_not_change_any_files(self):
        session = self.session()
        before = {str(p.relative_to(self.root)): p.read_bytes()
                  for p in self.root.rglob('*') if p.is_file()}
        result = self.run_script('--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('head.mp4', result.stdout)
        self.assertIn('initial.png', result.stdout)
        self.assertFalse((session / 'videos').exists())
        after = {str(p.relative_to(self.root)): p.read_bytes()
                 for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_batch_encodes_numeric_order_then_removes_both_image_directories(self):
        sessions = [self.session(cameras=('head', 'left_wrist', 'right_wrist')),
                    self.session('nested/record_two')]
        cameras = {session: [p.name for p in (session / 'images').iterdir()]
                   for session in sessions}
        logs = {p: p.read_bytes() for session in sessions for p in session.glob('*.jsonl')}
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        for session in sessions:
            self.assertFalse((session / 'assets').exists())
            self.assertFalse((session / 'images').exists())
            self.assertTrue((session / 'initial.png').is_file())
            self.assertEqual((session / 'initial.png').read_bytes(), png((240, 0, 0)))
            for camera in cameras[session]:
                video = session / 'videos' / (camera + '.mp4')
                stream = self.probe(video)
                self.assertEqual(stream['nb_read_frames'], '2')
                self.assertEqual(stream['avg_frame_rate'], '30/1')
                decoded = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(video),
                                          '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                                         check=True, capture_output=True).stdout
                self.assertGreater(decoded[0], decoded[1] + 150)
                offset = 8 * 6 * 3
                self.assertGreater(decoded[offset + 1], decoded[offset] + 150)
        for path, data in logs.items():
            self.assertEqual(path.read_bytes(), data)

    def test_running_session_is_not_processed(self):
        session = self.session(stopped=False)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((session / 'assets').exists())
        self.assertTrue((session / 'images/head/2.png').exists())
        self.assertFalse((session / 'videos').exists())
        self.assertIn('SKIP', result.stdout)

    def test_corrupt_camera_preserves_all_assets_and_publishes_no_videos(self):
        session = self.session(cameras=('head', 'left_wrist'))
        # Valid IHDR, broken compressed payload: reaches the real decoder.
        (session / 'images/left_wrist/10.png').write_bytes(png((0, 240, 0))[:40])
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('[ERROR]', result.stderr)
        self.assertTrue((session / 'assets/head/policy-only.png').exists())
        self.assertTrue((session / 'assets/left_wrist/policy-only.png').exists())
        self.assertEqual((session / 'images/head/2.png').read_bytes(), png((240, 0, 0)))
        self.assertTrue((session / 'images/left_wrist/10.png').exists())
        self.assertFalse(list(session.glob('videos/*.mp4')))
        self.assertFalse((session / 'initial.png').exists())
        self.assertFalse(list(session.glob('.recording-videos-*')))

    def test_symlinked_asset_directory_is_not_followed(self):
        session = self.session()
        outside = self.root / 'outside'
        (session / 'assets').rename(outside)
        (session / 'assets').symlink_to(outside, target_is_directory=True)
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('[ERROR]', result.stderr)
        self.assertTrue((outside / 'head/policy-only.png').exists())
        self.assertFalse((session / 'videos').exists())

    def test_existing_video_is_preserved_without_overwrite(self):
        session = self.session()
        (session / 'videos').mkdir()
        target = session / 'videos/head.mp4'
        target.write_bytes(b'original user video')
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_bytes(), b'original user video')
        self.assertTrue((session / 'assets').exists())
        self.assertTrue((session / 'images/head/2.png').exists())
        result = self.run_script('--overwrite')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.probe(target)['nb_read_frames'], '2')
        self.assertFalse((session / 'assets').exists())
        self.assertFalse((session / 'images').exists())

    def test_empty_camera_does_not_discard_policy_only_assets(self):
        session = self.session()
        (session / 'images/left_wrist').mkdir()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('[ERROR]', result.stderr)
        self.assertTrue((session / 'assets').exists())

    def test_odd_dimensions_are_padded_and_custom_fps_is_used(self):
        session = self.session()
        for frame in (session / 'images/head').glob('*.png'):
            frame.write_bytes(png((200, 100, 50), 7, 5))
        result = self.run_script('--fps', '15')
        self.assertEqual(result.returncode, 0, result.stderr)
        stream = self.probe(session / 'videos/head.mp4')
        self.assertEqual((stream['width'], stream['height']), (8, 6))
        self.assertEqual(stream['avg_frame_rate'], '15/1')
        self.assertTrue((session / 'initial.png').is_file())
        self.assertEqual((session / 'initial.png').read_bytes(), png((200, 100, 50), 7, 5))

    def test_unrelated_images_metadata_file_does_not_block_conversion(self):
        session = self.session()
        (session / 'images/.DS_Store').write_bytes(b'finder metadata')
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((session / 'videos/head.mp4').is_file())
        self.assertFalse((session / 'images').exists())

    def test_failed_session_does_not_prevent_other_sessions_from_finishing(self):
        broken = self.session('record_a_bad')
        good = self.session('record_b_good')
        (broken / 'images/head/10.png').write_bytes(b'not a png')
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((broken / 'assets').exists())
        self.assertFalse((good / 'assets').exists())
        self.assertEqual(self.probe(good / 'videos/head.mp4')['nb_read_frames'], '2')

    def test_record_prefixed_archive_directory_is_not_mistaken_for_a_session(self):
        session = self.session('record_archive/record_nested')
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((session / 'videos/head.mp4').is_file())
        self.assertFalse((session / 'assets').exists())

    def test_rerun_after_success_skips_without_changing_video_or_logs(self):
        session = self.session()
        first = self.run_script()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertFalse((session / 'images').exists())
        before = {p: p.read_bytes() for p in session.rglob('*') if p.is_file()}
        second = self.run_script()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn('[SKIP]', second.stdout)
        self.assertEqual({p: p.read_bytes() for p in session.rglob('*') if p.is_file()}, before)

    def test_symlinked_images_directory_is_never_deleted(self):
        session = self.session()
        outside = self.root / 'outside-images'
        (session / 'images').rename(outside)
        (session / 'images').symlink_to(outside, target_is_directory=True)
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('[ERROR]', result.stderr)
        self.assertEqual((outside / 'head/2.png').read_bytes(), png((240, 0, 0)))
        self.assertTrue((session / 'assets').exists())

    def test_initial_state_is_an_independent_lossless_copy_of_first_head_frame(self):
        session = self.session(cameras=('head', 'left_wrist'))
        original = self.root / 'retained-original.png'
        os.link(session / 'images/head/2.png', original)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        initial = session / 'initial.png'
        self.assertTrue(initial.is_file())
        self.assertFalse(initial.is_symlink())
        self.assertEqual(initial.read_bytes(), png((240, 0, 0)))
        self.assertNotEqual(initial.stat().st_ino, original.stat().st_ino)
        original.write_bytes(b'changed original')
        self.assertEqual(initial.read_bytes(), png((240, 0, 0)))

    def test_missing_head_preserves_other_camera_sources(self):
        session = self.session(cameras=('left_wrist',))
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('[ERROR]', result.stderr)
        self.assertTrue((session / 'assets').exists())
        self.assertTrue((session / 'images/left_wrist/2.png').exists())
        self.assertFalse((session / 'videos').exists())

    def test_existing_initial_state_requires_explicit_overwrite(self):
        session = self.session()
        initial = session / 'initial.png'
        initial.write_bytes(b'existing image')
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('[SKIP]', result.stdout)
        self.assertEqual(initial.read_bytes(), b'existing image')
        self.assertTrue((session / 'images').exists())
        result = self.run_script('--overwrite')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(initial.read_bytes(), png((240, 0, 0)))
        self.assertFalse((session / 'images').exists())

    def test_symlinked_initial_state_is_not_replaced_or_followed(self):
        session = self.session()
        original = self.root / 'outside.png'
        original.write_bytes(b'outside image')
        (session / 'initial.png').symlink_to(original)
        result = self.run_script('--overwrite')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('[ERROR]', result.stderr)
        self.assertEqual(original.read_bytes(), b'outside image')
        self.assertTrue((session / 'images').exists())
        self.assertTrue((session / 'assets').exists())


if __name__ == '__main__':
    unittest.main()
