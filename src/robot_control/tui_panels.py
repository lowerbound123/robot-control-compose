"""Display-only widgets; acquisition and image encoding live in preview.py."""
from itertools import count
from random import randint

from rich.control import Control
from rich.segment import Segment
from rich.text import Text
from textual.containers import Horizontal, HorizontalScroll, Vertical
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Static

from .graphics import kitty_delete, kitty_placeholder_lines, kitty_upload
from .telemetry import CAMERAS, CAMERA_PREVIEW_HZ, monitor_cells

_IMAGE_IDS = count(randint(1, 2**24 - 1))


class MonitorPanel(HorizontalScroll):
    DEFAULT_CSS = """
    MonitorPanel { display: none; height: 7; border: solid $accent; overflow-y: hidden; }
    #monitor-values { width: 147; height: 4; }
    """

    def __init__(self, settings):
        super().__init__(id="monitor-panel")
        self.settings = settings
        self.border_title = "monitor · qpos rad · epos m/rad · G raw"

    def compose(self):
        yield Static("", id="monitor-values")

    def on_mount(self):
        self.update_snapshot({"qpos": None, "epos": None, "error": "等待反馈"})

    def update_snapshot(self, snapshot):
        rows = monitor_cells(snapshot, self.settings)
        content = Text()
        for row, names in (("qpos", ("J1", "J2", "J3", "J4", "J5", "J6", "G")),
                           ("epos", ("X", "Y", "Z", "Rx", "Ry", "Rz", "G"))):
            content.append(" " * 7)
            for side in ("L", "R"):
                for name in names:
                    content.append(f"{side}.{name: <8}"[:10].ljust(10), style="bold")
            content.append("\n" + row.ljust(7))
            for value, red in zip(rows[row]["text"], rows[row]["red"]):
                if len(value) > 10:
                    value = f"{float(value):.3e}"
                content.append(value.rjust(10), style="red" if red else "")
            if row == "qpos":
                content.append("\n")
        self.query_one("#monitor-values", Static).update(content)
        self.border_subtitle = snapshot.get("error") or ""


class CameraImage(Widget):
    DEFAULT_CSS = """
    CameraImage { height: 1fr; width: 1fr; background: black; color: $text-muted;
                  content-align: center middle; }
    """

    def __init__(self, camera, support, write_protocol, on_error):
        super().__init__(id=f"image-{camera}")
        self.camera = camera
        self.support = support
        self.write_protocol = write_protocol
        self.on_error = on_error
        self.frame = None
        self._frame_id = None
        self._displayed_id = None
        self._retired_ids = set()
        self._sixel_region = None
        self._sixel_paint_pending = False
        self._error_reported = False

    @property
    def pixel_size(self):
        return (self.content_size.width * self.support.cell_size[0],
                self.content_size.height * self.support.cell_size[1])

    def present(self, frame):
        self.frame = frame
        self._frame_id = next(_IMAGE_IDS) % (2**24 - 1) + 1
        self._error_reported = False
        self.refresh()

    def clear(self):
        self.frame = None
        self._frame_id = None
        identities, self._retired_ids = self._retired_ids, set()
        if self._displayed_id is not None:
            identities.add(self._displayed_id)
            self._displayed_id = None
        if identities:
            self.write_protocol("".join(kitty_delete(identity) for identity in sorted(identities)))
        if self._sixel_region is not None:
            region, self._sixel_region = self._sixel_region, None
            # Erase only the cells previously occupied by this image, then let
            # Textual repaint the screen after the layout change.
            erase = "\x1b7" + "".join(
                f"\x1b[{row + 1};{region.x + 1}H\x1b[{region.width}X"
                for row in range(region.y, region.bottom)) + "\x1b8"
            self.write_protocol(erase)
        self.refresh()

    def on_resize(self):
        if self.frame is not None and self.frame.size != self.pixel_size:
            self.clear()

    def render(self):
        return Text("等待图像", justify="center")

    def render_lines(self, crop):
        if self.frame is None or not crop.height or not crop.width:
            return super().render_lines(crop)
        try:
            if not self.screen.is_active:
                return [Strip.blank(crop.width)] * crop.height
            if self.frame.protocol == "kitty":
                cells = (self.content_size.width, self.content_size.height)
                if self._displayed_id != self._frame_id:
                    previous_id = self._displayed_id
                    self.write_protocol(kitty_upload(self.frame, self._frame_id, cells))
                    self._displayed_id = self._frame_id
                    if previous_id is not None:
                        # The terminal still references the old ID until Textual
                        # writes the new placeholders. Retire only after that flush.
                        self._retired_ids.add(previous_id)
                        self.call_after_refresh(self._retire_kitty_image, previous_id)
                rows = kitty_placeholder_lines(self._frame_id, cells)
                return [Strip([Segment(row.text.rstrip("\n"), row.style)], cells[0]).crop(crop.x, crop.right)
                        for row in rows[crop.y:crop.bottom]]
            # Sixel protocol bytes cannot be cropped as text strips. Paint the
            # cached full image after Textual flushes even a partial dirty region.
            if not self._sixel_paint_pending:
                self._sixel_paint_pending = True
                self.call_after_refresh(self._paint_sixel)
            return [Strip([], crop.width)] * crop.height
        except Exception as exc:
            if not self._error_reported:
                self._error_reported = True
                self.app.call_later(self.on_error, str(exc))
            return [Strip.blank(crop.width)] * crop.height

    def _retire_kitty_image(self, identity):
        # clear() may already have reclaimed this image before the callback runs.
        if identity not in self._retired_ids:
            return
        try:
            self.write_protocol(kitty_delete(identity))
            self._retired_ids.discard(identity)
        except Exception as exc:
            if not self._error_reported:
                self._error_reported = True
                self.on_error(str(exc))

    def _paint_sixel(self):
        self._sixel_paint_pending = False
        try:
            # Use current state, never a frame/region captured before a toggle,
            # resize or newer frame. A clipped viewport must not cover neighbours.
            if self.frame is None or not self.is_mounted or not self.screen.is_active:
                return
            region = self.screen.find_widget(self).visible_region
            if region != self.content_region or self.frame.size != self.pixel_size:
                return
            self._sixel_region = region
            self.write_protocol("\x1b7" + Control.move_to(region.x, region.y).segment.text
                                + self.frame.payload + "\x1b8")
        except Exception as exc:
            if not self._error_reported:
                self._error_reported = True
                self.on_error(str(exc))


class CameraPanel(Horizontal):
    DEFAULT_CSS = """
    CameraPanel { display: none; height: 1fr; min-height: 4; border: solid $accent; }
    .camera-tile { width: 1fr; height: 1fr; }
    .camera-state { height: 1; text-align: center; }
    """

    def __init__(self, support, write_protocol, on_error):
        super().__init__(id="camera-panel")
        self.support, self.write_protocol, self.on_error = support, write_protocol, on_error
        self.border_title = f"camera · {support.protocol} · {CAMERA_PREVIEW_HZ} Hz"

    def compose(self):
        for camera in CAMERAS:
            with Vertical(classes="camera-tile"):
                yield CameraImage(camera, self.support, self.write_protocol, self.on_error)
                yield Static(Text(f"{camera} disconnected", style="red"),
                             id=f"camera-status-{camera}", classes="camera-state")

    def sizes(self):
        return {image.camera: image.pixel_size for image in self.query(CameraImage)}

    def update_frames(self, frames):
        for camera, frame in frames.items():
            self.query_one(f"#image-{camera}", CameraImage).present(frame)

    def update_status(self, preview, now):
        for camera in CAMERAS:
            connected = preview.connected(camera, now)
            self.query_one(f"#camera-status-{camera}", Static).update(Text(
                f"{camera} {'connected' if connected else 'disconnected'}",
                style="green" if connected else "red"))
        self.border_subtitle = preview.camera_error or ""

    def clear_images(self):
        for image in self.query(CameraImage):
            image.clear()
