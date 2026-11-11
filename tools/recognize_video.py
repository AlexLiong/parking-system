"""P3-06 CLI：视频/RTSP 抽帧 + 多帧融合识别（离线工具，不启服务/不写业务库）。

示例：
  python tools/recognize_video.py sample.avi --frames 3 --interval 0.5 --gate in
  python tools/recognize_video.py rtsp://user:pass@192.168.1.10/stream --frames 3
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.core.config import load_config  # noqa: E402
from server.lpr.hyperlpr_adapter import (DemoAwareRecognizer,  # noqa: E402
                                         HyperPlateRecognizer)
from server.lpr.recognizer import PlateRecognizer
from server.lpr.service import LPRService
from server.lpr.video import (DEFAULT_TIMEOUT_SEC, MAX_VIDEO_BYTES,  # noqa: E402
                              VideoError, sample_video)


def build_recognizer(cfg):
    if cfg.lpr.backend == "hyperlpr":
        return HyperPlateRecognizer(cfg.lpr)
    if cfg.lpr.backend == "auto":
        return DemoAwareRecognizer(cfg.lpr)
    return PlateRecognizer(cfg.lpr)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="视频/RTSP 多帧车牌识别（P3-06）")
    parser.add_argument("source", help="本地视频文件路径或 rtsp:// / rtsps:// URL")
    parser.add_argument("--frames", type=int, default=3, help="抽帧数 2-5（默认 3）")
    parser.add_argument("--interval", type=float, default=0.5,
                        help="抽帧间隔秒（默认 0.5）")
    parser.add_argument("--gate", choices=("in", "out"), default="in")
    parser.add_argument("--timeout-sec", type=float, default=DEFAULT_TIMEOUT_SEC,
                        help=f"视频源/抽帧超时秒（默认 {DEFAULT_TIMEOUT_SEC}）")
    parser.add_argument("--max-file-mb", type=int, default=MAX_VIDEO_BYTES // (1024 * 1024),
                        help="本地视频大小上限 MiB（默认 50）")
    parser.add_argument("--out-dir", default=None,
                        help="抽帧保存目录（默认临时目录）")
    parser.add_argument("--output", "-o", default=None, help="结果 JSON 输出路径")
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir) if args.out_dir else Path(
        tempfile.mkdtemp(prefix="recognize_video_"))
    try:
        sample = sample_video(
            args.source, out_dir=out_dir, frames=args.frames,
            interval=args.interval, timeout_sec=args.timeout_sec,
            max_file_bytes=max(1, args.max_file_mb) * 1024 * 1024)
    except (VideoError, ValueError) as exc:
        print(f"视频源错误: {exc}", file=sys.stderr)
        return 2

    cfg = load_config(force=True)
    service = LPRService(build_recognizer(cfg), cfg.lpr)
    crop_dir = out_dir / "crops"
    crop_dir.mkdir(parents=True, exist_ok=True)
    frame_results = []
    try:
        fused = service.recognize_frames(sample.frames, gate=args.gate,
                                         crop_dir=crop_dir,
                                         frame_results=frame_results)
    except Exception as exc:  # noqa: BLE001 - CLI 明确报错退出
        print(f"多帧识别失败: {exc}", file=sys.stderr)
        return 3

    result = {
        "video": sample.to_dict(),
        "frames": [
            {"index": index, "image_path": path,
             **({**r.to_log_dict(), "need_manual": r.need_manual,
                 "message": r.message, "source_status": r.source_status}
                if r is not None else {})}
            for index, (path, r) in enumerate(zip(sample.frames, frame_results))
        ],
        "fused": {
            **fused.to_log_dict(), "need_manual": fused.need_manual,
            "candidates": fused.candidates, "message": fused.message,
        },
    }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    return 0 if fused.plate else 1


if __name__ == "__main__":
    raise SystemExit(main())
