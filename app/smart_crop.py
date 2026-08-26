"""Suggest a tighter crop box around the component in an LCSC-style product
photo, for generating a more legible thumbnail (the component often occupies
only a small fraction of the frame, surrounded by a ruler grid, a red
crosshair, a blue corner marker and an "LCSC" watermark).

This is a heuristic, not a reliable detector — the ruler's "0cm"/"1cm" labels
are frequently mistaken for the component (similar ink color, similar size).
It's meant to seed a user-adjustable crop, not to run unattended: always show
the user the suggested box and let them confirm or drag it before it's used.
"""
import numpy as np
from PIL import Image

WORK_SIZE = 300
PAD_FRAC = 0.10
MIN_PIXELS = 3
MIN_SOLIDITY = 0.30
OVERSIZE_FRAC = 0.35  # a blob covering more of the frame than this is suspect (probably grid+part merged)


def _connected_components(mask):
    visited = np.zeros_like(mask, dtype=bool)
    h, w = mask.shape
    ys, xs = np.where(mask)
    comps = []
    for sy, sx in zip(ys, xs):
        if visited[sy, sx]:
            continue
        stack = [(sy, sx)]
        visited[sy, sx] = True
        comp = []
        while stack:
            y, x = stack.pop()
            comp.append((y, x))
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
                ny, nx = y + dy, x + dx
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not visited[ny, nx]:
                    visited[ny, nx] = True
                    stack.append((ny, nx))
        comps.append(comp)
    return comps


def _erode(mask, iters=1):
    out = mask.copy()
    for _ in range(iters):
        shifted = out.copy()
        shifted &= np.roll(out, 1, axis=0)
        shifted &= np.roll(out, -1, axis=0)
        shifted &= np.roll(out, 1, axis=1)
        shifted &= np.roll(out, -1, axis=1)
        out = shifted
    return out


def _best_blob(mask):
    best = None
    best_score = -1
    for comp in _connected_components(mask):
        if len(comp) < MIN_PIXELS:
            continue
        ys = [p[0] for p in comp]
        xs = [p[1] for p in comp]
        y0, y1, x0, x1 = min(ys), max(ys), min(xs), max(xs)
        bbox_area = (y1 - y0 + 1) * (x1 - x0 + 1)
        solidity = len(comp) / bbox_area
        if solidity < MIN_SOLIDITY:
            continue
        score = len(comp)
        if score > best_score:
            best_score = score
            best = (y0, y1, x0, x1, bbox_area)
    return best


def _valid_blobs(mask, total_area):
    """All connected components passing the solidity/size/oversize filters
    (unlike _best_blob, which only keeps the single largest one) — needed so
    the caller can apply the position preference across every plausible
    candidate, not just whichever happened to have the most pixels."""
    out = []
    for comp in _connected_components(mask):
        if len(comp) < MIN_PIXELS:
            continue
        ys = [p[0] for p in comp]
        xs = [p[1] for p in comp]
        y0, y1, x0, x1 = min(ys), max(ys), min(xs), max(xs)
        bbox_area = (y1 - y0 + 1) * (x1 - x0 + 1)
        solidity = len(comp) / bbox_area
        if solidity < MIN_SOLIDITY or bbox_area > total_area * OVERSIZE_FRAC:
            continue
        out.append((y0, y1, x0, x1, bbox_area))
    return out


def suggest_crop_box(path):
    """Returns (x0, y0, x1, y1) in the original image's pixel coordinates,
    or None if nothing plausible was found (caller should fall back to no
    crop / the full image)."""
    im = Image.open(path).convert("RGB")
    W, H = im.size
    small = im.resize((WORK_SIZE, WORK_SIZE), Image.BILINEAR)
    arr = np.array(small).astype(np.int16)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]

    is_white = (r > 200) & (g > 200) & (b > 200)
    is_red_cross = (r - g > 50) & (r - b > 30)
    is_corner_blue = (b - r > 150) & (b > 200)
    candidate = ~is_white & ~is_red_cross & ~is_corner_blue

    # Try several erosion levels (0 = none) and pool every plausible blob
    # across all of them, rather than stopping at the first one that merely
    # passes the filters (erosion at one level can reveal a blob that a
    # different level hides). Two more signals, both measured against 194
    # manually-confirmed crops:
    #   - size: the real component is typically 10-20x bigger (by bbox
    #     area) than a "0cm"/"1cm" ruler-label glyph, so prefer the largest
    #     candidate.
    #   - position: LCSC places the component well above the ruler's
    #     crosshair origin — wrong picks (grid/label remnants) cluster
    #     around cy≈0.56 (fraction of image height) while correct ones
    #     cluster around cy≈0.34. So among all candidates, prefer ones
    #     whose vertical center sits in the upper half of the frame, and
    #     only fall back to the full pool if none do.
    total_area = WORK_SIZE * WORK_SIZE
    all_candidates = []
    for iters in (0, 1, 2, 3, 4, 5, 6):
        mask = candidate if iters == 0 else _erode(candidate, iters)
        for cand_blob in _valid_blobs(mask, total_area):
            all_candidates.append((iters, cand_blob))

    if not all_candidates:
        # nothing plausible at any erosion level — fall back to the direct
        # pass's single best blob even if oversize, rather than giving up
        blob = _best_blob(candidate)
        erode_radius = 0
        if blob is None:
            return None
    else:
        upper = [
            (iters, b) for iters, b in all_candidates
            if (b[0] + b[1]) / 2 <= WORK_SIZE * 0.5
        ]
        pool = upper if upper else all_candidates
        erode_radius, blob = max(pool, key=lambda t: t[1][4])

    y0, y1, x0, x1, _bbox_area = blob
    y0, y1 = y0 - erode_radius, y1 + erode_radius
    x0, x1 = x0 - erode_radius, x1 + erode_radius
    pad_y = int((y1 - y0 + 1) * PAD_FRAC) + 3
    pad_x = int((x1 - x0 + 1) * PAD_FRAC) + 3
    y0, y1 = max(0, y0 - pad_y), min(WORK_SIZE - 1, y1 + pad_y)
    x0, x1 = max(0, x0 - pad_x), min(WORK_SIZE - 1, x1 + pad_x)

    sx, sy = W / WORK_SIZE, H / WORK_SIZE
    return (int(x0 * sx), int(y0 * sy), int((x1 + 1) * sx), int((y1 + 1) * sy))
