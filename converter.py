import os
import sys
import struct
import fitz  # PyMuPDF
from PIL import Image, ImageEnhance, ImageDraw, ImageFont, ImageOps, ImageFilter, ImageChops
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup, NavigableString
import pyphen
import base64
import customtkinter as ctk
from tkinter import filedialog, messagebox, simpledialog
import threading
import re
import json
import glob
import io
from urllib.parse import unquote
import time
import hashlib
import csv  # Added for AoA parsing
import tempfile


import tempfile
import uuid
import atexit
import shutil
import unicodedata
import zipfile
import posixpath
from collections import Counter

# --- OPTIONAL DEPENDENCIES ---

try:
    from fontTools.ttLib import TTFont

    HAS_FONTTOOLS = True
except ImportError:
    HAS_FONTTOOLS = False

try:
    import wordfreq

    HAS_WORDFREQ = True
except ImportError:
    HAS_WORDFREQ = False

try:
    from openai import OpenAI

    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

# --- CONFIGURATION DEFAULTS ---
DEFAULT_SCREEN_WIDTH = 480
DEFAULT_SCREEN_HEIGHT = 800
DEFAULT_RENDER_SCALE = 3.0
COMIC_MARKER = "xalibre-comic.json"

# --- DEVICE PROFILES (portrait W x H in pixels) ---
# Xteink panels are physically landscape (X3: 792x528, X4: 800x480) and used rotated.
DEVICE_PROFILES = {
    "Xteink X4 (480x800)": (480, 800),
    "Xteink X3 (528x792)": (528, 792),
}
DEFAULT_DEVICE = "Xteink X4 (480x800)"
DEVICES_FILE = None  # set by Xalibre.py -> optional devices.json: {"My device": [w, h]}
ACTIVE_DEVICE_SIZE = (DEFAULT_SCREEN_WIDTH, DEFAULT_SCREEN_HEIGHT)

try:
    import numpy as np
except ImportError:          # Tiers (Smart) and 'Dimmed' peeks need numpy; everything else keeps working
    np = None
from collections import deque

class TierBox(tuple):
    """4-tuple (x0, y0, x1, y1) with optional true gutter boundaries."""
    def __new__(cls, box, gutter_top=None, gutter_bot=None):
        inst = super().__new__(cls, box)
        inst.gutter_top = gutter_top
        inst.gutter_bot = gutter_bot
        return inst


import ctypes

_DITHER_ACCEL = None
try:
    _search_dirs = [os.path.dirname(os.path.abspath(__file__))]
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        _search_dirs.insert(0, sys._MEIPASS)
    for _d in _search_dirs:
        for _name in ("dither_accel.dylib", "dither_accel.so", "dither_accel.dll"):
            _p = os.path.join(_d, _name)
            if os.path.exists(_p):
                _DITHER_ACCEL = ctypes.CDLL(_p)
                break
        if _DITHER_ACCEL is not None:
            break
except Exception:
    _DITHER_ACCEL = None

BAYER_8X8 = None
if np is not None:
    BAYER_8X8 = np.array([
        [ 0, 32,  8, 40,  2, 34, 10, 42],
        [48, 16, 56, 24, 50, 18, 58, 26],
        [12, 44,  4, 36, 14, 46,  6, 38],
        [60, 28, 52, 20, 62, 30, 54, 22],
        [ 3, 35, 11, 43,  1, 33,  9, 41],
        [51, 19, 59, 27, 49, 17, 57, 25],
        [15, 47,  7, 39, 13, 45,  5, 37],
        [63, 31, 55, 23, 61, 29, 53, 21]
    ], dtype=np.float32)

def dither_bayer(img_gray, two_bit=False):
    """Ordered dithering using an 8x8 Bayer matrix (ideal for manga screentones)."""
    if np is None or BAYER_8X8 is None:
        return dither_floyd_steinberg(img_gray, two_bit=two_bit)
    arr = np.array(img_gray, dtype=np.float32)
    h, w = arr.shape
    tile = np.tile(BAYER_8X8, (h // 8 + 1, w // 8 + 1))[:h, :w]
    if two_bit:
        scaled = arr / 85.0 + (tile / 64.0 - 0.5)
        quant_idx = np.clip(np.round(scaled), 0, 3).astype(np.uint8)
        return Image.fromarray(quant_idx * 85, mode="L")
    else:
        thresh = (tile + 0.5) * (255.0 / 64.0)
        out = (arr > thresh).astype(np.uint8) * 255
        return Image.fromarray(out, mode="L")

def dither_atkinson(img_gray, two_bit=False):
    """Atkinson dithering: diffuses 75% error across 6 neighbors, discarding 25%.
    Produces clean whites and sharp text without muddy background specks."""
    w, h = img_gray.size
    if np is not None and _DITHER_ACCEL is not None:
        buf = np.array(img_gray, dtype=np.int16).copy()
        c_buf = buf.ctypes.data_as(ctypes.POINTER(ctypes.c_int16))
        if two_bit:
            _DITHER_ACCEL.atkinson_2bit(c_buf, w, h)
        else:
            _DITHER_ACCEL.atkinson_1bit(c_buf, w, h)
        out_arr = np.clip(buf, 0, 255).astype(np.uint8)
        return Image.fromarray(out_arr, mode="L")

    # Pure Python / numpy fallback
    if np is not None:
        buf = np.array(img_gray, dtype=np.int16)
        for y in range(h):
            has_next_y = (y + 1 < h)
            has_next2_y = (y + 2 < h)
            for x in range(w):
                old_val = buf[y, x]
                if two_bit:
                    v = (old_val + 42) // 85
                    if v < 0: v = 0
                    if v > 3: v = 3
                    new_val = v * 85
                else:
                    new_val = 255 if old_val > 127 else 0
                buf[y, x] = new_val
                err = (old_val - new_val) >> 3
                if err:
                    if x + 1 < w: buf[y, x + 1] += err
                    if x + 2 < w: buf[y, x + 2] += err
                    if has_next_y:
                        if x > 0: buf[y + 1, x - 1] += err
                        buf[y + 1, x] += err
                        if x + 1 < w: buf[y + 1, x + 1] += err
                    if has_next2_y:
                        buf[y + 2, x] += err
        out_arr = np.clip(buf, 0, 255).astype(np.uint8)
        return Image.fromarray(out_arr, mode="L")

    return dither_floyd_steinberg(img_gray, two_bit=two_bit)

def dither_floyd_steinberg(img_gray, two_bit=False):
    """Standard Floyd-Steinberg error-diffusion dithering."""
    if two_bit:
        pal = Image.new("P", (1, 1))
        pal.putpalette([255, 255, 255, 85, 85, 85, 170, 170, 170, 0, 0, 0] + [0] * 756)
        q = img_gray.convert("RGB").quantize(palette=pal, dither=Image.Dither.FLOYDSTEINBERG)
        return q.convert("RGB").convert("L")
    else:
        return img_gray.convert("1", dither=Image.Dither.FLOYDSTEINBERG).convert("L")

def apply_eink_conversion(img_gray, render_mode="Threshold", two_bit=False, thresh=130, invert=False):
    """Unified quantization & dithering for E-ink output (1-bit and 2-bit), with optional inversion."""
    if img_gray.mode != "L":
        img_gray = img_gray.convert("L")

    mode_clean = (render_mode or "Threshold").lower()
    if "atkinson" in mode_clean:
        res = dither_atkinson(img_gray, two_bit=two_bit)
    elif "bayer" in mode_clean or "ordered" in mode_clean:
        res = dither_bayer(img_gray, two_bit=two_bit)
    elif "floyd" in mode_clean or "dither" in mode_clean:
        res = dither_floyd_steinberg(img_gray, two_bit=two_bit)
    else:  # Threshold
        if two_bit:
            shift = 128 - thresh
            res = img_gray.point(lambda p: max(0, min(255, p + shift)))
            res = res.point(lambda p: (p // 64) * 85).convert("L")
        else:
            res = img_gray.point(lambda p: 255 if p > thresh else 0).convert("L")

    if invert:
        res = ImageOps.invert(res)
    return res


def build_hires_text_mask(im_full_res, out_size, downscale_ratio, thresh=130):
    """
    Builds a crisp binary ink mask (text + line-art) at the *original scan
    resolution* instead of on the already-downscaled device canvas.

    By the time the old pipeline thresholded text, LANCZOS had already blurred
    thin scanned strokes away -- this sharpens/thresholds first, while full
    detail is still available, then shrinks the resulting binary mask with an
    ink-preserving filter (any ink in a source block survives as ink) instead
    of plain averaging, so thin strokes don't fade into gray noise and vanish
    before 'Text bolden' or the speech-bubble compositing ever see them.
    """
    if im_full_res.mode != "L":
        im_full_res = im_full_res.convert("L")

    # The unsharp radius is tuned for device-resolution pixels, but scaling it
    # linearly with downscale_ratio makes the sharpening halo huge at full
    # scan resolution (e.g. ratio 4 -> 4px halo on every edge), which darkens
    # a wide band around every stroke and bloats/merges letters once
    # thresholded. A mild sqrt scaling, capped low, keeps edges crisp without
    # inflating stroke width.
    radius = max(1.0, min(2.0, downscale_ratio ** 0.5))
    sharp = im_full_res.filter(ImageFilter.UnsharpMask(radius=radius, percent=130, threshold=0))

    t_val = 148
    delta = thresh - 130
    t_val = max(110, min(200, t_val + delta))
    mask = sharp.point(lambda p: 0 if p < t_val else 255)

    if mask.size == out_size:
        return mask

    # Ink-preserving downsample: invert so ink=255, area-average via BOX, then
    # re-binarize. The cutoff is scaled to the downscale ratio: a genuinely
    # thin (~1px at full res) stroke only covers roughly 1/downscale_ratio of
    # each output box, so the cutoff is set just below that fraction -- thin
    # strokes still survive, but a cutoff near zero (which would mark a box as
    # ink from the faintest sliver, bloating and merging every letter) is
    # avoided.
    inv = ImageOps.invert(mask)
    inv_small = inv.resize(out_size, Image.Resampling.BOX)
    frac_cut = min(0.5, max(0.12, 0.8 / max(1.0, downscale_ratio)))
    cut_val = int(255 * frac_cut)
    small_mask = inv_small.point(lambda p: 255 if p > cut_val else 0)
    return ImageOps.invert(small_mask)


def apply_bolden_to_binary(layer_bin, mode="Off", downscale_ratio=1.0):
    """
    Applies typographic stroke boldening to the binarized text layer.
    - 'Off': Clean, regular weight.
    - 'Light': Directional horizontal dilation (+1px right), thickening vertical stems
      while keeping horizontal crossbars and counter heights untouched.
    - 'Strong': 4-connected cross expansion with counter-hole protection, keeping
      small enclosed letter holes ('O', 'e', 'a', 'R', 'B', '52') open and distinct.
    - 'Auto': Automatically applies 'Light' boldening if downscaled (ratio >= 1.5),
      or keeps 'Off' when near 1:1.

    Every dilation direction is "bridge-guarded": a white pixel is only filled
    in when it is adjacent to ink on one side AND not also adjacent to ink on
    the directly opposite side. Without this guard, a single 1px white pixel
    separating two distinct letters/lines gets filled too, visually fusing
    them together instead of just thickening each stroke.

    Dilation is also stroke-width-aware: each direction only grows outward from
    ink pixels whose local stroke is already thin (estimated via a distance
    transform to the nearest background pixel). Strokes that are already thick
    (bold headers, panel borders, large black shapes) are left alone instead of
    ballooning further -- only the genuinely thin text that needs help gets
    thickened. Falls back to thickening every stroke if scipy is unavailable.
    """
    if mode == "Off" or not mode or np is None:
        return layer_bin

    eff_mode = mode
    if mode == "Auto":
        eff_mode = "Light" if downscale_ratio >= 1.5 else "Off"
        if eff_mode == "Off":
            return layer_bin

    arr = np.array(layer_bin)
    black = (arr == 0)

    # Shifted views answering "is there ink at my immediate neighbor in this direction?"
    black_left = np.zeros_like(black)    # black_left[r, c]  == black[r, c-1]
    black_left[:, 1:] = black[:, :-1]
    black_right = np.zeros_like(black)   # black_right[r, c] == black[r, c+1]
    black_right[:, :-1] = black[:, 1:]
    black_up = np.zeros_like(black)      # black_up[r, c]    == black[r-1, c]
    black_up[1:, :] = black[:-1, :]
    black_down = np.zeros_like(black)    # black_down[r, c]  == black[r+1, c]
    black_down[:-1, :] = black[1:, :]

    # Estimate local stroke width via distance-to-background; only ink pixels
    # thinner than the cutoff are allowed to seed a dilation in their direction.
    width_cutoff = 2.2 if eff_mode == "Light" else 3.2
    thin_source = None
    try:
        from scipy.ndimage import distance_transform_edt
        dist = distance_transform_edt(black)
        stroke_width = dist * 2.0
        thin_source = black & (stroke_width < width_cutoff)
    except Exception:
        thin_source = None  # no scipy: fall back to thickening every stroke

    if thin_source is not None:
        thin_left = np.zeros_like(black); thin_left[:, 1:] = thin_source[:, :-1]
        thin_right = np.zeros_like(black); thin_right[:, :-1] = thin_source[:, 1:]
        thin_up = np.zeros_like(black); thin_up[1:, :] = thin_source[:-1, :]
        thin_down = np.zeros_like(black); thin_down[:-1, :] = thin_source[1:, :]
    else:
        thin_left = thin_right = thin_up = thin_down = np.ones_like(black)

    def safe_fill(near, near_thin, far):
        """White pixels touching ink via `near` become ink, unless ink also sits
        on the directly opposite (`far`) side -- that would bridge a 1px gap
        between two separate strokes and merge them. Only ink that is itself a
        thin stroke (`near_thin`) is allowed to seed the fill."""
        candidate = (~black) & near & near_thin
        return candidate & ~far

    if eff_mode == "Light":
        fill = safe_fill(black_left, thin_left, black_right)
        black_h = black | fill
        return Image.fromarray(np.where(black_h, 0, 255).astype(np.uint8))

    elif eff_mode == "Strong":
        fill_right = safe_fill(black_left, thin_left, black_right)
        fill_up = safe_fill(black_down, thin_down, black_up)
        fill_down = safe_fill(black_up, thin_up, black_down)
        black_cross = black | fill_right | fill_up | fill_down

        try:
            from scipy.ndimage import label
            white_orig = ~black
            labeled_white, _ = label(white_orig)
            counts = np.bincount(labeled_white.ravel())
            small_holes = (counts < 18) & (np.arange(len(counts)) > 0)
            hole_mask = small_holes[labeled_white]
            black_cross[hole_mask] = False
        except Exception:
            pass

        return Image.fromarray(np.where(black_cross, 0, 255).astype(np.uint8))

    return layer_bin


def apply_bubble_masked_eink(canvas, render_mode="Floyd-Steinberg", two_bit=False, thresh=130, bolden="Auto", downscale_ratio=1.0, invert=False, clean_bubbles=True, precomputed_text_mask=None):
    """
    Hybrid E-ink renderer:
    - Layer A: Selected dithering algorithm (Floyd-Steinberg, Atkinson, Bayer) for artwork & screentones.
    - Layer B: High-clarity thresholded dialogue text on pure white paper, eliminating salt-and-pepper dither noise.
    - Uses text-cluster-in-bubble detection to strictly isolate dialogue inside speech bubbles,
      preventing thresholding or boldening from altering artwork, faces, crosshatching, or clothes.
    - 'bolden' controls text stroke weight and connectivity without counter occlusion.
    - 'clean_bubbles' controls whether bubble backgrounds are flattened to pure white paper
      (Layer B fully replaces Layer A inside bubbles) or left as dithered artwork. 'bolden' is
      independent of this: when clean_bubbles is False, only the *extra* ink pixels added by
      boldening are painted onto the dithered background, so strokes thicken without wiping
      out the surrounding dither pattern.
    - 'precomputed_text_mask', if given, is a crisp binary ink mask built at the original scan
      resolution (see build_hires_text_mask) and already downscaled to canvas size with an
      ink-preserving filter. It replaces the low-resolution sharpen+threshold pass below so
      thin strokes don't get lost to LANCZOS blur before thresholding ever sees them.
    """
    if canvas.mode != "L":
        canvas = canvas.convert("L")

    mode_clean = (render_mode or "Threshold").lower()
    if mode_clean in ("threshold", ""):
        if precomputed_text_mask is not None and not two_bit:
            out = precomputed_text_mask
        else:
            out = apply_eink_conversion(canvas, render_mode="Threshold", two_bit=two_bit, thresh=thresh, invert=False)
        if not two_bit:
            # Threshold mode already renders the whole page as pure black/white, so it's
            # safe to bolden strokes across the page (not just inside speech bubbles).
            # Skipped for 2-bit output since it isn't pure binary (4 gray levels), where
            # a naive 0/255 ink check would only catch the darkest level.
            out = apply_bolden_to_binary(out, mode=bolden, downscale_ratio=downscale_ratio)
        if invert:
            out = ImageOps.invert(out)
        return out

    # 1. Layer A: Dithered artwork (uninverted initially)
    layer_art = apply_eink_conversion(canvas, render_mode=render_mode, two_bit=two_bit, thresh=thresh, invert=False)

    # 2. Text threshold selection
    t_val = 148
    delta = thresh - 130
    t_val = max(110, min(200, t_val + delta))

    # 3. Layer B: High-clarity text with sharpened stroke edges
    if precomputed_text_mask is not None:
        layer_text = precomputed_text_mask
    else:
        sharp_text = canvas.filter(ImageFilter.UnsharpMask(radius=1.0, percent=130, threshold=0))
        layer_text = sharp_text.point(lambda p: 0 if p < t_val else 255)

    # Apply directional stroke boldening (Off / Auto / Light / Strong)
    boldened_text = apply_bolden_to_binary(layer_text, mode=bolden, downscale_ratio=downscale_ratio)

    # 4. Speech Bubble Detection with Text-Centric Clustering:
    # A) White background candidates (speech bubble paper)
    local_bg = canvas.filter(ImageFilter.BoxBlur(10))
    is_white_bg = local_bg.point(lambda p: 255 if p > 192 else 0)

    # B) Dark strokes strictly inside white background
    diff = ImageChops.subtract(local_bg, canvas)
    is_stroke = diff.point(lambda p: 255 if p > 25 else 0)
    strokes_in_white = ImageChops.multiply(is_stroke, is_white_bg)

    # C) Text clustering: dialogue characters cluster together in words and lines
    text_cluster = strokes_in_white.filter(ImageFilter.MaxFilter(17))

    # D) Bubble body: broad contiguous white region
    bubble_body = is_white_bg.filter(ImageFilter.MaxFilter(15)).filter(ImageFilter.MinFilter(15))

    # E) Intersect: only text clusters within broad speech bubbles
    bubble_mask = ImageChops.multiply(bubble_body, text_cluster)
    # Erode slightly to prevent threshold bleeding into the bubble's outer black contour line
    bubble_mask = bubble_mask.filter(ImageFilter.MinFilter(3))

    if clean_bubbles:
        # 5. Composite Layer B over Layer A: full clean paper + bolded text inside bubbles.
        out = Image.composite(boldened_text, layer_art, bubble_mask)
    elif bolden and bolden != "Off" and np is not None:
        # Keep the dithered background intact; only paint the extra ink pixels that
        # boldening added (not the original stroke pixels), and only inside detected
        # dialogue text, so the surrounding dither pattern/paper texture is preserved.
        orig_arr = np.array(layer_text) == 0
        bold_arr = np.array(boldened_text) == 0
        mask_arr = np.array(bubble_mask) > 0
        added_ink = bold_arr & (~orig_arr) & mask_arr
        art_arr = np.array(layer_art.convert("L"))
        art_arr[added_ink] = 0
        out = Image.fromarray(art_arr)
    else:
        out = layer_art

    if invert:
        out = ImageOps.invert(out)
    return out


def extract_comic_tiers(pil_img, min_tier_height_pct=0.18, overlap_pct=0.0, rtl=False, is_landscape=False):
    """
    Extracts horizontal comic tiers at real gutters or panel borders.
    - Pre-normalizes contrast so yellowed/gray scans detect gutters reliably.
    - Detects both straight and slightly staggered/slanted horizontal gutters.
    - Penalizes cuts through speech bubbles or dense linework.
    - Balances cut geometry and adapts minimum tier height to Portrait vs Landscape screens.
    """
    orig_w, orig_h = pil_img.size
    if np is None or orig_h < 100 or orig_w < 100 or orig_w > orig_h * 1.15:
        return [TierBox((0, 0, orig_w, orig_h))]

    # Adapt minimum tier height: Portrait screens (tall) benefit from taller tiers (2-3 per page),
    # while Landscape screens (wide) work best with individual panel rows (3-4 per page).
    if min_tier_height_pct == 0.18 and not is_landscape:
        min_tier_height_pct = 0.24

    target_h = 600
    scale = target_h / float(orig_h)
    target_w = max(64, int(orig_w * scale))

    sm = pil_img.convert("L").resize((target_w, target_h), Image.Resampling.BILINEAR)
    arr = np.array(sm, dtype=np.float32)

    mx = max(2, int(target_w * 0.03))
    work = arr[:, mx:target_w - mx]
    w_eff = work.shape[1]

    # 0. Contrast normalization (2nd to 98th percentile) so gray/yellowed scans hit thresholds
    p_lo, p_hi = np.percentile(work, (2, 98))
    if p_hi - p_lo > 30:
        work = np.clip((work - p_lo) * (255.0 / (p_hi - p_lo)), 0.0, 255.0)

    # 1. Detect true gutters (white > 230 or black < 25)
    white_frac = np.mean(work > 230, axis=1)
    black_frac = np.mean(work < 25, axis=1)
    # Pages can mix both: a bright paper gutter between panels, but mostly-dark/black panel
    # interiors (e.g. night scenes, large black negative space) elsewhere on the same page.
    # Picking a single global white-vs-black channel for the whole page can make a real white
    # gutter score as "not a gutter" if black happens to dominate the page overall. Take
    # whichever is blank at each row independently, same as the per-strip check below.
    gutter_frac = np.maximum(white_frac, black_frac)

    # Strip-based gutter check (bridges narrow speech bubbles crossing a gutter)
    n_strips = 16
    sw = max(1, w_eff // n_strips)
    strip_clear = np.zeros((target_h, n_strips), dtype=np.float32)
    for i in range(n_strips):
        s_col = work[:, i * sw : (i + 1) * sw if i < n_strips - 1 else w_eff]
        is_clear = (np.mean(s_col > 230, axis=1) > 0.94) | (np.mean(s_col < 25, axis=1) > 0.94)
        strip_clear[:, i] = is_clear.astype(np.float32)
    clear_ratio = np.mean(strip_clear, axis=1)

    # Staggered / slanted gutter check: allow left & right halves to be offset by up to +-6px
    # Dilating both halves by +-3px places the score peak at the vertical midpoint between them
    half_strips = n_strips // 2
    left_clear = np.mean(strip_clear[:, :half_strips], axis=1)
    right_clear = np.mean(strip_clear[:, half_strips:], axis=1)
    stag_kernel = np.ones(7, dtype=np.float32) / 7.0
    left_dilated = np.convolve(left_clear, stag_kernel, mode='same')
    right_dilated = np.convolve(right_clear, stag_kernel, mode='same')
    staggered_bonus = np.minimum(left_dilated, right_dilated) * 0.35

    # Row-level mask of true full-width blank gutters (no art being crossed)
    is_true_gutter_row = (gutter_frac >= 0.92) | ((gutter_frac >= 0.85) & (clear_ratio >= 0.875))
    gutter_band_bonus = np.convolve(is_true_gutter_row.astype(np.float32), np.ones(5) / 5.0, mode='same') * 0.55

    # 2. Detect sustained horizontal panel border lines
    v_grad = np.abs(np.diff(work, axis=0, prepend=work[:1, :]))
    h_grad = np.abs(np.diff(work, axis=1, prepend=work[:, :1]))
    h_edge_row = np.mean((v_grad > 40) & (h_grad < 12), axis=1)

    # 3. Detail/ink busyness & speech bubble score
    local_detail = np.mean(h_grad > 25, axis=1) + np.mean(v_grad > 25, axis=1)
    busyness = np.convolve(local_detail, np.ones(15) / 15.0, mode='same')
    has_bubble_text = (np.mean(work > 215, axis=1) > 0.18) & (np.mean(work < 70, axis=1) > 0.010)
    bubble_penalty = np.convolve(has_bubble_text.astype(np.float32), np.ones(25) / 25.0, mode='same')
    busyness = busyness + (bubble_penalty * 10.0)

    # Combined panel cut score (penalized by busyness so internal speed-lines/text aren't mistaken for borders)
    real_cut_score = (
            (gutter_frac * 1.6)
            + (clear_ratio * 1.2)
            + (h_edge_row * 2.4)
            + gutter_band_bonus
            + staggered_bonus
            - np.minimum(0.8, busyness * 0.6)
    )

    min_h = int(target_h * min_tier_height_pct)
    ov_px = int(orig_h * overlap_pct)

    # Map cut_y -> (gutter_start_y, gutter_end_y) in scaled coordinates ONLY if a real blank gutter exists
    gutter_spans = {}

    def find_gutter_span(cut_y, radius=8):
        """Finds the contiguous blank gutter band around cut_y, or None if it cuts through art."""
        lo = max(0, cut_y - radius)
        hi = min(target_h - 1, cut_y + radius)
        candidates = [y for y in range(lo, hi + 1) if is_true_gutter_row[y]]
        if not candidates:
            return None
        seed = min(candidates, key=lambda y: abs(y - cut_y))
        g0, g1 = seed, seed
        while g0 > 0 and is_true_gutter_row[g0 - 1]:
            g0 -= 1
        while g1 < target_h - 1 and is_true_gutter_row[g1 + 1]:
            g1 += 1
        return (g0, g1)

    def split_region(y0, y1):
        h = y1 - y0
        if h < min_h * 2:
            return [(y0, y1)]

        s0 = y0 + min_h
        s1 = y1 - min_h
        if s1 <= s0:
            return [(y0, y1)]

        # A. Look for a panel gutter or panel border first, with a slight preference for balanced geometry
        rel_pos = np.linspace(0.0, 1.0, s1 - s0)
        balance_bonus = 0.22 * (1.0 - (2.0 * rel_pos - 1.0) ** 2)
        weighted_scores = real_cut_score[s0:s1] + balance_bonus

        best_rel = int(np.argmax(weighted_scores))
        best_y = s0 + best_rel
        if real_cut_score[best_y] >= 1.75:
            span = find_gutter_span(best_y)
            if span is not None:
                mid_g = (span[0] + span[1]) // 2
                if y0 < mid_g < y1:
                    gutter_spans[mid_g] = span
                    return split_region(y0, mid_g) + split_region(mid_g, y1)
            return split_region(y0, best_y) + split_region(best_y, y1)

        # B. No full panel border found: tall full-page splash split (favors low busyness + any partial border)
        if h > int(target_h * 0.72):
            mid0 = y0 + int(h * 0.30)
            mid1 = y0 + int(h * 0.70)
            splash_cost = busyness[mid0:mid1] - (real_cut_score[mid0:mid1] * 0.15)
            quiet_y = mid0 + int(np.argmin(splash_cost))
            return split_region(y0, quiet_y) + split_region(quiet_y, y1)

        return [(y0, y1)]

    bands = split_region(0, target_h)

    final_boxes = []
    n_bands = len(bands)
    for idx, (ty0, ty1) in enumerate(bands):
        g_top = int(round(gutter_spans[ty0][0] / scale)) if ty0 in gutter_spans else None
        g_bot = int(round(gutter_spans[ty1][1] / scale)) if ty1 in gutter_spans else None

        oy0 = int(ty0 / scale) - (ov_px if idx > 0 else 0)
        oy1 = int(ty1 / scale) + (ov_px if idx < n_bands - 1 else 0)
        oy0 = max(0, oy0)
        oy1 = min(orig_h, oy1)
        if (oy1 - oy0) >= int(orig_h * 0.12):
            final_boxes.append(TierBox((0, oy0, orig_w, oy1), gutter_top=g_top, gutter_bot=g_bot))

    return final_boxes if final_boxes else [TierBox((0, 0, orig_w, orig_h))]


# ----------------------------------------------------------------------
# Internal Helpers (No heavy external dependencies required)
# ----------------------------------------------------------------------

def _merge_boxes(box_list):
    """Merges multiple bounding boxes into an enveloping tier box."""
    min_x = min(b[0] for b in box_list)
    min_y = min(b[1] for b in box_list)
    max_x = max(b[2] for b in box_list)
    max_y = max(b[3] for b in box_list)
    return (min_x, min_y, max_x, max_y)


def _morph_close_rect(binary_mat, kw, kh):
    """Horizontal/rectangular binary closing (dilation then erosion)."""
    try:
        from scipy.ndimage import binary_dilation, binary_erosion
        struct = np.ones((kh, kw), dtype=bool)
        dilated = binary_dilation(binary_mat, structure=struct)
        closed = binary_erosion(dilated, structure=struct)
        return closed.astype(np.uint8)
    except ImportError:
        # Fast Pillow fallback using Min/Max filter approximations
        pil_mask = Image.fromarray(binary_mat * 255, mode="L")
        # Dilate
        dilated = pil_mask.filter(ImageFilter.MaxFilter(size=max(3, kh | 1)))
        # Erode
        closed = dilated.filter(ImageFilter.MinFilter(size=max(3, kh | 1)))
        return (np.array(closed) > 128).astype(np.uint8)


def _label_connected_components(binary_mat):
    """Labels 4-connected components."""
    try:
        from scipy.ndimage import label
        return label(binary_mat)
    except ImportError:
        # Standard BFS component labeling in pure numpy/python
        h, w = binary_mat.shape
        labeled = np.zeros((h, w), dtype=np.int32)
        current_label = 0
        visited = np.zeros((h, w), dtype=bool)

        for y in range(h):
            for x in range(w):
                if binary_mat[y, x] == 1 and not visited[y, x]:
                    current_label += 1
                    queue = deque([(y, x)])
                    visited[y, x] = True
                    labeled[y, x] = current_label

                    while queue:
                        cy, cx = queue.popleft()
                        for dy, dx in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                            ny, nx = cy + dy, cx + dx
                            if 0 <= ny < h and 0 <= nx < w:
                                if binary_mat[ny, nx] == 1 and not visited[ny, nx]:
                                    visited[ny, nx] = True
                                    labeled[ny, nx] = current_label
                                    queue.append((ny, nx))

        return labeled, current_label

def load_custom_devices():
    """Merge user-defined devices from devices.json ({"Name": [w, h]}) into DEVICE_PROFILES."""
    if not DEVICES_FILE or not os.path.exists(DEVICES_FILE):
        return
    try:
        with open(DEVICES_FILE, "r", encoding="utf-8") as f:
            for name, wh in json.load(f).items():
                w, h = int(wh[0]), int(wh[1])
                if w > 0 and h > 0:
                    DEVICE_PROFILES[str(name)] = (min(w, h), max(w, h))  # stored portrait
    except Exception as e:
        print(f"devices.json ignored: {e}")


def get_device_size(name):
    return DEVICE_PROFILES.get(name, DEVICE_PROFILES[DEFAULT_DEVICE])


def set_active_device(name):
    """Remember the selected device size for code outside the renderer (covers etc.)."""
    global ACTIVE_DEVICE_SIZE
    ACTIVE_DEVICE_SIZE = get_device_size(name)

# --- FACTORY DEFAULTS ---
FACTORY_DEFAULTS = {
    "device": DEFAULT_DEVICE,
    # --- COMIC / MANGA ---
    "comic_crop": "Auto",          # Off | Auto | Aggressive
    "comic_crop_pad": 1.5,         # % of page size kept around detected content
    "comic_spreads": "Split",      # Split | Full+Split | Rotate | Fit   (landscape / double pages)
    "comic_zoom": "Off",           # Off | Tiers (Smart) | 2 (top/bottom) | 3 (rows) | 4 (2x2)
    "comic_overlap": 6,            # % overlap between zoom parts
    "comic_rtl": "Auto",           # Auto | Left-to-right | Right-to-left
    "comic_fit": "Fit",            # Fit | Fill
    "comic_peek_mode": "Full Art", # Full Art | Dimmed | Off (Smart Tiers screen-fill context)
    "comic_fill_tiers": True,      # Backward-compatible alias for comic_peek_mode != "Off"
    "comic_scrollbar": "Right",    # Off | Right | Left (show page position indicator when zoomed)
    "comic_gamma": 1.0,
    "comic_sharpen": 0.6,
    "comic_autolevels": True,
    "comic_page_numbers": False,
    "comic_bolden": "Off",         # Off | Auto | Light | Strong
    "comic_clean_bubbles": False,  # Sharp speech bubbles via threshold mask
    "toc_insert_page": 1,
    "font_size": 28,
    "font_weight": 400,
    "line_height": 1.4,
    "paragraph_spacing": 0.0,
    "word_spacing": 0.0,
    "margin": 20,
    "top_padding": 15,
    "bottom_padding": 32,
    "orientation": "Portrait",
    "keep_cover_portrait": True,
    "text_align": "justify",
    "font_name": "",
    "preview_zoom": 380,
    "generate_toc": True,
    "show_footnotes": False,
    "hyphenate_text": True,

    # --- SPECTRA AI ANNOTATIONS ---
    "spectra_enabled": False,
    "spectra_threshold": 3.8,  # Zipf score
    "spectra_aoa_threshold": 10.0,  # NEW: AoA Threshold (0 = disabled)
    "spectra_api_key": "",
    "spectra_base_url": "https://api.openai.com/v1",
    "spectra_model": "gpt-4o-mini",
    "spectra_language": "en",

    # --- ELEMENT VISIBILITY & POSITION ---
    "pos_title": "Footer",
    "pos_pagenum": "Footer",
    "pos_chap_page": "Hidden",
    "pos_percent": "Hidden",

    # --- ELEMENT ORDERING ---
    "order_pagenum": 1,
    "order_title": 2,
    "order_chap_page": 3,
    "order_percent": 4,
    "order_progress": 5,

    # --- PROGRESS BAR POSITION ---
    "pos_progress": "Footer (Below Text)",

    # --- PROGRESS BAR STYLING ---
    "bar_height": 4,
    "bar_show_marker": True,
    "bar_marker_color": "Black",
    "bar_marker_radius": 5,
    "bar_show_ticks": True,
    "bar_tick_height": 6,

    # --- HEADER/FOOTER STYLING ---
    "header_font_size": 16,
    "header_align": "Center",
    "header_margin": 10,
    "footer_font_size": 16,
    "footer_align": "Center",
    "footer_margin": 10,
    "ui_font_source": "Body Font",
    "ui_separator": "   |   ",
    "ui_side_margin": 15,

    # --- RENDER MODES ---
    "bit_depth": "1-bit (XTG)",
    "render_mode": "Threshold",
    "invert_colors": False,
    "white_clip": 220,
    "contrast": 1.2,
    "text_threshold": 130,
    "text_blur": 1.0,
}

if getattr(sys, 'frozen', False):
    if sys.platform == 'darwin':
        EXTERNAL_DIR = os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', 'Xalibre')
        os.makedirs(EXTERNAL_DIR, exist_ok=True)
    else:
        EXTERNAL_DIR = os.path.dirname(sys.executable)
    INTERNAL_DIR = sys._MEIPASS
else:
    EXTERNAL_DIR = os.path.dirname(os.path.abspath(__file__))
    INTERNAL_DIR = EXTERNAL_DIR

# --- EXTERNAL FILES (User can edit these or drop files here) ---
SETTINGS_FILE = os.path.join(EXTERNAL_DIR, "default_settings.json")
PRESETS_DIR = os.path.join(EXTERNAL_DIR, "presets")

# --- INTERNAL FILES (Bundled inside the .exe so you don't have to distribute them) ---
AOA_FILE = os.path.join(INTERNAL_DIR, "AoA_51715_words.csv")

# Global AoA Database
AOA_DB = {}


def load_aoa_database():
    """Loads the Kuperman AoA CSV into a global dict."""
    global AOA_DB
    if not os.path.exists(AOA_FILE):
        print(f"AoA file not found: {AOA_FILE}")
        return

    try:
        with open(AOA_FILE, 'r', encoding='utf-8', errors='ignore') as f:
            reader = csv.DictReader(f)

            # Check required columns
            # Note: We now look for the lemmatized column as a fallback
            if 'Word' not in reader.fieldnames:
                print("AoA CSV missing 'Word' column.")
                return

            count = 0
            for row in reader:
                word = row['Word'].strip().lower()

                # 1. Try exact word AoA first
                val = row.get('AoA_Kup')

                # 2. If empty, try Lemmatized AoA (Fallback for plurals like 'dinners')
                if not val or val.lower() == 'na':
                    val = row.get('AoA_Kup_lem')

                try:
                    if val and val.lower() != 'na':
                        AOA_DB[word] = float(val)
                        count += 1
                except ValueError:
                    continue

            # print(f"Loaded {count} words from AoA database.")
    except Exception as e:
        print(f"Error loading AoA CSV: {e}")


# --- SPECTRA ANNOTATOR CLASS (PHRASE AWARE) ---
class SpectraAnnotator:
    def __init__(self, api_key, base_url, model, threshold=4.0, aoa_threshold=0.0, language='en',
                 target_lang='English'):
        self.enabled = HAS_WORDFREQ and HAS_OPENAI and bool(api_key)
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.threshold = threshold  # Zipf Threshold (Upper bound)
        self.aoa_threshold = aoa_threshold  # AoA Threshold (Lower bound)
        self.language = language
        self.target_lang = target_lang
        self.client = None
        # Cache Key: "word|context_hash" -> Value: "Definition"
        self.master_cache = {}

        if self.enabled:
            try:
                self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)
            except Exception as e:
                print(f"Spectra Init Error: {e}")
                self.enabled = False

    def get_difficulty(self, word):
        if not HAS_WORDFREQ: return 10.0
        return wordfreq.zipf_frequency(word, self.language)

    def get_aoa(self, word):
        """Returns Age of Acquisition or 0 if not found."""
        return AOA_DB.get(word, 0.0)

    def fetch_definitions_batch(self, word_context_map, force=False):
        if not word_context_map or not self.client: return {}

        # Filter what needs fetching
        if force:
            to_fetch = word_context_map
        else:
            to_fetch = {k: ctx for k, ctx in word_context_map.items() if k not in self.master_cache}

        if not to_fetch: return {}

        # --- 1. PREPARE INPUT & RECOVERY MAPS ---
        items_str = ""

        # RECOVERY MAPS
        reverse_lookup = {}  # "word" -> ["word|hash1", "word|hash2"]
        hash_lookup = {}  # "hash" -> "word|hash"
        ordered_keys = []  # ["word|hash", "word|hash"] (for index recovery 1, 2, 3...)

        for i, (unique_key, context) in enumerate(to_fetch.items()):
            target_word, ctx_hash = unique_key.split('|')

            # Map 1: Simple Word Recovery
            if target_word not in reverse_lookup:
                reverse_lookup[target_word] = []
            reverse_lookup[target_word].append(unique_key)

            # Map 2: Hash Recovery (Fixes "Orphaned Response: '3e8cdd'")
            hash_lookup[ctx_hash] = unique_key

            # Map 3: Index Recovery (Fixes "Orphaned Response: '1'")
            ordered_keys.append(unique_key)

            clean_context = context.replace('"', "'").replace('\n', ' ').strip()[:300]

            # We use a 1-based index in the prompt to match likely AI numbering
            items_str += (
                f"Item {i + 1}:\n"
                f"ID: {unique_key}\n"
                f"WORD: {target_word}\n"
                f"CONTEXT: {clean_context}\n"
                f"---\n"
            )

        # --- LANGUAGE & PROMPT SETUP (Kept largely the same, just tightened) ---
        lang_map = {
            "English": "en", "Spanish": "es", "French": "fr",
            "German": "de", "Italian": "it", "Polish": "pl",
            "Portuguese": "pt", "Russian": "ru", "Chinese": "zh", "Japanese": "ja"
        }
        target_code = lang_map.get(self.target_lang, "en")
        raw_lang = self.language.lower().strip()

        # ... (Language Code Logic omitted for brevity, it was fine) ...
        # Assume is_translation logic is here
        is_translation = (
                target_code != 'en' and self.language.startswith('en') == False)  # Simplified check for snippet

        if self.target_lang != "English" and not self.language.lower().startswith(target_code):
            is_translation = True

        # Determine Mode
        if is_translation:
            field_name = "translation"
            system_role = f"You are a professional translator. Output JSON only. Answer in {self.target_lang}."
            task_instruction = f"TASK: TRANSLATE 'WORD' accurately into {self.target_lang} based on 'CONTEXT'."
        else:
            field_name = "synonym"
            system_role = f"You are a Thesaurus. Output JSON only. Answer in {self.target_lang}."
            task_instruction = f"TASK: Provide a simpler SYNONYM or SHORT PHRASE for 'WORD' in {self.target_lang}."

        json_schema = {
            "name": "dictionary_response",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "entries": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                field_name: {"type": "string"}
                            },
                            "required": ["id", field_name],
                            "additionalProperties": False
                        }
                    }
                },
                "required": ["entries"],
                "additionalProperties": False
            }
        }

        prompt = (
            f"{task_instruction}\n"
            f"RULES:\n"
            f"1. Return the EXACT ID provided in the input.\n"
            f"2. Keep definitions short (1-3 words).\n"
            f"3. Output valid JSON.\n"
            f"\n"
            f"INPUT LIST:\n"
            f"{items_str}"
        )

        try:
            # 4. SEND TO API
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_role},
                    {"role": "user", "content": prompt}
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": json_schema
                },
                temperature=0.2
            )

            content = response.choices[0].message.content.strip()
            entries_found = []

            # A. Try Standard JSON
            try:
                clean = re.sub(r'^```json\s*', '', content)
                clean = re.sub(r'\s*```$', '', clean)
                json_data = json.loads(clean)
                if "entries" in json_data:
                    entries_found = json_data["entries"]
            except json.JSONDecodeError:
                pass

            # B. Regex Fallback (if JSON fails)
            if not entries_found:
                # Matches: "id": "ANYTHING", "synonym": "ANYTHING"
                pattern = r'"id"\s*:\s*"([^"]+)"\s*,\s*"(?:synonym|definition|translation|' + field_name + r')"\s*:\s*"([^"]+)"'
                matches = re.findall(pattern, content, flags=re.IGNORECASE)
                entries_found = [{"id": m[0], field_name: m[1]} for m in matches]

            for entry in entries_found:
                raw_id = str(entry.get("id", "")).strip()

                # --- RECOVERY LOGIC START ---

                # 1. Normalize ID (Remove "ID:", "Item", and extra spaces)
                # This fixes "ITEM 1" -> "1"
                clean_id_str = re.sub(r'^(ID:|Item)\s*', '', raw_id, flags=re.IGNORECASE).strip()

                # 2. Junk Filter (Instruction Echo Fix)
                if len(clean_id_str) > 40 and " " in clean_id_str:
                    # print(f"[Spectra DEBUG] 🗑️ Discarding junk ID: '{clean_id_str[:30]}...'")
                    continue

                final_key = None

                # --- STRATEGY A: Exact Match ---
                if raw_id in to_fetch:
                    final_key = raw_id

                # --- STRATEGY B: Number/Index Match ---
                # Fixes "Item 1", "ITEM 1", "1"
                elif clean_id_str.isdigit():
                    idx = int(clean_id_str) - 1
                    if 0 <= idx < len(ordered_keys):
                        final_key = ordered_keys[idx]

                # --- STRATEGY C: Hash Extraction ---
                # Fixes "word | hash" (spaces) or "gratfully|hash" (typos)
                elif '|' in raw_id:
                    possible_hash = raw_id.split('|')[-1].strip()
                    if possible_hash in hash_lookup:
                        final_key = hash_lookup[possible_hash]

                # --- STRATEGY D: Bare Hash Match ---
                # Fixes "3e8cdd"
                elif raw_id in hash_lookup:
                    final_key = hash_lookup[raw_id]

                # --- STRATEGY E: Word Fallback ---
                # Fixes cases where hash is missing but word is correct
                elif raw_id in reverse_lookup:
                    for complex_key in reverse_lookup[raw_id]:
                        val = entry.get(field_name) or entry.get("definition") or entry.get("synonym") or entry.get(
                            "translation")
                        if val:
                            clean_val = val.strip().rstrip('.,;!')
                            if self.target_lang != "German": clean_val = clean_val.lower()
                            if clean_val.lower() != raw_id.lower():
                                self.master_cache[complex_key] = clean_val
                    continue

                # --- ASSIGNMENT ---
                if final_key:
                    val = entry.get(field_name) or entry.get("definition") or entry.get("synonym") or entry.get(
                        "translation")
                    if val:
                        clean_val = val.strip().rstrip('.,;!')
                        if self.target_lang != "German": clean_val = clean_val.lower()

                        # Prevent circular definitions
                        original_word = final_key.split('|')[0]
                        if clean_val.lower() != original_word.lower():
                            self.master_cache[final_key] = clean_val

                # Only log genuine errors now
                elif len(raw_id) < 20:
                    print(f"[Spectra DEBUG] ❌ Orphaned Response: '{raw_id}'")

        except Exception as e:
            print(f"Spectra API Error: {e}")

    def analyze_chapters(self, chapters_list, selected_indices, progress_callback=None, force=False):
        if not self.enabled: return

        word_pattern = re.compile(r'\b[a-zA-Z]{3,}\b')
        split_pattern = r'(?<!\bMr)(?<!\bMrs)(?<!\bDr)(?<!\bMs)(?<!\bSt)(?<!\bProf)(?<!\bCapt)(?<!\bGen)(?<!\bSen)(?<!\bRev)(?<=[.!?])\s+'

        batch_items = {}

        total = len(selected_indices)
        for i, idx in enumerate(selected_indices):
            if progress_callback: progress_callback(0.1 + (i / total) * 0.4)

            soup = chapters_list[idx]['soup']
            full_text = soup.get_text(" ", strip=True)
            sentences = re.split(split_pattern, full_text)

            for sentence in sentences:
                sentence = sentence.strip()
                if not sentence: continue

                # Create a hash of the specific sentence context
                ctx_hash = hashlib.md5(sentence.encode('utf-8')).hexdigest()[:6]

                for match in word_pattern.finditer(sentence):
                    word_val = match.group()
                    start_index = match.start()
                    w_lower = word_val.lower()

                    # --- SMART PROPER NOUN FILTER ---
                    if word_val[0].isupper():
                        # 1. If in middle of sentence, it's a Name/Place -> Skip
                        if start_index > 0:
                            continue

                        # 2. If at start, check if it's a Name using Frequency Comparison
                        # "London" (high freq) vs "london" (low freq) -> Name -> SKIP
                        # "Apple" (low freq) vs "apple" (high freq) -> Noun -> KEEP
                        if HAS_WORDFREQ:
                            freq_cap = wordfreq.zipf_frequency(word_val, self.language)
                            freq_low = wordfreq.zipf_frequency(w_lower, self.language)

                            # If the Capitalized version is more common or equal, it's likely a Name
                            if freq_cap >= freq_low:
                                continue

                            # Extra safety: If lowercase word essentially doesn't exist
                            if freq_low == 0:
                                continue
                    # --------------------------------

                    zipf_score = self.get_difficulty(w_lower)

                    # 1. Zipf Check
                    is_candidate = (1.5 < zipf_score < self.threshold)

                    # 2. AoA Check
                    if is_candidate and self.aoa_threshold > 0.5 and self.language == 'en':
                        aoa_val = self.get_aoa(w_lower)
                        if aoa_val > 0 and aoa_val < self.aoa_threshold:
                            is_candidate = False

                    if is_candidate:
                        unique_key = f"{w_lower}|{ctx_hash}"
                        if force or unique_key not in self.master_cache:
                            batch_items[unique_key] = sentence

        if batch_items:
            items = list(batch_items.items())
            is_local = "localhost" in self.base_url or "127.0.0.1" in self.base_url
            batch_size = 15 if is_local else 40
            sleep_time = 0.1 if is_local else 4.5
            total_batches = (len(items) + batch_size - 1) // batch_size

            for b_idx, i in enumerate(range(0, len(items), batch_size)):
                if progress_callback:
                    pct = 0.5 + (b_idx / total_batches) * 0.5
                    progress_callback(pct)
                batch = dict(items[i:i + batch_size])
                self.fetch_definitions_batch(batch, force=force)
                time.sleep(sleep_time)

    def get_ordered_annotations(self, soup):
        if not self.master_cache: return {}
        ordered_defs = {}
        full_text = soup.get_text(" ", strip=True)
        split_pattern = r'(?<!\bMr)(?<!\bMrs)(?<!\bDr)(?<!\bMs)(?<!\bSt)(?<!\bProf)(?<!\bCapt)(?<!\bGen)(?<!\bSen)(?<!\bRev)(?<=[.!?])\s+'
        sentences = re.split(split_pattern, full_text)
        word_pattern = re.compile(r'\b[a-zA-Z]{3,}\b')

        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence: continue
            ctx_hash = hashlib.md5(sentence.encode('utf-8')).hexdigest()[:6]

            matches = word_pattern.findall(sentence)
            for w in matches:
                w_lower = w.lower()
                unique_key = f"{w_lower}|{ctx_hash}"
                if unique_key in self.master_cache:
                    if w_lower not in ordered_defs:
                        ordered_defs[w_lower] = []
                    ordered_defs[w_lower].append(self.master_cache[unique_key])
        return ordered_defs


# --- UTILITY FUNCTIONS ---
def compress_font_size(val, max_em=1.4):
    """Gradually compresses oversized font declarations so hierarchy is preserved

    without viewport blowouts.
    """
    if val <= 1.0:
        return val
    if val <= max_em:
        # Mild compression for modest headings/subtitles
        return 1.0 + (val - 1.0) * 0.75
    # Strong diminishing returns above max_em
    return 1.0 + (max_em - 1.0) * 0.75 + (val - max_em) * 0.15


def clamp_css_font_sizes(css_text, max_em=1.4):
    def replacer(match):
        try:
            val = float(match.group(1))
            unit = match.group(2).lower()

            if unit in ["em", "rem"]:
                new_val = compress_font_size(val, max_em)
                return f"font-size: {new_val:.2f}{unit}"
            elif unit == "%":
                em_val = val / 100.0
                new_val = compress_font_size(em_val, max_em)
                return f"font-size: {new_val * 100:.0f}%"
            elif unit in ["pt", "px"]:
                em_val = val / 16.0
                new_val = compress_font_size(em_val, max_em)
                return f"font-size: {new_val:.2f}em"
            elif unit in ["vh", "vw"]:
                # 1vh na ekranie 800px to ok. 8px (~0.5em)
                em_val = (val * 8.0) / 16.0
                new_val = compress_font_size(em_val, max_em)
                return f"font-size: {new_val:.2f}em"
        except (ValueError, TypeError):
            pass
        return match.group(0)

    pattern = re.compile(
        r"font-size\s*:\s*([\d\.]+)\s*(em|rem|%|pt|px|vh|vw)",
        flags=re.IGNORECASE,
    )
    return pattern.sub(replacer, css_text)


def fix_css_font_paths(css_text, target_font_family="'CustomFont'"):
    # If None, keep book CSS font-family rules intact!
    if target_font_family is None:
        # Only clean out broken relative url(...) inside existing @font-face,
        # or strip existing @font-face since we inject absolute paths ourselves.
        css_text = re.sub(r'@font-face\s*\{[^}]+\}', '', css_text, flags=re.IGNORECASE | re.DOTALL)
        return css_text

    # 1. Remove all @font-face blocks to prevent MuPDF errors about missing relative files
    css_text = re.sub(r'@font-face\s*\{[^}]+\}', '', css_text, flags=re.IGNORECASE | re.DOTALL)

    # 2. Force the custom font family on all elements
    css_text = re.sub(r'font-family\s*:\s*[^;!}]+', f'font-family: {target_font_family}', css_text, flags=re.IGNORECASE)

    return css_text


_FONT_META_CACHE = {}


def font_meta(path):
    """Read family / weight / italic / variable from the font's own tables
    (never from the filename). Returns dict or None."""
    if not HAS_FONTTOOLS or not path:
        return None
    key = os.path.abspath(path)
    if key in _FONT_META_CACHE:
        return _FONT_META_CACHE[key]
    meta = None
    try:
        f = TTFont(path, lazy=True, fontNumber=0)
        try:
            n = f['name']
            family = (n.getDebugName(16) or n.getDebugName(1) or "").strip()
            weight, italic = 400, False
            if 'OS/2' in f:
                weight = int(f['OS/2'].usWeightClass or 400)
                italic = bool(f['OS/2'].fsSelection & 1)
            if 'head' in f and (f['head'].macStyle & 2):
                italic = True
            if 'post' in f and getattr(f['post'], 'italicAngle', 0):
                italic = True
            # Legacy RIBBI naming: family (ID1) + subfamily (ID2) carry Bold
            sub = (n.getDebugName(2) or "").lower()
            if 'OS/2' not in f and 'bold' in sub:
                weight = 700
            meta = {
                "family": family,
                "weight": weight,
                "italic": italic,
                "variable": 'fvar' in f,
            }
        finally:
            f.close()
    except Exception:
        meta = None
    _FONT_META_CACHE[key] = meta
    return meta


def _norm_family(name):
    return re.sub(r'[^a-z0-9]', '', (name or "").lower())


def css_weight_keyword(weight):
    """MuPDF's CSS reliably understands only normal/bold."""
    return "bold" if weight >= 600 else "normal"


def classify_faces(face_paths):
    """face_paths: iterable of file paths belonging to ONE family.
    Returns {'regular','italic','bold','bold_italic'} -> path (or None),
    chosen by real weight/italic flags."""
    faces = []
    for p in face_paths:
        m = font_meta(p)
        if m:
            faces.append((p.replace("\\", "/"), m))

    def best(cands, target):
        if not cands:
            return None
        return sorted(cands, key=lambda c: (abs(c[1]["weight"] - target),
                                            c[1]["variable"]))[0][0]

    upright = [c for c in faces if not c[1]["italic"]]
    slanted = [c for c in faces if c[1]["italic"]]
    res = {
        # regular must not be a bold/heavy face -> only consider <= 500
        "regular": best([c for c in upright if c[1]["weight"] <= 500], 400)
                   or best(upright, 400),
        "italic": best([c for c in slanted if c[1]["weight"] <= 500], 400),
        "bold": best([c for c in upright if c[1]["weight"] >= 600], 700),
        "bold_italic": best([c for c in slanted if c[1]["weight"] >= 600], 700),
    }
    # Variable font fallback for missing styles
    vf = next((p for p, m in faces if m["variable"]), None)
    if vf:
        for k in res:
            if not res[k]:
                res[k] = vf
    return res


def get_font_variants(font_path):
    if not font_path or not os.path.exists(font_path):
        return {}
    directory = os.path.dirname(font_path)
    try:
        all_files = [f for f in os.listdir(directory) if f.lower().endswith((".ttf", ".otf"))]
    except:
        return {}

    # ---- Preferred path: real metadata, restricted to the SAME family ----
    base = font_meta(font_path)
    if base and base["family"]:
        fam = _norm_family(base["family"])
        same = []
        for f in all_files:
            full = os.path.join(directory, f)
            m = font_meta(full)
            if m and _norm_family(m["family"]) == fam:
                same.append(full)
        if same:
            res = classify_faces(same)
            if not res["regular"]:
                res["regular"] = font_path.replace("\\", "/")
            return res

    # ---- Fallback (no fontTools / unreadable): filename heuristics ----
    candidates = {"regular": [], "italic": [], "bold": [], "bold_italic": []}
    variable_fonts = []
    for f in all_files:
        full_path = os.path.join(directory, f).replace("\\", "/")
        stem = os.path.splitext(f)[0].lower()
        tokens = set(re.findall(r'[a-z]+', re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', os.path.splitext(f)[0]).lower()))
        if "variable" in tokens or "[wght]" in stem or stem.endswith("-vf"):
            variable_fonts.append(full_path)
        has_bold = bool(tokens & {"bold", "bd", "demi", "demibold", "semibold", "heavy", "black", "blk", "extrabold"}) \
                   or "bold" in stem
        has_italic = bool(tokens & {"italic", "oblique", "it", "ital", "obl", "bdit", "boldit"}) \
                     or "italic" in stem or "oblique" in stem
        if "bdit" in tokens or "boldit" in tokens:
            has_bold = has_italic = True
        if has_bold and has_italic:
            candidates["bold_italic"].append(full_path)
        elif has_bold:
            candidates["bold"].append(full_path)
        elif has_italic:
            candidates["italic"].append(full_path)
        else:
            candidates["regular"].append(full_path)

    def pick_best(lst):
        if not lst:
            return None
        def score(p):
            b = os.path.basename(p).lower()
            sc = len(b)
            if "semi" in b or "demi" in b: sc += 10
            if "black" in b or "heavy" in b: sc += 5
            return sc
        return sorted(lst, key=score)[0]

    res = {
        "regular": font_path.replace("\\", "/"),
        "italic": pick_best(candidates["italic"]),
        "bold": pick_best(candidates["bold"]),
        "bold_italic": pick_best(candidates["bold_italic"]),
    }
    if variable_fonts:
        vf = font_path.replace("\\", "/") if font_path.replace("\\", "/") in variable_fonts else variable_fonts[0]
        for k in ("bold", "italic", "bold_italic"):
            if not res[k]:
                res[k] = vf
    return res


def pick_regular_face(paths):
    """Given the files of one family folder, return the true Regular face."""
    paths = list(paths)
    if not paths:
        return None
    r = classify_faces(paths).get("regular")
    if r:
        return r
    plain = [p for p in paths if not re.search(r'bold|italic|oblique', os.path.basename(p), re.I)]
    return (plain or paths)[0]


def group_epub_fonts(extracted_fonts):
    """{filekey: path} -> {family_name: {'regular','italic','bold','bold_italic'}}
    using the fonts' own metadata, so names match the book's CSS."""
    by_family = {}
    for key, path in extracted_fonts.items():
        m = font_meta(path)
        fam = (m["family"] if m and m["family"] else key)
        by_family.setdefault(fam, []).append(path)
    out = {}
    for fam, paths in by_family.items():
        faces = classify_faces(paths)
        if HAS_FONTTOOLS and not any(faces.values()):
            continue
        out[fam] = faces
    return out


def pick_epub_body_family(families, book_css):
    """Prefer the family the book's own <body>/<p> rule asks for."""
    names = {_norm_family(f): f for f in families}
    for sel in (r'(?:^|[}\s,])body\s*(?:,[^{]*)?\{([^}]*)\}', r'(?:^|[}\s,])p\s*(?:,[^{]*)?\{([^}]*)\}'):
        for blk in re.findall(sel, book_css or "", flags=re.I):
            m = re.search(r'font-family\s*:\s*([^;}]+)', blk, flags=re.I)
            if m:
                for part in m.group(1).split(","):
                    key = _norm_family(part.strip().strip("'\""))
                    if key in names and families[names[key]].get("regular"):
                        return names[key]
    for f, faces in families.items():
        if faces.get("regular"):
            return f
    return next(iter(families), None)


# ---------------------------------------------------------------------------
# TEMP FONT MANAGEMENT
# All generated/extracted fonts live in ONE per-process folder that is removed at exit.
# Hacked fonts (tracking / word-spacing) are cached, so dragging a slider no longer
# writes a new copy of every font on each re-render.
# ---------------------------------------------------------------------------
_FONT_TMP_DIR = None
_FONT_TMP_LOCK = threading.RLock()
_HACK_FONT_CACHE = {}  # key -> {"path": str, "gen": int}
_RENDER_GEN = 0
HACK_FONT_CACHE_MAX = 24


def _purge_stale_font_temp(max_age_s=86400):
    """Remove leftovers from crashed sessions / older versions of this app (older than 1 day)."""
    tmp = tempfile.gettempdir()
    now = time.time()
    for pat in ("xalibre_fonts_*", "epub_fonts_*", "tracking_font_*", "spaced_font_*"):
        for p in glob.glob(os.path.join(tmp, pat)):
            try:
                if now - os.path.getmtime(p) < max_age_s:
                    continue
                shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
            except Exception:
                pass


def cleanup_font_temp():
    global _FONT_TMP_DIR
    with _FONT_TMP_LOCK:
        if _FONT_TMP_DIR:
            shutil.rmtree(_FONT_TMP_DIR, ignore_errors=True)
        _HACK_FONT_CACHE.clear()


def get_font_tmp_dir():
    global _FONT_TMP_DIR
    with _FONT_TMP_LOCK:
        if _FONT_TMP_DIR is None:
            _purge_stale_font_temp()
            atexit.register(cleanup_font_temp)
            _FONT_TMP_DIR = os.path.join(tempfile.gettempdir(), f"xalibre_fonts_{os.getpid()}")
        os.makedirs(_FONT_TMP_DIR, exist_ok=True)
        return _FONT_TMP_DIR


def begin_font_render():
    """Call at the start of every layout pass; fonts used by older passes become prunable."""
    global _RENDER_GEN
    with _FONT_TMP_LOCK:
        _RENDER_GEN += 1


def _hack_cache_key(kind, path, em):
    try:
        st = os.stat(path)
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        sig = (0, 0)
    return (kind, os.path.abspath(path), sig, round(float(em), 4))


def _hack_cache_get(key):
    with _FONT_TMP_LOCK:
        hit = _HACK_FONT_CACHE.get(key)
        if hit and os.path.exists(hit["path"]):
            hit["gen"] = _RENDER_GEN
            return hit["path"]
    return None


def _hack_cache_put(key, path):
    with _FONT_TMP_LOCK:
        _HACK_FONT_CACHE[key] = {"path": path, "gen": _RENDER_GEN}
        if len(_HACK_FONT_CACHE) <= HACK_FONT_CACHE_MAX:
            return
        # Prune oldest entries, but never fonts used by the current or previous layout pass.
        victims = sorted((k for k, v in _HACK_FONT_CACHE.items() if v["gen"] < _RENDER_GEN - 1),
                         key=lambda k: _HACK_FONT_CACHE[k]["gen"])
        for k in victims:
            if len(_HACK_FONT_CACHE) <= HACK_FONT_CACHE_MAX:
                break
            try:
                os.remove(_HACK_FONT_CACHE[k]["path"])
            except OSError:
                pass
            del _HACK_FONT_CACHE[k]


# ---------------------------------------------------------------------------
# GLYPH COVERAGE
# ---------------------------------------------------------------------------
_CMAP_CACHE = {}


def font_cmap(path):
    """Set of code points the font can draw, or None if it can't be read."""
    if not HAS_FONTTOOLS or not path:
        return None
    key = os.path.abspath(path)
    if key not in _CMAP_CACHE:
        cm = None
        try:
            f = TTFont(path, lazy=True, fontNumber=0)
            try:
                cm = set(f.getBestCmap().keys())
            finally:
                f.close()
        except Exception:
            cm = None
        _CMAP_CACHE[key] = cm
    return _CMAP_CACHE[key]


def find_missing_glyphs(font_path, char_counts):
    """Counter of visible characters used in the book that font_path has no glyph for.
    Returns None when the font can't be inspected."""
    cm = font_cmap(font_path)
    if cm is None:
        return None
    missing = Counter()
    for ch, n in char_counts.items():
        if unicodedata.category(ch)[0] in "ZCM":  # spaces, control/format (e.g. soft hyphen), combining marks
            continue
        if ord(ch) not in cm:
            missing[ch] = n
    return missing


def create_tracking_font(original_path, tracking_em):
    """
    Hacks the font to add 'tracking_em' width to EVERY character.
    Used to simulate letter-spacing without destroying text content.
    """
    if not HAS_FONTTOOLS or not original_path or not os.path.exists(original_path):
        return original_path

    try:
        _ckey = _hack_cache_key("tracking", original_path, tracking_em)
        _hit = _hack_cache_get(_ckey)
        if _hit:
            return _hit
        temp_dir = get_font_tmp_dir()
        ext = os.path.splitext(original_path)[1]
        unique_id = str(uuid.uuid4())[:8]
        temp_name = f"tracking_font_{unique_id}{ext}"
        temp_path = os.path.join(temp_dir, temp_name)

        font = TTFont(original_path)
        upm = font['head'].unitsPerEm
        extra_units = int(upm * tracking_em)

        # Iterate over the horizontal metrics table (hmtx)
        if 'hmtx' in font:
            hmtx = font['hmtx']
            metrics = hmtx.metrics
            for glyph_name in metrics:
                width, lsb = metrics[glyph_name]
                # Add the tracking space to every single character
                metrics[glyph_name] = (width + extra_units, lsb)

        # FIX FOR MACOS: Rename internal font to bypass CoreText cache
        if 'name' in font:
            for record in font['name'].names:
                if record.nameID in (1, 3, 4, 6):
                    try:
                        text = record.toUnicode()
                        new_text = f"{text}T{unique_id}"
                        # PostScript Name (ID 6) cannot contain spaces
                        if record.nameID == 6:
                            new_text = new_text.replace(" ", "")
                        record.string = new_text.encode(record.getEncoding())
                    except:
                        pass

        font.save(temp_path)
        _hack_cache_put(_ckey, temp_path)
        return temp_path

    except Exception as e:
        print(f"Tracking Font Hack Failed: {e}")
        return original_path


def create_spaced_font(original_path, spacing_em):
    """
    Hacks the TTF/OTF binary to physically widen the space character.
    Returns path to a temporary font file.
    """
    if not HAS_FONTTOOLS or not original_path or not os.path.exists(original_path):
        return original_path

    try:
        _ckey = _hack_cache_key("spaced", original_path, spacing_em)
        _hit = _hack_cache_get(_ckey)
        if _hit:
            return _hit
        # Generate a unique temp path (inside the per-process font temp dir)
        temp_dir = get_font_tmp_dir()
        ext = os.path.splitext(original_path)[1]
        unique_id = str(uuid.uuid4())[:8]
        temp_name = f"spaced_font_{unique_id}{ext}"
        temp_path = os.path.join(temp_dir, temp_name)

        # Load the font
        font = TTFont(original_path)

        # Calculate extra units
        upm = font['head'].unitsPerEm
        extra_units = int(upm * spacing_em)

        # 1. Identify the Space Glyph
        cmap = font.getBestCmap()
        space_name = cmap.get(32)

        if space_name:
            # 2. Modify Horizontal Metrics (hmtx)
            if 'hmtx' in font:
                hmtx = font['hmtx']
                current_width, lsb = hmtx[space_name]

                # Apply the stretch
                new_width = current_width + extra_units
                hmtx[space_name] = (new_width, lsb)

            # 3. Handle CFF fonts (OTF) if they exist
            if 'CFF ' in font:
                pass  # Usually hmtx sync is enough for PyMuPDF

        # FIX FOR MACOS: Rename internal font to bypass CoreText cache
        if 'name' in font:
            for record in font['name'].names:
                if record.nameID in (1, 3, 4, 6):
                    try:
                        text = record.toUnicode()
                        new_text = f"{text}S{unique_id}"
                        # PostScript Name (ID 6) cannot contain spaces
                        if record.nameID == 6:
                            new_text = new_text.replace(" ", "")
                        record.string = new_text.encode(record.getEncoding())
                    except:
                        pass

        # Save the hacked font
        font.save(temp_path)
        _hack_cache_put(_ckey, temp_path)
        return temp_path

    except Exception as e:
        print(f"Font Hack Failed: {e}")
        return original_path


def get_pil_font(font_path, size):
    try:
        if font_path and os.path.exists(font_path):
            return ImageFont.truetype(font_path, size)
        for f_dir in ("Fonts", "fonts"):
            if font_path:
                bp = os.path.join(INTERNAL_DIR, f_dir, font_path)
                if os.path.exists(bp):
                    return ImageFont.truetype(bp, size)
        return None
    except Exception:
        return None


def extract_all_css(book):
    css_rules = []
    for item in book.get_items_of_type(ebooklib.ITEM_STYLE):
        try:
            css_rules.append(item.get_content().decode('utf-8', errors='ignore'))
        except:
            pass
    return "\n".join(css_rules)


def extract_fonts_from_epub(book, target_dir):
    """
    Extracts embedded TTF/OTF fonts to target_dir.
    Returns a dict: { 'FontFileNameWithoutExt': '/absolute/path/to/font.ttf' }
    """
    if not os.path.exists(target_dir):
        os.makedirs(target_dir, exist_ok=True)

    extracted_font_map = {}

    for item in book.get_items():
        media_type = getattr(item, 'media_type', '').lower()
        name = item.get_name()
        name_lower = name.lower()

        is_font = (
                'font' in media_type or
                'opentype' in media_type or
                'truetype' in media_type or
                name_lower.endswith(('.ttf', '.otf'))
        )

        if is_font and name_lower.endswith(('.ttf', '.otf')):
            clean_filename = unquote(os.path.basename(name))
            if not clean_filename:
                continue

            dest_path = os.path.join(target_dir, clean_filename)
            try:
                with open(dest_path, "wb") as f:
                    f.write(item.get_content())

                font_key = os.path.splitext(clean_filename)[0]
                extracted_font_map[font_key] = os.path.abspath(dest_path).replace("\\", "/")
            except Exception as e:
                print(f"Failed to extract font {clean_filename}: {e}")

    return extracted_font_map


def extract_images_to_base64(book):
    image_map = {}
    # Iterate over ALL items to find images, not just those marked specifically as ITEM_IMAGE
    # (Some EPUBs mislabel the cover as a generic document)
    for item in book.get_items():
        try:
            # Check if it looks like an image based on media type or extension
            media_type = item.media_type.lower()
            name = item.get_name()

            if "image" in media_type or name.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp')):
                # 1. Get the content
                content = item.get_content()

                # 2. Rasterize SVG resources to PNG before embedding. MuPDF's
                # HTML renderer can reduce SVG table icons to tiny fallback
                # text. A high-resolution PNG remains stable and readable.
                is_svg = ('svg' in media_type or name.lower().endswith('.svg'))
                if is_svg:
                    try:
                        svg_doc = fitz.open(stream=content, filetype='svg')
                        svg_page = svg_doc[0]
                        rect = svg_page.rect
                        longest = max(rect.width, rect.height, 1)
                        scale = max(1.0, 384.0 / longest)
                        pix = svg_page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=True)
                        content = pix.tobytes('png')
                        media_type = 'image/png'
                        svg_doc.close()
                    except Exception as svg_error:
                        print(f"SVG rasterization failed for {name}: {svg_error}")

                b64_data = base64.b64encode(content).decode('utf-8')
                data_uri = f"data:{media_type};base64,{b64_data}"

                # 3. Store using the BASENAME (filename only)
                # We decode URL chars (e.g., "my%20image.jpg" -> "my image.jpg")
                clean_name = unquote(os.path.basename(name))

                # Store exact match
                image_map[clean_name] = data_uri

                # Store lowercase match for loose matching
                image_map[clean_name.lower()] = data_uri
        except Exception as e:
            print(f"Failed to extract image {item.get_name()}: {e}")
            pass

    return image_map


def get_official_toc_mapping(book):
    mapping = {}

    def add_entry(href, title):
        if '#' in href:
            href_clean, anchor = href.split('#', 1)
        else:
            href_clean, anchor = href, None
        filename = os.path.basename(href_clean)
        if filename not in mapping:
            mapping[filename] = []
        mapping[filename].append((anchor, title))

    def process_toc_item(item):
        if isinstance(item, tuple):
            if len(item) > 1 and isinstance(item[1], list):
                for sub in item[1]: process_toc_item(sub)
        elif isinstance(item, epub.Link):
            add_entry(item.href, item.title)

    for item in book.toc:
        process_toc_item(item)

    if not mapping:
        nav_item = next((item for item in book.get_items() if item.get_type() == ebooklib.ITEM_NAVIGATION), None)
        if nav_item:
            try:
                soup = BeautifulSoup(nav_item.get_content(), 'html.parser')
                nav_element = soup.find('nav', attrs={'epub:type': 'toc'}) or soup.find('nav')
                if nav_element:
                    for link in nav_element.find_all('a', href=True):
                        add_entry(link['href'], link.get_text().strip())
            except:
                pass
    return mapping


def hyphenate_html_text(soup, language_code):
    try:
        dic = pyphen.Pyphen(lang=language_code)
    except:
        try:
            dic = pyphen.Pyphen(lang='en')
        except:
            return soup
    word_pattern = re.compile(r'\w+', re.UNICODE)
    for text_node in soup.find_all(string=True):
        if text_node.parent.name in ['script', 'style', 'head', 'title', 'meta', 'rt']: continue
        if not text_node.strip(): continue
        original_text = str(text_node)
        clean_text = original_text.replace('\u00A0', ' ')

        def replace_match(match):
            word = match.group(0)
            if len(word) < 4: return word
            return dic.inserted(word, hyphen='\u00AD')

        new_text = word_pattern.sub(replace_match, clean_text)
        if new_text != original_text:
            text_node.replace_with(NavigableString(new_text))
    return soup


def get_local_fonts():
    user_home = os.path.expanduser("~")
    fonts_dir = os.path.join(user_home, "Xlibre", "Fonts")

    if not os.path.exists(fonts_dir):
        os.makedirs(fonts_dir, exist_ok=True)

    font_map = {}
    for item in os.listdir(fonts_dir):
        item_path = os.path.join(fonts_dir, item)

        if os.path.isdir(item_path):
            # Scan inside the family folder for .ttf or .otf
            files = [f for f in os.listdir(item_path) if f.lower().endswith((".ttf", ".otf"))]
            if not files: continue

            # Identify the "Main" font file (not Bold/Italic) to use as the base
            main_path = pick_regular_face([os.path.join(item_path, f) for f in files])

            # Store the absolute path for the renderer
            font_map[item] = os.path.abspath(main_path)

    # 2. Scan Xlibre/Fonts (Root files)
    for item in os.listdir(fonts_dir):
        item_path = os.path.join(fonts_dir, item)
        if os.path.isfile(item_path) and item.lower().endswith((".ttf", ".otf")):
            name = os.path.splitext(item)[0]
            if name not in font_map:
                font_map[name] = os.path.abspath(item_path)

    # 3. System Fallback (Ensure at least one font exists for hacks)
    if not font_map:
        if sys.platform.startswith("win"):
            sys_font_dir = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts")
            targets = ["times.ttf", "arial.ttf", "georgia.ttf", "seguiemj.ttf"]
            for t in targets:
                p = os.path.join(sys_font_dir, t)
                if os.path.exists(p):
                    font_map["System " + t.split('.')[0].capitalize()] = p
        elif sys.platform == "darwin":
            mac_dirs = ["/System/Library/Fonts", "/System/Library/Fonts/Supplemental", "/Library/Fonts", os.path.expanduser("~/Library/Fonts")]
            mac_targets = ["Helvetica.ttc", "Times.ttc", "Arial.ttf", "Georgia.ttf", "Geneva.ttf"]
            for d in mac_dirs:
                if os.path.exists(d):
                    for t in mac_targets:
                        p = os.path.join(d, t)
                        if os.path.exists(p) and ("System " + t.split('.')[0].capitalize()) not in font_map:
                            font_map["System " + t.split('.')[0].capitalize()] = p
        else:
            for d in ("/usr/share/fonts", "/usr/local/share/fonts", os.path.expanduser("~/.fonts")):
                if os.path.exists(d):
                    for root, _, files in os.walk(d):
                        for f in files:
                            if f.lower().endswith((".ttf", ".otf")):
                                name = "System " + os.path.splitext(f)[0].capitalize()
                                if name not in font_map:
                                    font_map[name] = os.path.join(root, f)
                                    if len(font_map) >= 5: break
                        if len(font_map) >= 5: break

    return font_map


# --- DIALOG CLASSES ---
class CoverExportDialog(ctk.CTkToplevel):
    def __init__(self, parent, callback):
        super().__init__(parent)
        self.callback = callback
        self.title("Export Cover Settings")
        self.geometry("400x350")
        self.transient(parent)
        self.grab_set()
        ctk.CTkLabel(self, text="Export Cover as BMP", font=("Arial", 16, "bold")).pack(pady=15)
        self.frm_dim = ctk.CTkFrame(self, fg_color="transparent")
        self.frm_dim.pack(pady=10)
        ctk.CTkLabel(self.frm_dim, text="Width:").grid(row=0, column=0, padx=5)
        self.entry_w = ctk.CTkEntry(self.frm_dim, width=60)
        self.entry_w.insert(0, str(ACTIVE_DEVICE_SIZE[0]))
        self.entry_w.grid(row=0, column=1, padx=5)
        ctk.CTkLabel(self.frm_dim, text="Height:").grid(row=0, column=2, padx=5)
        self.entry_h = ctk.CTkEntry(self.frm_dim, width=60)
        self.entry_h.insert(0, str(ACTIVE_DEVICE_SIZE[1]))
        self.entry_h.grid(row=0, column=3, padx=5)
        ctk.CTkLabel(self, text="Scaling Mode:").pack(pady=(15, 5))
        self.mode_var = ctk.StringVar(value="Crop to Fill (Best)")
        modes = ["Crop to Fill (Best)", "Fit (Add White Bars)", "Stretch (Distort)"]
        ctk.CTkOptionMenu(self, variable=self.mode_var, values=modes).pack(pady=5)
        ctk.CTkLabel(self,
                     text="Crop to Fill: Fills the screen, cuts off edges if ratio differs.\nFit: Keeps entire image, adds white bars.\nStretch: Forces image to size, may look squashed.",
                     font=("Arial", 12), text_color="gray").pack(pady=10)
        ctk.CTkButton(self, text="Export", command=self.confirm).pack(pady=20)

    def confirm(self):
        try:
            w = int(self.entry_w.get())
            h = int(self.entry_h.get())
        except ValueError:
            messagebox.showerror("Error", "Width and Height must be numbers.")
            return
        mode = self.mode_var.get()
        self.callback(w, h, mode)
        self.grab_release()
        self.destroy()


class ChapterSelectionDialog(ctk.CTkToplevel):
    def __init__(self, parent, chapters_list, initial_selection, callback):
        super().__init__(parent)
        self.callback = callback
        self.chapters_list = chapters_list
        self.title("Select Chapters for TOC")
        self.geometry("500x600")
        self.transient(parent)
        self.grab_set()

        self.lbl_info = ctk.CTkLabel(self,
                                     text="Uncheck chapters to hide them from the TOC and Progress Bar.\n(They will still be readable in the book)",
                                     wraplength=450)
        self.lbl_info.pack(pady=10)

        self.btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.btn_frame.pack(fill="x", padx=20)
        ctk.CTkButton(self.btn_frame, text="Select All", width=100, command=self.select_all).pack(side="left", padx=5)
        ctk.CTkButton(self.btn_frame, text="Select None", width=100, command=self.select_none).pack(side="left", padx=5)

        self.scroll_frame = ctk.CTkScrollableFrame(self)
        self.scroll_frame.pack(fill="both", expand=True, padx=20, pady=10)

        self.check_vars = []

        # Create a set for fast lookup if we have a selection
        selected_set = set(initial_selection) if initial_selection is not None else set()

        for i, chap in enumerate(chapters_list):

            # --- LOGIC FIX START ---
            if initial_selection is None:
                # FIRST RUN: Use your Regex logic to hide "Section X"
                is_generic = re.match(r"^Section \d+$", chap['title'])
                should_check = not is_generic
            else:
                # SUBSEQUENT RUNS: Restore exactly what the user had last time
                should_check = i in selected_set
            # --- LOGIC FIX END ---

            var = ctk.BooleanVar(value=should_check)
            self.check_vars.append(var)
            chk = ctk.CTkCheckBox(self.scroll_frame, text=f"{i + 1}. {chap['title']}", variable=var)
            chk.pack(anchor="w", pady=2, padx=5)

        self.btn_confirm = ctk.CTkButton(self, text="Confirm Selection", command=self.confirm)
        self.btn_confirm.pack(pady=20)

    def select_all(self):
        for var in self.check_vars: var.set(True)

    def select_none(self):
        for var in self.check_vars: var.set(False)

    def confirm(self):
        selected_indices = [i for i, var in enumerate(self.check_vars) if var.get()]

        # Allow empty selection (no TOC) but warn user
        if not selected_indices:
            if not messagebox.askyesno("Warning",
                                       "No chapters selected. The TOC will be empty. Continue?"):
                return

        self.grab_release()
        self.destroy()
        self.callback(selected_indices)


# --- EPUB PROCESSOR ---
class EpubProcessor:
    def __init__(self):
        self.cover_pages = set()
        self.input_file = ""
        self.raw_chapters = []
        self.book_css = ""
        self.book_images = {}
        self.book_lang = 'en'
        self.cover_image_obj = None
        self.font_path = ""
        self.font_size = FACTORY_DEFAULTS["font_size"]
        self.margin = FACTORY_DEFAULTS["margin"]
        self.line_height = FACTORY_DEFAULTS["line_height"]
        self.font_weight = FACTORY_DEFAULTS["font_weight"]
        self.bottom_padding = FACTORY_DEFAULTS["bottom_padding"]
        self.top_padding = FACTORY_DEFAULTS["top_padding"]
        self.text_align = FACTORY_DEFAULTS["text_align"]
        self.screen_width = DEFAULT_SCREEN_WIDTH
        self.screen_height = DEFAULT_SCREEN_HEIGHT
        self.layout_settings = {}
        self.fitz_docs = []
        self.toc_data_final = []
        self.toc_pages_images = []
        self.page_map = []
        self.total_pages = 0
        self.toc_items_per_page = 18
        self.is_ready = False
        self.global_id_map = {}
        self.annotator = None  # Instance of SpectraAnnotator
        self.is_comic = False
        self.comic_meta = None
        self.comic_units = []

    def _split_html_by_toc(self, soup, toc_entries):
        chunks = []
        if len(toc_entries) == 1 and not toc_entries[0][0]:
            return [{'title': toc_entries[0][1], 'soup': soup}]
        split_points = []
        for anchor, title in toc_entries:
            target = None
            if anchor: target = soup.find(id=anchor)
            if target or not anchor: split_points.append({'node': target, 'title': title})
        if not split_points: return [{'title': toc_entries[0][1], 'soup': soup}]

        current_idx = 0
        current_soup = BeautifulSoup("<body></body>", 'html.parser')
        body_children = list(soup.body.children) if soup.body else []
        for child in body_children:
            if isinstance(child, NavigableString) and not child.strip():
                if current_soup.body: current_soup.body.append(child.extract() if hasattr(child, 'extract') else child)
                continue
            if current_idx + 1 < len(split_points):
                next_node = split_points[current_idx + 1]['node']
                is_nested_target = False
                if hasattr(child, 'find_all'):
                    if next_node in child.find_all(): is_nested_target = True
                if next_node and (child == next_node or is_nested_target):
                    chunks.append({'title': split_points[current_idx]['title'], 'soup': current_soup})
                    current_idx += 1
                    current_soup = BeautifulSoup("<body></body>", 'html.parser')
            if current_soup.body: current_soup.body.append(child)
        chunks.append({'title': split_points[current_idx]['title'], 'soup': current_soup})
        return chunks

    def _smart_extract_content(self, elem):
        if elem.name == 'a':
            parent = elem.parent
            if parent and parent.name not in ['body', 'html', 'section']: return parent
            return elem
        if elem.name in ['aside', 'li', 'dd', 'div']: return elem
        text = elem.get_text(strip=True)
        if len(text) > 1: return elem
        parent = elem.parent
        if parent:
            if parent.name in ['body', 'html', 'section']: return elem
            return parent
        return elem

    def _build_global_id_map(self, book):
        id_map = {}
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            try:
                soup = BeautifulSoup(item.get_content(), 'html.parser')
                filename = os.path.basename(item.get_name())
                for elem in soup.find_all(id=True):
                    target_node = self._smart_extract_content(elem)
                    import copy
                    content_node = copy.copy(target_node)
                    original_raw_html = content_node.decode_contents().strip()
                    for a in content_node.find_all('a'):
                        if a.get('role') in ['doc-backlink', 'doc-noteref']:
                            a.decompose()
                            continue
                        text = a.get_text(strip=True)
                        if any(x in text for x in ['↑', 'site', 'back', 'return', '↩']):
                            a.decompose()
                            continue
                        if len(text) < 5 and re.match(r'^[\s\[\(]*\d+[\.\)\]]*$', text):
                            a.decompose()
                            continue
                    final_html = content_node.decode_contents().strip()
                    if not final_html and original_raw_html: final_html = original_raw_html
                    if final_html: id_map[f"{filename}#{elem['id']}"] = final_html
            except Exception:
                pass
        return id_map

    def _inject_inline_footnotes(self, soup, current_filename):
        if not self.global_id_map: return soup
        links = soup.find_all('a', href=True)
        for link in reversed(list(links)):
            raw_href = link['href']
            href = unquote(raw_href)
            text = link.get_text(strip=True)
            if not text and not link.find('sup'): continue
            parent_classes = []
            for parent in link.parents:
                if parent.get('class'): parent_classes.extend(parent.get('class'))
            if any(x in [c.lower() for c in parent_classes] for x in
                   ['footnote', 'endnote', 'reflist', 'bibliography']): continue
            is_footnote = False
            if 'noteref' in link.get('epub:type', '') or link.get('role') == 'doc-noteref': is_footnote = True
            css = link.get('class', [])
            if isinstance(css, list): css = " ".join(css)
            if any(x in css.lower() for x in ['footnote', 'noteref', 'ref']): is_footnote = True
            if not is_footnote and text:
                clean_t = text.strip()
                if re.match(r'^[\(\[]?\d+[\)\]]?$', clean_t) or clean_t == '*':
                    is_footnote = True
                elif re.match(r'^[\(\[]?[ivx]+[\)\]]?$', clean_t.lower()):
                    is_footnote = True
            if not is_footnote: continue
            content = None
            if '#' in href:
                parts = href.rsplit('#', 1)
                href_path = parts[0]
                href_id = parts[1]
                f_name = os.path.basename(href_path) if href_path else current_filename
                key = f"{f_name}#{href_id}"
                content = self.global_id_map.get(key)
                if not content:
                    suffix = f"#{href_id}"
                    for k, v in self.global_id_map.items():
                        if k.endswith(suffix):
                            content = v
                            break
            if content:
                new_marker = soup.new_tag("sup")
                new_marker.string = text if text else "*"
                new_marker['class'] = "fn-marker"
                link.replace_with(new_marker)
                note_box = soup.new_tag("div")
                note_box['class'] = "inline-footnote-box"
                header = soup.new_tag("strong")
                header.string = f"{text}: "
                note_box.append(header)
                content_soup = BeautifulSoup(content, 'html.parser')
                note_box.append(content_soup)
                parent_block = new_marker.find_parent(['p', 'div', 'li', 'h1', 'h2', 'blockquote'])
                if parent_block:
                    parent_block.insert_after(note_box)
                else:
                    new_marker.insert_after(note_box)
        return soup

    def _find_cover_image(self, book):
        # Reset so a failed lookup doesn't carry over a stale filename from
        # a previously converted book.
        self.cover_image_filename = None

        # 1. Collect EVERY item in the EPUB regardless of what ebooklib thinks it is
        all_items = list(book.get_items())

        # Potential candidates list (we'll rank them by probability)
        candidates = []

        for item in all_items:
            name = item.get_name().lower()
            content = item.get_content()

            # Skip empty items
            if not content:
                continue

            # A: Check if the filename looks like a cover
            # We look for 'cover', 'front', 'okladka' (Polish), or 'thumbnail'
            is_cover_named = any(x in name for x in ['cover', 'front', 'okladka', 'thumb', 'jacket'])

            # B: Check for image extensions
            is_image_ext = any(name.endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.webp'])

            if is_cover_named or is_image_ext:
                try:
                    # Try to force-open the bytes as an image
                    img = Image.open(io.BytesIO(content))

                    # Filter out tiny icons (like social media logos or dividers)
                    if img.width > 250 and img.height > 250:
                        # Give priority to things actually named "cover"
                        score = 100 if is_cover_named else 0
                        # Give priority to larger images (covers are usually the biggest)
                        score += (img.width * img.height) // 10000

                        candidates.append((score, img, name))
                except:
                    continue

        # 2. Sort by score (Highest first) and return the winner
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            winner_score, winner_img, winner_name = candidates[0]
            print(f"DEBUG: Selected Cover -> {winner_name} (Score: {winner_score})")
            self.cover_image_filename = os.path.basename(winner_name)
            return winner_img

        # 3. Final desperation: if metadata has a 'cover' tag, try to find THAT filename
        try:
            meta_id = book.get_metadata('OPF', 'cover')[0][1]
            item = book.get_item_with_id(meta_id)
            if item:
                self.cover_image_filename = os.path.basename(item.get_name().lower())
                return Image.open(io.BytesIO(item.get_content()))
        except:
            pass

        return None

    def parse_book_structure(self, input_path):
        # If passed a non-EPUB file (e.g. .pdf, .cbz) directly, convert to a temporary EPUB first
        if not input_path.lower().endswith(".epub"):
            try:
                import importers
                if importers.is_importable(input_path):
                    tmp_dir = tempfile.mkdtemp(prefix="xalibre_conv_")
                    atexit.register(shutil.rmtree, tmp_dir, ignore_errors=True)
                    meta = importers.convert_to_epub(input_path, tmp_dir)
                    input_path = meta["epub_path"]
            except Exception as e:
                print(f"Direct import conversion failed: {e}")
                return False

        self.input_file = input_path
        self.raw_chapters = []
        self.cover_image_obj = None
        self.extracted_fonts = {}
        self.is_comic = False
        self.comic_meta = None
        self.comic_units = []

        # Comic / PDF EPUBs (made by importers.py) are handled without ebooklib: no need to load every page in RAM.
        try:
            with zipfile.ZipFile(input_path) as _z:
                _marker = next((n for n in _z.namelist() if n.endswith(COMIC_MARKER)), None)
        except Exception:
            _marker = None
        if _marker:
            return self._parse_comic_structure(input_path, _marker)

        try:
            book = epub.read_epub(self.input_file)
        except Exception as e:
            print(f"Error reading EPUB: {e}")
            return False


        # Wypakowanie fontów książki do katalogu tymczasowego dedykowanego tej pozycji
        book_id = hashlib.md5(self.input_file.encode('utf-8')).hexdigest()[:8]
        fonts_dest = os.path.join(get_font_tmp_dir(), f"epub_{book_id}")
        self.extracted_fonts = extract_fonts_from_epub(book, fonts_dest)
        if self.extracted_fonts:
            print(
                f"Extracted {len(self.extracted_fonts)} font(s) from EPUB: {', '.join(self.extracted_fonts.keys())}")

        self.cover_image_obj = self._find_cover_image(book)

        # --- CAPTURE METADATA FOR XTC EXPORT ---
        try:
            # Extract Title
            titles = book.get_metadata('DC', 'title')
            self.title_metadata = titles[0][0] if titles else "Unknown Title"

            # Extract Author
            authors = book.get_metadata('DC', 'creator')
            self.author_metadata = authors[0][0] if authors else "Unknown Author"

            # Extract Language
            langs = book.get_metadata('DC', 'language')
            self.book_lang = langs[0][0] if langs else 'en'
        except Exception as e:
            print(f"Metadata extraction error: {e}")
            self.title_metadata = "Unknown Title"
            self.author_metadata = "Unknown Author"
            self.book_lang = 'en'
        self.book_images = extract_images_to_base64(book)
        self.book_css = extract_all_css(book)
        toc_mapping = get_official_toc_mapping(book)
        self.global_id_map = self._build_global_id_map(book)
        items = [book.get_item_with_id(item_ref[0]) for item_ref in book.spine if
                 isinstance(book.get_item_with_id(item_ref[0]), epub.EpubHtml)]
        for item in items:
            item_filename = os.path.basename(item.get_name())
            raw_html = item.get_content().decode('utf-8', errors='replace')
            soup = BeautifulSoup(raw_html, 'html.parser')
            has_image = bool(soup.find('img'))
            toc_entries = toc_mapping.get(item_filename)
            if toc_entries and len(toc_entries) > 1:
                split_chapters = self._split_html_by_toc(soup, toc_entries)
                for chunk in split_chapters:
                    self.raw_chapters.append(
                        {'title': chunk['title'], 'soup': chunk['soup'], 'has_image': bool(chunk['soup'].find('img')),
                         'filename': item_filename})
            else:
                chapter_title = toc_entries[0][1] if toc_entries else None
                if not chapter_title:
                    for tag in ['h1', 'h2', 'h3']:
                        header = soup.find(tag)
                        if header:
                            t = header.get_text().strip()
                            if t and len(t) < 150:
                                chapter_title = t
                                break
                    if not chapter_title: chapter_title = f"Section {len(self.raw_chapters) + 1}"
                self.raw_chapters.append(
                    {'title': chapter_title, 'soup': soup, 'has_image': has_image, 'filename': item_filename})
        return True

    def init_annotator(self, layout_settings):
        target_lang = layout_settings.get("spectra_target_lang", "English")
        new_api_key = layout_settings.get("spectra_api_key", "")
        new_base_url = layout_settings.get("spectra_base_url", "")
        new_model = layout_settings.get("spectra_model", "gpt-4o-mini")

        # Only re-init if needed, else update settings
        if not self.annotator:
            self.annotator = SpectraAnnotator(
                api_key=new_api_key,
                base_url=new_base_url,
                model=new_model,
                threshold=layout_settings.get("spectra_threshold", 4.0),
                aoa_threshold=layout_settings.get("spectra_aoa_threshold", 0.0),
                language=self.book_lang,
                target_lang=target_lang
            )
        else:
            # Update settings on existing instance
            self.annotator.api_key = new_api_key
            self.annotator.base_url = new_base_url
            self.annotator.model = new_model
            self.annotator.threshold = layout_settings.get("spectra_threshold", 4.0)
            self.annotator.aoa_threshold = layout_settings.get("spectra_aoa_threshold", 0.0)
            self.annotator.target_lang = target_lang

            # Re-initialize the OpenAI client with the NEW URL
            if self.annotator.api_key:
                try:
                    self.annotator.client = OpenAI(
                        api_key=self.annotator.api_key,
                        base_url=self.annotator.base_url
                    )
                    self.annotator.enabled = True
                except Exception as e:
                    print(f"Client Init Error: {e}")
                    self.annotator.enabled = False

    @staticmethod
    def _append_style(existing_style, new_rules):
        """
        Safely appends CSS declarations to a tag's existing style attribute.

        IMPORTANT: PyMuPDF's HTML/CSS layout engine, used to render pages,
        will silently discard an ENTIRE style attribute if it begins with a
        leading ';' (e.g. "; color: red;"). The old pattern used throughout
        this file -- tag.get('style','') + "; new: rules;" -- produces
        exactly that leading ';' whenever the tag had no pre-existing style
        (the common case), which meant most of these style overrides were
        silently doing nothing. This helper only inserts a separating ';'
        when there is actually existing content to separate from.
        """
        existing_style = (existing_style or "").strip()
        new_rules = (new_rules or "").strip().strip(';').strip()
        if not existing_style:
            return new_rules
        if not existing_style.endswith(';'):
            existing_style += ';'
        return f"{existing_style} {new_rules}"

    @staticmethod
    def _is_standalone_image(img_tag):
        """
        Returns True if img_tag is effectively alone in its immediate
        container (no meaningful text or other elements sharing the line),
        regardless of the image's raw pixel dimensions. This catches small
        decorative images (subchapter/section-break marks, ornaments) that
        should still be centered on their own line rather than treated as
        an inline icon.
        """
        parent = img_tag.parent
        if parent is None:
            return False

        for sibling in parent.contents:
            if sibling is img_tag:
                continue
            if isinstance(sibling, str):
                if sibling.strip():
                    return False
            else:
                # Any other tag with real text content, or another image,
                # means this image is sharing the line with something else.
                if sibling.name == 'img' or sibling.get_text(strip=True):
                    return False
        return True

    @staticmethod
    def _is_lone_body_image(img_tag, soup):
        """
        Returns True if img_tag is the ONLY image in the chapter and the
        chapter body has no other real text content -- i.e. this chapter
        is really just a full-page image (a title page, illustration
        page, etc.) even though it wasn't authored using the <svg><image>
        cover convention.
        """
        if not soup.body:
            return False
        all_imgs = soup.body.find_all('img')
        if len(all_imgs) != 1 or all_imgs[0] is not img_tag:
            return False
        return soup.body.get_text(strip=True) == ""

    def render_chapters(self, selected_indices, font_path, font_size, margin, line_height, font_weight, bottom_padding,
                        top_padding, text_align="justify", orientation="Portrait", add_toc=True, show_footnotes=True,
                        layout_settings=None, progress_callback=None):
        self.font_path = font_path
        self.font_size = font_size
        self.margin = margin
        self.font_weight = font_weight
        self.bottom_padding = bottom_padding
        self.top_padding = top_padding
        self.text_align = text_align
        self.layout_settings = layout_settings if layout_settings else {}
        self.cover_pages = set()

        begin_font_render()
        self.font_warnings = []
        self._font_load_failures = set()
        try:
            fitz.TOOLS.mupdf_warnings()  # flush stale warnings from earlier work
        except Exception:
            pass

        word_spacing = self.layout_settings.get("word_spacing", 0.0)
        para_spacing = self.layout_settings.get("paragraph_spacing", 0.5)

        computed_text_indent = "0 !important" if para_spacing >= 0.3 else "1.4em !important"

        # 1. Force comfortable line height if Spectra is enabled
        spectra_enabled = self.layout_settings.get("spectra_enabled", False)
        if spectra_enabled:
            self.line_height = max(line_height, 2.2)
        else:
            self.line_height = line_height

        # 2. Orientation logic
        dev_w, dev_h = get_device_size(self.layout_settings.get("device", DEFAULT_DEVICE))
        if "Landscape" in orientation:
            self.screen_width = dev_h
            self.screen_height = dev_w
        else:
            self.screen_width = dev_w
            self.screen_height = dev_h

        if getattr(self, "is_comic", False):
            return self._render_comic_chapters(selected_indices, progress_callback)


        # 3. Reset docs and maps
        for entry in self.fitz_docs:
            entry[0].close()
        self.fitz_docs = []
        self.page_map = []
        self.page_annotation_data = {}

        self.init_annotator(self.layout_settings)

        # 4. CSS Font Generation
        # 4. CSS Font Generation
        use_extracted = (self.font_path == "EPUB_ORIGINAL" or not self.font_path)
        font_rules = []
        css_word_spacing = word_spacing

        if use_extracted and getattr(self, 'extracted_fonts', None):
            # Group extracted files into real families (metadata, not filenames) so the
            # names match the book's own CSS and bold/italic map to real faces.
            families = group_epub_fonts(self.extracted_fonts)
            body_family = pick_epub_body_family(families, self.book_css)
            do_word_hack = word_spacing > 0.05 and HAS_FONTTOOLS
            if do_word_hack:
                css_word_spacing = 0

            _spaced_cache = {}

            def _word_spaced(path):
                if not do_word_hack or not path:
                    return path
                if path not in _spaced_cache:
                    _spaced_cache[path] = create_spaced_font(path, word_spacing)
                return _spaced_cache[path]

            def _emit(css_family, faces, transform=None):
                for key, weight, style in (("regular", "normal", "normal"),
                                           ("bold", "bold", "normal"),
                                           ("italic", "normal", "italic"),
                                           ("bold_italic", "bold", "italic")):
                    p = faces.get(key)
                    if not p:
                        continue
                    p = _word_spaced(p)
                    if transform:
                        p = transform(p)
                    font_rules.append(
                        f'@font-face {{ font-family: "{css_family}"; src: url("{p.replace(chr(92), "/")}"); '
                        f'font-weight: {weight}; font-style: {style}; }}')
                    # MuPDF may be asked for "oblique" by the book CSS
                    if key == "italic":
                        font_rules.append(
                            f'@font-face {{ font-family: "{css_family}"; src: url("{p.replace(chr(92), "/")}"); '
                            f'font-weight: normal; font-style: oblique; }}')

            for fam_name, faces in families.items():
                _emit(fam_name, faces)

            # Letter-spacing hack: CustomFontSpaced = tracked copy of the BODY family
            if body_family and HAS_FONTTOOLS:
                _emit("CustomFontSpaced", families[body_family],
                      transform=lambda p: create_tracking_font(p, 0.15))

            if body_family:
                self._check_glyph_coverage(families[body_family].get("regular"), body_family, selected_indices)

            fallback_body = f'"{body_family}", serif' if body_family else "serif"
            base_body_font = fallback_body
            font_val_body = None  # keep the book's own font-family rules
            font_val_header = None
        else:
            # Standardowa ścieżka: użytkownik wybrał konkretny font z listy
            # self.font_path can point at ANY file of the family (e.g. Bold) -> resolve real Regular
            _v = get_font_variants(self.font_path) if self.font_path else {}
            resolved_font_path = (_v.get("regular") or self.font_path)
            body_font_path = resolved_font_path
            header_font_path = resolved_font_path
            if resolved_font_path:
                self._check_glyph_coverage(resolved_font_path,
                                           os.path.splitext(os.path.basename(resolved_font_path))[0],
                                           selected_indices)

            if word_spacing > 0.05 and resolved_font_path and HAS_FONTTOOLS:
                body_font_path = create_spaced_font(resolved_font_path, word_spacing)
                css_word_spacing = 0

            # Spaced font hack
            base_spaced_path = resolved_font_path
            clean_base_spaced = ""
            if resolved_font_path and HAS_FONTTOOLS and os.path.exists(resolved_font_path):
                try:
                    base_spaced_path = create_tracking_font(resolved_font_path, 0.15)
                    clean_base_spaced = base_spaced_path.replace("\\", "/")
                except Exception as e:
                    print(f"Failed to create base tracking font: {e}")

            if clean_base_spaced:
                font_rules.append(
                    f'@font-face {{ font-family: "CustomFontSpaced"; src: url("{clean_base_spaced}"); font-weight: normal; font-style: normal; }}'
                )

            if HAS_FONTTOOLS and self.font_path:
                vars = get_font_variants(self.font_path)
                if vars.get('italic'):
                    try:
                        ital_path = create_tracking_font(vars['italic'], 0.15).replace("\\", "/")
                        font_rules.append(
                            f'@font-face {{ font-family: "CustomFontSpaced"; src: url("{ital_path}"); font-weight: normal; font-style: italic; }}')
                    except:
                        pass
                if vars.get('bold'):
                    try:
                        bold_path = create_tracking_font(vars['bold'], 0.15).replace("\\", "/")
                        font_rules.append(
                            f'@font-face {{ font-family: "CustomFontSpaced"; src: url("{bold_path}"); font-weight: bold; font-style: normal; }}')
                    except:
                        pass
                if vars.get('bold_italic'):
                    try:
                        bi_path = create_tracking_font(vars['bold_italic'], 0.15).replace("\\", "/")
                        font_rules.append(
                            f'@font-face {{ font-family: "CustomFontSpaced"; src: url("{bi_path}"); font-weight: bold; font-style: italic; }}')
                    except:
                        pass

            def add_font_face(name, path, orig_path_for_variants=None):
                if not path: return
                search_path = orig_path_for_variants if orig_path_for_variants else path
                v = get_font_variants(search_path)
                if not v: return
                regular_url = path.replace("\\", "/")
                font_rules.append(
                    f'@font-face {{ font-family: "{name}"; src: url("{regular_url}"); font-weight: normal; font-style: normal; }}')
                if v.get('bold'):
                    font_rules.append(
                        f'@font-face {{ font-family: "{name}"; src: url("{v["bold"]}"); font-weight: bold; font-style: normal; }}')
                if v.get('italic'):
                    font_rules.append(
                        f'@font-face {{ font-family: "{name}"; src: url("{v["italic"]}"); font-weight: normal; font-style: italic; }}')
                if v.get('bold_italic'):
                    font_rules.append(
                        f'@font-face {{ font-family: "{name}"; src: url("{v["bold_italic"]}"); font-weight: bold; font-style: italic; }}')

            if self.font_path:
                add_font_face("CustomFontBody", body_font_path, self.font_path)
                add_font_face("CustomFontHeader", header_font_path, self.font_path)
                font_val_body = '"CustomFontBody"'
                font_val_header = '"CustomFontHeader"'
                base_body_font = '"CustomFontBody"'
            else:
                font_val_body = "serif"
                font_val_header = "serif"
                base_body_font = "serif"

        font_face_rule = "\n".join(font_rules)
        patched_css = fix_css_font_paths(self.book_css, font_val_body)
        patched_css = clamp_css_font_sizes(patched_css, max_em=1.4)
        content_height = max(1, self.screen_height - self.top_padding - self.bottom_padding)

        body_font_rule = f"font-family: {base_body_font};" if use_extracted else f"font-family: {font_val_body} !important;"
        tag_font_rule = "" if use_extracted else "font-family: inherit !important;"
        h_font_rule = "" if use_extracted else f"font-family: {font_val_header} !important;"

        # --- 5. GLOBAL CSS (Optimized for EM Scaling & Calibre Override) ---
        custom_css = f"""
                        <style>
                            {font_face_rule}
                            @page {{ size: {self.screen_width}pt {content_height}pt; margin: 0; }}

                            /* 1. RESET EVERYTHING */
                            html, body {{
                                height: 100%;
                                width: 100%;
                                margin: 0 !important;
                                padding: 0 !important;
                            }}

                        /* 2. BODY CONTAINER CONTROLS */
                        /* 2. BODY CONTAINER CONTROLS */
                        body {{
                            {body_font_rule}
                            font-size: {self.font_size}pt !important;
                            font-weight: {self.font_weight} !important;
                            line-height: {self.line_height} !important;
                            text-align: {self.text_align} !important;
                            color: black !important;
                            background-color: white !important;
                            padding: {self.margin}px !important; 
                            box-sizing: border-box !important;
                        }}

                        /* 3. OVERRIDE CALIBRE/GENERIC TAGS (shared prose properties only --
                           margin/indent is handled per-tag-type below so list items,
                           blockquotes, and structural wrappers don't inherit a plain
                           paragraph's first-line indent / vertical rhythm) */
                        p, div, li, dd, dt, span, blockquote {{
                            {tag_font_rule}
                            line-height: inherit !important;
                            text-align: {self.text_align} !important;

                            /* FORCE WORD SPACING HERE */
                            word-spacing: {css_word_spacing}em !important; 


                            hyphens: manual !important;
                            adobe-hyphenate: explicit !important;

                            color: inherit !important;

                            padding-left: 0 !important;
                            padding-right: 0 !important;
                            max-width: 100% !important;
                        }}

                        /* 3b. PER-TAG SPACING & INDENT */
                        p {{
                          margin-top: {para_spacing}em !important;
                            margin-bottom: {para_spacing}em !important;
                            text-indent: {computed_text_indent};
                        }}
                        li {{
                            margin-top: {para_spacing}em !important;
                            margin-bottom: {para_spacing}em !important;
                            text-indent: 0 !important;
                            padding-left: 0.8em !important;
                        }}
                        dt {{
                            margin-top: 0.5em !important;
                            margin-bottom: 0.2em !important;
                            text-indent: 0 !important;
                            /* dt is a label for the definition that follows (like a
                               heading), not a marker-led list item -- it gets no
                               padding-left reservation, unlike li above. */
                            padding-left: 0 !important;
                        }}
                        dd {{
                            margin-top: 0 !important;
                            margin-bottom: 0.5em !important;
                            /* Browser/engine default UA stylesheet gives <dd> its own
                               ~40px margin-left; left un-reset, that stacks with the
                               inner <p>'s own indent and shifts the entire definition
                               body further right than intended on every line, not just
                               the first. Zero it so alignment is controlled by the
                               indent policy below instead of an inherited default. */
                            margin-left: 1.2em !important;
                            text-indent: 0 !important;
                        }}
                        div, span {{
                            margin: 0 !important;
                            text-indent: 0 !important;
                        }}

                        /* Blockquote: single source of truth for its indent/margin + styling
                           (previously duplicated as a separate block further down, which
                           set the same margin-left/right via a second rule) */
                        blockquote {{
                            margin: 0.5em 1.5em !important;
                            text-indent: 0 !important;

                        }}

                        /* Ensure centered text doesn't have an indent (Helper class if needed) */
                        .center-text {{
                            text-align: center !important;
                            text-indent: 0 !important;
                        }}


                        /* Conservative scale: Subtle enough to save space, distinct enough to read */
                        h1 {{ font-size: 1.35em !important; margin-top: 0.8em; margin-bottom: 0.4em; }}
                        h2 {{ font-size: 1.25em !important; margin-top: 0.7em; margin-bottom: 0.3em; }}
                        h3 {{ font-size: 1.15em !important; margin-top: 0.6em; margin-bottom: 0.2em; }}

                        /* H4-H6 fallbacks */
                        h4, h5, h6 {{ 
                            font-size: 1.05em !important; margin-top: 0.5em;
                        }}

                        h1, h2, h3, h4, h5, h6 {{ 

                            {h_font_rule}
                            text-indent: 0 !important;
                            font-weight: bold !important;
                            line-height: 1.1 !important; /* Tighter line height for headers looks cleaner */
                            page-break-after: avoid;
                        }}
                        
                        /* Prevent nested inline elements from compounding font sizes */
                        span, em, strong, b, i, a {{
                            font-size: inherit;
                            max-height: none;
                        }}
                        

                        
                        /* Preserve intentional formatting for poetry, verse, and code blocks */
                        pre, code, .poem, .verse, .stanza, .preformatted {{
                            text-align: left !important;
                            white-space: pre-wrap !important;
                            hyphens: none !important;
                            adobe-hyphenate: none !important;
                            text-indent: 0 !important;
                        }}
                        
                        code {{
                            font-family: monospace !important;
                            font-size: 0.9em;
                        }}
                        
                        /* Explicitly protect sub/superscript markers and drop caps */
                        sup, sub, .fn-marker {{
                            font-size: 0.75em !important;
                            line-height: 0 !important;
                        }}
                        
                        .dropcap, .first-letter {{
                            font-size: 1.4em !important;
                            line-height: 1.0 !important;
                        }}

                        /* 4. TABLES: local rules, without flattening genuine tables. */
                        table.xtc-table-data {{
                            width: 100% !important;
                            max-width: 100% !important;
                            border-collapse: collapse;
                            table-layout: auto;
                            margin: 0.45em 0 !important;
                            text-indent: 0 !important;
                            background: white !important;
                            background-color: white !important;
                            color: black !important;
                        }}
                        .xtc-table-page-break {{
                            display: block !important;
                            height: 0 !important;
                            min-height: 0 !important;
                            margin: 0 !important;
                            padding: 0 !important;
                            border: 0 !important;
                            page-break-after: always !important;
                            break-after: page !important;
                        }}
                        table.xtc-table-fresh-page {{
                            page-break-before: auto !important;
                            break-before: auto !important;
                        }}
                        table.xtc-table-data thead,
                        table.xtc-table-data tbody,
                        table.xtc-table-data tfoot,
                        table.xtc-table-data tr,
                        table.xtc-table-data td,
                        table.xtc-table-data th {{
                            background: white !important;
                            background-color: white !important;
                            color: black !important;
                            box-shadow: none !important;
                            filter: none !important;
                        }}
                        table.xtc-table-data td,
                        table.xtc-table-data th {{
                            box-sizing: border-box !important;
                            max-width: 100% !important;
                            padding: 0.22em !important;
                            vertical-align: top;
                            white-space: normal !important;
                            overflow-wrap: anywhere;
                            word-wrap: break-word;
                            word-break: normal;
                            text-indent: 0 !important;
                            line-height: 1.15 !important;
                        }}
                        table.xtc-table-data th *,
                        table.xtc-table-data thead * {{
                            background: transparent !important;
                            background-color: transparent !important;
                            color: black !important;
                        }}
                        table.xtc-table-data td > p,
                        table.xtc-table-data th > p {{
                            margin-top: 0 !important;
                            margin-bottom: 0.22em !important;
                            padding: 0 !important;
                            text-indent: 0 !important;
                            line-height: 1.15 !important;
                        }}
                        table.xtc-table-data td > p:last-child,
                        table.xtc-table-data th > p:last-child {{
                            margin-bottom: 0 !important;
                        }}
                        table.xtc-table-wide {{
                            table-layout: fixed !important;
                            font-size: 0.72em !important;
                        }}
                        table.xtc-table-very-wide {{
                            font-size: 0.64em !important;
                        }}
                        table.xtc-table-wide td,
                        table.xtc-table-wide th {{
                            padding: 0.12em !important;
                            line-height: 1.08 !important;
                        }}
                        .xtc-icon-callout {{
                            display: block !important;
                            width: 100% !important;
                            max-width: 100% !important;
                            box-sizing: border-box !important;
                            margin: 0.45em 0 !important;
                            padding: 0 !important;
                            border: 0 !important;
                            outline: 0 !important;
                            background: transparent !important;
                            color: black !important;
                            text-indent: 0 !important;
                        }}
                        .xtc-icon-callout img.xtc-table-icon {{
                            display: block !important;
                            float: left !important;
                            width: 80px !important;
                            min-width: 80px !important;
                            max-width: 80px !important;
                            height: auto !important;
                            min-height: 0 !important;
                            max-height: 140px !important;
                            object-fit: contain !important;
                            margin: 0.10em 0.55em 0.25em 0 !important;
                            padding: 0 !important;
                            border: 0 !important;
                            outline: 0 !important;
                            background: transparent !important;
                        }}
                        .xtc-icon-content {{
                            display: block !important;
                            min-width: 0 !important;
                            margin: 0 !important;
                            padding: 0 !important;
                            border: 0 !important;
                            background: transparent !important;
                            color: black !important;
                            overflow-wrap: anywhere;
                            word-wrap: break-word;
                        }}
                        .xtc-icon-content > p {{
                            margin-top: 0 !important;
                            margin-bottom: 0.30em !important;
                            padding: 0 !important;
                            text-indent: 0 !important;
                        }}
                        .xtc-icon-content > p:last-child {{ margin-bottom: 0 !important; }}
                        .xtc-icon-clear {{
                            display: block !important;
                            clear: both !important;
                            height: 0 !important;
                            margin: 0 !important;
                            padding: 0 !important;
                            border: 0 !important;
                        }}
                        /* One-cell callouts are blocks so MuPDF can split their
                           contents across pages without drawing an empty frame. */
                        .xtc-table-box {{
                            display: block !important;
                            box-sizing: border-box !important;
                            width: 100% !important;
                            max-width: 100% !important;
                            margin: 0.45em 0 !important;
                            padding: 0.35em !important;
                            border: 1px solid black;
                            text-indent: 0 !important;
                            overflow-wrap: anywhere;
                            word-wrap: break-word;
                            white-space: normal !important;
                        }}
                        .xtc-table-box > p {{
                            text-indent: 0 !important;
                            margin-top: 0 !important;
                            margin-bottom: 0.35em !important;
                        }}
                        .xtc-table-box > p:last-child {{
                            margin-bottom: 0 !important;
                        }}

                        /* Fix Image containers being restricted */
                        .svg-wrapper-replaced, img {{
                            max-width: 100% !important;
                            height: auto !important;
                        }}

                        /* ... Footnote styling  ... */
                        .inline-footnote-box {{
                            display: block; 
                            margin: 0.8em 0.8em !important; 
                            padding: 4px 0px 4px 8px !important; 
                            border-left: 2px solid black !important;
                            font-size: {max(9, int(self.font_size * 0.82))}pt !important;
                            line-height: {max(1.1, self.line_height * 0.9):.2f} !important;
                            text-indent: 0 !important;
                        }}
                        .inline-footnote-box strong, 
                        .inline-footnote-box p, 
                        .inline-footnote-box div, 
                        .inline-footnote-box span {{
                            display: inline !important; 
                            margin: 0 !important; 
                            padding: 0 !important; 
                            text-indent: 0 !important;
                        }}
                        .inline-footnote-box strong {{ margin-right: 4px !important; }}

                        .custom-spaced, .custom-spaced * {{
                            font-family: 'CustomFontSpaced' !important;
                            hyphens: manual !important;
                            word-break: normal !important;
                        }}
                    </style>
                """

        temp_chapter_starts = []
        running_page_count = 0
        render_dir = tempfile.gettempdir()

        final_toc_titles = []
        total_chaps = len(self.raw_chapters)
        selected_set = set(selected_indices)

        split_pattern = r'(?<!\bMr)(?<!\bMrs)(?<!\bDr)(?<!\bMs)(?<!\bSt)(?<!\bProf)(?<!\bCapt)(?<!\bGen)(?<!\bSen)(?<!\bRev)(?<=[.!?])\s+'
        word_pattern = re.compile(r'\b[a-zA-Z]{3,}\b')

        for idx, chapter in enumerate(self.raw_chapters):
            if progress_callback: progress_callback((idx / total_chaps) * 0.9)
            temp_html_path = os.path.join(render_dir, f"render_temp_{int(time.time())}_{idx}.html")
            import copy
            soup = copy.copy(chapter['soup'])

            # Spectra logic
            annotation_queues = {}
            if spectra_enabled and idx in selected_set and self.annotator and self.annotator.master_cache:
                full_text = soup.get_text(" ", strip=True)
                sentences = re.split(split_pattern, full_text)
                for sentence in sentences:
                    sentence = sentence.strip()
                    if not sentence: continue
                    ctx_hash = hashlib.md5(sentence.encode('utf-8')).hexdigest()[:6]
                    matches = word_pattern.findall(sentence)
                    for w in matches:
                        w_lower = w.lower()
                        unique_key = f"{w_lower}|{ctx_hash}"
                        if unique_key in self.annotator.master_cache:
                            if w_lower not in annotation_queues:
                                annotation_queues[w_lower] = []
                            annotation_queues[w_lower].append(self.annotator.master_cache[unique_key])

            # Normalize tables before the generic typography pass.  This keeps
            # real data tables intact but turns one-cell callout tables into
            # pageable blocks, avoiding empty table frames at page bottoms.
            soup = self._normalize_tables_for_mupdf(soup)
            # --- APPLY FORMATTING PROTECTION (2-arg version) ---
            soup = self._protect_formatting(soup, self.book_css)

            if show_footnotes:
                soup = self._inject_inline_footnotes(soup, chapter.get('filename', ''))

            if self.layout_settings.get("hyphenate_text", True):
                soup = hyphenate_html_text(soup, self.book_lang)

            # --- SVG HANDLING ---
            # SVG wrappers are used for icons and ordinary illustrations too,
            # so they must not all be promoted to full-page covers.
            for svg in soup.find_all('svg'):
                image_node = svg.find('image') or svg.find('image', recursive=True)
                if not image_node:
                    continue
                href = image_node.get('xlink:href') or image_node.get('href')
                if not href:
                    continue
                new_img = soup.new_tag('img', src=href)
                svg_classes = svg.get('class', [])
                if isinstance(svg_classes, str):
                    svg_classes = svg_classes.split()
                new_img['class'] = svg_classes + ['xtc-svg-derived']
                svg.replace_with(new_img)

            svg_derived = soup.find_all('img', class_='xtc-svg-derived')
            if len(svg_derived) == 1 and self._is_lone_body_image(svg_derived[0], soup):
                cover_img = svg_derived[0]
                cover_classes = cover_img.get('class', [])
                if isinstance(cover_classes, str):
                    cover_classes = cover_classes.split()
                cover_classes.append('svg-wrapper-replaced')
                cover_img['class'] = list(dict.fromkeys(cover_classes))
                cover_img['style'] = self._append_style(
                    cover_img.get('style', ''),
                    'width: 100% !important; height: auto !important; display: block !important; margin: 0 auto !important;'
                )

            # --- OPF-DECLARED COVER MATCH (authoritative signal) ---
            # The "exactly one image / zero text" heuristics above miss real
            # covers that ship with extra decorative images or stray markup
            # in the same XHTML file. If the EPUB declares an official cover
            # image (self.cover_image_filename, set in _find_cover_image),
            # match it by filename instead and flag it directly -- this
            # doesn't depend on the rest of the page being empty. Restricted
            # to the first few chapters since covers always live near the
            # start of the spine, so an unrelated chapter reusing the same
            # filename deep in the book won't be mistaken for the cover.
            cover_filename = getattr(self, 'cover_image_filename', None)
            already_tagged = any(
                'svg-wrapper-replaced' in (t.get('class') or []) for t in soup.find_all('img')
            )
            if cover_filename and idx < 3 and not already_tagged:
                for img_tag in soup.find_all('img'):
                    raw_src = img_tag.get('src', '')
                    if not raw_src:
                        continue
                    candidate_name = unquote(os.path.basename(raw_src.split('#', 1)[0].split('?', 1)[0]))
                    if candidate_name.lower() == cover_filename.lower():
                        classes = img_tag.get('class', [])
                        if isinstance(classes, str):
                            classes = classes.split()
                        classes.append('svg-wrapper-replaced')
                        img_tag['class'] = list(dict.fromkeys(classes))
                        img_tag['style'] = self._append_style(
                            img_tag.get('style', ''),
                            'width: 100% !important; height: auto !important; display: block !important; margin: 0 auto !important;'
                        )
                        print(f"DEBUG: Chapter {idx} image '{candidate_name}' matches OPF cover -> flagged as cover page")
                        break

            # --- IMAGE PROCESSING (SIZE DETECTION) ---

            # --- IMAGE PROCESSING (SIZE DETECTION) ---
            for img_tag in soup.find_all('img'):
                raw_src = img_tag.get('src', '')
                if not raw_src: continue
                clean_src = raw_src.split('#', 1)[0].split('?', 1)[0]
                src_filename = unquote(os.path.basename(clean_src))

                # Try to find the image data in our extracted dictionary
                found_data = self.book_images.get(src_filename) or self.book_images.get(
                    src_filename.lower())

                if found_data:
                    # 1. CRITICAL: Set the Base64 data FIRST
                    img_tag['src'] = found_data

                    # 2. Check special image classes before generic size detection.
                    classes = img_tag.get('class', [])
                    if isinstance(classes, str): classes = classes.split()
                    if 'xtc-table-icon' in classes:
                        img_tag['style'] = self._append_style(
                            img_tag.get('style', ''),
                            'display: block !important; float: left !important; width: 80px !important; min-width: 80px !important; max-width: 80px !important; height: auto !important; min-height: 0 !important; max-height: 140px !important; object-fit: contain !important; margin: 0.10em 0.55em 0.25em 0 !important; padding: 0 !important; border: 0 !important; outline: 0 !important; background: transparent !important;'
                        )
                        continue

                    if 'svg-wrapper-replaced' in classes:
                        # It is the cover. We already set width: 100% in the SVG block.
                        # Stop here so we don't overwrite it with "width: auto".
                        continue

                    # 2b. Some EPUBs author full-page title/illustration
                    # pages as a plain <img> instead of the <svg><image>
                    # cover convention. If this image is the ONLY image in
                    # the chapter and there's no other real text content,
                    # treat it the same way as a tagged cover so it also
                    # gets rendered full-screen instead of a normal
                    # centered-but-not-full-bleed image.
                    if self._is_lone_body_image(img_tag, soup):
                        img_tag['class'] = classes + ['svg-wrapper-replaced']
                        img_tag['style'] = self._append_style(
                            img_tag.get('style', ''),
                            "width: 100% !important; height: auto !important; display: block !important; margin: 0 auto !important;"
                        )
                        continue

                    # 3. Standard sizing logic for all other images
                    is_big_photo = False
                    try:
                        b64_str = found_data.split(',', 1)[1] if ',' in found_data else found_data
                        image_bytes = base64.b64decode(b64_str)
                        with Image.open(io.BytesIO(image_bytes)) as tmp_img:
                            w, h = tmp_img.size
                            if w > 150 or h > 150:
                                is_big_photo = True
                    except:
                        pass

                    # A raw pixel-size check misclassifies small decorative
                    # images (section/subchapter break marks, ornaments) as
                    # "inline icons" even when they are meant to stand alone
                    # on their own line. If the image has no real text/image
                    # siblings sharing its immediate container, treat it as
                    # a standalone (centered) image regardless of pixel size.
                    if not is_big_photo and self._is_standalone_image(img_tag):
                        is_big_photo = True

                    if is_big_photo:
                        # NOTE: PyMuPDF's Story/HTML layout engine (used to
                        # render these pages) has two real limitations that
                        # combine to break centering:
                        #   1. "margin: Npx auto" does NOT center a block
                        #      image -- it just leaves it pinned left.
                        #   2. A percentage "width" + percentage
                        #      "margin-left/right" on the CONTAINER (e.g. a
                        #      book's own <figure class="..."> rule) gets
                        #      miscomputed, silently shrinking/shifting the
                        #      available box regardless of what CSS we put
                        #      on the image itself.
                        # Fix: force the container to a plain full-width
                        # block (neutralizing any % width/margin from the
                        # book's own CSS) and center the image inside it
                        # using text-align + inline-block, which IS honored.
                        img_tag['style'] = self._append_style(
                            img_tag.get('style', ''),
                            "display: inline-block !important; margin: 10px 0 !important; max-width: 100% !important; width: auto !important; height: auto !important; clear: both;"
                        )

                        parent = img_tag.parent
                        if parent and parent.name in ['div', 'figure', 'p']:
                            # Only center the parent container if it has no prose text siblings (e.g., captions)
                            parent_text = ''.join(c for c in parent.contents if isinstance(c, str)).strip()
                            tag_siblings = [c for c in parent.find_all(True, recursive=False) if c is not img_tag]

                            if not parent_text and not tag_siblings:
                                parent['style'] = self._append_style(
                                    parent.get('style', ''),
                                    "text-align: center !important; display: block !important; width: 100% !important; max-width: 100% !important; margin: 10px 0 !important;"
                                )
                    else:
                        img_tag['style'] = self._append_style(
                            img_tag.get('style', ''),
                            "display: inline !important; vertical-align: middle !important; margin: 0 5px !important;"
                        )
                else:
                    img_tag['style'] = "display: none !important;"

            # --- NOWY BLOK: ---
            cover_candidates = (
                soup.find_all("img", class_="svg-wrapper-replaced")
                if soup.body
                else []
            )
            cover_node = (
                cover_candidates[0] if len(cover_candidates) == 1 else None
            )

            # Jeśli nie znaleziono przez SVG, sprawdź czy to pierwszy rozdział i czy jest to pojedynczy obraz bez tekstu
            if not cover_node and soup.body:
                all_body_imgs = soup.body.find_all("img")
                body_text = soup.body.get_text(strip=True)
                if (
                        len(all_body_imgs) == 1
                        and (len(body_text) < 10 or idx == 0)
                        and self._is_lone_body_image(all_body_imgs[0], soup)
                ):
                    cover_node = all_body_imgs[0]

            is_cover_chapter = False
            if cover_node and soup.body:
                is_cover_chapter = True
                cover_node = cover_node.extract()

                soup.body.clear()
                soup.body.append(cover_node)
                soup.body["style"] = (
                    "padding: 0 !important; margin: 0 !important; height: 100%"
                    " !important; overflow: hidden !important; text-align: center !important;"
                )
                cover_node["style"] = (
                    "max-width: 100% !important; max-height: 100% !important; width: auto !important; height: auto !important;"
                    " margin: 0 auto !important; display: inline-block !important;"
                )
            # =========================================================

            if idx in selected_set:
                temp_chapter_starts.append(running_page_count)
                final_toc_titles.append(chapter['title'])

            body_content = "".join([str(x) for x in soup.body.contents]) if soup.body else str(soup)
            if is_cover_chapter:
                cover_w = dev_w if ("Landscape" in orientation and self.layout_settings.get("keep_cover_portrait", True)) else self.screen_width
                cover_h = dev_h if ("Landscape" in orientation and self.layout_settings.get("keep_cover_portrait", True)) else self.screen_height
                cover_page_css = f"<style>@page {{ size: {cover_w}pt {cover_h}pt; margin: 0 !important; }} html, body {{ margin: 0 !important; padding: 0 !important; width: 100% !important; height: 100% !important; text-align: center !important; }}</style>"
                final_html = f"<html lang='{self.book_lang}'><head>{cover_page_css}</head><body>{body_content}</body></html>"
                rect = fitz.Rect(0, 0, cover_w, cover_h)
            else:
                final_html = f"<html lang='{self.book_lang}'><head><style>{patched_css}</style>{custom_css}</head><body>{body_content}</body></html>"
                rect = fitz.Rect(0, 0, self.screen_width, content_height)

            with open(temp_html_path, "w", encoding="utf-8") as f:
                f.write(final_html)

            doc = fitz.open(temp_html_path)
            doc.layout(rect=rect)
            self._collect_mupdf_font_warnings()
            self.fitz_docs.append((doc, chapter['has_image']))
            current_doc_idx = len(self.fitz_docs) - 1

            for page_i, page in enumerate(doc):
                page_defs = {}
                words_on_page = page.get_text("words")
                for *coords, text, block_n, line_n, word_n in words_on_page:
                    clean_text = text.replace('\u00ad', '').strip('.,!?;:"()[]{}“”‘’—–-').lower()
                    if clean_text in annotation_queues:
                        queue = annotation_queues[clean_text]
                        if queue:
                            definition = queue.pop(0)
                            coord_key = f"{int(coords[0])}_{int(coords[1])}"
                            page_defs[coord_key] = definition
                if page_defs:
                    self.page_annotation_data[(current_doc_idx, page_i)] = page_defs
                self.page_map.append((current_doc_idx, page_i))
            if is_cover_chapter:
                for p_offset in range(len(doc)):
                    self.cover_pages.add(running_page_count + p_offset)

            running_page_count += len(doc)


        # (TOC generation remains the same)
        num_toc_pages = 0
        if add_toc and final_toc_titles:
            toc_main_size = self.font_size
            toc_header_size = int(self.font_size * 1.2)
            toc_row_height = int(self.font_size * self.line_height * 1.2)
            toc_header_space = 100 + self.top_padding
            reserved_top = toc_header_space
            reserved_bottom = self.bottom_padding
            available_h = self.screen_height - reserved_top - reserved_bottom
            self.toc_items_per_page = max(1, int(available_h // toc_row_height))
            num_toc_pages = (len(final_toc_titles) + self.toc_items_per_page - 1) // self.toc_items_per_page

        requested_start = self.layout_settings.get("toc_insert_page", 1) - 1
        toc_insert_index = max(0, min(requested_start, len(self.page_map)))

        self.toc_data_final = []
        for i, title in enumerate(final_toc_titles):
            raw_start_index = temp_chapter_starts[i]
            display_page = raw_start_index + 1
            if raw_start_index >= toc_insert_index:
                display_page += num_toc_pages
            self.toc_data_final.append((title, display_page))

        if add_toc and final_toc_titles:
            self.toc_pages_images = self._render_toc_pages(self.toc_data_final, toc_row_height, toc_main_size,
                                                           toc_header_size)
        else:
            self.toc_pages_images = []

        self.total_pages = len(self.toc_pages_images) + len(self.page_map)
        self._finalize_font_warnings()
        if progress_callback: progress_callback(1.0)
        self.is_ready = True
        return True

    # ------------------------------------------------------------------
    # FONT DIAGNOSTICS
    # ------------------------------------------------------------------
    def _collect_mupdf_font_warnings(self):
        """MuPDF reports unloadable fonts only as warnings; pick those up after each layout."""
        try:
            raw = fitz.TOOLS.mupdf_warnings()
        except Exception:
            return
        if not raw:
            return
        for line in raw.splitlines():
            if "font" not in line.lower():
                continue
            m = (re.search(r"cannot locate font '([^']+)'", line)
                 or re.search(r"cannot load font-face:\s*(.+)", line))
            if m:
                self._font_load_failures.add(os.path.basename(m.group(1).strip(" '\"")))

    def _book_char_counts(self, indices):
        """Character frequencies of the selected chapters (per-chapter results are cached)."""
        cache = getattr(self, "_chapter_char_cache", None)
        if cache is None or cache.get("_book") is not self.raw_chapters:
            cache = {"_book": self.raw_chapters}
            self._chapter_char_cache = cache
        total = Counter()
        for i in indices:
            if not isinstance(i, int) or i < 0 or i >= len(self.raw_chapters):
                continue
            if i not in cache:
                try:
                    cache[i] = Counter(self.raw_chapters[i]["soup"].get_text())
                except Exception:
                    cache[i] = Counter()
            total.update(cache[i])
        return total

    def _check_glyph_coverage(self, font_path, label, selected_indices):
        """Warn when the body font has no glyph for characters the book actually uses."""
        if not font_path or font_path == "DEFAULT" or not os.path.exists(font_path):
            return
        try:
            missing = find_missing_glyphs(font_path, self._book_char_counts(selected_indices))
        except Exception as e:
            print(f"Glyph coverage check failed: {e}")
            return
        if not missing:
            return
        top = ", ".join(f"{ch} \u00d7{n}" for ch, n in missing.most_common(8))
        more = f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""
        self.font_warnings.append(
            f"'{label}' has no glyphs for {len(missing)} character(s) used in this book: {top}{more}. "
            f"A fallback font will be substituted for them.")

    def _finalize_font_warnings(self):
        self._collect_mupdf_font_warnings()
        if self._font_load_failures:
            names = ", ".join(sorted(self._font_load_failures)[:4])
            extra = f" (+{len(self._font_load_failures) - 4} more)" if len(self._font_load_failures) > 4 else ""
            ver = str(getattr(fitz, "__version__", None) or getattr(fitz, "VersionBind", "?"))
            hint = ""
            if not ver.startswith("1.24"):
                hint = (f" (PyMuPDF {ver}: if this hits every custom font, this version may refuse to load font "
                        f"files - 1.24.x is known to work: pip install pymupdf==1.24.14)")
            self.font_warnings.append(
                f"{len(self._font_load_failures)} font file(s) failed to load: {names}{extra}. "
                f"MuPDF used fallback fonts.{hint}")
        for w in self.font_warnings:
            print(f"[font warning] {w}")

    def _get_ui_font(self, size):
        mode = self.layout_settings.get("ui_font_source", "Body Font")

        # 1. Body Font requested
        if mode == "Body Font":
            # Direct font path chosen by user
            if self.font_path and self.font_path != "EPUB_ORIGINAL":
                f = get_pil_font(self.font_path, size)
                if f: return f

            # When "extracted from EPUB" (self.font_path == "EPUB_ORIGINAL" or not self.font_path):
            # Use the extracted font matching the book's body family
            if getattr(self, 'extracted_fonts', None):
                try:
                    families = group_epub_fonts(self.extracted_fonts)
                    body_family = pick_epub_body_family(families, getattr(self, 'book_css', ''))
                    if body_family and body_family in families:
                        face = families[body_family].get("regular") or next(iter(families[body_family].values()), None)
                        if face:
                            f = get_pil_font(face, size)
                            if f: return f
                    for fam, faces in families.items():
                        face = faces.get("regular") or next(iter(faces.values()), None)
                        if face:
                            f = get_pil_font(face, size)
                            if f: return f
                except Exception:
                    pass

        # Helper to search and load fonts from paths, bundled directory, or system
        def _resolve_font(cand):
            if not cand:
                return None
            if os.path.isabs(cand) and os.path.exists(cand):
                try:
                    return ImageFont.truetype(cand, size)
                except Exception:
                    return None
            for f_dir in ("Fonts", "fonts"):
                full_p = os.path.join(INTERNAL_DIR, f_dir, cand)
                if os.path.exists(full_p):
                    try:
                        return ImageFont.truetype(full_p, size)
                    except Exception:
                        pass
                f_dir_full = os.path.join(INTERNAL_DIR, f_dir)
                if os.path.isdir(f_dir_full):
                    cand_base = os.path.basename(cand)
                    for root, _, files in os.walk(f_dir_full):
                        if cand_base in files:
                            try:
                                return ImageFont.truetype(os.path.join(root, cand_base), size)
                            except Exception:
                                pass
            try:
                return ImageFont.truetype(cand, size)
            except Exception:
                return None

        # 2. Sans-Serif options
        if mode == "Sans-Serif":
            sans_candidates = [
                "DejavuSans-Regular.ttf",
                "DejaVuSans.ttf",
                "AtkinsonHyperlegible-Regular.ttf",
                "/System/Library/Fonts/Helvetica.ttc",
                "/System/Library/Fonts/Supplemental/Arial.ttf",
                "/Library/Fonts/Arial.ttf",
                "arial.ttf", "segoeui.ttf", "helvetica.ttf"
            ]
            for cand in sans_candidates:
                f = _resolve_font(cand)
                if f: return f

        # 3. Serif / Fallbacks (prioritize bundled Georgia, Charis, Bookerly, Literata, etc.)
        serif_candidates = [
            "Georgia/georgia.ttf",
            "Charis-Regular.ttf",
            "Bookerly/Bookerly.ttf",
            "DejaVuSerif.ttf",
            "Literata/Literata-VariableFont_opsz,wght.ttf",
            "/System/Library/Fonts/Supplemental/Georgia.ttf",
            "/System/Library/Fonts/Times.ttc",
            "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
            "/Library/Fonts/Georgia.ttf",
            "georgia.ttf", "times.ttf", "times.ttc"
        ]
        for cand in serif_candidates:
            f = _resolve_font(cand)
            if f: return f

        # 4. Fallback to any bundled font file
        for f_dir in ("Fonts", "fonts"):
            f_dir_full = os.path.join(INTERNAL_DIR, f_dir)
            if os.path.isdir(f_dir_full):
                for root, _, files in os.walk(f_dir_full):
                    for file in files:
                        if file.lower().endswith((".ttf", ".otf")):
                            f = _resolve_font(os.path.join(root, file))
                            if f: return f

        return ImageFont.load_default()

    def _render_toc_pages(self, toc_entries, row_height, font_size, header_size):
        pages = []

        font_main = self._get_ui_font(font_size)

        # --- NEW: Auto-scale Header ---
        header_text = "TABLE OF CONTENTS"
        safe_width = self.screen_width - 30

        # Start large and shrink until it fits
        current_header_size = header_size
        font_header = self._get_ui_font(current_header_size)

        while current_header_size > 12:
            if font_header.getlength(header_text) <= safe_width:
                break
            current_header_size -= 2
            font_header = self._get_ui_font(current_header_size)
        # -----------------------------

        left_margin = 40
        right_margin = 40
        column_gap = 20
        limit = self.toc_items_per_page

        for i in range(0, len(toc_entries), limit):
            chunk = toc_entries[i: i + limit]
            img = Image.new('1', (self.screen_width, self.screen_height), 1)
            draw = ImageDraw.Draw(img)

            # Draw Header using the new SAFE font size
            header_w = font_header.getlength(header_text)
            header_y = 40 + self.top_padding

            # 1. Draw Text (Only once!)
            draw.text(((self.screen_width - header_w) // 2, header_y), header_text, font=font_header, fill=0)

            # 2. Calculate Line Position (Using the NEW current_header_size)
            line_y = header_y + int(current_header_size * 1.5)

            # 3. Draw Divider Line
            draw.line((left_margin, line_y, self.screen_width - right_margin, line_y), fill=0)

            y = line_y + int(font_size * 1.2)
            for title, pg_num in chunk:
                pg_str = str(pg_num)
                pg_w = font_main.getlength(pg_str)
                max_title_w = self.screen_width - left_margin - right_margin - pg_w - column_gap
                display_title = title
                if font_main.getlength(display_title) > max_title_w:
                    while font_main.getlength(display_title + "...") > max_title_w and len(
                            display_title) > 0: display_title = display_title[:-1]
                    display_title += "..."
                draw.text((left_margin, y), display_title, font=font_main, fill=0)
                title_end_x = left_margin + font_main.getlength(display_title) + 5
                dots_end_x = self.screen_width - right_margin - pg_w - 10
                if dots_end_x > title_end_x:
                    dots_text = "." * int((dots_end_x - title_end_x) / font_main.getlength("."))
                    draw.text((title_end_x, y), dots_text, font=font_main, fill=0)
                draw.text((self.screen_width - right_margin - pg_w, y), pg_str, font=font_main, fill=0)
                y += row_height
            pages.append(img)
        return pages

    def _draw_progress_bar(self, draw, y, height, global_page_index, override_x=None, override_width=None):
        if self.total_pages <= 0: return
        show_ticks = self.layout_settings.get("bar_show_ticks", True)
        tick_h = self.layout_settings.get("bar_tick_height", 6)
        show_marker = self.layout_settings.get("bar_show_marker", True)
        marker_r = self.layout_settings.get("bar_marker_radius", 5)
        marker_col_str = self.layout_settings.get("bar_marker_color", "Black")
        marker_fill = (255, 255, 255) if marker_col_str == "White" else (0, 0, 0)
        if override_x is not None and override_width is not None:
            x_start = override_x
            bar_width_px = override_width
        else:
            x_start = 10
            bar_width_px = self.screen_width - 20

        draw.rectangle([x_start, y, x_start + bar_width_px, y + height], fill=(255, 255, 255), outline=(0, 0, 0))

        if show_ticks:
            bar_center_y = y + (height / 2)
            t_top = bar_center_y - (tick_h / 2)
            t_bot = bar_center_y + (tick_h / 2)
            chapter_pages = [item[1] for item in self.toc_data_final]
            for cp in chapter_pages:
                mx = int(((cp - 1) / self.total_pages) * bar_width_px) + x_start
                draw.line([mx, t_top, mx, t_bot], fill=(0, 0, 0), width=1)
        curr_page_disp = global_page_index + 1
        fill_width = int((curr_page_disp / self.total_pages) * bar_width_px)
        draw.rectangle([x_start, y, x_start + fill_width, y + height], fill=(0, 0, 0))

        if show_marker:
            cx = x_start + fill_width
            cy = y + (height / 2)
            draw.ellipse([cx - marker_r, cy - marker_r, cx + marker_r, cy + marker_r], fill=marker_fill,
                         outline=(0, 0, 0))

    def _get_page_text_elements(self, global_page_index):
        page_num_disp = global_page_index + 1
        percent = int((page_num_disp / self.total_pages) * 100)
        current_title = ""
        num_toc = len(self.toc_pages_images)
        if global_page_index < num_toc:
            current_title = "Table of Contents"
            chap_page_disp = f"{global_page_index + 1}/{num_toc}"
        else:
            for title, start_pg in reversed(self.toc_data_final):
                if page_num_disp >= start_pg:
                    current_title = title
                    break
            pm_idx = global_page_index - num_toc
            if 0 <= pm_idx < len(self.page_map):
                doc_idx, page_idx = self.page_map[pm_idx]
                doc_ref = self.fitz_docs[doc_idx][0]
                chap_total = len(doc_ref)
                chap_page_disp = f"{page_idx + 1}/{chap_total}"
            else:
                chap_page_disp = "1/1"
        return {'pagenum': f"{page_num_disp}/{self.total_pages}", 'title': current_title, 'chap_page': chap_page_disp,
                'percent': f"{percent}%"}

    def _draw_text_line(self, draw, y, font, elements_list, align, global_page_index=0):
        if not elements_list: return
        separator = self.layout_settings.get("ui_separator", "   |   ")
        margin_x = int(self.layout_settings.get("ui_side_margin", 15))
        bar_height = int(self.layout_settings.get("bar_height", 4))

        # --- STABILITY FIX: Calculate Reserved Widths ---
        processed_elements = []
        for k, v in elements_list:
            if k == 'progress':
                processed_elements.append({'key': k, 'text': v, 'width': 0, 'reserved': 0})
                continue

            real_w = font.getlength(v)
            reserved_w = real_w

            if k == 'pagenum':
                digits = len(str(self.total_pages))
                max_s = f"{'8' * digits}/{'8' * digits}"
                reserved_w = max(real_w, font.getlength(max_s))
            elif k == 'percent':
                reserved_w = max(real_w, font.getlength("100%"))
            elif k == 'chap_page':
                if '/' in v:
                    try:
                        _, total = v.split('/')
                        digits = len(total)
                        max_s = f"{'8' * digits}/{'8' * digits}"
                        reserved_w = max(real_w, font.getlength(max_s))
                    except:
                        pass

            processed_elements.append({'key': k, 'text': v, 'width': real_w, 'reserved': reserved_w})

        # Helper to truncate
        def truncate_to_fit(text, max_w):
            if max_w <= 0: return ""
            if font.getlength(text) <= max_w: return text
            ellipsis = "..."
            temp = text
            while font.getlength(temp + ellipsis) > max_w and len(temp) > 0:
                temp = temp[:-1]
            return temp + ellipsis

        has_prog = any(x['key'] == 'progress' for x in processed_elements)

        if has_prog:
            non_title_w = sum(x['reserved'] for x in processed_elements if x['key'] not in ['progress', 'title'])
            sep_width = font.getlength(separator)
            total_sep_width = sep_width * (len(processed_elements) - 1)

            if align == "Justify":
                min_bar_w = int(self.screen_width * 0.25)
                avail_for_title = self.screen_width - (2 * margin_x) - non_title_w - total_sep_width - min_bar_w
            else:
                bar_width = int(self.screen_width * 0.3)
                avail_for_title = self.screen_width - (2 * margin_x) - non_title_w - total_sep_width - bar_width

            for item in processed_elements:
                if item['key'] == 'title':
                    item['text'] = truncate_to_fit(item['text'], avail_for_title)
                    item['width'] = font.getlength(item['text'])
                    item['reserved'] = item['width']

            current_text_w = sum(x['reserved'] for x in processed_elements if x['key'] != 'progress')

            if align == "Justify":
                bar_width = self.screen_width - (2 * margin_x) - current_text_w - total_sep_width
                if bar_width < 10: bar_width = 10
            else:
                bar_width = int(self.screen_width * 0.3)
                # Safety: Ensure fixed bar doesn't push content off-screen
                avail = self.screen_width - (2 * margin_x) - current_text_w - total_sep_width
                if bar_width > avail: bar_width = max(10, avail)

            total_content_width = current_text_w + bar_width + total_sep_width

            if align == "Justify":
                start_x = margin_x
            elif align == "Center":
                start_x = (self.screen_width - total_content_width) // 2
            elif align == "Right":
                start_x = self.screen_width - margin_x - total_content_width
            else:
                start_x = margin_x

            if start_x < margin_x: start_x = margin_x

            ascent, descent = font.getmetrics()
            text_height = ascent + descent
            bar_y = y + (text_height / 2) - (bar_height / 2)

            current_x = start_x
            for i, item in enumerate(processed_elements):
                if item['key'] == 'progress':
                    self._draw_progress_bar(draw, bar_y, bar_height, global_page_index, override_x=current_x,
                                            override_width=bar_width)
                    current_x += bar_width
                else:
                    draw_x = current_x + (item['reserved'] - item['width']) // 2
                    draw.text((draw_x, y), item['text'], font=font, fill=(0, 0, 0))
                    current_x += item['reserved']

                if i < len(processed_elements) - 1:
                    if separator.strip():
                        draw.text((current_x, y), separator, font=font, fill=(0, 0, 0))
                    current_x += sep_width
            return

        # --- STANDARD LAYOUT ---
        non_title_w = sum(x['reserved'] for x in processed_elements if x['key'] != 'title')
        sep_w = font.getlength(separator)
        total_sep_w = sep_w * (len(processed_elements) - 1) if len(processed_elements) > 1 else 0
        content_max_w = self.screen_width - (2 * margin_x)

        if align == "Justify" and len(processed_elements) > 1:
            left_item = processed_elements[0]
            right_item = processed_elements[-1]
            mid_items = processed_elements[1:-1] if len(processed_elements) > 2 else []

            gap = 20
            title_loc = "none"
            if left_item['key'] == 'title':
                title_loc = "left"
            elif right_item['key'] == 'title':
                title_loc = "right"
            elif any(x['key'] == 'title' for x in mid_items):
                title_loc = "mid"

            w_left = left_item['reserved'] if left_item['key'] != 'title' else 0
            w_right = right_item['reserved'] if right_item['key'] != 'title' else 0
            w_mid_static = sum(x['reserved'] for x in mid_items if x['key'] != 'title')
            w_mid_sep = sep_w * (len(mid_items) - 1) if len(mid_items) > 1 else 0

            if title_loc == "left":
                avail = content_max_w - w_right - gap
                if mid_items: avail -= (w_mid_static + w_mid_sep + gap)
                left_item['text'] = truncate_to_fit(left_item['text'], avail)
                left_item['width'] = font.getlength(left_item['text'])
                left_item['reserved'] = left_item['width']
            elif title_loc == "right":
                avail = content_max_w - w_left - gap
                if mid_items: avail -= (w_mid_static + w_mid_sep + gap)
                right_item['text'] = truncate_to_fit(right_item['text'], avail)
                right_item['width'] = font.getlength(right_item['text'])
                right_item['reserved'] = right_item['width']
            elif title_loc == "mid":
                avail_total_mid = content_max_w - w_left - w_right - (2 * gap)
                avail_title = avail_total_mid - w_mid_static - w_mid_sep
                for item in mid_items:
                    if item['key'] == 'title':
                        item['text'] = truncate_to_fit(item['text'], avail_title)
                        item['width'] = font.getlength(item['text'])
                        item['reserved'] = item['width']

            # DRAW LEFT
            draw.text((margin_x, y), left_item['text'], font=font, fill=(0, 0, 0))

            # DRAW RIGHT
            slot_x_r = self.screen_width - margin_x - right_item['reserved']
            draw_x_r = slot_x_r + (right_item['reserved'] - right_item['width'])
            draw.text((draw_x_r, y), right_item['text'], font=font, fill=(0, 0, 0))

            # DRAW MIDDLE
            if mid_items:
                mid_block_w = sum(x['reserved'] for x in mid_items) + w_mid_sep
                mid_start_x = (self.screen_width - mid_block_w) // 2
                min_x = margin_x + left_item['reserved'] + gap
                max_x = self.screen_width - margin_x - right_item['reserved'] - gap - mid_block_w

                if mid_start_x < min_x: mid_start_x = min_x
                if mid_start_x > max_x: mid_start_x = max_x

                if max_x >= min_x:
                    curr_mx = mid_start_x
                    for i, item in enumerate(mid_items):
                        dx = curr_mx + (item['reserved'] - item['width']) // 2
                        draw.text((dx, y), item['text'], font=font, fill=(0, 0, 0))
                        curr_mx += item['reserved']
                        if i < len(mid_items) - 1:
                            draw.text((curr_mx, y), separator, font=font, fill=(0, 0, 0))
                            curr_mx += sep_w
        else:
            avail_for_title = content_max_w - non_title_w - total_sep_w
            for item in processed_elements:
                if item['key'] == 'title':
                    item['text'] = truncate_to_fit(item['text'], avail_for_title)
                    item['width'] = font.getlength(item['text'])
                    item['reserved'] = item['width']

            total_block_w = sum(x['reserved'] for x in processed_elements) + total_sep_w
            x_pos = margin_x
            if align == "Center":
                x_pos = (self.screen_width - total_block_w) // 2
            elif align == "Right":
                x_pos = self.screen_width - margin_x - total_block_w
            if x_pos < margin_x: x_pos = margin_x

            curr_x = x_pos
            for i, item in enumerate(processed_elements):
                dx = curr_x + (item['reserved'] - item['width']) // 2
                draw.text((dx, y), item['text'], font=font, fill=(0, 0, 0))
                curr_x += item['reserved']
                if i < len(processed_elements) - 1:
                    draw.text((curr_x, y), separator, font=font, fill=(0, 0, 0))
                    curr_x += sep_w

    def _get_active_elements(self, bar_role, text_data):
        s = self.layout_settings
        active = []
        for key in ['title', 'pagenum', 'chap_page', 'percent']:
            pos_val = s.get(f"pos_{key}", "Hidden")
            if pos_val == bar_role:
                order = int(s.get(f"order_{key}", 99))
                content = text_data.get(key, "")
                if content: active.append((order, key, content))
        # Check for Inline Progress Bar
        pos_prog = s.get("pos_progress", "Footer (Below Text)")
        if pos_prog == f"{bar_role} (Inline)":
            order = int(s.get("order_progress", 99))
            active.append((order, 'progress', '__PROGRESS_BAR__'))

        active.sort(key=lambda x: x[0])
        return [(x[1], x[2]) for x in active]

    def _draw_header(self, draw, global_page_index):
        settings = self.layout_settings
        font_size = settings.get("header_font_size", 16)
        margin = settings.get("header_margin", 0)
        align = settings.get("header_align", "Center")
        bar_height = settings.get("bar_height", 4)
        pos_prog = settings.get("pos_progress", "Footer (Below Text)")
        text_data = self._get_page_text_elements(global_page_index)
        elements = self._get_active_elements("Header", text_data)
        has_text = len(elements) > 0
        has_bar = False
        bar_above_text = True
        if pos_prog == "Header (Above Text)":
            has_bar = True;
            bar_above_text = True
        elif pos_prog == "Header (Below Text)":
            has_bar = True;
            bar_above_text = False
        gap = 6
        font_ui = self._get_ui_font(font_size)
        current_y = margin
        if bar_above_text and has_bar:
            self._draw_progress_bar(draw, current_y, bar_height, global_page_index)
            current_y += bar_height + gap
        if has_text:
            self._draw_text_line(draw, current_y, font_ui, elements, align, global_page_index)

            current_y += font_size + gap
        if not bar_above_text and has_bar: self._draw_progress_bar(draw, current_y, bar_height, global_page_index)

    def _draw_footer(self, draw, global_page_index):
        settings = self.layout_settings
        font_size = settings.get("footer_font_size", 16)
        margin = settings.get("footer_margin", 0)
        align = settings.get("footer_align", "Center")
        bar_height = settings.get("bar_height", 4)
        pos_prog = settings.get("pos_progress", "Footer (Below Text)")
        text_data = self._get_page_text_elements(global_page_index)
        elements = self._get_active_elements("Footer", text_data)
        has_text = len(elements) > 0
        has_bar = False
        bar_above_text = True
        if pos_prog == "Footer (Above Text)":
            has_bar = True;
            bar_above_text = True
        elif pos_prog == "Footer (Below Text)":
            has_bar = True;
            bar_above_text = False
        gap = 6
        font_ui = self._get_ui_font(font_size)
        anchor_y = self.screen_height - margin
        text_h = font_size if has_text else 0
        bar_h = bar_height if has_bar else 0
        text_y = 0
        bar_y = 0
        if has_bar and has_text:
            if bar_above_text:
                text_y = anchor_y - text_h
                bar_y = text_y - gap - bar_h
            else:
                bar_y = anchor_y - bar_h
                text_y = bar_y - gap - text_h
        elif has_text:
            text_y = anchor_y - text_h
        elif has_bar:
            bar_y = anchor_y - bar_h
        if has_bar: self._draw_progress_bar(draw, bar_y, bar_height, global_page_index)
        if has_text: self._draw_text_line(draw, text_y, font_ui, elements, align, global_page_index)

        # --- INSIDE CLASS EpubProcessor ---

    def _normalize_tables_for_mupdf(self, soup):
        """Classify and normalize tables before MuPDF pagination.

        MuPDF can paint the border of a table at the bottom of one page and
        move the actual row to the next page.  The most troublesome case is a
        one-cell table used as a multi-paragraph callout rather than tabular
        data.  Such callouts are converted to ordinary blocks, while genuine
        data tables keep their table structure.
        """
        for table in list(soup.find_all('table')):
            classes = table.get('class', [])
            if isinstance(classes, str):
                classes = classes.split()

            rows = table.find_all('tr')
            direct_cells = []
            for row in rows:
                direct_cells.extend(row.find_all(['td', 'th'], recursive=False))

            has_spans = any(
                cell.has_attr('rowspan') or cell.has_attr('colspan')
                for cell in direct_cells
            )
            max_columns = max(
                (sum(int(cell.get('colspan', 1) or 1)
                     for cell in row.find_all(['td', 'th'], recursive=False))
                 for row in rows),
                default=0,
            )

            # Invalid but fairly common EPUB CSS value.
            for cell in direct_cells:
                style = cell.get('style', '')
                style = re.sub(
                    r'vertical-align\s*:\s*center',
                    'vertical-align: middle',
                    style,
                    flags=re.IGNORECASE,
                )
                if style:
                    cell['style'] = style

            # A single cell containing several block elements is a callout,
            # not tabular data.  A normal div can split cleanly across pages.
            if len(rows) == 1 and len(direct_cells) == 1 and not has_spans:
                cell = direct_cells[0]
                block_count = len(cell.find_all(
                    ['p', 'div', 'blockquote', 'ul', 'ol', 'pre'],
                    recursive=False,
                ))
                text_len = len(cell.get_text(' ', strip=True))
                if block_count >= 2 or text_len >= 500:
                    box = soup.new_tag('div')
                    box['class'] = classes + ['xtc-table-box']
                    if table.get('style'):
                        box['style'] = table.get('style')
                    if table.get('id'):
                        box['id'] = table.get('id')
                    for child in list(cell.contents):
                        box.append(child.extract() if hasattr(child, 'extract') else child)
                    table.replace_with(box)
                    continue

            classes.append('xtc-table-data')
            # Starting a substantial data table in the small remainder of a
            # page makes MuPDF paint an empty table frame and move the first
            # row to the next page.  Start such tables on a fresh page.  The
            # table can still continue normally across later pages.
            if len(rows) >= 3 or max_columns >= 4:
                classes.append('xtc-table-fresh-page')
                # MuPDF may draw the table border before honoring a break on
                # the table itself.  A separate zero-height block before the
                # table forces the page transition before any table paint.
                breaker = soup.new_tag('div')
                breaker['class'] = ['xtc-table-page-break']
                table.insert_before(breaker)
            if max_columns >= 5:
                classes.append('xtc-table-wide')
            if max_columns >= 7:
                classes.append('xtc-table-very-wide')

            # Preserve icon + text tables.  Only label them for local CSS.
            is_icon_callout = (
                len(rows) == 1 and max_columns == 2 and
                any(
                    cell.find('img') is not None or
                    any('ikon' in str(c).lower() for c in (cell.get('class') or []))
                    for cell in direct_cells
                )
            )
            if is_icon_callout:
                # MuPDF renders a two-cell icon callout unreliably: the icon
                # cell may collapse into a tiny square instead of spanning the
                # full callout height.  Replace only this very specific table
                # shape with a flex callout.  Genuine data tables stay tables.
                icon_cell, text_cell = direct_cells[0], direct_cells[1]
                callout = soup.new_tag('div')
                callout['class'] = classes + ['xtc-icon-callout']
                if table.get('id'):
                    callout['id'] = table.get('id')

                # MuPDF Story does not reliably implement flexbox. Keep
                # icon and prose in one normal flow and float the image.
                icon_img = icon_cell.find('img')
                if icon_img is not None:
                    icon_img = icon_img.extract()
                    icon_classes = icon_img.get('class', [])
                    if isinstance(icon_classes, str):
                        icon_classes = icon_classes.split()
                    if 'xtc-table-icon' not in icon_classes:
                        icon_classes.append('xtc-table-icon')
                    icon_img['class'] = icon_classes
                    icon_img.attrs.pop('width', None)
                    icon_img.attrs.pop('height', None)
                    callout.append(icon_img)

                content_col = soup.new_tag('div')
                content_col['class'] = ['xtc-icon-content']
                for child in list(text_cell.contents):
                    content_col.append(child.extract() if hasattr(child, 'extract') else child)
                callout.append(content_col)

                clear_node = soup.new_tag('div')
                clear_node['class'] = ['xtc-icon-clear']
                callout.append(clear_node)
                table.replace_with(callout)
                continue

            table['class'] = list(dict.fromkeys(classes))

        return soup

    def _protect_formatting(self, soup, css_text):
        """
        Refined: Strips rigid layout locks but PRESERVES structural indentation.
        Aggressively propagates spacing classes to children to fix nested span resets.
        """

        spacing_classes = set()
        # Classes from the book's own CSS that set an explicit alignment,
        # e.g. ".poem { text-align: center; }" or ".dedication { text-align:
        # right; }". These are NOT caught by the inline-style scan in PHASE 1
        # below (tag.get('style') is empty for a class-based rule), so
        # without this, the blanket "text-align: {self.text_align}
        # !important" rule in custom_css silently overrides every
        # deliberately-aligned element that wasn't authored with an inline
        # style -- which, in practice, is most of them. This dict maps
        # class name -> alignment value so PHASE 3 can promote it to an
        # inline override the same way inline-authored alignment already
        # gets preserved.
        alignment_classes = {}

        # Same idea as alignment_classes, but for an explicit paragraph
        # indent set via a CSS class, e.g. ".noindent { text-indent: 0; }"
        # -- a very common convention for the first paragraph after a
        # heading. Without this, the blanket "text-indent: 1.5em" default
        # in custom_css silently re-indents every one of those paragraphs.
        indent_classes = {}

        margin_top_classes = {}
        margin_bottom_classes = {}

        margin_right_classes = {}
        font_style_classes = {}

        # 1. HARD KILL PATTERN (Layout locks)
        nuclear_pattern = re.compile(
            r'(?:^|;)\s*(width|height|max-width|max-height|line-height|background-color)\s*:[^;]+',
            flags=re.IGNORECASE
        )
        # Table cells/columns often use width for real column layout, not
        # decorative page-width locking -- strip everything else but leave
        # width/height alone for these tags.
        nuclear_pattern_no_dims = re.compile(
            r'(?:^|;)\s*(max-width|max-height|line-height|background-color)\s*:[^;]+',
            flags=re.IGNORECASE
        )
        TABLE_LAYOUT_TAGS = {'table', 'tr', 'td', 'th', 'col', 'colgroup', 'thead', 'tbody', 'tfoot'}




        # --- PHASE 1: CLEAN ATTRIBUTES & STYLES ---
        for tag in soup.find_all(True):
            is_table_tag = tag.name in TABLE_LAYOUT_TAGS

            # A. Attribute Cleaning
            if tag.name not in ['img', 'table', 'td', 'th']:
                if tag.has_attr('width'): del tag['width']
                if tag.has_attr('height'): del tag['height']

            # A2. Legacy `align="center"` HTML attribute. Not caught by the
            # style scan below (there's no style attribute for it to live
            # in), so on its own it would just get overridden by the
            # blanket text-align rule in custom_css. Translate it into the
            # same inline-override path used for CSS-authored alignment,
            # then remove the attribute (it's been absorbed into style).
            align_attr = tag.get('align', '').strip().lower() if tag.has_attr('align') else None
            if tag.has_attr('align'):
                del tag['align']

            # B. Style Cleaning
            style = tag.get('style')
            if style:
                alignment_match = re.search(r'text-align\s*:\s*(center|right|justify)', style, re.I)
                # Updated to catch right margins too
                indent_match = re.search(
                    r'(margin-left|padding-left|margin-right|padding-right)\s*:\s*([\d\.]+(?:em|px|%|pt))', style, re.I)
                top_margin_match = re.search(r'margin-top\s*:\s*([\d\.]+(?:em|px|%|pt))', style, re.I)
                # Explicit author-set paragraph indent (including "0" --
                # that's the whole point, "no indent" is a real, common,
                # deliberate choice and needs to be distinguishable from
                # "book didn't say anything, use the default").
                text_indent_match = re.search(r'text-indent\s*:\s*([\d\.]+(?:em|px|%|pt)|0)', style, re.I)

                active_nuclear = nuclear_pattern_no_dims if is_table_tag else nuclear_pattern
                clean_style = active_nuclear.sub("", style)
                clean_style = clamp_css_font_sizes(clean_style, max_em=1.3)
            else:
                alignment_match = indent_match = top_margin_match = text_indent_match = None
                clean_style = ""

            injections = []
            if alignment_match:
                align_val = alignment_match.group(1).lower()
                injections.append(f"text-align: {align_val} !important")
                if align_val in ['center', 'right']:
                    injections.append("text-indent: 0 !important")
            elif align_attr in ('center', 'right', 'justify'):
                injections.append(f"text-align: {align_attr} !important")
                if align_attr in ('center', 'right'):
                    injections.append("text-indent: 0 !important")

            if text_indent_match and not alignment_match and align_attr not in ('center', 'right'):
                injections.append(f"text-indent: {text_indent_match.group(1)} !important")

            if indent_match:
                prop_name = "margin-right" if "right" in indent_match.group(1).lower() else "margin-left"
                val = indent_match.group(2)
                if '%' in val and float(val.strip('%')) > 20: val = "10%"
                injections.append(f"{prop_name}: {val} !important")

            if top_margin_match:
                raw_val = top_margin_match.group(1)
                num_m = re.match(r'([\d.]+)', raw_val)
                unit_m = re.search(r'(em|px|%|pt)$', raw_val, re.I)
                if num_m and unit_m:
                    num = float(num_m.group(1))
                    unit = unit_m.group(1).lower()
                    cap = {'em': 3.0, 'px': 48.0, 'pt': 36.0, '%': 100.0}.get(unit, num)
                    num = max(0.0, min(num, cap))
                    injections.append(f"margin-top: {num:g}{unit} !important")
                else:
                    injections.append("margin-top: 1.5em !important")

            clean_style = clean_style.strip().strip(';')
            if injections:
                clean_style += ("; " if clean_style else "") + "; ".join(injections)

            if clean_style:
                tag['style'] = clean_style
            elif tag.has_attr('style'):
                del tag['style']

        # --- PHASE 2: PARSE CSS FOR SPACING CLASSES ---
        if css_text:
            css_clean = re.sub(r'\s+', ' ', css_text).lower()
            css_clean = re.sub(r'/\*.*?\*/', '', css_clean)

            def _simple_classes(selector_str):
                # Only trust a class as a deliberate per-element signal when
                # the selector is a plain ".foo" (optionally comma-listed
                # with other plain ".bar" selectors). A naive
                # re.findall(r'\.([a-z0-9_-]+)') across the whole selector
                # text -- which is what this used to do -- also matches
                # compound selectors like "p.foo" and descendant selectors
                # like ".wrapper .foo", silently attributing the wrapper's
                # class to the target rule. That's how a book-wide
                # structural/reset class (e.g. Calibre's generic
                # "calibre1", present on nearly every <p>) ends up treated
                # as "this element was deliberately given text-align: left",
                # clobbering the justify setting for the whole file.
                out = []
                for segment in selector_str.split(','):
                    m = re.fullmatch(r'(?:[a-z0-9_-]+)?\.([a-z0-9_-]+)', segment.strip(), flags=re.IGNORECASE)
                    if m:
                        out.append(m.group(1))
                return out

            blocks = css_clean.split('}')
            for block in blocks:
                if '{' not in block: continue
                parts = block.split('{')
                if len(parts) < 2: continue
                selector_str = parts[0].strip()
                prop_str = parts[1].strip()

                # Check for letter-spacing property
                if re.search(r'letter-spacing\s*:\s*(?!0|normal)', prop_str):
                    found_classes = _simple_classes(selector_str)
                    if found_classes:

                        for cls in found_classes:
                            spacing_classes.add(cls)

                # Check for an explicit text-align on a class-based rule.
                # NOTE: selector_str can be a descendant/compound selector
                # (e.g. ".chapter .poem-line"), and re.findall grabs every
                # class token in it -- same simplification already used
                # above for spacing_classes. This can over-match a wrapping
                # container class, but that's an existing, accepted
                # trade-off of this lightweight selector scan, not new.
                # NOTE: "left" is deliberately excluded here (matching the
                # inline-style scan in PHASE 1, which never captured it).
                # A class-authored "left" is virtually always just the
                # book's ambient default paragraph style, not a deliberate
                # "this element is special" signal -- unlike center/right/
                # justify, which almost always do mark something
                # intentional (a pull-quote, an attribution, a poem).
                # Promoting "left" here was silently overriding the user's
                # global justify setting on ordinary body text anywhere the
                # book's default text class happened to say "left".
                align_prop_match = re.search(r'text-align\s*:\s*(center|right|justify)', prop_str)
                if align_prop_match:
                    found_classes = _simple_classes(selector_str)
                    for cls in found_classes:
                        alignment_classes[cls] = align_prop_match.group(1)

                # Same lightweight scan, but for an explicit class-based
                # text-indent (e.g. ".chapter-open { text-indent: 0 }").
                indent_prop_match = re.search(r'text-indent\s*:\s*([\d.]+(?:em|px|%)|0)', prop_str)
                if indent_prop_match:
                    found_classes = _simple_classes(selector_str)
                    for cls in found_classes:
                        indent_classes[cls] = indent_prop_match.group(1)

                # Look for explicit margin-top classes
                m_top_match = re.search(r'margin-top\s*:\s*([\d.]+(?:em|px|%|pt)|0)', prop_str)
                if m_top_match:
                    for cls in _simple_classes(selector_str):
                        margin_top_classes[cls] = m_top_match.group(1)

                # Look for explicit margin-bottom classes
                m_bot_match = re.search(r'margin-bottom\s*:\s*([\d.]+(?:em|px|%|pt)|0)', prop_str)
                if m_bot_match:
                    for cls in _simple_classes(selector_str):
                        margin_bottom_classes[cls] = m_bot_match.group(1)

                # Look for explicit margin-right classes
                m_right_match = re.search(r'margin-right\s*:\s*([\d.]+(?:em|px|%|pt)|0)', prop_str)
                if m_right_match:
                    for cls in _simple_classes(selector_str):
                        margin_right_classes[cls] = m_right_match.group(1)

                # Look for explicit font styles/weights
                if re.search(r'font-style\s*:\s*italic', prop_str, re.I):
                    for cls in _simple_classes(selector_str):
                        font_style_classes[cls] = "font-style: italic"

                if re.search(r'font-weight\s*:\s*(bold|bolder|[789]00)', prop_str, re.I):
                    for cls in _simple_classes(selector_str):
                        # Merge into the same dictionary to keep the loop simple
                        font_style_classes[cls] = "font-weight: bold"

        # --- PHASE 2a: SAFETY NET AGAINST GENERIC/STRUCTURAL CLASSES ---
        # Even a "simple" .foo selector can be a generic class that a
        # converter (Calibre, etc.) stamps onto nearly every paragraph in
        # the book for unrelated reasons (resets, font mapping). A class
        # that shows up on the overwhelming majority of prose elements is
        # not a deliberate "this specific element is special" signal --
        # genuinely deliberate alignment/indent (poems, dedications,
        # pull-quotes) is applied to a minority of elements. Drop anything
        # that fails that sniff test before it gets promoted in 2b/2c.
        if alignment_classes or indent_classes or margin_top_classes or margin_bottom_classes or margin_right_classes:
            prose_tags = soup.find_all(['p', 'div', 'li', 'dd', 'dt', 'blockquote'])
            total_prose = len(prose_tags) or 1
            class_counts = {}
            for t in prose_tags:
                for c in (t.get('class') or []):
                    class_counts[c.lower()] = class_counts.get(c.lower(), 0) + 1

            GENERIC_THRESHOLD = 0.5
            for cls, count in class_counts.items():
                if count / total_prose > GENERIC_THRESHOLD:
                    alignment_classes.pop(cls, None)
                    indent_classes.pop(cls, None)
                    margin_top_classes.pop(cls, None)
                    margin_bottom_classes.pop(cls, None)
                    margin_right_classes.pop(cls, None)
                    # Note: We deliberately do NOT pop font_style_classes here.
                    # If an author italicizes 60% of a book, it's an artistic choice, not a structural reset.

        # --- PHASE 2b: PROMOTE CLASS-BASED ALIGNMENT TO INLINE ---
        # Mirrors what PHASE 1 already does for inline-authored alignment
        # (style="text-align: center") -- except this covers the far more
        # common case of alignment expressed via a CSS class in the book's
        # own stylesheet. Without this, custom_css's blanket
        # "text-align: {self.text_align} !important" on every p/div/li/...
        # silently overrides these elements, since they have no inline
        # style for it to lose to.
        if alignment_classes:
            for tag in soup.find_all(True):
                classes = tag.get('class', [])
                if isinstance(classes, str):
                    classes = [classes]
                if not classes:
                    continue
                align_val = None
                for cls in classes:
                    if cls.lower() in alignment_classes:
                        align_val = alignment_classes[cls.lower()]
                        break
                if align_val:
                    rules = f"text-align: {align_val} !important"
                    if align_val in ('center', 'right'):
                        rules += "; text-indent: 0 !important"
                    tag['style'] = self._append_style(tag.get('style', ''), rules)

        # --- PHASE 2c: PROMOTE CLASS-BASED TEXT-INDENT TO INLINE ---
        # Mirrors 2b for text-indent. Skips tags that Phase 2b already
        # forced to text-indent: 0 via center/right alignment, so an
        # indent class on a centered heading can't fight the alignment
        # rule that already (correctly) zeroed it.
        if indent_classes:
            for tag in soup.find_all(True):
                classes = tag.get('class', [])
                if isinstance(classes, str):
                    classes = [classes]
                if not classes:
                    continue
                existing_style = tag.get('style', '')
                if 'text-indent: 0 !important' in existing_style:
                    continue
                indent_val = None
                for cls in classes:
                    if cls.lower() in indent_classes:
                        indent_val = indent_classes[cls.lower()]
                        break
                if indent_val is not None:
                    tag['style'] = self._append_style(existing_style, f"text-indent: {indent_val} !important")

        # --- PHASE 2d: NO INDENT ON THE FIRST PARAGRAPH AFTER A HEADING/TERM ---
        # Classic print convention: a paragraph that opens a new section
        # doesn't need a first-line indent, since the heading (or, in a
        # glossary/definition list, the <dt> term) above it already marks
        # the start clearly. Indenting it anyway is a common tell that a
        # book has been run through a converter rather than typeset with
        # care. This only fires when the book didn't already say something
        # explicit about this paragraph's indent -- an inline/class-based
        # override from PHASE 1/2c always wins, since a deliberate author
        # choice outranks this structural default.
        HEADING_TAGS = ('h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'dt')

        def _follows_heading(tag):
            node = tag
            while node is not None:
                prev = node.find_previous_sibling(True)
                if prev is not None:
                    return prev.name in HEADING_TAGS
                parent = node.parent
                if parent is None or parent.name in ('[document]', 'body', 'html'):
                    return False
                node = parent
            return False

        for p_tag in soup.find_all('p'):
            existing_style = p_tag.get('style', '')
            if 'text-indent' in existing_style:
                continue  # explicit book-authored indent (incl. "0") already won upstream
            if _follows_heading(p_tag):
                p_tag['style'] = self._append_style(existing_style, "text-indent: 0 !important")

        # --- PHASE 2e: PROMOTE CLASS-BASED MARGINS AND FONTS TO INLINE ---
        if margin_top_classes or margin_bottom_classes or margin_right_classes or font_style_classes:
            for tag in soup.find_all(True):
                classes = tag.get('class', [])
                if isinstance(classes, str):
                    classes = [classes]
                if not classes:
                    continue

                existing_style = tag.get('style', '')
                injections = []

                if 'margin-top' not in existing_style:
                    for cls in classes:
                        if cls.lower() in margin_top_classes:
                            injections.append(f"margin-top: {margin_top_classes[cls.lower()]} !important")
                            break

                if 'margin-bottom' not in existing_style:
                    for cls in classes:
                        if cls.lower() in margin_bottom_classes:
                            injections.append(f"margin-bottom: {margin_bottom_classes[cls.lower()]} !important")
                            break

                if 'margin-right' not in existing_style:
                    for cls in classes:
                        if cls.lower() in margin_right_classes:
                            injections.append(f"margin-right: {margin_right_classes[cls.lower()]} !important")
                            break

                for cls in classes:
                    if cls.lower() in font_style_classes:
                        injections.append(f"{font_style_classes[cls.lower()]} !important")

                if injections:
                    tag['style'] = self._append_style(existing_style, "; ".join(injections))

        # --- PHASE 3: APPLY SPACING (RECURSIVE FIX) ---

        # Helper to safely add class
        def add_class(element, cls_name):
            existing = element.get('class', [])
            if isinstance(existing, str): existing = [existing]
            if cls_name not in existing:
                existing.append(cls_name)
                element['class'] = existing

        # Broad list of tags that might carry text or formatting
        target_tags = ['p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                       'span', 'blockquote', 'li', 'i', 'em', 'b', 'strong', 'a', 'font', 'small', 'big']

        # 1. Identify ROOTS: Elements that trigger the spacing
        roots_to_space = []
        for tag in soup.find_all(target_tags):
            should_space = tag.get('data-letter-spacing') == 'true'

            # Check class match
            if not should_space:
                classes = tag.get('class', [])
                if isinstance(classes, str): classes = [classes]
                for cls in classes:
                    if cls.lower() in spacing_classes:
                        should_space = True
                        break

            if should_space:
                roots_to_space.append(tag)

        # 2. Apply to Roots AND ALL DESCENDANTS
        # 2. Apply to Roots AND ALL DESCENDANTS
        for root in roots_to_space:
            # Apply class
            add_class(root, 'custom-spaced')

            # FORCE INLINE STYLE (The Nuclear Option)
            # We prepend our font rule to any existing styles
            current_style = root.get('style', '')
            root['style'] = "font-family: 'CustomFontSpaced' !important; " + current_style

            # Find all nested children that might hold text
            children = root.find_all(target_tags)

            for child in children:
                add_class(child, 'custom-spaced')

                # FORCE INLINE STYLE ON CHILDREN TOO
                # This defeats .reset { font-family: inherit !important }
                child_style = child.get('style', '')
                child['style'] = "font-family: 'CustomFontSpaced' !important; " + child_style

                # Debug print to confirm injection
                txt = child.get_text(strip=True).lower()

        return soup

    def render_page(self, global_page_index):
        if not self.is_ready: return None
        if getattr(self, "is_comic", False):
            return self._render_comic_page(global_page_index)

        # 1. Get current settings
        contrast = self.layout_settings.get("contrast", 1.2)
        white_clip = self.layout_settings.get("white_clip", 220)
        spectra_enabled = self.layout_settings.get("spectra_enabled", False)

        num_toc = len(self.toc_pages_images)
        footer_padding = max(0, self.bottom_padding)
        header_padding = max(0, self.top_padding)
        content_height = max(1, self.screen_height - footer_padding - header_padding)

        # 2. Prepare content layer
        requested_start = self.layout_settings.get("toc_insert_page", 1) - 1

        # Calculate available book pages (excluding TOC)
        book_pages_count = len(self.page_map)

        # Clamp insertion point so it doesn't exceed book length
        toc_start = max(0, min(requested_start, book_pages_count))

        if num_toc > 0 and toc_start <= global_page_index < (toc_start + num_toc):
            is_toc = True
            is_cover_page = False
            cur_w, cur_h = self.screen_width, self.screen_height
            toc_local_idx = global_page_index - toc_start
            img_content = self.toc_pages_images[toc_local_idx].copy().convert("L")
            page = None
            sx, sy = 1.0, 1.0
        else:
            is_toc = False
            if global_page_index < toc_start:
                book_idx = global_page_index
            else:
                book_idx = global_page_index - num_toc

            is_cover_page = (
                book_idx in getattr(self, "cover_pages", set())
                or global_page_index in getattr(self, "cover_pages", set())
            )
            ori = self.layout_settings.get("orientation", "Portrait")
            is_landscape = "Landscape" in ori
            keep_portrait = is_cover_page and is_landscape and self.layout_settings.get("keep_cover_portrait", True)
            dev_w, dev_h = get_device_size(self.layout_settings.get("device", DEFAULT_DEVICE))
            cur_w = dev_w if keep_portrait else self.screen_width
            cur_h = dev_h if keep_portrait else self.screen_height
            cur_content_h = cur_h if is_cover_page else (dev_h if keep_portrait else content_height)

            doc_idx, page_idx = self.page_map[book_idx]
            doc, _ = self.fitz_docs[doc_idx]
            page = doc[page_idx]

            sx = cur_w / page.rect.width
            sy = cur_content_h / page.rect.height

            mat = fitz.Matrix(sx, sy)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img_content = Image.frombytes("RGB", [pix.width, pix.height], pix.samples).convert("L")

        # Assemble full page (Grayscale)
        full_page = Image.new("L", (cur_w, cur_h), 255)
        paste_x = (cur_w - img_content.width) // 2
        paste_y = (cur_h - img_content.height) // 2 if is_cover_page else (0 if is_toc else header_padding)
        full_page.paste(img_content, (paste_x, paste_y))

        # Center cover image perfectly horizontally and vertically if it doesn't fill the canvas
        cover_centered_pos = None
        if is_cover_page and page is not None:
            image_info = page.get_image_info()
            if image_info and len(image_info) > 0:
                c_bbox = image_info[0].get('bbox')
                if c_bbox:
                    cx0, cy0, cx1, cy1 = [int(c_bbox[0] * sx), int(c_bbox[1] * sy), int(c_bbox[2] * sx), int(c_bbox[3] * sy)]
                    cw = cx1 - cx0
                    ch = cy1 - cy0
                    if 0 < cw <= cur_w and 0 < ch <= cur_h:
                        cover_crop = img_content.crop((cx0, cy0, cx1, cy1))
                        target_cx = (cur_w - cw) // 2
                        target_cy = (cur_h - ch) // 2
                        full_page = Image.new("L", (cur_w, cur_h), 255)
                        full_page.paste(cover_crop, (target_cx, target_cy))
                        cover_centered_pos = (target_cx, target_cy, cw, ch)

        # 3. Enhance Grayscale (Global Contrast/White Clip)
        if not is_toc:
            if contrast != 1.0:
                full_page = ImageEnhance.Contrast(full_page).enhance(contrast)
            if white_clip < 255:
                full_page = full_page.point(lambda p: 255 if p > white_clip else p)

            # --- NEW: THE ANTI-BURN IMAGE CAPTURE ---
            # We grab the images NOW while they are still grayscale
            captured_images = []
            image_info = page.get_image_info()
            for info in image_info:
                bbox = info['bbox']
                if is_cover_page and cover_centered_pos:
                    tcx, tcy, cw, ch = cover_centered_pos
                    crop_box = (tcx, tcy, tcx + cw, tcy + ch)
                    pos = (tcx, tcy)
                else:
                    ix0, iy0, ix1, iy1 = [int(bbox[0] * sx), int(bbox[1] * sy), int(bbox[2] * sx), int(bbox[3] * sy)]
                    crop_box = (ix0 + paste_x, iy0 + paste_y, ix1 + paste_x, iy1 + paste_y)
                    pos = (ix0 + paste_x, iy0 + paste_y)

                # Verify crop is valid
                if crop_box[2] > crop_box[0] and crop_box[3] > crop_box[1]:
                    # 1. Crop the clean grayscale image
                    img_crop = full_page.crop(crop_box)
                    # 2. Lighten it slightly so e-ink doesn't get too muddy
                    img_crop = ImageEnhance.Brightness(img_crop).enhance(1.1)
                    # 3. Dither it to 1-bit immediately
                    img_crop = img_crop.convert("1", dither=Image.Dither.FLOYDSTEINBERG).convert("L")
                    # 4. Store for later pasting
                    captured_images.append((img_crop, pos))

            render_mode = self.layout_settings.get("render_mode", "Threshold")
            bit_depth = self.layout_settings.get("bit_depth", "1-bit (XTG)")
            two_bit = "2-bit" in bit_depth
            text_blur = self.layout_settings.get("text_blur", 1.0)
            thresh = self.layout_settings.get("text_threshold", 130)

            if render_mode == "Threshold" and text_blur > 0:
                full_page = ImageEnhance.Sharpness(full_page).enhance(1.0 + (text_blur * 0.5))

            full_page = apply_eink_conversion(full_page, render_mode=render_mode, two_bit=two_bit, thresh=thresh, invert=False)

            # --- 5. RE-PASTE DITHERED IMAGES ---
            # This overwrites the "blobs" with our preserved dithered versions
            for img_crop, pos in captured_images:
                full_page.paste(img_crop, pos)



        # 6. UI Overlay & Final Conversion
        is_cover_page = (not is_toc) and (
                book_idx in getattr(self, "cover_pages", set())
                or global_page_index in getattr(self, "cover_pages", set())
        )

        img_final = full_page.convert("RGB")
        draw = ImageDraw.Draw(img_final)

        if not is_toc and not is_cover_page:
            # Clear Header/Footer areas for UI
            if header_padding > 0:
                draw.rectangle(
                    [0, 0, self.screen_width, header_padding], fill=(255, 255, 255)
                )
            if footer_padding > 0:
                draw.rectangle(
                    [
                        0,
                        self.screen_height - footer_padding,
                        self.screen_width,
                        self.screen_height,
                    ],
                    fill=(255, 255, 255),
                )

            self._draw_header(draw, global_page_index)
            self._draw_footer(draw, global_page_index)

            # Draw Spectra Annotations
            # Draw Spectra Annotations
            page_annotations = self.page_annotation_data.get((doc_idx, page_idx), {})
            if page_annotations and spectra_enabled:
                # ... (rest of your annotation drawing logic remains the same) ...
                annot_font_size = max(9, int(self.font_size * 0.65))
                annot_font = self._get_ui_font(annot_font_size)
                for x0, y0, x1, y1, text, block, line, word_idx in page.get_text("words"):
                    coord_key = f"{int(x0)}_{int(y0)}"
                    if coord_key in page_annotations:
                        defi = page_annotations[coord_key]
                        px_x, px_y = x0 * sx, y0 * sy
                        px_w = (x1 - x0) * sx
                        text_len = annot_font.getlength(defi)
                        draw_x = max(2, min(paste_x + px_x + (px_w - text_len) / 2, self.screen_width - text_len - 2))
                        draw_y = max(header_padding, paste_y + px_y - annot_font_size + 2)
                        draw.text((draw_x, draw_y), defi, font=annot_font, fill=(0, 0, 0))

        # --- PREVIEW ROTATION LOGIC ---
        ori = self.layout_settings.get("orientation", "Portrait")
        keep_portrait = is_cover_page and ("Landscape" in ori) and self.layout_settings.get("keep_cover_portrait", True)
        if "Landscape" in ori and not keep_portrait:
            # Determine angle: 90 (CCW) or 270 (CW)
            angle = 270 if "270" in ori else 90
            img_final = img_final.rotate(angle, expand=True)
        # ------------------------------

        # Invert colors (Dark Mode / Negative) - keeps covers in natural tone
        invert_colors = bool(self.layout_settings.get("invert_colors", False))
        if invert_colors and not is_cover_page:
            img_final = ImageOps.invert(img_final)

        return img_final

    # ------------------------------------------------------------------
    # COMIC / MANGA MODE
    # ------------------------------------------------------------------
    def _parse_comic_structure(self, path, marker):
        try:
            import importers
        except ImportError:
            print("importers.py is missing - cannot open comic books.")
            return False
        try:
            with zipfile.ZipFile(path) as z:
                data = json.loads(z.read(marker))
            self._comic_base = posixpath.dirname(marker)
            meta = importers.read_epub_light(path, cover_max=1600)
        except Exception as e:
            print(f"Comic parse error: {e}")
            return False
        self.comic_meta = data
        self.is_comic = True
        self.title_metadata = meta.get("title") or os.path.splitext(os.path.basename(path))[0]
        self.author_metadata = meta.get("author") or "Unknown Author"
        self.book_lang = meta.get("language") or "en"
        self.cover_image_obj = meta.get("cover")
        self.book_css, self.book_images, self.global_id_map = "", {}, {}
        chap_defs = data.get("chapters") or [{"title": self.title_metadata, "start": 0}]
        n_pages = len(data["pages"])
        for i, ch in enumerate(chap_defs):
            end = chap_defs[i + 1]["start"] if i + 1 < len(chap_defs) else n_pages
            self.raw_chapters.append({
                "title": ch["title"], "soup": None, "has_image": True,
                "filename": f"comic_{i}", "comic_pages": list(range(ch["start"], end))})
        if getattr(self, "_current_comic_input", None) != path:
            self._comic_cache = {}
            self._comic_crop_cache = {}
            self._tier_cache = {}
            self._spread_cache = {}
            self._last_comic_layout_sig = None
            self._current_comic_input = path
        else:
            self._comic_cache = getattr(self, "_comic_cache", {})
            self._comic_crop_cache = getattr(self, "_comic_crop_cache", {})
            self._tier_cache = getattr(self, "_tier_cache", {})
            self._spread_cache = getattr(self, "_spread_cache", {})
        return True

    def _comic_is_rtl(self):
        pref = self.layout_settings.get("comic_rtl", "Auto")
        if pref == "Right-to-left":
            return True
        if pref == "Left-to-right":
            return False
        return bool((self.comic_meta or {}).get("rtl", False))

    @staticmethod
    def _comic_grid(zoom):
        z = str(zoom or "Off").strip()
        if z.startswith("Tiers"):
            return (1, 1)
        k = z[:1]
        return {"2": (2, 1), "3": (3, 1), "4": (2, 2)}.get(k, (1, 1))

    def _render_comic_chapters(self, selected_indices, progress_callback=None):
        """Builds the list of output pages ("units"). No pixels are touched here - pages are
        cropped / scaled / dithered on demand in _render_comic_page, so setting changes are instant."""
        ls = self.layout_settings

        # Check if the page list ("comic_units") needs to be rebuilt at all.
        # Visual / tone settings (gamma, sharpen, contrast, white_clip, autolevels, render_mode,
        # text_threshold, scrollbar, peek_mode, fit, page numbers) only affect rendering pixels
        # on demand in _render_comic_page and do NOT alter the pagination or tier boxes.
        layout_sig = (
            tuple(selected_indices) if selected_indices is not None else tuple(range(len(self.raw_chapters))),
            ls.get("orientation", "Portrait"),
            bool(ls.get("keep_cover_portrait", True)),
            getattr(self, "screen_width", 0),
            getattr(self, "screen_height", 0),
            ls.get("comic_spreads", "Split"),
            ls.get("comic_zoom", "Off"),
            ls.get("comic_crop", "Auto"),
            round(float(ls.get("comic_crop_pad", 1.5)), 2),
            int(ls.get("comic_overlap", 6)),
            ls.get("comic_rtl", "Auto"),
        )
        if getattr(self, "_last_comic_layout_sig", None) == layout_sig and getattr(self, "comic_units", None):
            self.is_ready = True
            if progress_callback:
                progress_callback(1.0)
            return True
        self._last_comic_layout_sig = layout_sig
        for d, _ in self.fitz_docs:
            try:
                d.close()
            except Exception:
                pass
        self.fitz_docs, self.page_map, self.toc_pages_images = [], [], []
        self.cover_pages = set()
        self.font_warnings = []
        self.comic_units = []

        pages = self.comic_meta["pages"]
        rtl = self._comic_is_rtl()
        spreads = ls.get("comic_spreads", "Split")
        zoom_mode = ls.get("comic_zoom", "Off")
        rows, cols = self._comic_grid(zoom_mode)
        col_order = list(range(cols))[::-1] if rtl else list(range(cols))
        cells = [(r, c) for r in range(rows) for c in col_order]
        chosen = set(selected_indices or range(len(self.raw_chapters)))

        toc = []
        for ci, chap in enumerate(self.raw_chapters):
            start = len(self.comic_units)
            for pi in chap["comic_pages"]:
                is_cover = (ci == 0 and pi == 0)
                if is_cover and "Landscape" in ls.get("orientation", "Portrait") and ls.get("keep_cover_portrait",
                                                                                            True):
                    self.comic_units.append({
                        "p": pi,
                        "half": None,
                        "rot": False,
                        "cell": (0, 0),
                        "grid": (1, 1),
                        "tier_box": None
                    })
                    continue

                pw, ph = pages[pi]["w"], pages[pi]["h"]
                is_spread = pw > ph * 1.2
                mode = spreads
                if is_spread and spreads == "Auto":
                    if getattr(self, "screen_width", 0) > getattr(self, "screen_height", 1):
                        mode = "Fit"
                    else:
                        mode = "Split" if self._comic_is_true_spread(pi) else "Rotate"

                if is_spread and mode == "Full+Split":
                    self.comic_units.append({
                        "p": pi,
                        "half": None,
                        "rot": False,
                        "cell": (0, 0),
                        "grid": (1, 1),
                        "tier_box": None,
                        "force_fit": True
                    })

                halves = [None]
                if is_spread and mode in ("Split", "Full+Split"):
                    halves = ["R", "L"] if rtl else ["L", "R"]
                rot = is_spread and mode == "Rotate"

                for half in halves:
                    if zoom_mode == "Tiers (Smart)":
                        ov_pct = float(ls.get("comic_overlap", 6)) / 100.0
                        is_land = getattr(self, "screen_width", 0) > getattr(self, "screen_height", 1)
                        tcache = self.__dict__.setdefault("_tier_cache", {})
                        tkey = (
                            pi,
                            half,
                            rot,
                            ls.get("comic_crop", "Auto"),
                            round(float(ls.get("comic_crop_pad", 1.5)), 2),
                            round(ov_pct, 4),
                            is_land,
                            rtl
                        )
                        tiers = tcache.get(tkey)
                        if tiers is None:  # decoding + analysing a page costs ~60 ms: do it once per settings
                            im = self._comic_load_page(pi)
                            box, _ = self._comic_crop_box(pi, im)
                            base_crop = im.crop(box)

                            if half:
                                w, h = base_crop.size
                                base_crop = base_crop.crop((0, 0, w // 2, h)) if half == "L" else base_crop.crop(
                                    (w // 2, 0, w, h))
                            if rot:
                                base_crop = base_crop.transpose(Image.ROTATE_270)

                            tiers = extract_comic_tiers(base_crop, overlap_pct=ov_pct, rtl=rtl, is_landscape=is_land)
                            tcache[tkey] = tiers
                        for t_idx, tier_bbox in enumerate(tiers):
                            self.comic_units.append({
                                "p": pi,
                                "half": half,
                                "rot": rot,
                                "cell": (0, 0),
                                "grid": (1, 1),
                                "tier_box": tier_bbox,
                                "gutter_top": getattr(tier_bbox, "gutter_top", None),
                                "gutter_bot": getattr(tier_bbox, "gutter_bot", None),
                            })
                    else:
                        for cell in cells:
                            self.comic_units.append({
                                "p": pi,
                                "half": half,
                                "rot": rot,
                                "cell": cell,
                                "grid": (rows, cols),
                                "tier_box": None
                            })

            if len(self.comic_units) > start and ci in chosen:
                toc.append((chap["title"], start + 1))
            if progress_callback:
                progress_callback((ci + 1) / max(1, len(self.raw_chapters)))

        if not toc and self.comic_units:
            toc = [(self.title_metadata, 1)]
        self.toc_data_final = toc
        self.total_pages = len(self.comic_units)
        self.page_map = [(0, i) for i in range(self.total_pages)]  # placeholder, keeps UI guards happy
        self.is_ready = True
        if progress_callback:
            progress_callback(1.0)
        return True

    # ---- double-page (spread) detection --------------------------------
    @staticmethod
    def _detect_center_gap(im):
        """True if a landscape page looks like TWO facing pages: a blank vertical gutter near the middle with
        content on both sides. Single wide pages (slides, tables, panoramic art, splashes) return False."""
        w, h = im.size
        s = 600.0 / w if w > 600 else 1.0
        sm = im.convert("L").resize((max(40, int(w * s)), max(40, int(h * s))), Image.BILINEAR)
        W, H = sm.size
        t = max(2, int(min(W, H) * 0.02))
        ring = b"".join(strip.tobytes() for strip in (
            sm.crop((0, 0, W, t)), sm.crop((0, H - t, W, H)), sm.crop((0, 0, t, H)), sm.crop((W - t, 0, W, H))))
        bg = sorted(ring)[len(ring) // 2]
        mask = ImageChops.difference(sm, Image.new("L", sm.size, bg)).point(lambda p: 255 if p > 40 else 0)
        mask = mask.crop((0, int(H * 0.04), W, int(H * 0.96)))
        cols = [v / 255.0 for v in mask.resize((W, 1), Image.BOX).tobytes()]
        active = sorted(v for v in cols if v > 0.02)
        if len(active) < W * 0.2:
            return False  # almost blank page: nothing to split
        scale = active[len(active) // 2]
        eps = max(0.012, 0.15 * scale)
        lo, hi = int(W * 0.40), int(W * 0.60)
        min_w = max(3, int(W * 0.012))
        band = max(4, int(W * 0.18))
        x = lo
        while x < hi:
            if cols[x] <= eps:
                x0 = x
                while x < W and cols[x] <= eps:
                    x += 1
                if x - x0 >= min_w:
                    left = cols[max(0, x0 - band):x0]
                    right = cols[x:x + band]
                    if left and right and sum(left) / len(left) >= 0.25 * scale and sum(right) / len(
                            right) >= 0.25 * scale:
                        return True
            else:
                x += 1
        return False

    def _comic_is_true_spread(self, page_idx):
        cache = self.__dict__.setdefault("_spread_cache", {})
        if page_idx not in cache:
            try:
                cache[page_idx] = self._detect_center_gap(self._comic_load_page(page_idx))
            except Exception as e:
                print(f"Spread detection failed for page {page_idx + 1}: {e}")
                cache[page_idx] = True  # unknown: behave like the classic 'Split'
        return cache[page_idx]

    # ---- page loading ------------------------------------------------
    def _comic_load_page(self, page_idx):
        cache = getattr(self, "_comic_cache", None)
        if cache is None:
            cache = self._comic_cache = {}
        if page_idx in cache:
            return cache[page_idx]
        href = self.comic_meta["pages"][page_idx]["href"]
        name = posixpath.join(getattr(self, "_comic_base", ""), href) if getattr(self, "_comic_base", "") else href
        with zipfile.ZipFile(self.input_file) as z:
            data = z.read(name)
        im = Image.open(io.BytesIO(data))
        im.load()
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[-1])
            im = bg
        im = im.convert("L")
        if len(cache) >= 3:
            cache.pop(next(iter(cache)))
        cache[page_idx] = im
        return im

    # ---- auto crop ---------------------------------------------------
    @staticmethod
    def _comic_find_content_box(im, mode, pad_pct):
        """Returns ((l,t,r,b), bg_value). Works on a small copy; ignores specks and scanner edges."""
        w, h = im.size
        s = 640.0 / max(w, h) if max(w, h) > 640 else 1.0
        sm = im.resize((max(8, int(w * s)), max(8, int(h * s))), Image.BILINEAR) if s < 1 else im.copy()
        sm = sm.filter(ImageFilter.MedianFilter(3))
        sw, sh = sm.size
        # background = most common value in a thin ring around the page
        t = max(2, int(min(sw, sh) * 0.03))
        hist = [0] * 256
        for strip in (sm.crop((0, 0, sw, t)), sm.crop((0, sh - t, sw, sh)), sm.crop((0, 0, t, sh)), sm.crop((sw - t, 0, sw, sh))):
            for i, v in enumerate(strip.histogram()):
                hist[i] += v
        sm_hist = [sum(hist[max(0, i - 3):i + 4]) for i in range(256)]
        bg = max(range(256), key=lambda i: sm_hist[i])
        tol = 48 if mode == "Aggressive" else 24
        diff = ImageChops.difference(sm, Image.new("L", sm.size, bg)).point(lambda p: 255 if p > tol else 0)
        e = max(1, int(min(sw, sh) * 0.008))          # ignore scanner borders / edge shadows
        d = ImageDraw.Draw(diff)
        d.rectangle([0, 0, sw, e], fill=0)
        d.rectangle([0, sh - e, sw, sh], fill=0)
        d.rectangle([0, 0, e, sh], fill=0)
        d.rectangle([sw - e, 0, sw, sh], fill=0)
        minfrac = 0.012 if mode == "Aggressive" else 0.004
        cols = diff.resize((sw, 1), Image.BOX).point(lambda p: 255 if p >= 255 * minfrac else 0).getbbox()
        rows = diff.resize((1, sh), Image.BOX).point(lambda p: 255 if p >= 255 * minfrac else 0).getbbox()
        if not cols or not rows:
            return (0, 0, w, h), bg
        l, r = cols[0], cols[2]
        tp, bt = rows[1], rows[3]
        l, r, tp, bt = l / s, r / s, tp / s, bt / s
        pad_x, pad_y = w * pad_pct / 100.0, h * pad_pct / 100.0
        box = (int(max(0, l - pad_x)), int(max(0, tp - pad_y)), int(min(w, r + pad_x)), int(min(h, bt + pad_y)))
        if (box[2] - box[0]) * (box[3] - box[1]) < 0.35 * w * h:   # suspicious (nearly blank page) -> keep whole page
            return (0, 0, w, h), bg
        return box, bg

    def _comic_crop_box(self, page_idx, im):
        mode = self.layout_settings.get("comic_crop", "Auto")
        pad = round(float(self.layout_settings.get("comic_crop_pad", 1.5)), 2)
        key = (page_idx, mode, pad)
        cc = getattr(self, "_comic_crop_cache", None)
        if cc is None:
            cc = self._comic_crop_cache = {}
        if key not in cc:
            if mode == "Off":
                w, h = im.size
                # still estimate the background so letterbox bars match dark pages
                _, bg = self._comic_find_content_box(im, "Auto", pad)
                cc[key] = ((0, 0, w, h), bg)
            else:
                cc[key] = self._comic_find_content_box(im, mode, pad)
        return cc[key]

    @staticmethod
    def _find_safe_tier_boundary(img_gray, y_cut, is_bottom_cut=True, search_radius=35):
        """Finds the outer edge of a true full-width blank gutter near y_cut. Returns None if no blank gutter exists."""
        arr = np.asarray(img_gray, dtype=np.uint8)
        h, w = arr.shape
        mx = max(2, int(w * 0.03))
        work = arr[:, mx:w - mx] if w > mx * 2 else arr

        y_min = max(0, y_cut - search_radius)
        y_max = min(h - 1, y_cut + search_radius)
        if y_max <= y_min:
            return None

        # Slice the region and only convert the small strip to float32
        sub = work[y_min:y_max + 1, :].astype(np.float32)

        # Fast percentile check on 4x-strided uint8 buffer
        sample = work[::4, ::4] if (work.shape[0] >= 16 and work.shape[1] >= 16) else work
        p_lo, p_hi = np.percentile(sample, (2, 98))
        if p_hi - p_lo > 30:
            sub = np.clip((sub - p_lo) * (255.0 / (p_hi - p_lo)), 0.0, 255.0)

        # Keep 228 / 22 on full-res image to tolerate minor JPEG ringing near panel borders
        white_frac = np.mean(sub > 228, axis=1)
        black_frac = np.mean(sub < 22, axis=1)
        is_gutter = (white_frac >= 0.92) | (black_frac >= 0.92)

        if not np.any(is_gutter):
            return None

        # Find the gutter row closest to y_cut, then walk to the outer edge of the gutter
        rel_cut = y_cut - y_min
        candidates = np.where(is_gutter)[0]
        seed = int(candidates[np.argmin(np.abs(candidates - rel_cut))])

        if is_bottom_cut:
            while seed + 1 < len(is_gutter) and is_gutter[seed + 1]:
                seed += 1
        else:
            while seed - 1 >= 0 and is_gutter[seed - 1]:
                seed -= 1

        return y_min + seed

    # ---- the actual page pipeline -----------------------------------
    def _render_comic_page(self, idx):
        ls = self.layout_settings
        u = self.comic_units[idx]
        im = self._comic_load_page(u["p"])
        box, bg = self._comic_crop_box(u["p"], im)
        im = im.crop(box)


        # double-page spread -> left / right half
        if u["half"]:
            w, h = im.size
            im = im.crop((0, 0, w // 2, h)) if u["half"] == "L" else im.crop((w // 2, 0, w, h))
        if u["rot"]:
            im = im.transpose(Image.ROTATE_270)

        ori = ls.get("orientation", "Portrait")
        is_landscape = "Landscape" in ori
        is_cover = (u["p"] == 0 and u.get("half") is None and u.get("tier_box") is None and u.get("grid", (1, 1)) == (1, 1))
        keep_portrait = is_cover and is_landscape and ls.get("keep_cover_portrait", True)

        dev_w, dev_h = get_device_size(ls.get("device", DEFAULT_DEVICE))
        if keep_portrait:
            sw, sh = dev_w, dev_h
        else:
            sw, sh = self.screen_width, self.screen_height
        strip = 20 if ls.get("comic_page_numbers", False) else 0
        area_h = sh - strip

        # Smart Tiers Crop OR fixed zoom grid
        crop_y0, crop_y1 = 0, im.size[1]
        full_ph = im.size[1]
        orig_tb = u.get("tier_box")
        if orig_tb:
            tb = orig_tb
            peek_mode = ls.get("comic_peek_mode")
            if not peek_mode:
                peek_mode = "Full Art" if ls.get("comic_fill_tiers", True) else "Off"

            if peek_mode != "Off":
                pw, ph = im.size

                # Bound the screen-fill padding so it can never swallow MORE than one full
                # neighboring tier on a side - that alone already guarantees we never reach a
                # second/unrelated tier further away. A tile with a neighbor on only ONE side
                # (first/last tier on the page) can safely peek all the way to that neighbor's
                # own far edge - there is nothing beyond it to protect, so this restores the
                # full "Full Art" screen-fill feel for ordinary 2-tier pages and for the
                # outermost tiers of longer pages. Only an INTERIOR tile (one with a real
                # neighbor on BOTH sides) is capped more tightly, at each neighbor's midpoint,
                # since letting it fully consume both terminal neighbors at once is what used to
                # reconstruct the entire page for the middle tier of a 3+-tier page.
                prev_u = self.comic_units[idx - 1] if idx > 0 else None
                next_u = self.comic_units[idx + 1] if idx + 1 < len(self.comic_units) else None

                def _same_page_series(other):
                    return (other is not None and other.get("tier_box") is not None
                            and other["p"] == u["p"] and other.get("half") == u.get("half")
                            and other.get("rot") == u.get("rot"))

                has_prev = _same_page_series(prev_u)
                has_next = _same_page_series(next_u)
                is_interior = has_prev and has_next

                if has_prev:
                    p_tb = prev_u["tier_box"]
                    pad_min_y = (p_tb[1] + p_tb[3]) / 2.0 if is_interior else p_tb[1]
                else:
                    pad_min_y = 0

                if has_next:
                    n_tb = next_u["tier_box"]
                    pad_max_y = (n_tb[1] + n_tb[3]) / 2.0 if is_interior else n_tb[3]
                else:
                    pad_max_y = ph

                target_h = pw * (area_h / float(sw))
                tier_h = tb[3] - tb[1]
                if target_h > tier_h:
                    extra_h = target_h - tier_h
                    add_top = extra_h / 2.0
                    add_bot = extra_h - add_top
                    y0 = tb[1] - add_top
                    y1 = tb[3] + add_bot
                    if y0 < pad_min_y:
                        y1 += (pad_min_y - y0)
                        y0 = pad_min_y
                    if y1 > pad_max_y:
                        y0 -= (y1 - pad_max_y)
                        y1 = pad_max_y
                    y0 = max(pad_min_y, int(round(y0)))
                    y1 = min(pad_max_y, int(round(y1)))
                    tb = (0, y0, pw, y1)

                    if peek_mode == "Dimmed" and np is not None:
                        # Dim strictly from the true panel gutters outward into the adjacent tier.
                        # Never dim across the artwork if there is no gutter!
                        im_arr = np.array(im, dtype=np.float32)
                        alpha = np.ones(ph, dtype=np.float32)
                        feather = 12

                        g_top = u.get("gutter_top")
                        g_bot = u.get("gutter_bot")

                        # Refine to the exact full-res outer gutter boundary
                        if g_top is not None:
                            refined_top = self._find_safe_tier_boundary(im, g_top, is_bottom_cut=False)
                            g_top = refined_top if refined_top is not None else g_top

                        if g_bot is not None:
                            refined_bot = self._find_safe_tier_boundary(im, g_bot, is_bottom_cut=True)
                            g_bot = refined_bot if refined_bot is not None else g_bot

                        # Top peek zone: dim strictly from the top of the gutter upwards into the previous tier
                        if g_top is not None and g_top > y0:
                            f_start = max(y0, g_top - feather)
                            for y in range(f_start, g_top):
                                t = (g_top - y) / float(feather)
                                alpha[y] = 1.0 - 0.55 * (0.5 - 0.5 * np.cos(np.pi * t))
                            if f_start > y0:
                                alpha[y0:f_start] = 0.45

                        # Bottom peek zone: dim strictly from the bottom of the gutter downwards into the next tier
                        if g_bot is not None and g_bot < y1:
                            f_end = min(y1, g_bot + feather)
                            for y in range(g_bot, f_end):
                                t = (y - g_bot) / float(feather)
                                alpha[y] = 1.0 - 0.55 * (0.5 - 0.5 * np.cos(np.pi * t))
                            if f_end < y1:
                                alpha[f_end:y1] = 0.45

                        dimmed_slice = im_arr[y0:y1, :] * alpha[y0:y1, None] + 255.0 * (1.0 - alpha[y0:y1, None])
                        im_arr[y0:y1, :] = dimmed_slice
                        im = Image.fromarray(np.clip(im_arr, 0, 255).astype(np.uint8))

            crop_y0, crop_y1 = tb[1], tb[3]
            im = im.crop(tb)
        else:
            rows, cols = u["grid"]
            if rows * cols > 1:
                w, h = im.size
                ov = float(ls.get("comic_overlap", 6)) / 100.0
                r, c = u["cell"]
                cw, ch = w / cols, h / rows
                x0, x1 = c * cw - (cw * ov if c > 0 else 0), (c + 1) * cw + (cw * ov if c < cols - 1 else 0)
                y0, y1 = r * ch - (ch * ov if r > 0 else 0), (r + 1) * ch + (ch * ov if r < rows - 1 else 0)
                crop_y0, crop_y1 = y0, y1
                im = im.crop((int(max(0, x0)), int(max(0, y0)), int(min(w, x1)), int(min(h, y1))))
        w, h = im.size
        if ls.get("comic_fit", "Fit") == "Fill" and not u.get("force_fit", False):
            scale = max(sw / w, area_h / h)
        else:
            scale = min(sw / w, area_h / h)
        nw, nh = max(1, round(w * scale)), max(1, round(h * scale))

        # Settings needed before the downscale, so the crisp hi-res text mask (if any)
        # can be built from the full-resolution crop instead of the blurred device canvas.
        bolden_mode = ls.get("comic_bolden", "Off")
        clean_bubbles = bool(ls.get("comic_clean_bubbles", False))
        render_mode = ls.get("render_mode", "Threshold")
        two_bit = "2-bit" in ls.get("bit_depth", "1-bit (XTG)")
        thresh = ls.get("text_threshold", 130)
        downscale_ratio = max(w / float(nw), h / float(nh))
        bolden_active = bool(bolden_mode) and bolden_mode != "Off"
        wants_text_mask = render_mode.lower() in ("threshold", "") or clean_bubbles or bolden_active

        # --- PRE-DOWNSCALE TEXT CRISPING ---
        # Build the crisp binary text/line-art mask from the full-resolution crop before
        # LANCZOS blurs thin strokes away, then shrink that mask with an ink-preserving
        # filter (see build_hires_text_mask) instead of relying on thresholding the
        # already-downscaled canvas further down.
        hires_text_mask = None
        if wants_text_mask and downscale_ratio > 1.01 and not two_bit:
            hires_text_mask = build_hires_text_mask(im, (nw, nh), downscale_ratio, thresh=thresh)

        im = im.resize((nw, nh), Image.LANCZOS)
        if nw > sw or nh > area_h:                       # Fill: centre-crop the overflow
            l, t = max(0, (nw - sw) // 2), max(0, (nh - area_h) // 2)
            im = im.crop((l, t, l + min(nw, sw), t + min(nh, area_h)))
            if hires_text_mask is not None:
                hires_text_mask = hires_text_mask.crop((l, t, l + min(nw, sw), t + min(nh, area_h)))

        # tone: auto-levels -> gamma -> contrast -> white clip -> sharpen
        if ls.get("comic_autolevels", True):
            im = ImageOps.autocontrast(im, cutoff=0.5)
        gamma = float(ls.get("comic_gamma", 1.0))
        if abs(gamma - 1.0) > 0.01:
            lut = [min(255, int(255 * ((i / 255.0) ** (1.0 / gamma)) + 0.5)) for i in range(256)]
            im = im.point(lut)
        contrast = ls.get("contrast", 1.2)
        if contrast != 1.0:
            im = ImageEnhance.Contrast(im).enhance(contrast)
        white_clip = ls.get("white_clip", 220)
        if white_clip < 255:
            im = im.point(lambda p: 255 if p > white_clip else p)
        sharpen = float(ls.get("comic_sharpen", 0.6))
        if sharpen > 0:
            im = im.filter(ImageFilter.UnsharpMask(radius=1.2, percent=int(120 * sharpen), threshold=1))

        # Always use crisp white (or pure black for dark pages) so letterbox bars never dither into gray noise
        bg_val = 0 if bg < 65 else 255
        canvas = Image.new("L", (sw, sh), 255 if strip else bg_val)
        if strip:
            ImageDraw.Draw(canvas).rectangle([0, 0, sw, area_h], fill=bg_val)
        canvas.paste(im, ((sw - im.width) // 2, (area_h - im.height) // 2))

        hires_text_canvas = None
        if hires_text_mask is not None:
            hires_text_canvas = Image.new("L", (sw, sh), 255)
            hires_text_canvas.paste(hires_text_mask, ((sw - im.width) // 2, (area_h - im.height) // 2))

        # quantise exactly like the XTC exporter does, so preview == output
        invert_colors = bool(ls.get("invert_colors", False))
        should_invert = invert_colors and not is_cover

        # Route through apply_bubble_masked_eink whenever: Threshold mode (its own early-return
        # path already renders the whole page as pure black/white and can safely bolden
        # page-wide), "Crisp speech bubbles" is on, or "Text bolden" is active -- boldening
        # needs the dialogue-text detection in that function even when bubbles aren't cleaned.
        if wants_text_mask:
            canvas = apply_bubble_masked_eink(canvas, render_mode=render_mode, two_bit=two_bit, thresh=thresh,
                                              bolden=bolden_mode, downscale_ratio=downscale_ratio, invert=should_invert,
                                              clean_bubbles=clean_bubbles, precomputed_text_mask=hires_text_canvas)
        else:
            canvas = apply_eink_conversion(canvas, render_mode=render_mode, two_bit=two_bit, thresh=thresh, invert=should_invert)

        out = canvas.convert("RGB")
        if strip:
            d = ImageDraw.Draw(out)
            strip_bg = (0, 0, 0) if should_invert else (255, 255, 255)
            strip_fg = (255, 255, 255) if should_invert else (0, 0, 0)
            d.rectangle([0, area_h, sw, sh], fill=strip_bg)
            actual_page = u["p"] + 1
            total_actual = len(self.comic_meta["pages"]) if self.comic_meta and "pages" in self.comic_meta else 0
            half_str = f" {u['half']}" if u.get("half") else ""
            if total_actual:
                label = f"{idx + 1} / {self.total_pages} (p. {actual_page}{half_str}/{total_actual})"
            else:
                label = f"{idx + 1} / {self.total_pages}"
            font = self._get_ui_font(12)
            d.text(((sw - d.textlength(label, font=font)) / 2, area_h + 3), label, font=font, fill=strip_fg)

        scrollbar_pos = ls.get("comic_scrollbar", "Right")
        f0 = max(0.0, min(1.0, crop_y0 / float(max(1, full_ph))))
        f1 = max(0.0, min(1.0, crop_y1 / float(max(1, full_ph))))
        if scrollbar_pos in ("Right", "Left") and (f1 - f0) < 0.98 and not is_cover:
            d_sb = ImageDraw.Draw(out)
            track_top = 15
            track_bot = area_h - 15
            track_h = max(10, track_bot - track_top)
            bar_w = 4
            x0 = (sw - 8) if scrollbar_pos == "Right" else 4
            x1 = x0 + bar_w
            # Subtle track line
            track_fill = (80, 80, 80) if should_invert else (180, 180, 180)
            d_sb.line([(x0 + bar_w // 2, track_top), (x0 + bar_w // 2, track_bot)], fill=track_fill, width=1)
            # High-contrast thumb
            thumb_y0 = track_top + int(round(f0 * track_h))
            thumb_y1 = track_top + int(round(f1 * track_h))
            if thumb_y1 - thumb_y0 < 14:
                thumb_y1 = min(track_bot, thumb_y0 + 14)
            thumb_border = (0, 0, 0) if should_invert else (255, 255, 255)
            thumb_body = (255, 255, 255) if should_invert else (0, 0, 0)
            d_sb.rectangle([x0 - 1, thumb_y0 - 1, x1 + 1, thumb_y1 + 1], fill=thumb_border)
            d_sb.rectangle([x0, thumb_y0, x1, thumb_y1], fill=thumb_body)

        # Horizontal scrollbar on top for split double-page spreads or multi-column zoom
        has_h_split = bool(u.get("half")) or (not u.get("tier_box") and u.get("grid", (1, 1))[1] > 1)
        if scrollbar_pos in ("Right", "Left") and has_h_split and not is_cover:
            if u.get("half"):
                hx0 = 0.0 if u["half"] == "L" else 0.5
                hx1 = 0.5 if u["half"] == "L" else 1.0
            else:
                cols = max(1, u.get("grid", (1, 1))[1])
                c = u.get("cell", (0, 0))[1]
                hx0 = c / float(cols)
                hx1 = (c + 1) / float(cols)

            d_hsb = ImageDraw.Draw(out)
            htrack_left = 20
            htrack_right = sw - 20
            htrack_w = max(10, htrack_right - htrack_left)
            hbar_h = 4
            hy0 = 3
            hy1 = hy0 + hbar_h
            # Subtle track line across top
            track_fill = (80, 80, 80) if should_invert else (180, 180, 180)
            d_hsb.line([(htrack_left, hy0 + hbar_h // 2), (htrack_right, hy0 + hbar_h // 2)], fill=track_fill, width=1)
            # Center tick mark for the spread split
            htrack_mid = htrack_left + htrack_w // 2
            d_hsb.line([(htrack_mid, hy0 - 1), (htrack_mid, hy1 + 1)], fill=(120, 120, 120) if should_invert else (150, 150, 150), width=1)
            # High-contrast thumb for horizontal half
            thumb_x0 = htrack_left + int(round(hx0 * htrack_w))
            thumb_x1 = htrack_left + int(round(hx1 * htrack_w))
            if thumb_x1 - thumb_x0 < 14:
                thumb_x1 = min(htrack_right, thumb_x0 + 14)
            thumb_border = (0, 0, 0) if should_invert else (255, 255, 255)
            thumb_body = (255, 255, 255) if should_invert else (0, 0, 0)
            d_hsb.rectangle([thumb_x0 - 1, hy0 - 1, thumb_x1 + 1, hy1 + 1], fill=thumb_border)
            d_hsb.rectangle([thumb_x0, hy0, thumb_x1, hy1], fill=thumb_body)

        if is_landscape and not keep_portrait:
            out = out.rotate(270 if "270" in ori else 90, expand=True)
        return out

    def _pack_metadata(self, title, author, lang, chapter_count):
        """Packs metadata into a 256-byte fixed-size block."""
        # Fields: Title(128), Author(64), Publisher(32), Language(16),
        # CreateTime(4), CoverPage(2), ChapterCount(2), Reserved(8)
        blob = bytearray(256)
        struct.pack_into("<128s", blob, 0x00, title.encode('utf-8')[:127])
        struct.pack_into("<64s", blob, 0x80, author.encode('utf-8')[:63])
        struct.pack_into("<32s", blob, 0xC0, b"EPUB2XTC")  # Publisher
        struct.pack_into("<16s", blob, 0xE0, lang.encode('utf-8')[:15])
        struct.pack_into("<I", blob, 0xF0, int(time.time()))
        struct.pack_into("<H", blob, 0xF4, 0)  # Cover Page (usually 0)
        struct.pack_into("<H", blob, 0xF6, chapter_count)
        return bytes(blob)

    def _pack_chapter(self, name, start_pg, end_pg):
        """Packs a single chapter into a 96-byte fixed-size block."""
        # Fields: Name(80), Start(2), End(2), Reserved1-3(4+4+4)
        blob = bytearray(96)
        struct.pack_into("<80s", blob, 0x00, name.encode('utf-8')[:79])
        struct.pack_into("<H", blob, 0x50, start_pg)
        struct.pack_into("<H", blob, 0x52, end_pg)
        return bytes(blob)

    def save_xtc(self, out_name, progress_callback=None):
        if not self.is_ready: return

        # 1. SETUP PARAMETERS & DEPTH
        bit_depth = self.layout_settings.get("bit_depth", "1-bit (XTG)")
        render_mode = self.layout_settings.get("render_mode", "Threshold")
        is_2bit = "2-bit" in bit_depth
        file_id = 0x00485458 if is_2bit else 0x00475458

        # Determine how many TOC pages exist (to sync chapter start pages)
        num_toc_pages = len(self.toc_pages_images)

        # 2. CALCULATE FILE OFFSETS
        metadata_off = 56
        chapter_off = metadata_off + 256
        num_chaps = len(self.toc_data_final)
        index_off = chapter_off + (num_chaps * 96)
        data_off = index_off + (self.total_pages * 16)

        # 3. PACK METADATA
        book_title = getattr(self, 'title_metadata', "Unknown Title")
        book_author = getattr(self, 'author_metadata', "Unknown Author")
        metadata_block = self._pack_metadata(book_title, book_author, self.book_lang, num_chaps)

        # 4. PACK CHAPTERS (Synchronized with Visual TOC)
        chapter_block = bytearray()
        for i, (title, start_pg_from_renderer) in enumerate(self.toc_data_final):
            # start_pg_from_renderer is already 1-based and includes TOC offset
            # XTC internal structure expects 0-based index
            start_idx = start_pg_from_renderer - 1

            # Calculate end index (inclusive)
            if i + 1 < num_chaps:
                next_chap_start = self.toc_data_final[i + 1][1]
                end_idx = next_chap_start - 2  # Page before next chapter starts
            else:
                end_idx = self.total_pages - 1

            chapter_block.extend(self._pack_chapter(title, start_idx, end_idx))

        # 5. PROCESS ALL PAGES (TOC + CONTENT)
        blob_data, index_table = bytearray(), bytearray()

        for i in range(self.total_pages):
            if progress_callback: progress_callback((i + 1) / self.total_pages)

            # 1. Get the page
            # render_page ALREADY handles the rotation for Landscape modes now.
            # So img_rgb comes out as 480x800 (Portrait dimensions, sideways text).
            img_rgb = self.render_page(i)
            img_gray = img_rgb.convert("L")

            # 2. Get dimensions (No extra rotation needed here!)
            w, h = img_gray.size

            if is_2bit:
                # render_page already quantizes to {0, 85, 170, 255} with the chosen method and dark mode
                quant = img_gray.point(lambda p: 3 - (p // 64))
                pix = quant.load()
                bytes_per_col = (h + 7) // 8
                p1, p2 = bytearray(bytes_per_col * w), bytearray(bytes_per_col * w)

                for x_idx, x in enumerate(range(w - 1, -1, -1)):
                    for y in range(h):
                        val = pix[x, y] & 0x03
                        bit1 = (val >> 1) & 0x01
                        bit2 = val & 0x01
                        target_byte = (x_idx * bytes_per_col) + (y // 8)
                        bit_pos = 7 - (y % 8)
                        if bit1: p1[target_byte] |= (1 << bit_pos)
                        if bit2: p2[target_byte] |= (1 << bit_pos)

                bitmap_data = bytes(p1) + bytes(p2)
                data_size = len(bitmap_data)

            else:
                # --- XTG 1-BIT ROW-MAJOR ---
                thresh = self.layout_settings.get("text_threshold", 130)
                img_final = img_gray.point(lambda p: 255 if p > thresh else 0).convert("1")

                bitmap_data = img_final.tobytes()
                data_size = ((w + 7) // 8) * h

            # Page Blob = 22-byte XTG/XTH Header + Bitmap
            xt_header = struct.pack("<IHHBBIQ", file_id, w, h, 0, 0, data_size, 0)
            page_blob = xt_header + bitmap_data

            # Index entry (absolute offset from file start)
            index_table.extend(struct.pack("<QIHH", data_off + len(blob_data), len(page_blob), w, h))
            blob_data.extend(page_blob)

        # 6. FINAL CONTAINER HEADER
        header_magic = 0x48435458 if is_2bit else 0x00435458
        header = struct.pack("<IHHBBBBIQQQQQ",
                             header_magic,  # Identifier (XTCH for 2-bit, XTC\0 for 1-bit)
                             0x0100,  # Version (Must be here at Offset 0x04)
                             self.total_pages,  # Page Count (Must be here at Offset 0x06)
                             0, 1, 0, 1, 1,  # Flags
                             metadata_off, index_off, data_off, 0, chapter_off
                             )
        # Note: The format of struct.pack above must match the 56-byte definition exactly.
        # Ensure the number of 'Q' (uint64) and 'I' (uint32) matches your spec.

        with open(out_name, "wb") as f:
            f.write(header)
            f.write(metadata_block)
            f.write(chapter_block)
            f.write(index_table)
            f.write(blob_data)


# --- UI COLORS ---
COLOR_BG = "#111111"
COLOR_TOOLBAR = "#1a1a1a"
COLOR_CARD = "#2B2B2B"
COLOR_ACCENT = "#3498DB"
COLOR_SUCCESS = "#2ECC71"
COLOR_WARNING = "#E67E22"
COLOR_DANGER = "#C0392B"
COLOR_TEXT_GRAY = "#AAAAAA"


class SettingsCard(ctk.CTkFrame):
    def __init__(self, parent, title, expanded=False):
        super().__init__(parent, fg_color=COLOR_CARD, corner_radius=10, border_width=1, border_color="#333")
        self.pack(fill="x", pady=5, padx=10)

        self.title_text = title
        self.is_expanded = expanded

        # 1. HEADER: Use a Button instead of a Label to make it clickable
        # We use transparent fg_color so it looks like a header but reacts to hover
        self.btn_header = ctk.CTkButton(
            self,
            text=f"{'▼' if expanded else '▶'} {title}",
            command=self.toggle,
            fg_color="transparent",
            hover_color="#404040",
            anchor="w",
            font=("Arial", 13, "bold"),
            text_color="#FFF",
            height=35
        )
        self.btn_header.pack(fill="x", padx=2, pady=2)

        # 2. CONTENT FRAME: Created immediately so widgets can be added to it,
        # but only packed (shown) if expanded is True.
        self.content = ctk.CTkFrame(self, fg_color="transparent")

        if self.is_expanded:
            self.content.pack(fill="x", padx=10, pady=(0, 10))

    def toggle(self):
        """Show/Hide the content frame"""
        self.is_expanded = not self.is_expanded

        if self.is_expanded:
            self.btn_header.configure(text=f"▼ {self.title_text}")
            self.content.pack(fill="x", padx=10, pady=(0, 10))
        else:
            self.btn_header.configure(text=f"▶ {self.title_text}")
            self.content.pack_forget()


class ModernApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        # Load AoA on startup
        load_aoa_database()

        self.processor = EpubProcessor()
        self.current_page_index = 0
        self.debounce_timer = None
        self.is_processing = False
        self.pending_rerun = False
        self.selected_chapter_indices = None
        self.title("EPUB2XTC Converter")
        self.geometry("1400x950")
        self.configure(fg_color=COLOR_BG)
        if not os.path.exists(PRESETS_DIR): os.makedirs(PRESETS_DIR)
        self.startup_settings = FACTORY_DEFAULTS.copy()
        self.load_startup_defaults()
        self._build_toolbar()
        self.main_container = ctk.CTkFrame(self, fg_color=COLOR_BG)
        self.main_container.pack(fill="both", expand=True)
        self._build_sidebar()
        self._build_preview_area()
        self.refresh_presets_list()
        self.apply_settings_dict(self.startup_settings)

        # Keyboard navigation for page preview
        self.bind("<Left>", lambda e: self._on_arrow_key(-1))
        self.bind("<Right>", lambda e: self._on_arrow_key(1))
        self.bind("<Prior>", lambda e: self._on_arrow_key(-1))
        self.bind("<Next>", lambda e: self._on_arrow_key(1))

    def _on_arrow_key(self, direction):
        focused = self.focus_get()
        if focused:
            w = focused
            while w:
                cls_name = type(w).__name__.lower()
                if "entry" in cls_name or "text" in cls_name:
                    return
                w = getattr(w, "master", None)
        if direction < 0:
            self.prev_page()
        else:
            self.next_page()

    def _build_toolbar(self):
        tb = ctk.CTkFrame(self, height=70, fg_color=COLOR_TOOLBAR, corner_radius=0)
        tb.pack(fill="x", side="top")
        logo_f = ctk.CTkFrame(tb, fg_color="transparent")
        logo_f.pack(side="left", padx=(20, 30))
        ctk.CTkLabel(logo_f, text="EPUB2XTC", font=("Arial", 20, "bold"), text_color=COLOR_ACCENT).pack(anchor="w")
        self.lbl_file = ctk.CTkLabel(logo_f, text="No file selected", font=("Arial", 12), text_color="gray", anchor="w")
        self.lbl_file.pack(anchor="w")
        self._create_icon_btn(tb, "＋ Import EPUB", COLOR_SUCCESS, self.select_file)
        self._create_divider(tb)
        self.btn_chapters = self._create_icon_btn(tb, "☰ Edit TOC", COLOR_WARNING, self.open_chapter_dialog, "disabled")
        self.btn_export = self._create_icon_btn(tb, "⚡ Save .XTC", COLOR_ACCENT, self.export_file, "disabled")
        self.btn_cover = self._create_icon_btn(tb, "🖼 Export Cover", "#8E44AD", self.open_cover_export, "disabled")
        self._create_divider(tb)
        self._create_icon_btn(tb, "⟲ Reset Settings", "#555", self.reset_to_factory)
        right_f = ctk.CTkFrame(tb, fg_color="transparent")
        right_f.pack(side="right", padx=20, fill="y")
        self.progress_label = ctk.CTkLabel(right_f, text="Ready", font=("Arial", 12), text_color="gray", anchor="e")
        self.progress_label.pack(side="top", pady=(15, 0), anchor="e")
        self.progress_bar = ctk.CTkProgressBar(right_f, width=200, height=8, progress_color=COLOR_ACCENT)
        self.progress_bar.set(0)
        self.progress_bar.pack(side="bottom", pady=(0, 15))

    def refresh_all_slider_labels(self):
        """Forces all labels and entry boxes to match the current slider values (used after loading presets)."""
        # List of all standard slider attributes
        sliders = [
            ("lbl_size", "slider_font_size"),
            ("lbl_weight", "slider_font_weight"),
            ("lbl_line", "slider_line_height"),
            ("lbl_para_space", "slider_paragraph_spacing"),
            ("lbl_word_space", "slider_word_spacing"),
            ("lbl_margin", "slider_margin"),
            ("lbl_top_padding", "slider_top_padding"),
            ("lbl_padding", "slider_bottom_padding"),
            ("lbl_spectra_thresh", "slider_spectra_threshold"),
            ("lbl_spectra_aoa", "slider_spectra_aoa_threshold"),
            ("lbl_white_clip", "slider_white_clip"),
            ("lbl_contrast", "slider_contrast"),
            ("lbl_threshold", "slider_text_threshold"),
            ("lbl_blur", "slider_text_blur"),
            ("lbl_header_size", "slider_header_font_size"),
            ("lbl_header_margin", "slider_header_margin"),
            ("lbl_footer_size", "slider_footer_font_size"),
            ("lbl_footer_margin", "slider_footer_margin"),
            ("lbl_preview_zoom", "slider_preview_zoom"),
            ("lbl_marker_size", "slider_bar_marker_radius"),
            ("lbl_tick_height", "slider_bar_tick_height"),
            ("lbl_bar_thick", "slider_bar_height"),
            ("lbl_ui_margin", "slider_ui_side_margin"),
        ]

        for lbl_attr, sld_attr in sliders:
            if hasattr(self, lbl_attr) and hasattr(self, sld_attr):
                label_widget = getattr(self, lbl_attr)
                slider_widget = getattr(self, sld_attr)
                if hasattr(self, f"fmt_{sld_attr}"):
                    formatter = getattr(self, f"fmt_{sld_attr}")
                    label_widget.configure(text=formatter(slider_widget.get()))
                if hasattr(slider_widget, "entry") and hasattr(slider_widget, "get_fmt_val"):
                    formatted = str(slider_widget.get_fmt_val(slider_widget.get()))
                    if slider_widget.entry.get() != formatted:
                        slider_widget.entry.delete(0, "end")
                        slider_widget.entry.insert(0, formatted)

        # Dynamically refresh any slider_ attribute on self that has an entry or label
        for attr in dir(self):
            if attr.startswith("slider_"):
                sld = getattr(self, attr)
                if hasattr(sld, "entry") and hasattr(sld, "get_fmt_val") and hasattr(sld, "get"):
                    formatted = str(sld.get_fmt_val(sld.get()))
                    if sld.entry.get() != formatted:
                        sld.entry.delete(0, "end")
                        sld.entry.insert(0, formatted)
                if hasattr(sld, "lbl") and hasattr(sld, "get_label_text") and hasattr(sld, "get"):
                    sld.lbl.configure(text=sld.get_label_text(sld.get()))

    def _build_sidebar(self):
        self.sidebar = ctk.CTkScrollableFrame(self.main_container, width=400, fg_color="transparent")
        self.sidebar.pack(side="left", fill="y", padx=10, pady=10)

        # --- PRESETS ---
        c_pre = SettingsCard(self.sidebar, "PRESETS")
        row_pre = ctk.CTkFrame(c_pre.content, fg_color="transparent")
        row_pre.pack(fill="x")
        self.preset_var = ctk.StringVar(value="Select Preset...")
        self.preset_dropdown = ctk.CTkOptionMenu(row_pre, variable=self.preset_var, values=[],
                                                 command=self.load_selected_preset, fg_color="#444",
                                                 button_color="#555", height=22, font=("Arial", 12))
        self.preset_dropdown.pack(side="left", fill="x", expand=True, padx=(0, 5))
        ctk.CTkButton(row_pre, text="💾", width=30, height=22, command=self.save_new_preset,
                      fg_color=COLOR_SUCCESS).pack(side="left")
        ctk.CTkButton(c_pre.content, text="Set Current as Default", command=self.save_current_as_default,
                      fg_color="#444", hover_color=COLOR_ACCENT, height=24).pack(fill="x", pady=(5, 0))

        # --- RENDER ENGINE ---
        c_ren = SettingsCard(self.sidebar, "RENDER ENGINE")

        # 1. Selection: Bit Depth / Format
        r_depth = ctk.CTkFrame(c_ren.content, fg_color="transparent")
        r_depth.pack(fill="x", pady=2)
        ctk.CTkLabel(r_depth, text="Target Format:", font=("Arial", 12), width=130, anchor="w").pack(side="left")
        self.bit_depth_var = ctk.StringVar(value=self.startup_settings.get("bit_depth", "1-bit (XTG)"))
        ctk.CTkOptionMenu(r_depth, values=["1-bit (XTG)", "2-bit (XTH)"], variable=self.bit_depth_var,
                          command=self.schedule_update, height=22).pack(side="right", fill="x", expand=True)

        # 2. Selection: Conversion Algorithm
        r_mode = ctk.CTkFrame(c_ren.content, fg_color="transparent")
        r_mode.pack(fill="x", pady=2)
        ctk.CTkLabel(r_mode, text="Conversion:", font=("Arial", 12), width=130, anchor="w").pack(side="left")
        rm_val = self.startup_settings.get("render_mode", "Threshold")
        if rm_val == "Dither":
            rm_val = "Floyd-Steinberg"
        self.render_mode_var = ctk.StringVar(value=rm_val)
        ctk.CTkOptionMenu(r_mode, values=["Threshold", "Floyd-Steinberg", "Atkinson", "Bayer"], variable=self.render_mode_var,
                          command=self.toggle_render_controls, height=22).pack(side="right", fill="x", expand=True)

        self.var_invert = ctk.BooleanVar(value=bool(self.startup_settings.get("invert_colors", False)))
        ctk.CTkCheckBox(c_ren.content, text="Invert Colors (Dark Mode)", variable=self.var_invert,
                        command=self.schedule_update, height=22, font=("Arial", 12)).pack(fill="x", pady=(4, 2))

        self.frm_render_dynamic = ctk.CTkFrame(c_ren.content, fg_color="transparent")
        self.frm_render_dynamic.pack(fill="x", pady=5)

        # 3. Sliders: Dither Path
        self.frm_dither = ctk.CTkFrame(self.frm_render_dynamic, fg_color="transparent")
        self.sld_white = self._create_slider(self.frm_dither, "lbl_white_clip", "White Clip", "slider_white_clip", 150,
                                             255)
        self.sld_contrast = self._create_slider(self.frm_dither, "lbl_contrast", "Contrast", "slider_contrast", 0.5,
                                                2.0, is_float=True)

        # 4. Sliders: Threshold Path
        self.frm_thresh = ctk.CTkFrame(self.frm_render_dynamic, fg_color="transparent")
        self.sld_thresh = self._create_slider(self.frm_thresh, "lbl_threshold", "Threshold", "slider_text_threshold",
                                              50, 200)
        self.sld_blur = self._create_slider(self.frm_thresh, "lbl_blur", "Definition", "slider_text_blur", 0.0, 3.0,
                                            is_float=True)

        self.toggle_render_controls()

        # --- TYPOGRAPHY ---
        c_type = SettingsCard(self.sidebar, "TYPOGRAPHY")
        r_font = ctk.CTkFrame(c_type.content, fg_color="transparent")
        r_font.pack(fill="x", pady=2)
        ctk.CTkLabel(r_font, text="Font Family:", font=("Arial", 12), width=130, anchor="w").pack(side="left")
        self.available_fonts = get_local_fonts()
        if not self.available_fonts:
            self.available_fonts = {"No Fonts Found": ""}

        self.font_options = sorted(list(self.available_fonts.keys()))
        self.font_map = self.available_fonts.copy()
        self.font_dropdown = ctk.CTkOptionMenu(r_font, values=self.font_options, command=self.on_font_change, height=22,
                                               font=("Arial", 12))

        start_font = self.startup_settings.get("font_name", "")
        if start_font in self.font_options:
            self.font_dropdown.set(start_font)
        elif self.font_options:
            self.font_dropdown.set(self.font_options[0])

        self.font_dropdown.pack(side="right", fill="x", expand=True)
        r_align = ctk.CTkFrame(c_type.content, fg_color="transparent")
        r_align.pack(fill="x", pady=2)
        ctk.CTkLabel(r_align, text="Alignment:", font=("Arial", 12), width=130, anchor="w").pack(side="left")
        self.align_dropdown = ctk.CTkOptionMenu(r_align, values=["justify", "left"], command=self.schedule_update,
                                                height=22, font=("Arial", 12))
        self.align_dropdown.set(self.startup_settings["text_align"])
        self.align_dropdown.pack(side="right", fill="x", expand=True)

        self.var_hyphenate = ctk.BooleanVar(value=self.startup_settings.get("hyphenate_text", True))
        ctk.CTkCheckBox(c_type.content, text="Hyphenate Text", variable=self.var_hyphenate,
                        command=self.schedule_update, font=("Arial", 12)).pack(anchor="w", pady=5, padx=5)

        self._create_slider(c_type.content, "lbl_size", "Font Size", "slider_font_size", 12, 48)
        self._create_slider(c_type.content, "lbl_weight", "Font Weight", "slider_font_weight", 100, 900)
        self._create_slider(c_type.content, "lbl_line", "Line Height", "slider_line_height", 1.0, 2.5, is_float=True)
        self._create_slider(c_type.content, "lbl_para_space", "Paragraph Spacing", "slider_paragraph_spacing", 0.0, 2.0,
                            is_float=True)
        self._create_slider(c_type.content, "lbl_word_space", "Word Spacing", "slider_word_spacing", 0.0, 2.0,
                            is_float=True)

        # --- LAYOUT ---
        c_lay = SettingsCard(self.sidebar, "PAGE LAYOUT")
        r_ori = ctk.CTkFrame(c_lay.content, fg_color="transparent")
        r_ori.pack(fill="x", pady=2)
        ctk.CTkLabel(r_ori, text="Orientation:", font=("Arial", 12), width=130, anchor="w").pack(side="left")
        self.orientation_var = ctk.StringVar(value=self.startup_settings["orientation"])
        ctk.CTkOptionMenu(r_ori, values=["Portrait", "Landscape (90°)", "Landscape (270°)"],
                          variable=self.orientation_var,
                          command=self.schedule_update, height=22, font=("Arial", 12)).pack(side="right", fill="x",
                                                                                            expand=True)
        r_cover_rot = ctk.CTkFrame(c_lay.content, fg_color="transparent")
        r_cover_rot.pack(fill="x", pady=2)
        self.var_keep_cover_portrait = ctk.BooleanVar(value=self.startup_settings.get("keep_cover_portrait", True))
        ctk.CTkCheckBox(r_cover_rot, text="Keep Cover in Portrait", variable=self.var_keep_cover_portrait,
                        command=self.schedule_update, font=("Arial", 12)).pack(side="left")
        r_tog = ctk.CTkFrame(c_lay.content, fg_color="transparent")
        r_tog.pack(fill="x", pady=5)
        self.var_toc = ctk.BooleanVar(value=self.startup_settings["generate_toc"])
        ctk.CTkCheckBox(r_tog, text="Generate TOC", variable=self.var_toc, command=self.schedule_update,
                        font=("Arial", 12)).pack(side="left")
        self.var_footnotes = ctk.BooleanVar(value=self.startup_settings.get("show_footnotes", True))
        ctk.CTkCheckBox(r_tog, text="Inline Footnotes", variable=self.var_footnotes, command=self.schedule_update,
                        font=("Arial", 12)).pack(side="right")
        self._create_slider(c_lay.content, "lbl_margin", "Side Margin", "slider_margin", 0, 100)
        self._create_slider(c_lay.content, "lbl_top_padding", "Top Padding", "slider_top_padding", 0, 150)
        self._create_slider(c_lay.content, "lbl_padding", "Bottom Padding", "slider_bottom_padding", 0, 150)

        # In ModernApp._build_sidebar, inside the 'c_lay' (Layout) card:
        r_toc_pos = ctk.CTkFrame(c_lay.content, fg_color="transparent")
        r_toc_pos.pack(fill="x", pady=2)
        ctk.CTkLabel(r_toc_pos, text="TOC Insert Page:", font=("Arial", 12), width=130, anchor="w").pack(side="left")
        self.var_toc_page = ctk.StringVar(value=str(self.startup_settings.get("toc_insert_page", 1)))
        # We use a trace to trigger the auto-reload when the user types
        self.var_toc_page.trace_add("write", lambda *args: self.schedule_update())
        ctk.CTkEntry(r_toc_pos, textvariable=self.var_toc_page, width=50, height=22).pack(side="right")

        # --- HEADER & FOOTER ---
        c_hf = SettingsCard(self.sidebar, "HEADER & FOOTER")
        grid_hf = ctk.CTkFrame(c_hf.content, fg_color="transparent")
        grid_hf.pack(fill="x")

        # --- SPECTRA AI ANNOTATIONS (ISSUE 4 FIX: SEPARATE GEN FROM VIEW) ---
        c_spectra = SettingsCard(self.sidebar, "SPECTRA AI ANNOTATIONS")

        if not HAS_WORDFREQ or not HAS_OPENAI:
            ctk.CTkLabel(c_spectra.content, text="Missing libraries:\npip install wordfreq openai",
                         text_color=COLOR_DANGER).pack(pady=5)
            self.var_spectra_enabled = ctk.BooleanVar(value=False)
        else:

            self.var_spectra_enabled = ctk.BooleanVar(value=self.startup_settings.get("spectra_enabled", False))
            ctk.CTkCheckBox(c_spectra.content, text="Show Definitions Overlay", variable=self.var_spectra_enabled,
                            command=self.schedule_update).pack(anchor="w", pady=5)

            # --- NEW LANGUAGE DROPDOWN ---
            ctk.CTkLabel(c_spectra.content, text="Target Language:", font=("Arial", 12), anchor="w").pack(anchor="w")
            self.var_spectra_lang = ctk.StringVar(value=self.startup_settings.get("spectra_target_lang", "English"))
            languages = ["English", "Spanish", "French", "German", "Italian", "Polish", "Portuguese", "Russian",
                         "Chinese", "Japanese"]
            ctk.CTkOptionMenu(c_spectra.content, variable=self.var_spectra_lang, values=languages).pack(fill="x",
                                                                                                        pady=(0, 5))

            # Manual Trigger for API calls
            self.btn_spectra_gen = ctk.CTkButton(c_spectra.content, text="⚡ Analyze & Generate Definitions",
                                                 command=self.run_spectra_analysis, fg_color="#E67E22",
                                                 hover_color="#D35400")
            self.btn_spectra_gen.pack(fill="x", pady=5)

            def set_level(choice):
                if choice == "A2 (Beginner)":
                    self.slider_spectra_threshold.set(5.5)  # Common words
                    self.slider_spectra_aoa_threshold.set(4.0)  # Child words okay
                elif choice == "B1 (Intermediate)":
                    self.slider_spectra_threshold.set(4.5)
                    self.slider_spectra_aoa_threshold.set(8.0)
                elif choice == "B2 (Upper Intermediate)":
                    self.slider_spectra_threshold.set(3.8)  # Rarer words
                    self.slider_spectra_aoa_threshold.set(10.0)  # No child words
                elif choice == "C1 (Advanced)":
                    self.slider_spectra_threshold.set(3.2)  # Very rare words
                    self.slider_spectra_aoa_threshold.set(13.0)  # Academic only

                # Update label text for sliders
                self.lbl_spectra_thresh.configure(
                    text=f"Zipf Difficulty (Max): {self.slider_spectra_threshold.get():.1f}")
                self.lbl_spectra_aoa.configure(
                    text=f"Min. Age of Acquisition: {self.slider_spectra_aoa_threshold.get():.1f}")

                self.schedule_update()

            ctk.CTkLabel(c_spectra.content, text="Quick Level Select:", font=("Arial", 12, "bold"), anchor="w").pack(
                anchor="w", pady=(5, 0))
            self.level_var = ctk.StringVar(value="Select Level...")
            self.level_dropdown = ctk.CTkOptionMenu(
                c_spectra.content,
                variable=self.level_var,
                values=["A2 (Beginner)", "B1 (Intermediate)", "B2 (Upper Intermediate)", "C1 (Advanced)"],
                command=set_level
            )
            self.level_dropdown.pack(fill="x", pady=5)

            # Inside _build_sidebar -> Spectra Section
            self._create_slider(c_spectra.content, "lbl_spectra_thresh", "Zipf Difficulty (Max)",
                                "slider_spectra_threshold",
                                1.0, 7.0, is_float=True, trigger_auto_update=False)

            self._create_slider(c_spectra.content, "lbl_spectra_aoa", "Min. Age of Acquisition",
                                "slider_spectra_aoa_threshold",
                                0.0, 25.0, is_float=True, trigger_auto_update=False)

            # API Key Input
            ctk.CTkLabel(c_spectra.content, text="API Key:", font=("Arial", 12), anchor="w").pack(anchor="w",
                                                                                                  pady=(5, 0))
            self.entry_spectra_key = ctk.CTkEntry(c_spectra.content, show="*")
            self.entry_spectra_key.insert(0, self.startup_settings.get("spectra_api_key", ""))
            self.entry_spectra_key.pack(fill="x", pady=(0, 5))
            # Base URL
            ctk.CTkLabel(c_spectra.content, text="Base URL:", font=("Arial", 12), anchor="w").pack(anchor="w")
            self.entry_spectra_url = ctk.CTkEntry(c_spectra.content)
            self.entry_spectra_url.insert(0, self.startup_settings.get("spectra_base_url", "https://api.openai.com/v1"))
            self.entry_spectra_url.pack(fill="x", pady=(0, 5))
            # Model
            ctk.CTkLabel(c_spectra.content, text="Model:", font=("Arial", 12), anchor="w").pack(anchor="w")
            self.entry_spectra_model = ctk.CTkEntry(c_spectra.content)
            self.entry_spectra_model.insert(0, self.startup_settings.get("spectra_model", "gpt-4o-mini"))
            self.entry_spectra_model.pack(fill="x", pady=(0, 5))

        def add_elem_row(txt, var_pos_name, var_ord_name):
            r = ctk.CTkFrame(grid_hf, fg_color="transparent")
            r.pack(fill="x", pady=2)
            ctk.CTkLabel(r, text=txt, font=("Arial", 12), width=110, anchor="w").pack(side="left")
            var_p = ctk.StringVar(value=self.startup_settings.get(var_pos_name, "Hidden"))
            setattr(self, f"var_{var_pos_name}", var_p)
            ctk.CTkOptionMenu(r, variable=var_p, values=["Header", "Footer", "Hidden"], width=90, height=22,
                              font=("Arial", 12), command=self.schedule_update).pack(side="left", padx=5)
            var_o = ctk.StringVar(value=str(self.startup_settings.get(var_ord_name, 1)))
            var_o.trace_add("write", lambda *args: self.schedule_update())
            setattr(self, f"var_{var_ord_name}", var_o)
            ctk.CTkEntry(r, textvariable=var_o, width=35, height=22).pack(side="right")

        add_elem_row("Chapter Title", "pos_title", "order_title")
        add_elem_row("Page Number", "pos_pagenum", "order_pagenum")
        add_elem_row("Chapter Page", "pos_chap_page", "order_chap_page")
        add_elem_row("Reading %", "pos_percent", "order_percent")
        self._create_divider_horizontal(c_hf.content)
        ctk.CTkLabel(c_hf.content, text="Progress Bar", font=("Arial", 12, "bold"), text_color=COLOR_TEXT_GRAY).pack(
            anchor="w", pady=(10, 2))
        row_progress = ctk.CTkFrame(c_hf.content, fg_color="transparent")
        row_progress.pack(fill="x", pady=2)
        ctk.CTkLabel(row_progress, text="Position:", font=("Arial", 12), width=130, anchor="w").pack(side="left")
        self.var_pos_progress = ctk.StringVar(value=self.startup_settings.get("pos_progress", "Footer (Below Text)"))
        ctk.CTkOptionMenu(row_progress, variable=self.var_pos_progress,
                          values=["Header (Above Text)", "Header (Below Text)", "Header (Inline)",
                                  "Footer (Above Text)", "Footer (Below Text)", "Footer (Inline)", "Hidden"],
                          command=self.schedule_update, height=22, font=("Arial", 12)).pack(side="left", fill="x",
                                                                                            expand=True, padx=5)

        self.var_order_progress = ctk.StringVar(value=str(self.startup_settings.get("order_progress", 5)))
        self.var_order_progress.trace_add("write", lambda *args: self.schedule_update())
        ctk.CTkEntry(row_progress, textvariable=self.var_order_progress, width=35, height=22).pack(side="right")

        row_chk = ctk.CTkFrame(c_hf.content, fg_color="transparent")
        row_chk.pack(fill="x", pady=5)
        self.var_bar_ticks = ctk.BooleanVar(value=self.startup_settings.get("bar_show_ticks", True))
        ctk.CTkCheckBox(row_chk, text="Show Ticks", variable=self.var_bar_ticks, command=self.schedule_update,
                        font=("Arial", 12)).pack(side="left")
        self.var_bar_marker = ctk.BooleanVar(value=self.startup_settings.get("bar_show_marker", True))
        ctk.CTkCheckBox(row_chk, text="Show Marker", variable=self.var_bar_marker, command=self.schedule_update,
                        font=("Arial", 12)).pack(side="right")
        row_mc = ctk.CTkFrame(c_hf.content, fg_color="transparent")
        row_mc.pack(fill="x", pady=2)
        ctk.CTkLabel(row_mc, text="Marker Color:", font=("Arial", 12), width=130, anchor="w").pack(side="left")
        self.var_marker_color = ctk.StringVar(value=self.startup_settings.get("bar_marker_color", "Black"))
        ctk.CTkOptionMenu(row_mc, variable=self.var_marker_color, values=["Black", "White"], width=90, height=22,
                          font=("Arial", 12), command=self.schedule_update).pack(side="left", fill="x", expand=True)
        self._create_slider(c_hf.content, "lbl_marker_size", "Marker Radius", "slider_bar_marker_radius", 2, 10)
        self._create_slider(c_hf.content, "lbl_tick_height", "Tick Height", "slider_bar_tick_height", 2, 20)
        self._create_slider(c_hf.content, "lbl_bar_thick", "Bar Thickness", "slider_bar_height", 1, 10)
        self._create_divider_horizontal(c_hf.content)
        ctk.CTkLabel(c_hf.content, text="Specific Styles", font=("Arial", 12, "bold"), text_color=COLOR_TEXT_GRAY).pack(
            anchor="w", pady=(10, 2))
        f_adv = ctk.CTkFrame(c_hf.content, fg_color="transparent")
        f_adv.pack(fill="x")
        align_options = ["Left", "Center", "Right", "Justify"]
        r_h_align = ctk.CTkFrame(f_adv, fg_color="transparent")
        r_h_align.pack(fill="x", pady=2)
        ctk.CTkLabel(r_h_align, text="Header Align:", font=("Arial", 12), width=130, anchor="w").pack(side="left")
        self.var_header_align = ctk.StringVar(value=self.startup_settings.get("header_align", "Center"))
        ctk.CTkOptionMenu(r_h_align, variable=self.var_header_align, values=align_options, command=self.schedule_update,
                          height=22, font=("Arial", 12)).pack(side="right", fill="x", expand=True)
        self._create_slider(f_adv, "lbl_header_size", "Header Font Size", "slider_header_font_size", 8, 30)
        self._create_slider(f_adv, "lbl_header_margin", "Header Y-Offset", "slider_header_margin", 0, 80)
        self._create_divider_horizontal(f_adv)

        r_ui_style = ctk.CTkFrame(f_adv, fg_color="transparent")
        r_ui_style.pack(fill="x", pady=2)
        ctk.CTkLabel(r_ui_style, text="UI Font:", font=("Arial", 12), width=130, anchor="w").pack(side="left")
        self.var_ui_font = ctk.StringVar(value=self.startup_settings.get("ui_font_source", "Body Font"))
        ctk.CTkOptionMenu(r_ui_style, variable=self.var_ui_font, values=["Body Font", "Sans-Serif", "Serif"],
                          command=self.schedule_update, height=22, font=("Arial", 12)).pack(side="right", fill="x",
                                                                                            expand=True)

        r_sep = ctk.CTkFrame(f_adv, fg_color="transparent")
        r_sep.pack(fill="x", pady=2)
        ctk.CTkLabel(r_sep, text="Separator:", font=("Arial", 12), width=130, anchor="w").pack(side="left")
        self.entry_separator = ctk.CTkComboBox(r_sep, height=22,
                                               values=["   |   ", "   -   ", "   •   ", "   ~   ", "   //   ", "   * "],
                                               command=self.schedule_update, font=("Arial", 12))
        self.entry_separator.set(self.startup_settings.get("ui_separator", "   |   "))
        self.entry_separator.pack(side="right", fill="x", expand=True)
        if hasattr(self.entry_separator, "_entry"): self.entry_separator._entry.bind("<KeyRelease>",
                                                                                     self.schedule_update)

        self._create_slider(f_adv, "lbl_ui_margin", "Side Margin", "slider_ui_side_margin", 0, 100)
        self._create_divider_horizontal(f_adv)

        r_f_align = ctk.CTkFrame(f_adv, fg_color="transparent")
        r_f_align.pack(fill="x", pady=2)
        ctk.CTkLabel(r_f_align, text="Footer Align:", font=("Arial", 12), width=130, anchor="w").pack(side="left")
        self.var_footer_align = ctk.StringVar(value=self.startup_settings.get("footer_align", "Center"))
        ctk.CTkOptionMenu(r_f_align, variable=self.var_footer_align, values=align_options, command=self.schedule_update,
                          height=22, font=("Arial", 12)).pack(side="right", fill="x", expand=True)
        self._create_slider(f_adv, "lbl_footer_size", "Footer Font Size", "slider_footer_font_size", 8, 30)
        self._create_slider(f_adv, "lbl_footer_margin", "Footer Y-Offset", "slider_footer_margin", 0, 80)

    def _build_preview_area(self):
        self.preview_frame = ctk.CTkFrame(self.main_container, fg_color="#181818")
        self.preview_frame.pack(side="left", fill="both", expand=True, padx=(0, 10), pady=10)

        # Instantiate it ONCE with the new colors
        self.preview_scroll = ctk.CTkScrollableFrame(
            self.preview_frame,
            fg_color="transparent",
            scrollbar_button_color="#2B2B2B",  # Matches your card color
            scrollbar_button_hover_color="#404040"
        )

        # CRITICAL: Put the pack() method back so it actually shows up on screen!
        self.preview_scroll.pack(fill="both", expand=True)

        self.preview_scroll.grid_columnconfigure(0, weight=1)
        self.preview_scroll.grid_rowconfigure(0, weight=1)

        self.img_label = ctk.CTkLabel(self.preview_scroll, text="Open an EPUB to begin", font=("Arial", 16, "bold"),
                                      text_color="#333")
        self.img_label.grid(row=0, column=0, pady=20, padx=20)

        def pass_scroll_event(event):
            # Depending on the OS, delta is handled differently
            if event.num == 4 or event.delta > 0:
                self.preview_scroll._parent_canvas.yview_scroll(-1, "units")
            elif event.num == 5 or event.delta < 0:
                self.preview_scroll._parent_canvas.yview_scroll(1, "units")

        # Bind for Windows / MacOS
        self.img_label.bind("<MouseWheel>", pass_scroll_event)
        # Bind for Linux
        self.img_label.bind("<Button-4>", pass_scroll_event)
        self.img_label.bind("<Button-5>", pass_scroll_event)
        self.img_label.bind("<Button-1>", lambda e: self.focus_set())

        ctrl_bar = ctk.CTkFrame(self.preview_frame, height=50, fg_color=COLOR_TOOLBAR, corner_radius=15)
        ctrl_bar.pack(side="bottom", fill="x", padx=20, pady=20)
        f_nav = ctk.CTkFrame(ctrl_bar, fg_color="transparent")
        f_nav.place(relx=0.5, rely=0.5, anchor="center")
        ctk.CTkButton(f_nav, text="◀", width=40, command=self.prev_page, fg_color="#333").pack(side="left", padx=5)
        f_page_stack = ctk.CTkFrame(f_nav, fg_color="transparent")
        f_page_stack.pack(side="left", padx=10)
        self.lbl_page = ctk.CTkLabel(f_page_stack, text="0 / 0", font=("Arial", 13, "bold"), width=160)
        self.lbl_page.pack(side="top")
        self.entry_page = ctk.CTkEntry(f_page_stack, width=50, height=20, placeholder_text="#", justify="center",
                                       font=("Arial", 10))
        self.entry_page.pack(side="top", pady=(2, 0))
        self.entry_page.bind('<Return>', lambda e: self.go_to_page())
        ctk.CTkButton(f_nav, text="▶", width=40, command=self.next_page, fg_color="#333").pack(side="left", padx=5)
        f_zoom = ctk.CTkFrame(ctrl_bar, fg_color="transparent")
        f_zoom.place(relx=0.97, rely=0.5, anchor="e")  # Pins it to the right edge vertically centered
        self._create_slider(f_zoom, "lbl_preview_zoom", "Zoom", "slider_preview_zoom", 200, 800, width=150,
                            label_width=50, show_value=False)

    def _create_icon_btn(self, parent, text, hover_col, cmd, state="normal"):
        b = ctk.CTkButton(parent, text=text, command=cmd, state=state, width=110, height=35, corner_radius=8,
                          font=("Arial", 12, "bold"), fg_color=COLOR_CARD, hover_color=hover_col)
        b.pack(side="left", padx=5)
        return b

    def _create_divider(self, parent):
        ctk.CTkFrame(parent, width=2, height=30, fg_color="#333").pack(side="left", padx=10)

    def _create_divider_horizontal(self, parent):
        ctk.CTkFrame(parent, height=2, fg_color="#333").pack(fill="x", pady=10)

    def _create_slider(self, parent, label_attr, text, slider_attr, min_v, max_v, is_float=False, width=None,
                       trigger_auto_update=True, label_width=130, show_value=True):
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.pack(fill="x", pady=2)

        setting_key = slider_attr.replace('slider_', '')
        default_val = self.startup_settings.get(setting_key, min_v)

        # Helper to format values
        def get_fmt_val(val):
            return float(f"{val:.1f}") if is_float else int(val)

        def get_label_text(val):
            if not show_value:
                return text
            v_num = get_fmt_val(val)
            return f"{text}: {v_num}"

        # 1. Label
        lbl = ctk.CTkLabel(f, text=get_label_text(default_val), font=("Arial", 12), anchor="w", width=label_width)
        lbl.pack(side="left")

        # 2. Entry Field (Created before slider so callback can reference it)
        entry = ctk.CTkEntry(f, width=50, height=22, font=("Arial", 11))
        entry.pack(side="right", padx=(5, 0))

        # 3. Slider Callback (Updates Entry + Label)
        def on_slide(val):
            lbl.configure(text=get_label_text(val))

            # Sync slider drag to entry text
            current_val = get_fmt_val(val)
            # Only update if different to prevent typing glitches
            if entry.get() != str(current_val):
                entry.delete(0, "end")
                entry.insert(0, str(current_val))

            if trigger_auto_update:
                self.schedule_update()

        # 4. Slider Widget
        sld = ctk.CTkSlider(f, from_=min_v, to=max_v, command=on_slide, height=16)
        if width: sld.configure(width=width)
        sld.set(default_val)
        sld.pack(side="left", fill="x", expand=True, padx=(5, 0))

        # 5. Entry Callback (Validates Input -> Updates Slider)
        def on_entry_change(event=None):
            try:
                val_str = entry.get()
                val = float(val_str) if is_float else int(val_str)

                # Clamp value to min/max
                if val < min_v: val = min_v
                if val > max_v: val = max_v

                # Update UI
                sld.set(val)
                lbl.configure(text=get_label_text(val))

                # Format entry text nicely
                entry.delete(0, "end")
                entry.insert(0, str(val))

                # Trigger App Update
                if trigger_auto_update:
                    self.schedule_update()

                # Remove focus from entry so hotkeys work again
                self.focus_set()

            except ValueError:
                # Invalid number? Revert to current slider value
                entry.delete(0, "end")
                entry.insert(0, str(get_fmt_val(sld.get())))

        # Bind Enter Key and Focus Out
        entry.bind("<Return>", on_entry_change)
        entry.bind("<FocusOut>", on_entry_change)

        # Set Initial Entry Value
        entry.insert(0, str(get_fmt_val(default_val)))

        # Wrap slider.set() to keep companion entry field and label in sync when set programmatically (e.g. Presets)
        orig_set = sld.set
        def custom_set(val):
            orig_set(val)
            lbl.configure(text=get_label_text(val))
            formatted = str(get_fmt_val(val))
            if entry.get() != formatted:
                entry.delete(0, "end")
                entry.insert(0, formatted)
        sld.set = custom_set
        sld.entry = entry
        sld.lbl = lbl
        sld.get_fmt_val = get_fmt_val
        sld.get_label_text = get_label_text

        # Register attributes
        if label_attr:
            setattr(self, label_attr, lbl)
        setattr(self, f"fmt_{slider_attr}", get_label_text)
        setattr(self, slider_attr, sld)

        return sld

    def toggle_render_controls(self, _=None):
        mode = self.render_mode_var.get()

        # Always hide both first
        self.frm_dither.pack_forget()
        self.frm_thresh.pack_forget()

        # If any dithering algorithm is selected, show Contrast/White Clip for cleaning gradients
        if mode in ("Dither", "Floyd-Steinberg", "Atkinson", "Bayer"):
            self.frm_dither.pack(fill="x")
        # If Threshold is selected, show Threshold/Blur for text sharpening
        elif mode == "Threshold":
            self.frm_thresh.pack(fill="x")

        self.schedule_update()

    def select_file(self):
        try:
            import importers
            ftypes = importers.file_dialog_types()
        except ImportError:
            ftypes = [("EPUB", "*.epub")]
        path = filedialog.askopenfilename(filetypes=ftypes)
        if path:
            self.processor.input_file = path
            self.current_page_index = 0
            self.lbl_file.configure(text=os.path.basename(path))
            self.progress_label.configure(text="Parsing...")
            threading.Thread(target=self._task_parse_structure).start()

    def _task_parse_structure(self):
        success = self.processor.parse_book_structure(self.processor.input_file)
        self.after(0, lambda: self._on_structure_parsed(success))

    def _on_structure_parsed(self, success):
        if not success:
            messagebox.showerror("Error", "Failed to parse file.")
            return

        # Refresh local font cache and dropdown
        self.available_fonts = get_local_fonts()
        self.font_options = sorted(list(self.available_fonts.keys()))
        self.font_map = self.available_fonts.copy()
        self.font_dropdown.configure(values=self.font_options)

        self.btn_chapters.configure(state="normal")
        self.open_chapter_dialog()

    def open_chapter_dialog(self):
        # Simply pass self.selected_chapter_indices (which is None on first run)
        ChapterSelectionDialog(
            self,
            self.processor.raw_chapters,
            self.selected_chapter_indices,
            self._on_chapters_selected
        )

    def _on_chapters_selected(self, selected_indices):
        self.selected_chapter_indices = selected_indices
        self.run_processing()

    def run_spectra_analysis(self):
        """Manually trigger API Analysis with Regenerate Option"""
        if not self.processor.input_file or not self.selected_chapter_indices: return
        if not self.entry_spectra_key.get():
            messagebox.showerror("Missing Key", "Please enter an OpenAI API Key.")
            return

        # NEW LOGIC: Ask user if they want to force regenerate
        force_regen = False

        # Check if we already have some definitions in memory
        has_cache = hasattr(self.processor.annotator, 'master_cache') and bool(self.processor.annotator.master_cache)

        if has_cache:
            # Ask the user
            ans = messagebox.askyesnocancel("Regenerate Annotations?",
                                            "Definitions already exist.\n\n"
                                            "Yes = Force Regenerate ALL (Overwrites existing, uses more API credits)\n"
                                            "No = Only Scan for Missing words (Cheaper)\n"
                                            "Cancel = Stop")
            if ans is None:
                return  # Cancelled
            if ans:
                force_regen = True

        self.is_processing = True
        self.progress_label.configure(text="Scanning Words...")
        settings = self.gather_current_ui_settings()

        # Pass the force_regen flag to the task
        threading.Thread(target=lambda: self._task_analyze(settings, force_regen)).start()

    def _task_analyze(self, layout_settings, force=False):
        # Init annotator with current settings
        self.processor.init_annotator(layout_settings)

        # Run scanning/fetching with FORCE flag
        self.processor.annotator.analyze_chapters(
            self.processor.raw_chapters,
            self.selected_chapter_indices,
            progress_callback=lambda v: self.update_progress_ui(v, "Analyzing"),
            force=force  # <--- Passed here
        )

        # After analysis, re-render the layout to apply new cache
        self.after(0, self.run_processing)

    def run_processing(self):
        if not self.processor.input_file: return

        self.is_processing = True
        self.pending_rerun = False  # We are starting the latest job now

        self.progress_label.configure(text="Rendering Layout...")

        # Gather settings NOW (so we get the very latest slider values)
        settings = self.gather_current_ui_settings()

        threading.Thread(target=lambda: self._task_render(settings)).start()

    def _task_render(self, layout_settings):
        success = self.processor.render_chapters(
            self.selected_chapter_indices,
            self.processor.font_path,
            int(self.slider_font_size.get()),
            int(self.slider_margin.get()),
            float(self.slider_line_height.get()),
            int(self.slider_font_weight.get()),
            int(self.slider_bottom_padding.get()),
            int(self.slider_top_padding.get()),
            text_align=self.align_dropdown.get(),
            orientation=self.orientation_var.get(),
            add_toc=self.var_toc.get(),
            show_footnotes=self.var_footnotes.get(),
            layout_settings=layout_settings,
            progress_callback=lambda v: self.update_progress_ui(v, "Layout")
        )
        self.after(0, lambda: self._done(success))

    def _done(self, success):
        self.is_processing = False

        # CHECK IF A NEW CHANGE CAME IN WHILE WE WERE WORKING
        if self.pending_rerun:
            # Recursive restart: Run again immediately with the new settings
            self.after(10, self.run_processing)
            return

        # Normal finish
        self.progress_label.configure(text="Ready")
        self.progress_bar.set(0)

        if success:
            self.btn_export.configure(state="normal")
            self.btn_cover.configure(state="normal")
            self.show_page(self.current_page_index)
        else:
            messagebox.showerror("Error", "Processing failed.")

    def update_progress_ui(self, val, stage_text="Processing"):
        try:
            # Check if the widget still exists before trying to update it
            if not self.winfo_exists(): return

            self.after(0, lambda: [
                self.progress_bar.set(val),
                self.progress_label.configure(text=f"{stage_text} {int(val * 100)}%")
            ])
        except:
            pass

    def show_page(self, idx):
        if not self.processor.is_ready: return
        self.current_page_index = idx
        img = self.processor.render_page(idx)
        base_size = int(self.slider_preview_zoom.get())
        if img.width > img.height:
            target_h = base_size
            aspect_ratio = img.width / img.height
            target_w = int(target_h * aspect_ratio)
        else:
            target_w = base_size
            aspect_ratio = img.height / img.width
            target_h = int(target_w * aspect_ratio)
        ctk_img = ctk.CTkImage(light_image=img, size=(target_w, target_h))
        self.img_label.configure(image=ctk_img, text="")
        if getattr(self.processor, "is_comic", False) and getattr(self.processor, "comic_units", None):
            u = self.processor.comic_units[idx] if idx < len(self.processor.comic_units) else None
            total_actual = len(self.processor.comic_meta["pages"]) if self.processor.comic_meta and "pages" in self.processor.comic_meta else 0
            if u and total_actual:
                actual_page = u["p"] + 1
                half_str = f" {u['half']}" if u.get("half") else ""
                self.lbl_page.configure(text=f"{idx + 1} / {self.processor.total_pages} (p. {actual_page}{half_str}/{total_actual})")
            else:
                self.lbl_page.configure(text=f"{idx + 1} / {self.processor.total_pages}")
        else:
            self.lbl_page.configure(text=f"{idx + 1} / {self.processor.total_pages}")
        self.preview_scroll.update_idletasks()

    def prev_page(self):
        self.show_page(max(0, self.current_page_index - 1))

    def next_page(self):
        self.show_page(min(self.processor.total_pages - 1, self.current_page_index + 1))

    def go_to_page(self):
        try:
            target = int(self.entry_page.get())
            self.show_page(max(0, min(target - 1, self.processor.total_pages - 1)))
        except:
            pass
        self.entry_page.delete(0, 'end')

    def export_file(self):
        is_2bit = "2-bit" in self.bit_depth_var.get()
        default_ext = ".xtch" if is_2bit else ".xtc"
        if is_2bit:
            filetypes = [("XTCH 2-bit files", "*.xtch"), ("XTC files", "*.xtc"), ("All files", "*.*")]
        else:
            filetypes = [("XTC files", "*.xtc"), ("XTCH 2-bit files", "*.xtch"), ("All files", "*.*")]
        path = filedialog.asksaveasfilename(defaultextension=default_ext, filetypes=filetypes)
        if path: threading.Thread(target=lambda: self._run_export(path)).start()

    def _run_export(self, path):
        self.processor.save_xtc(path, progress_callback=lambda v: self.update_progress_ui(v, "Exporting"))
        self.after(0, lambda: messagebox.showinfo("Success", "XTC file saved."))

    def open_cover_export(self):
        if self.processor.cover_image_obj:
            fname = "cover.bmp"
            if hasattr(self.processor, 'title_metadata') and self.processor.title_metadata:
                safe = "".join([c for c in self.processor.title_metadata if c.isalnum() or c in " -_"]).strip()
                if safe: fname = f"{safe}.bmp"
            path = filedialog.asksaveasfilename(defaultextension=".bmp", initialfile=fname,
                                                filetypes=[("Bitmap", "*.bmp")])
            if path: threading.Thread(
                target=lambda: self._run_cover_export(path, *ACTIVE_DEVICE_SIZE, "Crop to Fill (Best)")).start()
        else:
            messagebox.showinfo("Info", "No cover found.")

    def _run_cover_export(self, path, w, h, mode):
        try:
            self.update_progress_ui(0.5, "Exporting Cover")
            img = self.processor.cover_image_obj.convert("RGB")
            if "Stretch" in mode:
                img = img.resize((w, h), Image.Resampling.LANCZOS)
            elif "Fit" in mode:
                img = ImageOps.pad(img, (w, h), color="white", centering=(0.5, 0.5))
            else:
                img = ImageOps.fit(img, (w, h), centering=(0.5, 0.5))
            img = img.convert("L")
            img = ImageEnhance.Contrast(img).enhance(1.6)
            img = img.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
            img.save(path, format="BMP")
            self.update_progress_ui(1.0, "Done")
            self.after(0, lambda: messagebox.showinfo("Success", "Cover saved."))
        except Exception as e:
            self.after(0, lambda: messagebox.showerror("Error", str(e)))

    def schedule_update(self, _=None):
        if not self.processor.input_file: return

        # Immediate visual feedback
        self.progress_label.configure(text="Pending changes...")

        # Cancel previous timer if it exists (standard debounce)
        if self.debounce_timer:
            try:
                self.after_cancel(self.debounce_timer)
            except:
                pass

        # Wait 500ms (increased from 200ms for stability)
        self.debounce_timer = self.after(500, self.trigger_processing)

    def trigger_processing(self):
        if self.is_processing:
            # If busy, mark that we need to run again as soon as we finish
            self.pending_rerun = True
            return

        self.run_processing()

    def gather_current_ui_settings(self):
        def get_order(var_name):
            try:
                return int(getattr(self, var_name).get())
            except:
                return 99

        spectra_en = self.var_spectra_enabled.get() if hasattr(self, 'var_spectra_enabled') else False
        spectra_key = self.entry_spectra_key.get() if hasattr(self, 'entry_spectra_key') else ""
        spectra_url = self.entry_spectra_url.get() if hasattr(self, 'entry_spectra_url') else ""
        spectra_model = self.entry_spectra_model.get() if hasattr(self, 'entry_spectra_model') else ""
        spectra_thresh = float(self.slider_spectra_threshold.get()) if hasattr(self,
                                                                               'slider_spectra_threshold') else 4.0
        # NEW AoA
        spectra_aoa = float(self.slider_spectra_aoa_threshold.get()) if hasattr(self,
                                                                                'slider_spectra_aoa_threshold') else 0.0
        spectra_lang = self.var_spectra_lang.get() if hasattr(self, 'var_spectra_lang') else "English"

        try:
            toc_insert = int(self.var_toc_page.get())
        except (ValueError, AttributeError):
            toc_insert = 1

        return {
            "toc_insert_page": toc_insert,
            "font_size": int(self.slider_font_size.get()),
            "font_weight": int(self.slider_font_weight.get()),
            "line_height": float(self.slider_line_height.get()),
            "paragraph_spacing": float(self.slider_paragraph_spacing.get()),
            "word_spacing": float(self.slider_word_spacing.get()),
            "margin": int(self.slider_margin.get()),
            "top_padding": int(self.slider_top_padding.get()),
            "bottom_padding": int(self.slider_bottom_padding.get()),
            "orientation": self.orientation_var.get(),
            "keep_cover_portrait": self.var_keep_cover_portrait.get() if hasattr(self, 'var_keep_cover_portrait') else True,
            "text_align": self.align_dropdown.get(),
            "hyphenate_text": self.var_hyphenate.get(),
            "font_name": self.font_dropdown.get(),
            "preview_zoom": int(self.slider_preview_zoom.get()),
            "generate_toc": self.var_toc.get(),
            "show_footnotes": self.var_footnotes.get(),
            "bar_height": int(self.slider_bar_height.get()),
            "pos_title": self.var_pos_title.get(),
            "pos_pagenum": self.var_pos_pagenum.get(),
            "pos_chap_page": self.var_pos_chap_page.get(),
            "pos_percent": self.var_pos_percent.get(),
            "pos_progress": self.var_pos_progress.get(),
            "order_progress": get_order("var_order_progress"),
            "order_title": get_order("var_order_title"),
            "order_pagenum": get_order("var_order_pagenum"),
            "order_chap_page": get_order("var_order_chap_page"),
            "order_percent": get_order("var_order_percent"),
            "bar_show_ticks": self.var_bar_ticks.get(),
            "bar_show_marker": self.var_bar_marker.get(),
            "bar_marker_color": self.var_marker_color.get(),
            "bar_marker_radius": int(self.slider_bar_marker_radius.get()),
            "bar_tick_height": int(self.slider_bar_tick_height.get()),
            "header_align": self.var_header_align.get(),
            "header_font_size": int(self.slider_header_font_size.get()),
            "header_margin": int(self.slider_header_margin.get()),
            "ui_font_source": self.var_ui_font.get(),
            "ui_separator": self.entry_separator.get(),
            "ui_side_margin": int(self.slider_ui_side_margin.get()),
            "footer_align": self.var_footer_align.get(),
            "footer_font_size": int(self.slider_footer_font_size.get()),
            "footer_margin": int(self.slider_footer_margin.get()),
            "render_mode": self.render_mode_var.get(),
            "bit_depth": self.bit_depth_var.get(),  # Critical: 1-bit vs 2-bit
            "invert_colors": bool(self.var_invert.get()) if hasattr(self, "var_invert") else False,
            "white_clip": int(self.slider_white_clip.get()),
            "contrast": float(self.slider_contrast.get()),
            "text_threshold": int(self.slider_text_threshold.get()),
            "text_blur": float(self.slider_text_blur.get()),
            "comic_bolden": self.startup_settings.get("comic_bolden", "Off"),
            "comic_clean_bubbles": bool(self.startup_settings.get("comic_clean_bubbles", False)),

            # Spectra
            "spectra_enabled": spectra_en,
            "spectra_api_key": spectra_key,
            "spectra_base_url": spectra_url,
            "spectra_model": spectra_model,
            "spectra_threshold": spectra_thresh,
            "spectra_aoa_threshold": spectra_aoa,  # NEW
            "spectra_target_lang": spectra_lang,
        }

    def load_startup_defaults(self):
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r") as f:
                    self.startup_settings.update(json.load(f))
            except:
                pass

    def refresh_presets_list(self):
        files = glob.glob(os.path.join(PRESETS_DIR, "*.json"))
        names = [os.path.basename(f).replace(".json", "") for f in files]
        names.sort()
        self.preset_dropdown.configure(values=names if names else ["No Presets"])

    def load_selected_preset(self, choice):
        if choice in ["No Presets", "Select Preset..."]: return
        path = os.path.join(PRESETS_DIR, f"{choice}.json")
        if os.path.exists(path):
            with open(path, "r") as f: self.apply_settings_dict(json.load(f))

    def save_new_preset(self):
        name = simpledialog.askstring("New Preset", "Name:")
        if name:
            safe = "".join(x for x in name if x.isalnum() or x in " -_")
            with open(os.path.join(PRESETS_DIR, f"{safe}.json"), "w") as f:
                json.dump(self.gather_current_ui_settings(), f, indent=4)
            self.refresh_presets_list()
            self.preset_var.set(safe)

    def save_current_as_default(self):
        with open(SETTINGS_FILE, "w") as f: json.dump(self.gather_current_ui_settings(), f, indent=4)
        messagebox.showinfo("Saved", "Settings saved as default.")

    def reset_to_factory(self):
        if messagebox.askyesno("Reset", "Reset to factory settings?"): self.apply_settings_dict(FACTORY_DEFAULTS)

    def apply_settings_dict(self, s):
        defaults = FACTORY_DEFAULTS.copy()
        defaults.update(s)
        s = defaults

        if hasattr(self, 'var_toc_page'):
            self.var_toc_page.set(str(s.get("toc_insert_page", 1)))

        # --- Standard Sliders & Dropdowns ---
        self.bit_depth_var.set(s.get("bit_depth", "1-bit (XTG)"))
        self.slider_font_size.set(s['font_size'])
        self.slider_font_weight.set(s['font_weight'])
        self.slider_line_height.set(s['line_height'])
        self.slider_paragraph_spacing.set(s.get('paragraph_spacing', 0.5))
        self.slider_word_spacing.set(s.get('word_spacing', 0.0))
        self.slider_margin.set(s['margin'])
        self.slider_top_padding.set(s['top_padding'])
        self.slider_bottom_padding.set(s['bottom_padding'])
        self.orientation_var.set(s['orientation'])
        if hasattr(self, 'var_keep_cover_portrait'):
            self.var_keep_cover_portrait.set(s.get('keep_cover_portrait', True))
        self.align_dropdown.set(s['text_align'])
        self.var_hyphenate.set(s.get('hyphenate_text', True))
        self.slider_preview_zoom.set(s['preview_zoom'])
        self.var_toc.set(s['generate_toc'])
        self.var_footnotes.set(s.get('show_footnotes', True))
        self.slider_bar_height.set(s['bar_height'])

        # --- Header/Footer/Progress ---
        self.var_pos_title.set(s['pos_title'])
        self.var_pos_pagenum.set(s['pos_pagenum'])
        self.var_pos_chap_page.set(s['pos_chap_page'])
        self.var_pos_percent.set(s['pos_percent'])
        self.var_pos_progress.set(s['pos_progress'])
        self.var_order_progress.set(str(s.get('order_progress', 5)))
        self.var_order_title.set(str(s['order_title']))
        self.var_order_pagenum.set(str(s['order_pagenum']))
        self.var_order_chap_page.set(str(s['order_chap_page']))
        self.var_order_percent.set(str(s['order_percent']))
        self.var_bar_ticks.set(s['bar_show_ticks'])
        self.var_bar_marker.set(s['bar_show_marker'])
        self.var_marker_color.set(s['bar_marker_color'])
        self.slider_bar_marker_radius.set(s['bar_marker_radius'])
        self.slider_bar_tick_height.set(s['bar_tick_height'])
        self.var_header_align.set(s['header_align'])
        self.slider_header_font_size.set(s['header_font_size'])
        self.slider_header_margin.set(s['header_margin'])
        self.var_ui_font.set(s.get('ui_font_source', "Body Font"))
        self.entry_separator.set(s.get('ui_separator', "   |   "))
        self.slider_ui_side_margin.set(s.get('ui_side_margin', 15))
        self.var_footer_align.set(s['footer_align'])
        self.slider_footer_font_size.set(s['footer_font_size'])
        self.slider_footer_margin.set(s['footer_margin'])

        # --- Rendering ---
        rm_val = s.get("render_mode", "Threshold")
        if rm_val == "Dither":
            rm_val = "Floyd-Steinberg"
        self.render_mode_var.set(rm_val)
        if hasattr(self, 'var_invert'):
            self.var_invert.set(bool(s.get('invert_colors', False)))
        self.slider_white_clip.set(s.get("white_clip", 220))
        self.slider_contrast.set(s.get("contrast", 1.2))
        self.slider_text_threshold.set(s.get("text_threshold", 130))
        self.slider_text_blur.set(s.get("text_blur", 1.0))

        # --- SPECTRA AI SETTINGS (FIXED) ---
        if hasattr(self, 'var_spectra_enabled'):
            self.var_spectra_enabled.set(s.get('spectra_enabled', False))

        if hasattr(self, 'slider_spectra_threshold'):
            self.slider_spectra_threshold.set(s.get('spectra_threshold', 4.0))

        if hasattr(self, 'slider_spectra_aoa_threshold'):
            self.slider_spectra_aoa_threshold.set(s.get('spectra_aoa_threshold', 0.0))

        # FIX: Explicitly update the text entry fields
        if hasattr(self, 'entry_spectra_key'):
            self.entry_spectra_key.delete(0, 'end')
            self.entry_spectra_key.insert(0, s.get('spectra_api_key', ""))

        if hasattr(self, 'entry_spectra_url'):
            self.entry_spectra_url.delete(0, 'end')
            self.entry_spectra_url.insert(0, s.get('spectra_base_url', "https://api.openai.com/v1"))

        if hasattr(self, 'entry_spectra_model'):
            self.entry_spectra_model.delete(0, 'end')
            self.entry_spectra_model.insert(0, s.get('spectra_model', "gpt-4o-mini"))

        if hasattr(self, 'var_spectra_lang'):
            self.var_spectra_lang.set(s.get('spectra_target_lang', "English"))

        # --- Final UI Refresh ---
        self.refresh_all_slider_labels()

        # Handle Font Change
        if s["font_name"] in self.font_options:
            self.font_dropdown.set(s["font_name"])
            self.processor.font_path = self.font_map[s["font_name"]]
        elif self.font_options:
            self.font_dropdown.set(self.font_options[0])
            self.processor.font_path = self.font_map[self.font_options[0]]

        self.toggle_render_controls()
        self.schedule_update()

    def on_font_change(self, choice):
        self.processor.font_path = self.font_map[choice]
        self.schedule_update()


if __name__ == "__main__":
    app = ModernApp()
    app.mainloop()