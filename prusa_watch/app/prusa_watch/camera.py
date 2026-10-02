"""Frame capture via ffmpeg (RTSP stream or HTTP snapshot URL)."""
import subprocess

THUMB_W, THUMB_H = 64, 36


class CameraError(RuntimeError):
    pass


def _ffmpeg(args: list[str], stdin: bytes | None = None, timeout: float = 30) -> bytes:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", *args]
    try:
        res = subprocess.run(cmd, input=stdin, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise CameraError("ffmpeg timed out") from e
    except FileNotFoundError as e:
        raise CameraError("ffmpeg is not installed") from e
    if res.returncode != 0 or not res.stdout:
        raise CameraError(res.stderr.decode(errors="replace").strip() or f"ffmpeg exit {res.returncode}")
    return res.stdout


ROTATE_FILTERS = {0: "", 90: ",transpose=1", 180: ",hflip,vflip", 270: ",transpose=2"}


def grab_jpeg(url: str, width: int = 960, rotate: int = 0, timeout: float = 30) -> bytes:
    """Return one JPEG frame, downscaled to `width` px (keeps vision tokens and cost low)
    and rotated clockwise by `rotate` degrees."""
    pre = ["-rtsp_transport", "tcp"] if url.startswith("rtsp") else []
    vf = f"scale='min({width},iw)':-2{ROTATE_FILTERS.get(rotate, '')}"
    return _ffmpeg(
        [*pre, "-i", url, "-frames:v", "1", "-vf", vf,
         "-q:v", "4", "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
        timeout=timeout,
    )


def thumbnail(jpeg: bytes) -> bytes:
    """Tiny grayscale raw thumbnail used for a cheap frame-difference score."""
    return _ffmpeg(
        ["-f", "image2pipe", "-i", "pipe:0", "-vf", f"scale={THUMB_W}:{THUMB_H},format=gray",
         "-f", "rawvideo", "pipe:1"],
        stdin=jpeg, timeout=15,
    )


def change_score(a: bytes, b: bytes) -> float:
    """Mean absolute pixel difference between two thumbnails, 0.0 (identical) .. 1.0."""
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    return sum(abs(x - y) for x, y in zip(a[:n], b[:n])) / (255.0 * n)
