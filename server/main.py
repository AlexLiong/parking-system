"""FastAPI 应用装配与进程入口（server/main.py）。

对应设计文档 §12。职责：
- ``create_app``：装配路由（admin/layout/sim）、CORS、静态托管、全局异常处理、
  WebSocket 实时通道；
- lifespan：加载配置 → 建库 → 装配容器 → bootstrap 缺省数据 → **启动恢复**
  （StartupRecovery）→ 启动事件订阅 → 挂载广播器 → 启动道闸 tick 后台任务；
- 运行：``uvicorn server.main:app --host 0.0.0.0 --port 8000``
  或 ``python -m server.main`` / 工具脚本 ``run.py``。

设计约束落地：
- 业务真理在后端；网关只转发（§2.2）；
- 断连兜底：全部 WS 断开 → access.on_disconnect() 锁定道闸安全侧（§6.2）；
- 先落库后动作由服务层保证，网关不额外包事务。
"""
from __future__ import annotations

import asyncio
import os
import time
import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .core.config import AppConfig, load_config
from .core.errors import ParkingError
from .core.events import PlateRecognized
from .core.event_store import EventStore
from .gateway.api_admin import router as admin_router
from .gateway.api_layout import router as layout_router
from .gateway.api_sim import router as sim_router
from .gateway.api_scenario import router as scenario_router
from .gateway.api_state import router as state_router
from .gateway.auth import decode_token
from .gateway.auth import validate_token
from .gateway.security import SecurityMiddleware
from sqlalchemy.exc import IntegrityError, OperationalError
from .gateway.context import Container, set_container
from .gateway.ws_realtime import Broadcaster
from .retention import cleanup_photos

log = logging.getLogger("main")


# ----------------------------- 日志 -----------------------------
def setup_logging(cfg: AppConfig) -> None:
    level = getattr(logging, str(cfg.logging.level).upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    if root.handlers:                      # 避免重复添加（多次 create_app / 测试）
        return
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    try:
        Path(cfg.logging.dir).mkdir(parents=True, exist_ok=True)
        file_h = RotatingFileHandler(
            Path(cfg.logging.dir) / "server.log", maxBytes=cfg.logging.max_bytes,
            backupCount=cfg.logging.backup_count, encoding="utf-8")
        file_h.setFormatter(fmt)
        root.addHandler(file_h)
    except Exception:  # noqa: BLE001 - 文件日志不可用时仅用控制台
        log.exception("初始化文件日志失败，仅使用控制台输出")


# ----------------------------- 后台任务 -----------------------------
def _background_call(container, method, *args):
    try:
        return method(*args)
    finally:
        container.db.remove()


async def _barrier_tick_loop(container: Container) -> None:
    """按 access.tick_interval_ms 驱动道闸状态机（纯 dt，不依赖墙钟）。"""
    interval = max(1, int(container.cfg.access.tick_interval_ms)) / 1000.0
    log.info("道闸 tick 后台任务启动，间隔 %.3fs", interval)
    while True:
        try:
            await asyncio.sleep(interval)
            await asyncio.to_thread(_background_call, container, container.simulation.tick)
        except asyncio.CancelledError:
            log.info("道闸 tick 后台任务停止")
            raise
        except Exception:  # noqa: BLE001 - 单次 tick 异常不应终止循环
            log.exception("道闸 tick 异常")
        finally:
            container.db.remove()


async def _pending_expiry_loop(container: Container) -> None:
    while True:
        try:
            await asyncio.sleep(1)
            await asyncio.to_thread(_background_call, container, container.access.expire_pending)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("人工确认待办过期扫描失败")
        finally:
            container.db.remove()


async def _photo_retention_loop(container: Container) -> None:
    """按 retention.interval_hours 周期清理 photos 上传/裁剪/相机目录。"""
    interval = max(0.25, float(container.cfg.retention.interval_hours)) * 3600.0
    log.info("照片保留期后台清理启动，间隔 %.2f 小时", interval / 3600.0)
    while True:
        try:
            await asyncio.sleep(interval)
            report = await asyncio.to_thread(cleanup_photos, container.cfg,
                                             db=container.db)
            if report.errors:
                log.warning("照片保留期清理完成但有告警: %s", report.errors)
        except asyncio.CancelledError:
            log.info("照片保留期后台清理停止")
            raise
        except Exception:  # noqa: BLE001 - 清理失败不影响业务
            log.exception("照片保留期后台清理失败")


def _wire_watchdog(container: Container) -> None:
    """仅按图像源健康状态驱动 camera incident。"""
    def _feed(e: PlateRecognized) -> None:
        if e.gate not in ("in", "out"):
            return
        if e.reason not in ("camera_missing", "image_unreadable", "camera_blank", "low_conf", "ok"):
            return
        try:
            container.watchdog.feed(
                e.gate, success=(not e.need_manual and bool(e.plate)),
                image_present=e.reason != "camera_missing",
                source_healthy=e.reason not in ("image_unreadable", "camera_blank"))
        except Exception:  # noqa: BLE001
            log.exception("看门狗 feed 失败")
    container.bus.subscribe(PlateRecognized, _feed)


# ----------------------------- lifespan -----------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg: AppConfig = app.state.cfg
    setup_logging(cfg)

    container = Container(cfg)
    set_container(container)
    app.state.container = container

    # 1) 缺省数据（布局/费率/管理员）
    container.bootstrap()
    # 2) 启动恢复（半截交易/道闸复位/车位对账，FR-9.1）
    report = container.recovery.recover()
    if report.messages:
        log.warning("启动恢复: %s", report.messages)
    # 3) 事件订阅 + 看门狗
    container.start()
    _wire_watchdog(container)
    # 4) 广播器挂载到当前事件循环
    loop = asyncio.get_running_loop()
    broadcaster = Broadcaster(
        container.bus, cfg, on_empty=container.access.on_disconnect,
        state=container.state)
    broadcaster.attach(loop, subscribe=False)
    container.broadcaster = broadcaster
    container.bus.outbound_handler = EventStore(container.state, broadcaster).publish
    app.state.broadcaster = broadcaster
    # 5) 后台 tick
    tick_task = asyncio.create_task(_barrier_tick_loop(container))
    expiry_task = asyncio.create_task(_pending_expiry_loop(container))
    retention_task = None
    if cfg.retention.enabled:
        if cfg.retention.run_on_startup:
            try:
                retention_report = await asyncio.to_thread(cleanup_photos, cfg,
                                                            db=container.db)
                log.info("启动照片保留期清理: %s",
                         {k: retention_report.to_dict()[k] for k in
                          ("scanned_files", "deleted_age", "deleted_capacity",
                           "bytes_freed", "kept_files")})
            except Exception:  # noqa: BLE001 - 清理失败不影响启动
                log.exception("启动照片保留期清理失败")
        retention_task = asyncio.create_task(_photo_retention_loop(container))

    log.info("停车场系统已启动: http://%s:%s/admin/", cfg.server.host, cfg.server.port)
    try:
        yield
    finally:
        tick_task.cancel()
        expiry_task.cancel()
        if retention_task is not None:
            retention_task.cancel()
        try:
            await tick_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        try:
            await expiry_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        if retention_task is not None:
            try:
                await retention_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        container.close()
        set_container(None)
        log.info("停车场系统已关闭")


# ----------------------------- 应用工厂 -----------------------------
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def validate_demo_exposure(cfg: AppConfig) -> None:
    """M8：demo_mode 仅允许 loopback；非 loopback 明确拒绝并提供可操作提示。"""
    if not getattr(cfg.server, "demo_mode", False):
        return
    host = str(getattr(cfg.server, "host", "") or "").strip().lower()
    if host in LOOPBACK_HOSTS or host.startswith("127."):
        return
    if os.environ.get("PARKING_ALLOW_EXPOSED_DEMO", "").strip().lower() in (
            "1", "true", "yes", "on"):
        log.warning("demo_mode=true 且监听 %r：已通过 PARKING_ALLOW_EXPOSED_DEMO "
                    "显式允许暴露（不推荐，演示数据/无鉴权风险）", host)
        return
    raise RuntimeError(
        f"拒绝启动：server.demo_mode=true 且 server.host={host!r} 非 loopback。"
        "演示模式含模拟数据/演示接口，仅允许本机访问：请把 server.host 改为 "
        "127.0.0.1（或 localhost/::1）；若确需暴露请显式设置环境变量 "
        "PARKING_ALLOW_EXPOSED_DEMO=1（不推荐，风险自担）；生产部署请设置 "
        "server.demo_mode=false。")


def create_app(cfg: AppConfig | None = None) -> FastAPI:
    cfg = cfg or load_config()
    validate_demo_exposure(cfg)
    app = FastAPI(title="停车场收费系统", version="1.0.0", lifespan=lifespan)
    app.state.cfg = cfg

    app.add_middleware(
        CORSMiddleware, allow_origins=[o for o in cfg.server.cors_origins if o != '*'],
        allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(SecurityMiddleware)

    # ---- 全局异常处理：领域异常 → 结构化 HTTP ----
    @app.exception_handler(ParkingError)
    async def _parking_error_handler(request, exc: ParkingError):  # noqa: ANN001
        status = exc.http_status if exc.http_status >= 400 else 400
        return JSONResponse(status_code=status, content=exc.to_dict())

    @app.exception_handler(Exception)
    async def _unhandled_handler(request, exc: Exception):  # noqa: ANN001
        log.exception("未处理异常: %s", exc)
        return JSONResponse(status_code=500, content={
            "error": "internal_error", "message": "服务器处理失败，请查看操作日志", "detail": {}})

    @app.exception_handler(IntegrityError)
    async def integrity_error(request, exc):
        return JSONResponse(status_code=409, content={'error':'data_conflict','message':'数据已被其他请求更新'})

    @app.exception_handler(OperationalError)
    async def busy_error(request, exc):
        return JSONResponse(status_code=503, content={'error':'database_busy','message':'数据库繁忙，请重试'})

    # ---- 路由 ----
    app.include_router(admin_router)
    app.include_router(layout_router)
    app.include_router(sim_router)
    app.include_router(scenario_router)
    app.include_router(state_router)

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    # ---- WebSocket 实时通道 ----
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        broadcaster: Broadcaster = ws.app.state.broadcaster
        container: Container = ws.app.state.container
        wants_v2 = ws.query_params.get("features") == "v2"
        if wants_v2 and not container.cfg.server.ws_v2:
            await ws.close(code=1013)
            return
        v2 = wants_v2
        subprotocol = None
        offered = [part.strip() for part in ws.headers.get(
            "sec-websocket-protocol", "").split(",")]
        credential = next((part[6:] for part in offered if part.startswith("token.")), None)
        if not credential:
            await ws.close(code=1008)
            return
        try:
            claims = validate_token(credential, container)
        except Exception:  # noqa: BLE001
            await ws.close(code=1008)
            return
        if claims.get("role") not in ("admin", "operator"):
            await ws.close(code=1008)
            return
        origin = ws.headers.get('origin')
        expected = ('https' if ws.url.scheme=='wss' else 'http')+'://'+ws.headers.get('host','')
        if origin and origin != expected and origin not in cfg.server.cors_origins:
            await ws.close(code=1008)
            return
        subprotocol = "v2" if v2 else None
        container.db.remove()
        try:
            since = max(0, int(ws.query_params.get("since", "0")))
        except ValueError:
            since = 0
        try:
            await broadcaster.connect(ws, v2=v2, since=since,
                                      session=ws.query_params.get("session"),
                                      subprotocol=subprotocol)
        except Exception:  # noqa: BLE001 - 握手失败时移除已经登记的连接
            await broadcaster.disconnect(ws)
            return
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(ws.receive_json(), timeout=min(5, max(.1, claims["exp"]-time.time())))
                except asyncio.TimeoutError:
                    try:
                        validate_token(credential, container)
                    except Exception:
                        await ws.close(code=1008)
                        break
                    continue
                if time.time() >= claims['exp']:
                    await ws.close(code=1008)
                    break
                try:
                    validate_token(credential,container)
                    _handle_ws_message(msg, container, v2=v2)
                finally:
                    container.db.remove()
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001 - 单连接异常不影响服务
            log.exception("WS 处理异常")
        finally:
            await broadcaster.disconnect(ws)

    # ---- 静态托管（前端产物存在才挂载）----
    _mount_static(app, cfg)

    @app.get("/")
    def root() -> RedirectResponse:
        admin_dir = Path(cfg.paths.web_admin)
        return RedirectResponse("/admin/" if admin_dir.exists() else "/api/health")

    return app


def _handle_ws_message(msg: dict, container: Container, *, v2: bool = False) -> None:
    """处理前端 → 后端 WS 消息（道闸回执 / 心跳）。"""
    mtype = msg.get("type")
    if mtype == "barrier_ack":
        container.access.on_barrier_ack(msg.get("gate", ""), msg.get("state", ""),
                                        msg.get("cmd_id"), require_cmd=v2, arm_position=msg.get("arm_position"))
    elif mtype == "ping":
        # 心跳：无需业务处理（连接活性由 receive 维持）
        return
    else:
        log.debug("忽略未知 WS 消息类型: %s", mtype)


def _mount_static(app: FastAPI, cfg: AppConfig) -> None:
    """挂载前端静态资源与媒体目录（存在才挂，避免首启缺目录报错）。"""
    class NoCacheStaticFiles(StaticFiles):
        """前端/资产静态文件每次 revalidate，避免重启后浏览器继续用旧 JS/GLB。"""
        async def get_response(self, path, scope):
            resp = await super().get_response(path, scope)
            if resp.status_code in (200, 304):
                resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                resp.headers["Pragma"] = "no-cache"
                resp.headers["Expires"] = "0"
            return resp

    admin_dir = Path(cfg.paths.web_admin)
    sim_dir = Path(cfg.paths.web_sim)
    photos_dir = Path(cfg.paths.photos_dir)
    try:
        photos_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.exception("无法创建照片目录: %s", photos_dir)
    if admin_dir.exists():
        app.mount("/admin", NoCacheStaticFiles(directory=str(admin_dir), html=True),
                  name="admin")
    else:
        log.warning("管理端静态目录不存在，跳过挂载: %s", admin_dir)
    if sim_dir.exists():
        app.mount("/sim", NoCacheStaticFiles(directory=str(sim_dir), html=True),
                  name="sim")
    else:
        log.warning("仿真端静态目录不存在，跳过挂载: %s", sim_dir)
    common_dir = Path(cfg.paths.web_sim).parent / "common"
    if common_dir.exists():
        app.mount("/common", NoCacheStaticFiles(directory=str(common_dir)),
                  name="common")
    if photos_dir.exists():
        # 车牌照片/裁剪图供前端展示（识别输入与建模解耦，FR-1.3）
        app.mount("/media", StaticFiles(directory=str(photos_dir)), name="media")
    # 车牌源图库（datasets/plates）只读挂载：管理端记录/识别页展示原始车牌照片，
    # 便于识别结果溯源（识别输入与 3D 建模解耦，FR-1.3）
    class ImageFiles(StaticFiles):
        async def get_response(self, path, scope):
            if Path(path).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".svg"}:
                return JSONResponse({"error": "not_found"}, status_code=404)
            return await super().get_response(path, scope)

    datasets_dir = Path(cfg.paths.plates_dataset).parent
    if datasets_dir.exists():
        app.mount("/datasets", ImageFiles(directory=str(datasets_dir)),
                  name="datasets")
    # route-v2：assets_3d 只读挂载（layout/lot_v2.json manifest + glb + 渲染图）。
    # 前端（3D 端）可直接 GET /assets3d/layout/lot_v2.json 作为路网单一事实源；
    # 目录不存在时跳过（旧部署/无 3D 资产环境仍可启动）。
    from .layout.manifest import resolve_assets_root
    assets_root = resolve_assets_root(cfg)
    if assets_root is not None and assets_root.is_dir():
        app.mount("/assets3d", NoCacheStaticFiles(directory=str(assets_root)),
                  name="assets3d")
        log.info("已挂载 /assets3d（只读，项目根 assets_3d）→ %s", assets_root)
    else:
        log.warning("未找到 assets_3d 目录，跳过 /assets3d 静态挂载")


# 模块级 app，供 ``uvicorn server.main:app`` 直接使用
app = create_app()


def main() -> None:
    import uvicorn
    cfg = load_config()
    uvicorn.run("server.main:app", host=cfg.server.host, port=cfg.server.port,
                reload=False)


if __name__ == "__main__":
    main()
