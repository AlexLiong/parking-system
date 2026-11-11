# T5验证矩阵与调试契约
|层级|入口|断言|
|---|---|---|
|后端|pytest tests -q|业务/鉴权/WS/迁移/时间一致|
|静态图|node --test web/sim3d/test/route.test.js|32条原图契约，不冒充运行时|
|运行时|node --import ./tools/three-node-register.mjs tools/audit_traffic.mjs|160条最长车路径；20车600秒；OBB全部静态体/车车；队列边界；Z7/同区窗口互斥；全部离场|
|车辆道闸|ci.py Node阶段|运动连续/慢帧/预测停车/动画/精确授权/八车队列|
|统计|tools/bench_stats.py|隔离10万条，计数/50万元正确，day/month/year/search均≤2秒|
|真实GLB|tools/ci.py --browser|每个脚本独立临时DB，barrier/pause/traffic/business/soak，截图与事件|
|长测|tools/soak.py --seconds 7200|真实墙钟2小时HTTP/RSS，不替代重负载或GPU证明|

统一入口tools/ci.sh、tools/ci.ps1，--bench运行统计，--browser需安装tools/e2e的Playwright和对应Chromium。Node22.15+（registerHooks），Python按requirements锁定；资产必须同级。失败非0立即停止，不删断言。CI不更改用户DB。

window.__SIM__含scene/manifest/paths/planner/scheduler/reservations/controls/dump。dump是可JSON序列化的数据，含simTime/paused/vehicles的pose/nose/state/zones、barriers角度、traffic队列/待派车/预约表、scene渲染计数。controls.step每步≤50ms，预览上限5秒，不能把该时间作墙钟性能。
