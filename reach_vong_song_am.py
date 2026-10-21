"""Render a circular audio-reactive ring (podcast/radio style) on a transparent background.

Usage:
    python reach-vong-song-am.py giong-noi.wav vong-song.mov
    python reach-vong-song-am.py giong-noi.mp3 vong-song.webm --size 720 --fps 30
    python reach-vong-song-am.py giong-noi.mp3 vong-song.mov --style bars   (wave | bars | dots | ripple)

Mặc định: kiểu ripple, màu cam-vàng của web (#FFC531 -> #F26A1B).

Output has NO audio (overlay only) so the editor does not double the voice.
Needs ffmpeg: on PATH, or `pip install imageio-ffmpeg`, or pass --ffmpeg <path>.
Needs: pip install numpy opencv-python
"""
import argparse
import math
import shutil
import subprocess
import sys

import cv2
import numpy as np

SAMPLE_RATE = 44100
FFT_SIZE = 2048
FREQ_MIN_HZ = 80
FREQ_MAX_HZ = 6000
BANDS_PER_SIDE = 48
GATE = 0.04
ATTACK = 0.6
RELEASE = 0.18
SUBPROCESS_TIMEOUT_S = 3600


def hex_to_bgr(value):
    h = value.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (4, 2, 0))


def find_ffmpeg(explicit):
    if explicit:
        return explicit
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        sys.exit("Không tìm thấy ffmpeg. Cài bằng: pip install imageio-ffmpeg (hoặc dùng --ffmpeg).")


def decode_audio(ffmpeg, path):
    cmd = [ffmpeg, "-v", "error", "-i", path, "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"]
    result = subprocess.run(cmd, capture_output=True, timeout=SUBPROCESS_TIMEOUT_S)
    if result.returncode != 0:
        sys.exit(f"Không đọc được audio: {result.stderr.decode(errors='replace')}")
    samples = np.frombuffer(result.stdout, dtype=np.float32)
    if samples.size == 0:
        sys.exit("File audio rỗng.")
    return samples


def band_edges():
    return np.geomspace(FREQ_MIN_HZ, FREQ_MAX_HZ, BANDS_PER_SIDE + 1)


def compute_levels(samples, fps):
    """Return (frames, bands) levels in 0..1 and per-frame loudness in 0..1."""
    n_frames = int(len(samples) / SAMPLE_RATE * fps)
    window = np.hanning(FFT_SIZE)
    freqs = np.fft.rfftfreq(FFT_SIZE, 1 / SAMPLE_RATE)
    edges = band_edges()
    masks = [(freqs >= lo) & (freqs < hi) for lo, hi in zip(edges[:-1], edges[1:])]
    padded = np.pad(samples, (FFT_SIZE // 2, FFT_SIZE))
    spectra = np.zeros((n_frames, BANDS_PER_SIDE), dtype=np.float32)
    loud = np.zeros(n_frames, dtype=np.float32)
    for f in range(n_frames):
        start = int(f / fps * SAMPLE_RATE)
        chunk = padded[start:start + FFT_SIZE]
        mag = np.abs(np.fft.rfft(chunk * window))
        for b, mask in enumerate(masks):
            spectra[f, b] = mag[mask].mean() if mask.any() else 0.0
        loud[f] = float(np.sqrt(np.mean(chunk ** 2)))
    ref = np.percentile(spectra, 98) or 1.0
    spectra = np.clip(np.sqrt(spectra / ref), 0, 1)  # sqrt lifts quiet bands
    loud_ref = np.percentile(loud, 98) or 1.0
    loud = np.clip(loud / loud_ref, 0, 1)
    return spectra, loud


def smooth(levels):
    out = np.zeros_like(levels)
    cur = np.zeros(levels.shape[1], dtype=np.float32)
    for f in range(len(levels)):
        target = np.where(levels[f] < GATE, 0.0, levels[f])
        rate = np.where(target > cur, ATTACK, RELEASE)
        cur = cur + (target - cur) * rate
        out[f] = cur
    return out


def build_color_map(size, color_a, color_b):
    """Static BGR image: gradient from color_a (top) to color_b (bottom), mirrored left/right."""
    ys, xs = np.mgrid[0:size, 0:size].astype(np.float32)
    angle = np.arctan2(xs - size / 2, -(ys - size / 2))  # 0 at top, +-pi at bottom
    t = (np.abs(angle) / math.pi)[..., None]
    a = np.array(color_a, dtype=np.float32)
    b = np.array(color_b, dtype=np.float32)
    return a * (1 - t) + b * t


def ring_radii(bands, points):
    """Smooth closed curve of per-angle values (0..1) from mirrored band levels."""
    sides = np.asarray(bands, dtype=np.float32)
    full = np.concatenate([sides, sides[::-1], sides, sides[::-1]])  # bass at top and bottom, treble at the sides
    x = (np.arange(points) + 0.5) / points * len(full)
    ext = np.concatenate([full[-4:], full, full[:4]])
    vals = np.interp(x + 4, np.arange(len(ext)) + 0.5, ext)
    width = max(3, int(points / 21.6) | 1)  # keep smoothing proportional to point count
    k = np.hanning(width + 2)[1:-1].astype(np.float32)
    k /= k.sum()
    pad = len(k) // 2
    wrapped = np.concatenate([vals[-pad:], vals, vals[:pad]])
    return np.convolve(wrapped, k, mode="valid")


def curve_points(center, base_r, amp, values, phase=0.0):
    points = len(values)
    ang = -math.pi / 2 + (np.arange(points) + phase) / points * 2 * math.pi
    r = base_r + amp * values
    pts = np.stack([center + np.cos(ang) * r, center + np.sin(ang) * r], axis=1)
    return pts


SS = 2  # supersample factor for clean edges
STYLES = ("wave", "bars", "dots", "ripple")


def style_wave(size, bands, loudness, t):
    """Smooth layered closed curve. Returns (mask, fill) at supersampled size."""
    big = size * SS
    center = big / 2
    base_r = big * 0.25 * (1 + 0.025 * loudness)
    amp = big * 0.15
    values = ring_radii(bands, 540)
    mask = np.zeros((big, big), dtype=np.uint8)
    fill = np.zeros((big, big), dtype=np.uint8)
    outer = curve_points(center, base_r, amp, values)
    inner = curve_points(center, base_r, 0, np.zeros_like(values))
    cv2.fillPoly(fill, [np.concatenate([outer, inner[::-1]]).astype(np.int32)], 255, cv2.LINE_AA)
    for scale, thick, alpha in [(1.0, 5 * SS, 255), (0.62, 3 * SS, 150), (0.32, 2 * SS, 90)]:
        pts = curve_points(center, base_r, amp * scale, values).astype(np.int32)
        layer = np.zeros_like(mask)
        cv2.polylines(layer, [pts], True, alpha, thick, cv2.LINE_AA)
        mask = np.maximum(mask, layer)
    cv2.circle(mask, (int(center), int(center)), int(base_r), 200, 2 * SS, cv2.LINE_AA)
    return mask, fill


def style_bars(size, bands, loudness, t):
    """Rounded radial bars, thicker and fewer than the old version."""
    big = size * SS
    center = big / 2
    base_r = big * 0.24 * (1 + 0.02 * loudness)
    values = ring_radii(bands, 72)
    thick = int(big * 0.016)
    mask = np.zeros((big, big), dtype=np.uint8)
    for i, v in enumerate(values):
        ang = -math.pi / 2 + (i + 0.5) / len(values) * 2 * math.pi
        length = big * 0.012 + v * big * 0.15
        c, s_ = math.cos(ang), math.sin(ang)
        p0 = (int(center + c * base_r), int(center + s_ * base_r))
        p1 = (int(center + c * (base_r + length)), int(center + s_ * (base_r + length)))
        cv2.line(mask, p0, p1, 255, thick, cv2.LINE_AA)
        cv2.circle(mask, p0, thick // 2, 255, -1, cv2.LINE_AA)
        cv2.circle(mask, p1, thick // 2, 255, -1, cv2.LINE_AA)
    return mask, None


def style_dots(size, bands, loudness, t):
    """Rings of dots whose size and distance follow the voice."""
    big = size * SS
    center = big / 2
    mask = np.zeros((big, big), dtype=np.uint8)
    values = ring_radii(bands, 90)
    for ring, (radius, gain) in enumerate([(0.22, 1.0), (0.29, 0.7), (0.36, 0.45)]):
        count = 90 - ring * 18
        step = len(values) / count
        for i in range(count):
            v = float(values[int(i * step) % len(values)])
            ang = -math.pi / 2 + i / count * 2 * math.pi
            r = big * (radius + 0.025 * v * gain)
            dot = big * (0.0045 + 0.011 * v * gain)
            cv2.circle(mask, (int(center + math.cos(ang) * r), int(center + math.sin(ang) * r)),
                       max(1, int(dot)), 255 - ring * 55, -1, cv2.LINE_AA)
    return mask, None


def style_ripple(size, bands, loudness, t):
    """Concentric rings that expand outward and brighten with loudness."""
    big = size * SS
    center = (int(big / 2), int(big / 2))
    mask = np.zeros((big, big), dtype=np.uint8)
    core = big * (0.14 + 0.04 * loudness)
    cv2.circle(mask, center, int(core), 255, 3 * SS, cv2.LINE_AA)
    for k in range(4):
        phase = (t * 0.5 + k / 4) % 1.0
        radius = big * (0.16 + 0.30 * phase)
        strength = (1 - phase) ** 1.5 * (0.25 + 0.75 * loudness)
        cv2.circle(mask, center, int(radius), int(255 * strength), max(1, int((2 + 3 * (1 - phase)) * SS)), cv2.LINE_AA)
    return mask, None


STYLE_FUNCS = {"wave": style_wave, "bars": style_bars, "dots": style_dots, "ripple": style_ripple}


def draw_frame(size, bands, loudness, color_map, style="wave", t=0.0):
    mask, fill = STYLE_FUNCS[style](size, bands, loudness, t)
    mask = cv2.resize(mask, (size, size), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
    glow = cv2.GaussianBlur(mask, (0, 0), size * 0.014) * 1.6
    alpha = np.maximum(mask, glow * 0.75)
    if fill is not None:
        fill = cv2.resize(fill, (size, size), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
        alpha = np.maximum(alpha, fill * 0.16)
    out = np.dstack([np.clip(color_map, 0, 255), np.clip(alpha, 0, 1) * 255])
    return out.astype(np.uint8)


def encoder_args(fmt):
    if fmt == "webm":
        return ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-auto-alt-ref", "0", "-b:v", "4M"]
    return ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le"]


def render(args):
    ffmpeg = find_ffmpeg(args.ffmpeg)
    samples = decode_audio(ffmpeg, args.audio)
    levels, loud = compute_levels(samples, args.fps)
    levels = smooth(levels)
    print(f"{len(levels)} khung hình, {len(samples) / SAMPLE_RATE:.1f}s audio")
    fmt = "webm" if args.output.lower().endswith(".webm") else "mov"
    cmd = [ffmpeg, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgra",
           "-s", f"{args.size}x{args.size}", "-r", str(args.fps), "-i", "-",
           *encoder_args(fmt), args.output]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    color_map = build_color_map(args.size, hex_to_bgr(args.color), hex_to_bgr(args.color2))
    try:
        for f in range(len(levels)):
            rgba = draw_frame(args.size, levels[f], float(loud[f]), color_map, args.style, f / args.fps)
            proc.stdin.write(rgba.tobytes())
        proc.stdin.close()
        if proc.wait(timeout=SUBPROCESS_TIMEOUT_S) != 0:
            sys.exit("ffmpeg gặp lỗi khi ghi video.")
    except BrokenPipeError:
        sys.exit("ffmpeg dừng giữa chừng (xem thông báo lỗi ở trên).")
    print("Xong:", args.output)


def main():
    parser = argparse.ArgumentParser(description="Vòng sóng âm trên nền trong suốt")
    parser.add_argument("audio")
    parser.add_argument("output", help=".mov (ProRes 4444) hoặc .webm (VP9 alpha)")
    parser.add_argument("--size", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--color", default="#FFC531", help="màu hex phía trên vòng")
    parser.add_argument("--color2", default="#F26A1B", help="màu hex phía dưới vòng")
    parser.add_argument("--style", choices=STYLES, default="ripple", help="kiểu vòng: wave, bars, dots, ripple")
    parser.add_argument("--ffmpeg", default=None)
    render(parser.parse_args())


if __name__ == "__main__":
    main()
