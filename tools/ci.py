"""Cross-platform local CI（P1-13：单次可全绿、显式超时/重试/清理/退出码）。

用法（在仓库任意目录）：
  python tools/ci.py                      # 非浏览器 CI：pytest + Node + 3D 审计
  python tools/ci.py --bench              # 追加 100k 统计基准门禁
  python tools/ci.py --browser            # 追加真实浏览器 E2E（需 Playwright+Chromium）
  python tools/ci.py --browser-only       # 只跑浏览器 E2E
  python tools/ci.py --browser --browser-if-available
                                          # 浏览器依赖缺失时跳过并 exit 0，否则 exit 2

退出码：
  0 = 所有执行项通过（或 --browser-if-available 下依赖缺失而跳过浏览器项）
  1 = 测试/断言失败（pytest/node/浏览器脚本返回非 0）
  2 = 环境/依赖/启动/超时问题（缺 node、缺 assets、Playwright 缺失、隔离服务启动失败）
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import yaml

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))
from ci_support import browser_deps, resolve_node, run_with_timeout  # noqa: E402

DEFAULT_SCRIPTS = ("barrier,pause,traffic,business,admin-display,displays,layout,"
                   "loading,scenario,vehicle-visual,find-car,accessibility,soak")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _read_log(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _tail(text: str, lines: int = 200) -> str:
    rows = text.splitlines()
    if len(rows) <= lines:
        return text
    return f"...（仅显示最后 {lines} 行，完整日志见证据目录）...\n" + "\n".join(rows[-lines:])


def _terminate(proc: subprocess.Popen) -> None:
    """尽力终止服务及其子进程（Windows 下 uvicorn 可能拉起 reloader/子进程）。"""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               check=False)
            else:
                proc.kill()
            proc.wait(timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            pass


def _wait_ready(server: subprocess.Popen, log: Path, base: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError(f"隔离服务启动失败（exit={server.returncode}）:\n"
                               + _read_log(log)[-3000:])
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"隔离服务 {timeout:.0f}s 内未就绪:\n" + _read_log(log)[-3000:])


def _record(results: list[dict], name: str, status: str, seconds: float = 0.0,
            detail: str = "") -> None:
    results.append({"name": name, "status": status, "seconds": round(seconds, 1),
                    "detail": detail})
    mark = {"pass": "PASS", "fail": "FAIL", "error": "ERROR", "skip": "SKIP"}[status]
    print(f"[{mark}] {name} ({seconds:.1f}s){(' - ' + detail) if detail else ''}", flush=True)


def _run_step(name: str, cmd: list[str], env: dict, timeout: float,
              results: list[dict], cwd: Path = ROOT) -> int:
    print("+ " + " ".join(cmd), flush=True)
    started = time.monotonic()
    rc, timed_out = run_with_timeout(cmd, cwd=cwd, env=env, timeout=timeout)
    elapsed = time.monotonic() - started
    if timed_out:
        _record(results, name, "error", elapsed, f"超过 {timeout:.0f}s 超时，已终止进程树")
        return 2
    if rc != 0:
        _record(results, name, "fail", elapsed, f"退出码 {rc}")
        return 1
    _record(results, name, "pass", elapsed)
    return 0


def _browser_env(env: dict) -> dict:
    child = dict(env)
    if os.environ.get("CHROMIUM_PATH"):
        child["CHROMIUM_PATH"] = os.environ["CHROMIUM_PATH"]
    return child


def _run_browser_script(script: str, node: str, env: dict, args: argparse.Namespace,
                        results: list[dict]) -> int:
    """在独立临时库/端口上运行单个浏览器脚本；失败按 --browser-retries 重试且每次全新环境。

    返回 0=通过，1=脚本失败，2=环境/启动问题（重试耗尽）。
    """
    if not script.replace("-", "").isalnum() or not (ROOT / f"tools/e2e/{script}.cjs").is_file():
        _record(results, f"browser:{script}", "error", 0, "未知脚本名或文件不存在")
        return 2
    attempts = max(0, args.browser_retries) + 1
    last_rc = 1
    for attempt in range(1, attempts + 1):
        label = f"browser:{script}" + (f" (attempt {attempt}/{attempts})" if attempt > 1 else "")
        print(f"+ {node} tools/e2e/{script}.cjs (isolated)", flush=True)
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="parking-e2e-",
                                         ignore_cleanup_errors=True) as tmp:
            folder = Path(tmp)
            cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
            cfg["server"]["demo_mode"] = True
            for key, value in {
                "data_dir": folder, "db_file": folder / "parking.db",
                "photos_dir": folder / "photos", "reports_dir": folder / "reports",
                "logs_dir": folder / "logs",
            }.items():
                cfg["paths"][key] = str(value)
            configuration = folder / "config.yaml"
            configuration.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
            port = _free_port()
            base = f"http://127.0.0.1:{port}"
            server_log = folder / "server.log"
            script_log = folder / "browser.log"
            child_env = {**_browser_env(env), "PARKING_CONFIG": str(configuration),
                         "PARKING_URL": base, "PARKING_ADMIN_PASSWORD": "admin123"}
            rc, timed_out = 124, False
            setup_error: str | None = None
            started = time.monotonic()
            try:
                with server_log.open("w", encoding="utf-8") as output:
                    server = subprocess.Popen(
                        [sys.executable, "-m", "uvicorn", "server.main:app",
                         "--host", "127.0.0.1", "--port", str(port)],
                        cwd=ROOT, env=child_env, stdout=output, stderr=subprocess.STDOUT)
                    try:
                        _wait_ready(server, server_log, base, args.server_timeout)
                        with script_log.open("wb") as script_out:
                            rc, timed_out = run_with_timeout(
                                [node, f"tools/e2e/{script}.cjs"], cwd=ROOT,
                                env=child_env, timeout=args.browser_timeout,
                                stdout=script_out, stderr=subprocess.STDOUT)
                    finally:
                        _terminate(server)
            except Exception as exc:  # noqa: BLE001 - 隔离环境/启动失败
                setup_error = f"{type(exc).__name__}: {exc}"
            evidence = args.evidence_dir / script
            evidence.mkdir(parents=True, exist_ok=True)
            for src, name in ((server_log, "server.log"), (script_log, "browser.log")):
                if src.exists():
                    shutil.copyfile(src, evidence / name)
            script_text = _read_log(script_log)
            elapsed = time.monotonic() - started
        if setup_error:
            last_rc = 2
            _record(results, label, "error", elapsed, setup_error[:500])
            continue
        if timed_out:
            last_rc = 2
            _record(results, label, "error", elapsed, f"超过 {args.browser_timeout:.0f}s 超时")
            continue
        if rc != 0:
            last_rc = 1
            print(_tail(script_text), flush=True) if script_text else None
            _record(results, label, "fail", elapsed, f"退出码 {rc}")
            continue
        print(_tail(script_text), flush=True) if script_text else None
        _record(results, label, "pass", elapsed)
        return 0
    return last_rc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="停车场收费系统跨平台本地 CI（P1-13）")
    parser.add_argument("--bench", action="store_true", help="追加 100k 统计基准")
    parser.add_argument("--browser", action="store_true", help="追加真实浏览器 E2E")
    parser.add_argument("--browser-only", action="store_true", help="只运行浏览器 E2E")
    parser.add_argument("--browser-if-available", "--browser-optional", dest="browser_if_available",
                        action="store_true",
                        help="浏览器依赖缺失时跳过（exit 0），否则默认报错 exit 2")
    parser.add_argument("--scripts", default=DEFAULT_SCRIPTS, help="浏览器脚本逗号列表")
    parser.add_argument("--soak-seconds", type=int, default=600,
                        help="浏览器 soak 脚本的仿真时长（SOAK_SECONDS）")
    parser.add_argument("--browser-retries", type=int, default=1,
                        help="浏览器脚本失败重试次数（每次全新库/端口，默认 1）")
    parser.add_argument("--keep-going", action="store_true",
                        help="浏览器脚本失败后继续执行剩余脚本（默认首个失败即停止）")
    parser.add_argument("--server-timeout", type=float, default=60,
                        help="隔离服务就绪超时秒数（默认 60）")
    parser.add_argument("--step-timeout", type=float, default=1800,
                        help="非浏览器单步超时秒数（默认 1800）")
    parser.add_argument("--browser-timeout", type=float, default=900,
                        help="单个浏览器脚本超时秒数（默认 900）")
    parser.add_argument("--evidence-dir", type=Path,
                        default=ROOT.parent / "_qa_audit/repair/final/e2e",
                        help="浏览器证据目录（默认 _qa_audit/repair/final/e2e）")
    parser.add_argument("--node", default=None, help="显式指定 node 可执行文件")
    parser.add_argument("--limit-seconds", type=float, default=None,
                        help="透传给 tools/bench_stats.py 的显式阈值（默认读 tools/bench_limits.json）")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.evidence_dir = args.evidence_dir.resolve()
    node = resolve_node(args.node)
    env = {**os.environ,
           "PYTHONUTF8": os.environ.get("PYTHONUTF8", "1"),
           "PYTHONIOENCODING": "utf-8",
           "SOAK_SECONDS": str(args.soak_seconds)}
    if not (ROOT.parent / "assets_3d/layout/lot_v2.json").is_file():
        print("[ERROR] assets_3d 必须与 parking-system 同级（只读资产包），未找到 "
              "assets_3d/layout/lot_v2.json", file=sys.stderr)
        return 2
    if node is None:
        print("[ERROR] 未找到 node。请安装 Node 或设置 PARKING_NODE_BIN/--node。", file=sys.stderr)
        return 2
    print(f"[env] python={sys.version.split()[0]} node={node} "
          f"platform={sys.platform} UTF8={env['PYTHONUTF8']}", flush=True)
    results: list[dict] = []

    if not args.browser_only:
        commands: list[tuple[str, list[str]]] = [
            ("pytest", [sys.executable, "-m", "pytest", "tests", "-q"]),
            ("node:route+scenario", [node, "--test", "web/sim3d/test/route.test.js",
                                     "web/sim3d/test/scenario.test.js"]),
            ("node:barrier+vehicle+traffic+display",
             [node, "--import", "./tools/three-node-register.mjs", "--test",
              "web/sim3d/test/barrier.test.js", "web/sim3d/test/vehicle-motion.test.js",
              "web/sim3d/test/traffic-safety.test.js",
              "web/sim3d/test/display-resources.test.js"]),
            ("audit:traffic", [node, "--import", "./tools/three-node-register.mjs",
                               "tools/audit_traffic.mjs"]),
            ("audit:runtime-lanes", [node, "tools/audit_runtime_lanes.mjs"]),
            ("audit:parked-neighbors", [node, "--import", "./tools/three-node-register.mjs",
                                        "tools/audit_parked_neighbors.mjs"]),
        ]
        if args.bench:
            bench = [sys.executable, "tools/bench_stats.py"]
            if args.limit_seconds is not None:
                bench.append(f"--limit-seconds={args.limit_seconds}")
            commands.append(("bench:100k", bench))
        for name, command in commands:
            code = _run_step(name, command, env, args.step_timeout, results)
            if code:
                _print_summary(results)
                return code

    browser_rc = 0
    if args.browser or args.browser_only:
        deps = browser_deps(node)
        if not deps["ok"]:
            print(f"[ERROR] 浏览器 E2E 依赖不可用：{deps['error']}", file=sys.stderr)
            print("        修复：cd tools/e2e && npm ci && npx playwright install chromium\n"
                  "        或使用 --browser-if-available 跳过浏览器项（非浏览器 CI 仍可全绿）。",
                  file=sys.stderr)
            if args.browser_if_available:
                _record(results, "browser:all", "skip", 0, "playwright/chromium 缺失，已按参数跳过")
                _print_summary(results)
                return 0
            return 2
        scripts = [s.strip() for s in args.scripts.split(",") if s.strip()]
        for script in scripts:
            rc = _run_browser_script(script, node, env, args, results)
            if rc:
                # 2（环境/超时）优先于 1（断言失败）；--keep-going 时继续跑完所有脚本
                browser_rc = max(browser_rc, rc)
                if not args.keep_going:
                    break

    _print_summary(results)
    return browser_rc


def _print_summary(results: list[dict]) -> None:
    if not results:
        return
    failed = [r for r in results if r["status"] in ("fail", "error")]
    passed = [r for r in results if r["status"] == "pass"]
    skipped = [r for r in results if r["status"] == "skip"]
    total_seconds = sum(r["seconds"] for r in results)
    print("\n===== CI SUMMARY =====")
    print(f"passed={len(passed)} failed={len(failed)} skipped={len(skipped)} "
          f"total_passed_seconds={total_seconds:.1f}")
    for row in failed:
        print(f"  FAILED {row['name']}: {row['detail']}")
    print("======================")


if __name__ == "__main__":
    raise SystemExit(main())
