"""CI/基准共用辅助（P1-13）。

只放 CI 与 benchmark 的进程/配置工具，不改变产品运行路径：
- resolve_node：跨平台定位 node（PATH / PARKING_NODE_BIN / Windows 常见安装位置）；
- bench_limit_for：基准阈值显式配置（默认 2.0s；平台/CLI/环境变量显式覆盖，禁止静默放宽）；
- run_with_timeout：带超时与进程树清理的子进程执行；
- browser_deps：浏览器 E2E 依赖预检（playwright 包 + chromium 可执行文件）。
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
DEFAULT_LIMITS_FILE = TOOLS / "bench_limits.json"
DEFAULT_BENCH_SECONDS = 2.0

_WIN_NODE_CANDIDATES = (
    r"D:\nodejs\node.exe",
    r"C:\Program Files\nodejs\node.exe",
    r"C:\Program Files (x86)\nodejs\node.exe",
)


def resolve_node(explicit: str | None = None) -> str | None:
    """返回 node 可执行文件绝对路径；找不到返回 None。"""
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    env_node = os.environ.get("PARKING_NODE_BIN") or os.environ.get("NODE_BIN")
    if env_node:
        candidates.append(env_node)
    for name in ("node", "node.exe"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    if os.name == "nt":
        candidates.extend(_WIN_NODE_CANDIDATES)
    for cand in candidates:
        p = Path(cand)
        if p.is_file():
            return str(p)
    return None


def load_bench_config(path: str | Path | None = None) -> dict:
    p = Path(path) if path else DEFAULT_LIMITS_FILE
    if not p.is_file():
        return {"default_seconds": DEFAULT_BENCH_SECONDS, "platforms": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def bench_limit_for(platform_name: str | None = None,
                    override: float | None = None,
                    config: dict | None = None) -> tuple[float, str]:
    """返回 (阈值秒, 来源)。优先级：CLI override > env > 平台配置 > default。

    所有覆盖都必须显式出现（命令行/环境变量/配置文件），不会静默改变门禁。
    """
    if override is not None:
        return float(override), "cli:--limit-seconds"
    env_value = os.environ.get("PARKING_BENCH_MAX_SECONDS")
    if env_value:
        return float(env_value), "env:PARKING_BENCH_MAX_SECONDS"
    cfg = config if config is not None else load_bench_config()
    platforms = cfg.get("platforms") or {}
    platform_name = platform_name or sys.platform
    if platform_name in platforms:
        return float(platforms[platform_name]), f"config:platforms.{platform_name}"
    return float(cfg.get("default_seconds", DEFAULT_BENCH_SECONDS)), "config:default"


def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=False)
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def run_with_timeout(args: list[str], *, cwd: str | Path,
                     env: dict | None = None, timeout: float | None = None,
                     stdout=None, stderr=None) -> tuple[int, bool]:
    """运行子进程；超时则杀进程树。返回 (returncode, timed_out)。

    rc=124 表示超时（GNU timeout 约定）。stdout/stderr 默认继承父进程，
    便于 `tools/ci.py > test.log` 保留完整证据。
    """
    kwargs: dict = {"cwd": str(cwd), "env": env, "stdout": stdout, "stderr": stderr}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(args, **kwargs)  # noqa: S603 - CI 内部固定命令
    try:
        return proc.wait(timeout=timeout), False
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        return 124, True


def browser_deps(node: str) -> dict:
    """预检 Playwright 包与 Chromium；返回 {ok, node, chromium, error}。"""
    e2e = TOOLS / "e2e"
    result: dict = {"ok": False, "node": node, "chromium": None, "error": None}
    if not (e2e / "package.json").is_file():
        result["error"] = f"缺少 {e2e / 'package.json'}"
        return result
    script = (
        "const{chromium}=require('playwright');"
        "const p=chromium.executablePath();"
        "console.log(JSON.stringify({path:p}));"
    )
    try:
        proc = subprocess.run([node, "-e", script], cwd=str(e2e), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:  # noqa: PERF203
        result["error"] = f"playwright 预检失败: {exc}"
        return result
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        result["error"] = ("playwright 不可用（tools/e2e 需要 npm ci）：" + " | ".join(tail))
        return result
    # P1-13-browser：优先使用显式 CHROMIUM_PATH（系统 Edge/Chrome），不强制 Playwright 自带 Chromium。
    override = os.environ.get("CHROMIUM_PATH")
    if override:
        if Path(override).is_file():
            result["chromium"] = override
            result["ok"] = True
            return result
        result["error"] = f"CHROMIUM_PATH 指向的文件不存在：{override}"
        return result
    try:
        info = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        result["error"] = "playwright 预检输出无法解析: " + (proc.stdout or "")[:200]
        return result
    exe = info.get("path")
    result["chromium"] = exe
    if not exe or not Path(exe).is_file():
        result["error"] = (f"Chromium 未安装（期望 {exe}）；请执行 "
                           "cd tools/e2e && npx playwright install chromium "
                           "或设置 CHROMIUM_PATH 指向系统 Chrome/Edge")
        return result
    result["ok"] = True
    return result
