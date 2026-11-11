# T1 跨端同步任务档案

关联 C-03、C-04、P1-06、P2-12、R-05。目标是清场、刷新、断线、重启及多客户端情况下，以后端状态和可重放事件恢复管理端与仿真端。

## 基线与复现

- 基线提交：`670ceab`；输入包中 33 个文件的工作区差异为行尾格式变化，`git diff --ignore-space-at-eol --stat` 为空。
- `.venv/bin/python -m pytest tests/ -q`：150 passed，退出码 0（macOS、Python 3.14.5）。
- `node --test web/sim3d/test/route.test.js`：32 passed，退出码 0。
- 源码复现：`web/sim3d/js/ui/ControlPanel.js` 的清场按钮只调用 `scenario.reset()`、`scheduler.clearAll()` 和本地 `onReset()`；`server/gateway/` 不存在 `/api/demo/reset` 路由。
- 源码复现：`server/gateway/ws_realtime.py` 的 `_buffer` 在首个连接时清空；新连接不能获得其他客户端已取走的事件，也没有持久 `seq` 和状态快照。
- 源码复现：`server/main.py` 在所有 WS 断开时调用 `access.on_disconnect()`；`web/sim3d/js/main.js` 加载后自动播放剧本，刷新恢复没有后端快照。

## 评审范围

- [design.md](design.md)：状态、事务、时序、兼容、迁移与回滚。
- [api-contract.md](api-contract.md)：REST、WS、错误和幂等契约。
- [test-plan.md](test-plan.md)：定向测试、跨端验证和证据。

## 实施检查点

- 设计提交：`b7e6c46`；后端会话/快照/事件/复位、WS v2、前端 hydrate 与多端镜像已实现。
- 浏览器证据：交付根目录 `_qa_audit/repair/T1/evidence/` 的清场历史、刷新、双端、移动端截图与 JSON。测试命令和状态见 `_qa_audit/repair/T1/test.log`。
- T1 只覆盖既有人工待办的快照字段占位；低置信待办表和处理流属于 T2/T6。道闸重试/超时完整协议属于 T3，仿真端演示管理员凭据隔离属于 T4。上述依赖不得在全量修复报告中标为已修复。
