"""Save captured feedback and original RGB frames without holding control locks."""
from datetime import datetime
import json
from pathlib import Path
import shutil
import tempfile

from .telemetry import CAMERAS


def save_snapshot(payload, frames, directory):
    from PIL import Image

    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(payload["captured_at"]).astimezone().strftime("%Y%m%d_%H%M%S_%f")
    staging = Path(tempfile.mkdtemp(prefix=f".{stamp}-", dir=root))
    destination = root / staging.name[1:]
    try:
        document = {"schema_version": 1, **payload, "images": {}}
        for camera in CAMERAS:
            pixels, timestamp = frames[camera]
            image = Image.fromarray(pixels)
            filename = f"{camera}.png"
            image.save(staging / filename, format="PNG")
            document["images"][camera] = {
                "file": filename, "width": image.width, "height": image.height,
                "timestamp": timestamp, "mode": image.mode,
            }
        (staging / "state.json").write_text(
            json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging)
        raise
    return destination
