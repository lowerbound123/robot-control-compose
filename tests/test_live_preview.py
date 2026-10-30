import threading
import time
import subprocess
import sys

import numpy as np
from PIL import Image
import pytest

from test_controller import until


def frame(sequence=1, received=None):
    image = np.zeros((12, 16, 3), dtype=np.uint8)
    image.flags.writeable = False
    return {"image": image, "sequence": sequence, "received": time.monotonic() if received is None else received}


def test_preview_deduplicates_frames_and_updates_connection_without_encoding():
    from robot_control.preview import LivePreview
    samples = {"head": frame(received=10.)}
    calls = []
    def prepare(image, size, protocol):
        calls.append((size, threading.get_ident()))
        return size
    preview = LivePreview(lambda: samples.copy(), lambda: {}, prepare, "kitty")
    try:
        preview.set_enabled("camera", True)
        def rendered():
            return preview.poll({"head": (80, 40)})["frames"]
        until(rendered)
        for _ in range(20):
            preview.poll({"head": (80, 40)})
        assert len(calls) == 1 and calls[0][1] != threading.get_ident()
        assert preview.connected("head", 10.999)
        assert not preview.connected("head", 11.)
        samples["head"] = frame(2, 11.1)
        until(rendered)
        assert len(calls) == 2 and preview.connected("head", 11.2)
        # Resize must re-encode even if no new camera frame arrived.
        until(lambda: preview.poll({"head": (100, 50)})["frames"])
        assert len(calls) == 3
    finally:
        preview.close()


def test_slow_encoding_is_bounded_and_late_results_cannot_reopen_panel():
    from robot_control.preview import LivePreview
    gate, entered, completed = threading.Event(), threading.Event(), threading.Event()
    samples = {"head": frame()}
    calls = []
    def slow(image, size, protocol):
        calls.append(size)
        entered.set()
        assert gate.wait(3)
        completed.set()
        return size
    preview = LivePreview(lambda: samples.copy(), lambda: {"qpos": [0] * 14}, slow, "sixel")
    try:
        preview.set_enabled("camera", True)
        preview.set_enabled("monitor", True)
        until(lambda: (preview.poll({"head": (80, 40)}), entered.is_set())[1])
        for seq in range(2, 52):
            samples["head"] = frame(seq)
            preview.poll({"head": (80, 40)})
        assert calls == [(80, 40)]
        # Slow image encoding must not block monitoring or the UI caller.
        until(lambda: preview.poll({"head": (80, 40)})["monitor"] is not None)
        preview.set_enabled("camera", False)
        gate.set()
        # Wait for the superseded encoder to finish before proving its result is
        # still discarded by the next poll; a fixed sleep could pass vacuously.
        assert completed.wait(3)
        assert preview.poll({})["frames"] == {}
        preview.set_enabled("camera", True)
        until(lambda: preview.poll({"head": (100, 50)})["frames"])
        assert calls[-1] == (100, 50)
    finally:
        gate.set()
        preview.close()
    assert preview.poll({})["frames"] == {}


@pytest.mark.parametrize("protocol", ["kitty", "sixel"])
def test_prepare_real_graphics_without_terminal_io(protocol, monkeypatch):
    import base64
    import io
    from robot_control.graphics import prepare_frame
    import textual_image.renderable.tgp as tgp
    monkeypatch.setattr(tgp, "_send_tgp_message", lambda **kwargs: pytest.fail("worker wrote to terminal"))
    prepared = prepare_frame(frame()["image"], (32, 24), protocol)
    assert prepared.size == (32, 24) and prepared.protocol == protocol
    if protocol == "kitty":
        image = Image.open(io.BytesIO(base64.b64decode(prepared.payload)))
        assert image.size == (32, 24)
    else:
        assert prepared.payload.startswith("\x1bP") and prepared.payload.endswith("\x1b\\")


def test_kitty_upload_and_delete_are_scoped_to_owned_image():
    from robot_control.graphics import prepare_frame, kitty_upload, kitty_delete
    prepared = prepare_frame(frame()["image"], (32, 24), "kitty")
    upload = kitty_upload(prepared, 123, (4, 2))
    assert "i=123" in upload and "U=1" in upload and "c=4,r=2" in upload
    assert kitty_delete(123) == "\x1b_Ga=d,d=I,i=123,q=2\x1b\\"


def test_slow_preview_cannot_hold_process_exit():
    result = subprocess.run([sys.executable, "-c", """
import threading
import time
from robot_control.preview import LivePreview
entered = threading.Event()
def slow(*args):
    entered.set()
    threading.Event().wait(30)
p = LivePreview(lambda: {'head': {'image': None, 'sequence': 1, 'received': 0}},
                lambda: {}, slow, 'sixel')
p.set_enabled('camera', True)
while not entered.is_set():
    p.poll({'head': (10, 10)})
    time.sleep(.005)
p.close()
print('closed', flush=True)
"""], capture_output=True, text=True, timeout=3, check=True)
    assert result.stdout.strip() == "closed"
