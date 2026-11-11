"""接入层（gateway）。

对应设计文档 §3.7 / §11 / §12。职责边界：
- **只做协议转换与装配**，不写业务：HTTP/WebSocket ⇄ EventBus/服务调用；
- ``context.Container``：依赖注入根，一次性装配 db/bus/repos/services；
- ``ws_realtime.Broadcaster``：订阅总线事件转 WS 广播（含断连缓冲追平）；
- ``auth``：JWT 登录与鉴权依赖；
- ``api_admin`` / ``api_layout`` / ``api_sim``：三组 REST 路由；
- ``server.main``：FastAPI 应用装配 + lifespan（启动恢复/后台 tick/静态托管）。

业务真理始终在后端服务层；前端无权自行开闸（§2.2）。
"""
from .context import Container, build_container, get_container, set_container
from .ws_realtime import Broadcaster, event_to_message

__all__ = [
    "Container", "build_container", "get_container", "set_container",
    "Broadcaster", "event_to_message",
]
