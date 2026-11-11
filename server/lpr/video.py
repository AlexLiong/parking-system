"""P3-06：视频/RTSP 多帧取样（OpenCV VideoCapture，不引入新依赖）。

支持：
- 本地视频文件（mp4/avi/mov/mkv/m4v/mpg/mpeg/webm）；
- RTSP URL（rtsp:// / rtsps://，依赖 OpenCV 的 FFmpeg 后端与可达的 RTSP 服务）。

只会把 2–5 帧抽到目录并返回路径，识别与业务决策由调用方（API / tools）完成；
不在此模块内做入场/放行或识别日志。
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import cv2

log = logging.getLogger("lpr.video")

MIN_FRAMES = 2
MAX_FRAMES = 5
MAX_VIDEO_BYTES = 50 * 1024 * 1024          # 本地视频文件上限（RTSP 不适用）
DEFAULT_TIMEOUT_SEC = 15.0
SUPPORTED_SUFFIXES = {".mp4", ".avi", ".mov", ".mkv", ".m4v", ".mpg", ".mpeg", ".webm"}


class VideoError(Exception):
    """视频源/解码错误（API 层转为明确 4xx/5xx）。"""


class VideoSourceError(VideoError):
    """无法打开视频源（文件不存在/格式不支持/RTSP 不可达）。"""


class VideoDecodeError(VideoError):
    """能打开但抽帧失败/帧数不足。"""


@dataclass
class VideoSample:
    frames: list[str]
    source: str
    kind: str                               # "file" | "rtsp"
    fps: float = 0.0
    width: int = 0
    height: int = 0
    duration_sec: Optional[float] = None
    frame_indices: list[int] = field(default_factory=list)
    elapsed_ms: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "frames": list(self.frames),
            "source": self.source,
            "kind": self.kind,
            "fps": round(float(self.fps), 3),
            "width": int(self.width),
            "height": int(self.height),
            "duration_sec": (round(float(self.duration_sec), 3)
                             if self.duration_sec is not None else None),
            "frame_indices": list(self.frame_indices),
            "elapsed_ms": int(self.elapsed_ms),
            "warnings": list(self.warnings),
        }


def is_rtsp_source(source: str) -> bool:
    value = (source or "").strip().lower()
    return value.startswith(("rtsp://", "rtsps://"))


def _validate_params(frames: int, interval: float, timeout_sec: float) -> None:
    if not MIN_FRAMES <= int(frames) <= MAX_FRAMES:
        raise ValueError(f"frames 必须在 {MIN_FRAMES}~{MAX_FRAMES} 之间")
    if not math.isfinite(float(interval)) or float(interval) <= 0 or float(interval) > 10:
        raise ValueError("interval 必须在 (0, 10] 秒之间")
    if not math.isfinite(float(timeout_sec)) or not 0.2 <= float(timeout_sec) <= 120:
        raise ValueError("timeout_sec 必须在 0.2~120 秒之间")


def _open_capture(source: str, *, kind: str, timeout_sec: float) -> cv2.VideoCapture:
    timeout_ms = max(200, int(float(timeout_sec) * 1000))
    if kind == "rtsp":
        # OpenCV 5 支持在构造时传 open/read 超时参数（FFmpeg 后端生效）；
        # 若后端不支持再回退到 open 后 set，避免 RTSP 不可达时长时间阻塞。
        try:
            params = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms,
                      cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms]
            cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG, params)
        except (AttributeError, TypeError, cv2.error):
            cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG)
            for prop, value in ((getattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC", None), timeout_ms),
                                (getattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC", None), timeout_ms)):
                if prop is not None:
                    try:
                        cap.set(prop, value)
                    except Exception:  # noqa: BLE001 - 后端不支持时忽略
                        pass
    else:
        cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        cap.release()
        if kind == "rtsp":
            raise VideoSourceError(
                "无法打开 RTSP 视频源（请检查 URL/网络，且 OpenCV 需带 FFmpeg 后端）")
        raise VideoSourceError(f"无法打开视频文件或格式不支持: {source}")
    return cap


def _save_frame(out_dir: Path, index: int, frame) -> str:
    path = out_dir / f"frame_{index:02d}.png"
    if not cv2.imwrite(str(path), frame):
        raise VideoDecodeError(f"视频帧写入失败: {path}")
    return str(path)


def _sample_file(cap: cv2.VideoCapture, out_dir: Path, frames: int,
                 interval: float, sample: VideoSample) -> None:
    """按时间位置 seek 抽帧；短视频/末尾 seek 失败时顺序补齐。"""
    fps = sample.fps or 10.0
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    saved = 0
    for index in range(frames):
        target_sec = index * float(interval)
        cap.set(cv2.CAP_PROP_POS_MSEC, target_sec * 1000.0)
        ok, frame = cap.read()
        if not ok and frame_count > 0:
            # 末尾 seek 失败：顺序读完剩余帧，取最后一帧作为本位置样本
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, target_sec * 1000.0 - 500.0))
            ok, frame = cap.read()
        if not ok:
            continue
        saved += 1
        sample.frames.append(_save_frame(out_dir, saved - 1, frame))
        sample.frame_indices.append(min(frame_count - 1, int(round(target_sec * fps)))
                                    if frame_count > 0 else int(round(target_sec * fps)))
        if saved >= frames:
            break
    if saved < MIN_FRAMES:
        raise VideoDecodeError(f"视频可抽取帧不足（需要至少 {MIN_FRAMES} 帧，实际 {saved}）")
    if saved < frames:
        sample.warnings.append(f"仅抽取到 {saved}/{frames} 帧（视频可能过短）")


def _sample_rtsp(cap: cv2.VideoCapture, out_dir: Path, frames: int,
                 interval: float, timeout_sec: float, sample: VideoSample) -> None:
    """实时流按 interval 间隔抓帧；READ_TIMEOUT 由 FFmpeg 后端控制。"""
    deadline = time.monotonic() + float(timeout_sec)
    ok, frame = cap.read()
    if not ok or frame is None:
        raise VideoDecodeError("RTSP 已连接但读取首帧失败（码流不可用/超时）")
    sample.frames.append(_save_frame(out_dir, 0, frame))
    sample.frame_indices.append(0)
    for index in range(1, frames):
        target = time.monotonic() + float(interval)
        latest = frame
        while time.monotonic() < target:
            if time.monotonic() > deadline:
                break
            ok, candidate = cap.read()
            if not ok or candidate is None:
                break
            latest = candidate
        if time.monotonic() > deadline:
            sample.warnings.append("RTSP 抽帧达到 timeout_sec，提前结束")
            break
        sample.frames.append(_save_frame(out_dir, index, latest))
        sample.frame_indices.append(index)
    if len(sample.frames) < MIN_FRAMES:
        raise VideoDecodeError(
            f"RTSP 可抽取帧不足（需要至少 {MIN_FRAMES} 帧，实际 {len(sample.frames)}）")


def sample_video(source: str, *, out_dir: str | Path, frames: int = 3,
                 interval: float = 0.5, timeout_sec: float = DEFAULT_TIMEOUT_SEC,
                 max_file_bytes: int = MAX_VIDEO_BYTES) -> VideoSample:
    """打开本地视频/RTSP，按 interval 抽取 2–5 帧到 out_dir。"""
    raw = (source or "").strip()
    if not raw:
        raise VideoSourceError("视频源为空")
    _validate_params(frames, interval, timeout_sec)
    frames = int(frames)
    kind = "rtsp" if is_rtsp_source(raw) else "file"
    if kind == "file":
        path = Path(raw)
        if not path.is_absolute():
            path = Path.cwd() / path
        path = path.resolve()
        if not path.exists():
            raise VideoSourceError(f"视频文件不存在: {raw}")
        if not path.is_file():
            raise VideoSourceError(f"视频源不是文件: {raw}")
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise VideoSourceError(
                f"不支持的视频格式 {path.suffix or '(无后缀)'}；支持 "
                + "/".join(sorted(SUPPORTED_SUFFIXES)))
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise VideoSourceError(f"无法读取视频文件信息: {raw}") from exc
        if size > max_file_bytes:
            raise VideoSourceError(
                f"视频文件超过上限（{size}/{max_file_bytes} 字节，约 "
                f"{max_file_bytes // (1024 * 1024)} MiB）")
        if size <= 0:
            raise VideoSourceError("视频文件为空")
        source_value = str(path)
    else:
        source_value = raw

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    sample = VideoSample(frames=[], source=source_value, kind=kind)
    cap = _open_capture(source_value, kind=kind, timeout_sec=float(timeout_sec))
    try:
        sample.fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        if not math.isfinite(sample.fps) or sample.fps <= 0:
            sample.fps = 10.0
            sample.warnings.append("无法读取视频 FPS，取样按 10 FPS 估算")
        sample.width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        sample.height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        frame_count = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if kind == "file" and frame_count > 0 and sample.fps > 0:
            sample.duration_sec = round(frame_count / sample.fps, 3)
        if kind == "file":
            _sample_file(cap, out, frames, interval, sample)
        else:
            _sample_rtsp(cap, out, frames, interval, float(timeout_sec), sample)
    finally:
        cap.release()
        sample.elapsed_ms = int((time.monotonic() - started) * 1000)
    return sample
