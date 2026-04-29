"""Brand-logo overlay for cover images.

Composite a brand wordmark onto a generated cover. Designed to read on dark
hero imagery: by default any near-black pixels in the source logo are
recolored to white before pasting, so a black-on-white logo still works on
a navy/teal cover. Anchor points and sizing are configurable.

Used by ``agent_d2.image_generator`` whenever a cover-role placeholder is
resolved AND ``preferences.image_generation.brand_overlay.enabled`` is true.

The function operates in-place on the cover file (overwrites it) and returns
the path back so callers can chain. Failures fall through to the caller as
exceptions — the image_generator decides whether to surface them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_ANCHORS = {
    "bottom_left",
    "bottom_right",
    "bottom_center",
    "top_left",
    "top_right",
    "top_center",
    "center",
}


def _resolve_anchor(
    anchor: str, canvas: tuple[int, int], logo: tuple[int, int], pad_x: int, pad_y: int
) -> tuple[int, int]:
    cw, ch = canvas
    lw, lh = logo
    if anchor == "bottom_left":
        return pad_x, ch - lh - pad_y
    if anchor == "bottom_right":
        return cw - lw - pad_x, ch - lh - pad_y
    if anchor == "bottom_center":
        return (cw - lw) // 2, ch - lh - pad_y
    if anchor == "top_left":
        return pad_x, pad_y
    if anchor == "top_right":
        return cw - lw - pad_x, pad_y
    if anchor == "top_center":
        return (cw - lw) // 2, pad_y
    if anchor == "center":
        return (cw - lw) // 2, (ch - lh) // 2
    raise ValueError(f"unsupported anchor: {anchor!r} (use one of {sorted(_ANCHORS)})")


def apply_overlay(cover_path: Path | str, config: dict[str, Any]) -> Path:
    """Composite the brand logo onto ``cover_path`` in place.

    Required config keys:
      ``logo_path`` — absolute path to the brand image (PNG with alpha).

    Optional config keys (defaults shown):
      ``anchor``                 ``"bottom_left"``
      ``width_ratio``            ``0.18``
      ``padding_ratio_x``        ``0.04``
      ``padding_ratio_y``        ``0.06``
      ``recolor_dark_to_light``  ``True``  — repaint near-black logo pixels white
      ``dark_threshold``         ``60``    — RGB cutoff for "near-black"

    Returns the cover path (now containing the composited image).
    """
    from PIL import Image
    import numpy as np

    cover_path = Path(cover_path)
    logo_path = Path(config["logo_path"]).expanduser()
    if not logo_path.exists():
        raise FileNotFoundError(f"brand logo not found: {logo_path}")

    anchor = config.get("anchor", "bottom_left")
    if anchor not in _ANCHORS:
        raise ValueError(f"unsupported anchor: {anchor!r}")
    width_ratio = float(config.get("width_ratio", 0.18))
    pad_x_ratio = float(config.get("padding_ratio_x", 0.04))
    pad_y_ratio = float(config.get("padding_ratio_y", 0.06))
    recolor = bool(config.get("recolor_dark_to_light", True))
    threshold = int(config.get("dark_threshold", 60))

    cover = Image.open(cover_path).convert("RGBA")
    logo = Image.open(logo_path).convert("RGBA")

    if recolor:
        arr = np.array(logo)
        mask = (
            (arr[..., 0] < threshold)
            & (arr[..., 1] < threshold)
            & (arr[..., 2] < threshold)
            & (arr[..., 3] > 0)
        )
        arr[mask, 0] = 255
        arr[mask, 1] = 255
        arr[mask, 2] = 255
        logo = Image.fromarray(arr, mode="RGBA")

    cw, ch = cover.size
    target_w = max(1, int(cw * width_ratio))
    ratio = target_w / logo.size[0]
    target_h = max(1, int(round(logo.size[1] * ratio)))
    logo_resized = logo.resize((target_w, target_h), Image.LANCZOS)

    pad_x = int(cw * pad_x_ratio)
    pad_y = int(ch * pad_y_ratio)
    pos = _resolve_anchor(anchor, (cw, ch), (target_w, target_h), pad_x, pad_y)

    canvas = cover.copy()
    canvas.alpha_composite(logo_resized, dest=pos)
    canvas.convert("RGB").save(cover_path, format="PNG", optimize=True)
    return cover_path
