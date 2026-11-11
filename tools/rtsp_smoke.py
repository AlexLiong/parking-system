"""P3-06 真实 RTSP 联调 harness（需要外部 ffmpeg + mediamtx，均为便携 exe）。

做四件事并把结果写 JSON：
1. 启动 mediamtx（本地 RTSP server）；
2. 用 ffmpeg 把本地视频循环推流为 RTSP URL；
3. 用真实 `/api/recognize/video`（FastAPI TestClient + 真实 HyperLPR3）识别该 RTSP；
4. 用 `tools/recognize_video.py` CLI 再识别一次。

本机没有 ffmpeg/RTSP server 时脚本会明确报错并退出 2；不伪造实测结果。
可复跑示例（在项目根）：
  .\.venv\Scripts\python.exe tools\rtsp_smoke.py `
      --video data/photos/uploads/smoke_video.avi `
      --ffmpeg E:\path\ffmpeg.exe --mediamtx E:\path\mediamtx.exe `
      --output _qa_audit\repair_tasks_20261023\B-C-backend\rtsp_real.json
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.core.config import load_config  # noqa: E402


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_port(host: str, port: int, deadline: float) -> bool:
    while time.monotonic() < deadline:
        if _port_open(host, port):
            return True
        time.sleep(0.2)
    return False


def _tail(path: Path, n: int = 40) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return ""


def _kill(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, timeout=15)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="真实 RTSP 多帧识别联调")
    parser.add_argument("--video", required=True, help="要推流的本地视频文件")
    parser.add_argument("--ffmpeg", default=None, help="ffmpeg.exe 路径（默认 PATH）")
    parser.add_argument("--mediamtx", default=None, help="mediamtx.exe 路径（默认 PATH）")
    parser.add_argument("--port", type=int, default=18554, help="RTSP 端口")
    parser.add_argument("--path", default="live", help="RTSP path")
    parser.add_argument("--frames", type=int, default=3)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--timeout-sec", type=float, default=30.0)
    parser.add_argument("--codec", default="libx264",
                        help="ffmpeg 视频编码器（无 libx264 时可用 mpeg4）")
    parser.add_argument("--output", default=None, help="结果 JSON 路径")
    parser.add_argument("--work-dir", default=None, help="日志/临时目录（保留）")
    args = parser.parse_args(argv)

    ffmpeg = args.ffmpeg or shutil.which("ffmpeg")
    mediamtx = args.mediamtx or shutil.which("mediamtx")
    video = Path(args.video)
    missing = []
    if not ffmpeg:
        missing.append("ffmpeg")
    if not mediamtx:
        missing.append("mediamtx")
    if not video.exists():
        missing.append(f"video:{video}")
    if missing:
        print(f"缺少外部依赖/输入：{', '.join(missing)}；"
              "请提供 --ffmpeg / --mediamtx 或安装后在 PATH 中。", file=sys.stderr)
        return 2

    work = Path(args.work_dir) if args.work_dir else Path(
        tempfile.mkdtemp(prefix="rtsp_smoke_"))
    work.mkdir(parents=True, exist_ok=True)
    mtx_log = work / "mediamtx.log"
    ffmpeg_log = work / "ffmpeg.log"
    url = f"rtsp://127.0.0.1:{args.port}/{args.path}"
    result: dict = {"rtsp_url": url, "video": str(video), "work_dir": str(work),
                    "ffmpeg": str(ffmpeg), "mediamtx": str(mediamtx)}
    mtx_proc = None
    ff_proc = None
    t0 = time.monotonic()
    try:
        with open(mtx_log, "w", encoding="utf-8") as ml:
            mtx_proc = subprocess.Popen([str(mediamtx)], cwd=str(Path(mediamtx).parent),
                                        stdout=ml, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + args.timeout_sec
        if not _wait_port("127.0.0.1", args.port, deadline):
            result["error"] = "mediamtx 未在超时内监听端口"
            result["mediamtx_log_tail"] = _tail(mtx_log)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 3
        result["mediamtx_ready_ms"] = int((time.monotonic() - t0) * 1000)

        ff_cmd = [str(ffmpeg), "-hide_banner", "-loglevel", "warning", "-re",
                  "-stream_loop", "-1", "-i", str(video),
                  "-c:v", args.codec, "-preset", "ultrafast", "-tune", "zerolatency",
                  "-pix_fmt", "yuv420p", "-f", "rtsp", "-rtsp_transport", "tcp", url]
        with open(ffmpeg_log, "w", encoding="utf-8") as fl:
            ff_proc = subprocess.Popen(ff_cmd, cwd=str(ROOT), stdout=fl,
                                       stderr=subprocess.STDOUT)
        # 等推流可被 OpenCV 打开（RTSP 首帧可能需要几秒）
        from server.lpr.video import sample_video
        sample = None
        attempts = []
        sample_deadline = time.monotonic() + args.timeout_sec
        while time.monotonic() < sample_deadline:
            if ff_proc.poll() is not None:
                result["error"] = f"ffmpeg 提前退出 code={ff_proc.returncode}"
                result["ffmpeg_log_tail"] = _tail(ffmpeg_log)
                break
            started = time.monotonic()
            try:
                sample = sample_video(url, out_dir=work / "frames",
                                      frames=args.frames, interval=args.interval,
                                      timeout_sec=max(3.0, args.timeout_sec / 2))
                attempts.append({"ok": True,
                                 "elapsed_ms": int((time.monotonic() - started) * 1000)})
                break
            except Exception as exc:  # noqa: BLE001 - 首帧未就绪时重试
                attempts.append({"ok": False, "error": str(exc),
                                 "elapsed_ms": int((time.monotonic() - started) * 1000)})
                time.sleep(0.5)
        result["sample_attempts"] = attempts
        if sample is None:
            result.setdefault("error", "RTSP 抽帧失败（超时/推流未就绪）")
            result["ffmpeg_log_tail"] = _tail(ffmpeg_log)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 4
        result["sample"] = sample.to_dict()
        result["sample_ready_ms"] = int((time.monotonic() - t0) * 1000)

        # 3) 真实 /api/recognize/video
        cfg = copy.deepcopy(load_config(force=True))
        data = work / "api-data"
        cfg.paths.data_dir = data
        cfg.paths.db_file = data / "smoke.db"
        cfg.paths.photos_dir = data / "photos"
        cfg.paths.reports_dir = data / "reports"
        cfg.paths.logs_dir = data / "logs"
        cfg.ensure_dirs()
        from fastapi.testclient import TestClient
        from server.main import create_app
        with TestClient(create_app(cfg)) as client:
            login = client.post("/api/auth/login",
                                json={"username": "admin", "password": "admin123"})
            headers = {"Authorization": f"Bearer {login.json()['token']}"}
            started = time.monotonic()
            resp = client.post("/api/recognize/video",
                               json={"rtsp_url": url, "frames": args.frames,
                                     "interval": args.interval,
                                     "timeout_sec": args.timeout_sec},
                               headers=headers)
            result["api"] = {
                "status": resp.status_code,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "body": resp.json() if resp.headers.get("content-type", "").startswith(
                    "application/json") else resp.text[:2000],
            }
            if resp.status_code == 200:
                body = resp.json()
                log_id = body.get("recognition_log_id")
                result["api"]["recognition_log_row"] = (
                    client.app.state.container.recog_repo.get(log_id)
                    if log_id else None)
                result["api"]["open_records"] = (
                    client.app.state.container.parking_repo.count_open())

        # 4) CLI 真识别
        cli_out = work / "cli.json"
        cli_cmd = [sys.executable, str(ROOT / "tools" / "recognize_video.py"), url,
                   "--frames", str(args.frames), "--interval", str(args.interval),
                   "--timeout-sec", str(args.timeout_sec), "--output", str(cli_out)]
        started = time.monotonic()
        cli = subprocess.run(cli_cmd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace",
                             timeout=max(60, int(args.timeout_sec) + 60),
                             env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        result["cli"] = {"returncode": cli.returncode,
                         "elapsed_ms": int((time.monotonic() - started) * 1000),
                         "stderr_tail": (cli.stderr or "")[-2000:]}
        try:
            result["cli"]["result"] = json.loads(cli_out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            result["cli"]["stdout_tail"] = (cli.stdout or "")[-2000:]
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False,
                                                    indent=2), encoding="utf-8")
        return 0
    finally:
        _kill(ff_proc)
        _kill(mtx_proc)
        result["cleanup_done"] = True


if __name__ == "__main__":
    raise SystemExit(main())
