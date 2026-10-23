"""Prepared terminal graphics. No terminal probing occurs on module import.

The few textual-image private interfaces are isolated here and covered against
the pinned 0.8.5 release. Encoding never writes to the terminal.
"""
from dataclasses import dataclass
import base64
import io
import sys
from types import SimpleNamespace


@dataclass(frozen=True)
class GraphicsSupport:
    protocol: str
    cell_size: tuple[int, int] = (10, 20)
    error: str | None = None


@dataclass(frozen=True)
class PreparedFrame:
    protocol: str
    size: tuple[int, int]
    payload: str


def probe_graphics(protocol):
    """Call before App.run(), while this process still owns terminal input."""
    if not sys.__stdout__ or not sys.__stdout__.isatty():
        return GraphicsSupport(protocol, error="相机预览需要支持所选协议的交互终端")
    try:
        from textual_image import widget  # initializes terminal cell-size cache
        from textual_image._terminal import get_cell_size
        from textual_image.renderable import sixel, tgp
        # Explicit selection; never use widget.Image's automatic fallback.
        backend = {"kitty": tgp, "sixel": sixel}[protocol]
        if not backend.query_terminal_support():
            raise RuntimeError(f"终端不支持配置的 {protocol} 图像协议")
        size = get_cell_size()
        # Also validate both public widget interfaces on the selected release.
        assert widget.TGPImage and widget.SixelImage
        return GraphicsSupport(protocol, (size.width, size.height))
    except Exception as exc:
        return GraphicsSupport(protocol, error=f"相机协议初始化失败：{exc}")


def prepare_frame(rgb, size, protocol):
    from PIL import Image, ImageOps
    if min(size) <= 0:
        raise ValueError("image viewport must be positive")
    image = ImageOps.contain(Image.fromarray(rgb), size, Image.Resampling.BILINEAR)
    canvas = Image.new("RGB", size, "black")
    canvas.paste(image, ((size[0] - image.width) // 2, (size[1] - image.height) // 2))
    if protocol == "sixel":
        from textual_image._sixel import image_to_sixels
        payload = image_to_sixels(canvas)
    elif protocol == "kitty":
        stream = io.BytesIO()
        canvas.save(stream, format="PNG", compress_level=1)
        payload = base64.b64encode(stream.getvalue()).decode("ascii")
    else:
        raise ValueError("unknown graphics protocol")
    return PreparedFrame(protocol, size, payload)


def kitty_upload(frame, identity, cells):
    chunks = []
    for offset in range(0, len(frame.payload), 4096):
        part = frame.payload[offset:offset + 4096]
        more = int(offset + 4096 < len(frame.payload))
        chunks.append(f"\x1b_Gi={identity},f=100,q=2,m={more};{part}\x1b\\")
    chunks.append(f"\x1b_Ga=p,i={identity},c={cells[0]},r={cells[1]},U=1,q=2\x1b\\")
    return "".join(chunks)


def kitty_delete(identity):
    # d=I deletes this ID's placements AND stored image data. I=<id> would
    # instead be an image *number* and is not the ID used during upload.
    return f"\x1b_Ga=d,d=I,i={identity},q=2\x1b\\"


def kitty_placeholder_lines(identity, cells):
    from textual_image.renderable.tgp import Image
    # Reuse the library's Unicode placeholder encoding, without its synchronous
    # PNG encoding/stdout writes or its image-number based cleanup.
    return list(Image._render_diacritics(SimpleNamespace(terminal_image_id=identity), *cells))
