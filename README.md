# 停车场收费系统（修复交付版）

FastAPI + SQLite(WAL)、原生管理端、Three.js r169 仿真端，本地 ONNX 车牌识别。程序和模型离线运行；首次建立 Python 环境需要下载依赖。3D 仿真入口/出口使用**场景 LPR 相机截图识别**，管理端提供**上传识别演示**（不产生入场/放行）；本轮按演示/上传基本可用口径交付，PR-2 不属于本轮验收口径（见文末）。

## 启动

目录必须保持 `parking-system/` 与 `assets_3d/` 同级。需要 Python 3.11–3.14，实测 Python 3.14；建议使用这一版本安装精确锁。

macOS/Linux：
```sh
cd parking-system
python3 tools/run.py --no-browser
```
Windows PowerShell：
```powershell
cd parking-system
py -3.14 tools/run.py --no-browser
```
脚本创建并保留 `.venv`，安装缺少依赖、检查场地与本地模型 SHA；不会清空数据库。也可双击 macOS `start.command` 或运行 Windows `start.ps1`。管理端 http://127.0.0.1:8000/admin/ ，仿真端 http://127.0.0.1:8000/sim/ 。`--port 9625` 可更换端口，`--check` 只做启动检查。已有旧环境的依赖版本变化时，主动执行 `.venv/bin/python -m pip install -r requirements.lock.txt`（Windows 将解释器路径改为 `.venv\Scripts\python.exe`）。

> **Windows 中文路径/UTF-8**：本根目录版本已修复 `requirements.lock.txt` BOM、`run.py`/`start.ps1` UTF-8、LPR `read_text` 与模板 `cv2.imread` 中文路径问题；Windows 中文目录下无需设置环境变量即可启动。若使用旧交付包，请先设置 `$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'`，或解压到纯 ASCII 路径。

默认 `config.yaml` 是本地演示模式：新库管理员 admin/admin123，操作员 operator/operator123；操作员只能做监控、记录、报表、人工确认/放行、缴费、设备/摄像头处置等运营操作，费率/布局/名单/剧本写入仅管理员可用。3D 使用显式演示登录。`config.yaml` 的 `paths.plates_dataset` 指向 `datasets/demo-lpr`（旧 `datasets/plates` 已删除）；使用 `PARKING_CONFIG=config.production.yaml` 时请确认该路径也指向 `datasets/demo-lpr`。对外部署请使用 `config.production.yaml`：新库创建随机首启管理员凭据（data 下0600文件），或显式设置 `PARKING_ADMIN_PASSWORD`；操作员账号在非 demo 模式下需显式设置 `PARKING_OPERATOR_PASSWORD` 才会创建。**关闭演示模式不会更改已有库中旧账号的密码**，旧账号必须由所有者主动处理。密钥默认生成在 `data/.jwt-secret`。`.env.example` 仅示例，不自动加载。

## 现有功能

- 3D 仿真入口/出口**场景 LPR 相机截图识别**：车辆到停止线自动抓拍 1280×720 → `POST /sim/events/arrived-frame` → 统一识别管线 → `expected_plate` + 置信度 0.85 先验决策 → 开闸/分配/管理端卡片；旧 `arrived(image_path)` 自动回退。
- 车牌贴图来自 `datasets/external/chinese_license_plate_generator` 的 **140 套 blue/yellow/green_car/green_truck 模板+字体**（MIT，固定 commit `df4757f…`）；`datasets/demo-lpr` 18 张（蓝5/小绿5/大绿4/黄4）已按外部模板重生成，**HyperLPR3 2D 18/18**；3D 入口 **18/18**、出口 **18/18**、后牌 **6/6**、动态 6 牌 **12/12 exact**。车辆 GLB 的 `Plate_F/Plate_R` 不再是纯文字 Canvas；管理端实时监控与 3D HUD 显示"识别输入"卡片（相机截图/裁剪/conf/expected/匹配）。
- 管理端 `/admin/#/recognize` 为**上传识别演示**：仅调用 `/api/recognize`、`/api/recognize/gallery` 与只读 `/api/recognition-logs` 展示结果（source=upload、阈值 0.85、conf/candidates/evidence_url），不产生入场/记录/车位/抬杆副作用。
- 相机视角：全景/入口相机/出口相机快捷切换；入口 (-5.6,3.405,21.0)/出口 (5.6,3.405,39.0)，FOV 30°。
- 3D 视觉尺寸（P2-17）：入口排队/停止线/跟车使用 GLB Box3 实测视觉包围盒，OBB 碰撞尺寸不变；三车混合排队视觉车距 **2.159/1.898 m**（Node repair-p2 29/29，route 32/32）。
- 闸口屏（P2-02/P2-01）：出口屏显示抓拍照片+车牌/费用/余位；入口/出口屏在空闲/识别/收费/支付态显示“余位 N”并随 `space_update` 刷新。
- 上传识别日志：`/api/recognize`、`/api/recognize/frames`、`/api/recognize/video` 返回 `recognition_log_id` 并写 `recognition_log`（管理端日志可见）；仍无入场/记录/车位副作用。
- 视频识别（P3-06）：`POST /api/recognize/video` 与 `tools/recognize_video.py` 支持本地视频/RTSP URL 抽取 2–5 帧融合；本地视频 smoke 通过，**loopback RTSP 已实测**（ffmpeg+mediamtx，API/CLI 识别 湘N7D071；首次首帧约 20.5s 需重试），**公网/鉴权 RTSP 未测**。
- 布局能力边界（P2-07）：当前能力为 80 位、每区 20；C25/自定义坐标仍返回 409（固定网格校验，`/api/admin/layout` 拒绝）；**C25 预演/`layout/validate`/`layout_constraints`/`zone_capacity`/`runtime_geometry_fixed` 已删除**；**P2-07 C25 仍暂缓/部分（需求降级）**，未删校验、未伪造车位。
- 管理端 i18n/无障碍/移动（P3-11）：436 条中→英短语、9 个页面英文、≤800px 移动适配、基础 WCAG（lang/landmark/skip-link/label/focus/对比度）；业务数据（车牌号、事件 ID）保持原值。
- 统一加载失败重试（P2-10）：monitor/records/stats/fee/layout/lists/incidents/find-car/recognize 等页面提供 `role=alert` 错误卡+Retry；阻断 API 实测可恢复。
- R-04（视觉-only）：8 个贴墙边界位应用 +0.12m 视觉偏移（仅 `model.position`），视觉越墙 0.1097→0，OBB/路线/车位中心不变；截图 `_qa_audit/repair_tasks_20261023/B-C-sim/`。
- LPR 立杆静态碰撞体（B/C）：manifest `static_colliders` 19→21（`lpr_in/out` 0.3×0.3m，kind=camera），manifest hash `sha256:593617b665b80945`；route/traffic/满停邻车审计无回归。
- 保留期清理：`server/retention.py` + `tools/cleanup_photos.py` + 配置，默认 7 天/2 GiB/每目录保留 50 个最新文件/启动+每 6h；只清 `data/photos/{uploads,crops,camera}`，不碰 datasets/logs/reports。
- 3D 首帧抓拍预热：SwiftShader 首抓拍 1012→462ms（-54.3%），生产首车 640ms；`?warmup=0` 可对照关闭。
- 手动派车（2026-10-24/25）：3D 独立 modal 输入 7/8 位车牌 + `blue/green/green_large/yellow` + 停车时长；`POST /api/sim/dispatch-plan` 按外部模板渲染图（`/media/uploads/dispatch/`）并返回 `vehicle_kind`；同牌在场/排队前置拒绝 `duplicate_plate`、后端 409 `duplicate_entry`；3D 贴后端图、按类型选车型、入库后按仿真时长自动出场；错误在 modal/HUD 明示。
- 仿真业务时间（2026-10-25）：demo 模式 `business_now = epoch + sim_time`，记录/报价/待办/人工时间按仿真时间，新增 `entry_sim_sec/exit_sim_sec/duration_sim_sec`，`created_at` 仍为真实墙钟；speed **0.25–10**，暂停冻结；E2E 10× 停车 600 仿真秒 quote 13min、真实墙钟远小。
- 重置/CLI（2026-10-25）：`POST /api/demo/reset {force:true}` 跳过闸区/pending 检查，开放记录 aborted（保留审计）、完成历史保留、pending rejected、车位释放、新会话/仿真归零、3D 清场；`tools/run.py --resetdata` 完整备份重建+seed，`--reset` 仅开放记录；实测 60 历史/6 在场→aborted6、out60 保留。
- 人工授权/黑名单（2026-10-25）：`/api/manual/authorize-entry|exit` 替代裸抬杆，入口/出口授权后 3D 自动进/出；重复牌 409；黑名单无后车倒车离场、有后车进场后直接出场，**不建普通记录**，仅 incident/recognition + blacklist-resolution 审计；前车离场后后车自动重识别入场。
- 演示剧本（2026-10-25）：`t` 为唯一时间轴（同 t 按列表序），预计入场/出场/缴费；默认 6 车（粤F4K1CZ/贵JD8H5PF/鄂ABWTXHF/桂ES6EKB/甘N35MYY/渝GDKLV32）；源 JSON 的 `t+duration` 合计 **2520s**，当前 `duration` 语义（入口→出口总时长）下 HUD/播放总长 **2529s**（含入口估计 9s）；管理端编辑 `vehicles`、`GET/PUT /api/admin/scenario`、reset 内置；3D 进度显示已入场/已出场/总车次。
- 管理端/3D UI（2026-10-25）：手动派车独立 modal；#control/#hud/#freeboard/#recognition-input/#scenario-progress 可拖拽/最小化/位置持久；事件/错误解包 data/payload、未知事件不丢、detail 可读；速度 0.25–10；删除旧说明文字；剧本进度面板独立。
- 车牌规范（2026-10-25）：`server/lpr/plate_city_codes.json` 含 31 省/**406** 合法发牌机关字母（无 I/O），`blue`7/`green`小绿8/`green_large`大绿8/`yellow`黄7；`server/lpr/render.py` 全部走外部模板；非法城市码/特殊牌/非四类型 422。
- 实时事件流（2026-10-24）：摘要行 + `<details>` 展开时间/类型/车牌/门/金额/原因/图片；键盘 Enter/Space 可展开；最多 80 条。
- 小 bug 修复（2026-10-24/25）：暂停时可拖拽视角；camera preset 下拉显示修复；管理端待人工输入不再被 4s 轮询清空；删除识别输入旧说明文字。
- 黑名单闸杆（2026-10-26）：无后车时入口杆保持 **CLOSED/passable=false**，黑名单车在杆外倒车（reverse→close，不穿杆、不开入口杆，这是正确安全语义）；有后车时入口 OPEN→场内绕行→出口 OPEN→离开→双杆 CLOSED；后车自动 parked/pending=0/violations=0；全程不建 parking_record，仅 incident/recognition/blacklist-resolution + `blacklist_gate` 审计；后端 POST /api/sim/blacklist-gate。
- 人工异常处理（2026-10-26）：管理端正常态无大段人工区；道闸/摄像头异常时按情况显示抬/落/故障/恢复；待确认卡片输入车牌后显示**正常费用**（实测 35 分钟 ¥5.00，不再 0 元），按正常计费/免费/作废结算；人工抬杆只是物理通行（manual/passable=true），未授权/未付费车仍不走，已授权+杆开才走。
- sim 控件（2026-10-26）：重置 force 清场；出口摄像头故障/恢复按钮；剧本“下一事件”合并预计出场/缴费；通行条件 spaceNo/exitAuthorized + 闸杆 passable；黑名单走正常闸杆路径。
- admin 事件流（2026-10-26）：event-view.js 解包 data/payload、未知事件不裸 JSON、detail 可读；删除识别/人工冗长说明；正常态仅紧凑演示控制/重置。
- 出场自动扣费（2026-10-31）：所有正常有记录车辆出口识别→写 `exit_photo`→按费率自动结算（`method=auto`，含 0 元）→`PaymentCompleted`→out/释放/出口自动开闸，无需剧本 pay/人工；剧本 pay 幂等；白名单/月租/新能源/封顶照常计算；无入场记录转人工。
- 异常纠正（2026-10-31）：`POST /api/manual/correct-exit` 纠正开放记录车牌/入场 sim 时间后按 sim 时长自动结算；无记录 `action=auto` 409、free/void 人工兜底；黑名单出场 **0 费用/0 普通记录**（遗留开放记录置 abnormal 仅审计）。
- 管理端设备控制（2026-10-31）：设备控制卡正常态常显（入口/出口道闸抬/落/故障/恢复；入口/出口摄像头故障/恢复，12 按钮）；人工业务仅异常待办时出现；事件流规范化、删冗长文字。
- 仿真端（2026-10-31）：剧本 finished 后暂停仍可用（冻结/继续/重播）；13 车型 × 前后牌 × 入口/出口 52 组矩阵未复现翻转；正常入口前牌/出口后牌 13/13 exact；不修改 `Vehicle.js`。
- 时长/暂停/跟随（2026-11-04）：未播放时点暂停不启动剧本（暂停只冻结 sim，继续不 play）；finished 后暂停/继续/重播可用；跟随选择器可跟随任意车辆（含 parked），目标离场保留最后位置，不自动切下一辆；`duration` 改为**入口道闸→出口道闸总时长**，`scheduledExitAt=entryGate+duration`、`autoExitAt=entryGate+duration-exitTravelEst`（误差 +14.8s 为出口排队/出场路径），默认剧本总长约 **2529s**。
- 管理端记录/费率/报表（2026-11-04）：记录详情固定中文字段、支付方式翻译、历史费率标注；费率页只显示当前生效费率 + 查看历史（历史只读、`effective_to`）；统计报表预览 day/week/month/quarter/year（日报车辆、周报每日+车辆、月报周汇总、季报月/年报月），CSV/PDF **公开导出**，只统计完整 entry+exit 记录。
- 人工抬杆保持（2026-11-06）：管理端人工抬杆进入 **hold/keep_open=true**，跨 `keep_open_sec` 不自动落；需手动“落杆”才关闭（杆区有车 409 `zone_occupied`）；故障人工抬杆同样保持；黑名单/普通业务自动落杆不变。
- 管理端/仿真 UI（2026-11-06）：footer 仅保留 `v1.0.0`；仿真 HUD“下一事件”拆两行完整显示（不 ellipsis/不截断，桌面/390 移动均无溢出）；报表表格固定列宽/对齐；基准控件 day=日期、week=周一、month=月、quarter=年+Q、year=年，预览/CSV/PDF 同 anchor。
- 报表 PDF/CSV（2026-11-06/11-08）：day/week 横向 A4、`Paragraph` CJK 换行、`LongTable` 分页重复表头；month/quarter/year 纵向；支付方式中文、区间可读；实际 Edge 渲染无重叠。CSV/PDF 支持 `lang=zh|en`（默认 zh，英文 UI 自动 en），文件名 `report_<period>_<anchor>_cn|_en`；仅表头/标签翻译，数据值不译，CSV BOM 不变。
- 低置信人工确认/拒绝/超时，读图错误与摄像头故障分离，批量2–5帧识别建议。
- 车位分配、名单/月租、计费/幂等缴费、无入场记录结算及审计。
- 完整道闸协议和手动操作、防砸、日志导出，暂停/恢复使用共享仿真时钟。
- 持久事件序号、快照、清场会话、刷新/断线恢复；单个仿真调度客户端驱动。
- 反向寻车（车位/入场照片/位置图）、异常实时更新、移动导航、键盘弹窗。
- 只读80车位场景、运行时全OBB碰撞检查、有限队列和可复现六车演示。

## 3D 相机识别与上传演示

- **3D 仿真主流程**：前端从 manifest `props.lpr_cameras` 的入口/出口相机离屏渲染 1280×720 PNG（瞄准车辆前牌），multipart 上传到 `/sim/events/arrived-frame`（vehicle_id/gate/expected_plate/sim_ts/image），后端保存 `data/photos/camera/` 并走统一识别；识别输入卡片显示相机截图/裁剪/conf/expected/匹配。旧 `arrived(image_path)` 与 `?camrec=0` 为回退路径。
- **expected 先验**：只有 `source=sim_camera` 参与 expected 匹配；识别串规范化后等于 `expected_plate` 且 `conf≥sim_conf_threshold(0.85)` 才自动放行，否则转人工。**仿真自动通过依赖 expected_plate 先验**；上传演示没有 expected，只展示结果。
- **独立集成验证（qa-runtime，GPU 临时实例）**：2 轮×6 车 = 12 进 12 出；24 次识别全部 `source=sim_camera`、expected 匹配 24/24、自动通过 24/24、人工 0、pending 0；12 条记录全部离场、余位 80。相机截图常规 51–60 ms（定稿；早期 66–104）（每轮首车冷启动 581/574 ms），识别平均入口 108 ms/出口 111 ms（定稿重跑；早期 274/294）。证据：`_qa_audit/main_runtime/REPORT.md`、`sim_summary.json`；系统截图：`_qa_audit/main_camera/REPORT.md`、`evidence/system-dashboard.png`、`system-view-*.png`、`camera-frames/`。
- **上传演示验证（qa-runtime）**：65 张真实图片（EasyPR 40 + treeandgrass 25）：HTTP 65/65、有结果 64/65=98.5%、自动通过 63/65=96.9%、EasyPR 有标签整牌一致 38/40=95%、平均约 1.07s；上传前后 records/free/spaces/pending 不变。证据：`_qa_audit/main_runtime/REPORT.md`、`upload_summary.json`；页面证据：`_qa_audit/main_upload/REPORT.md`、`evidence/upload_easypr_result.png`。
- 数据来源：`datasets/demo-lpr` 18 张 MIT 样式图用于贴图/历史照片（仅演示）；EasyPR（GDSL-0.1 非商业）与 treeandgrass（MIT 仓库）用于上传演示验证与可选字符复核，不进入 3D 主流程；旧合成图库 `datasets/plates` 已删除。
- 可选真实字符复核 `lpr.real_char_verify` 默认 false，不影响演示主流程；详见 [数据与来源](docs/07-识别数据与复现.md)。

## 验证与构建

```sh
.venv/bin/python tools/ci.py                 # 非浏览器 CI：pytest + Node + 3D 审计
.venv/bin/python tools/ci.py --bench         # 追加 100k 统计基准（阈值显式配置，默认 2.0s）
# 可选真实浏览器测试：先到 tools/e2e 运行 npm ci，再 npx playwright install chromium
.venv/bin/python tools/ci.py --browser --soak-seconds 600
.venv/bin/python tools/ci.py --browser --browser-if-available   # 无浏览器依赖时跳过该项
.venv/bin/python tools/verify_live_sync.py
```
Windows 使用相应 Python 路径，Node 需已安装（CI 可用 `PARKING_NODE_BIN` 或 `--node` 指定；默认自动解析 PATH/常见安装位置）。若本机安装的浏览器版本与 Playwright 默认不一致，显式设置 `CHROMIUM_PATH`。CI 每个浏览器场景创建独立临时数据库，不使用主演示库。具体矩阵见 [测试说明](docs/05-测试结果说明.md)。

CI 退出码：`0`=全部通过（`--browser-if-available` 下依赖缺失而跳过浏览器项也算 0）；`1`=测试/断言失败；`2`=环境/依赖/启动/超时问题。非浏览器单步默认超时 1800s，浏览器单脚本默认 900s；浏览器脚本失败默认重试 1 次，且每次重试用全新临时库与空闲端口（`--browser-retries`/`--server-timeout`/`--browser-timeout` 可调）。基准阈值默认仍为 **2.0s**（功能断言不变，不静默放宽）；平台或慢机需要覆盖时必须显式修改 `tools/bench_limits.json` 或设置 `PARKING_BENCH_MAX_SECONDS`，校准记录见 `../_qa_audit/repair_tasks_20261023/P1-13/REPORT.md`。

Docker、原生打包、旧库审计/迁移见 [跨平台部署](docs/03-跨平台部署教程.md)。识别数据/演示流程见 [数据与来源](docs/07-识别数据与复现.md)。[接口说明](docs/02-类与接口详细说明.md)（新增 `/sim/events/arrived-frame` 以运行 `/docs` 为准）；[OpenAPI 快照](docs/api-openapi.json) 由 `tools/export_openapi.py` 从当前 FastAPI app 生成。安全与运行边界、数据库、测试覆盖、运维、报表口径见 [docs/08](docs/08-安全与运行边界.md)、[docs/09](docs/09-数据库ER与迁移说明.md)、[docs/10](docs/10-测试覆盖矩阵.md)、[docs/11](docs/11-运行维护与故障处置.md)、[docs/12](docs/12-报表与计费口径.md)。

完整审查与历史修复状态见 `../停车场收费系统修复交付审查报告_20261021.md`、`../停车场收费系统修复报告.md`。2026-11-08/11-06 最终证据见 `../_qa_audit/repair_tasks_20261108/`、`../_qa_audit/repair_tasks_20261107/`、`../_qa_audit/repair_tasks_20261106/`（`BACKEND/evidence/full_pytest_after_fix_d.log`、`E2E/REPORT.md`、`RESULTS.json`、`ci_bench_final.log`、`E2E/logs/pytest_final.log`、`ADMIN/`、`SIM/`）。当前后端 pytest **454 passed**（2026-11-08 报表双语后；`_qa_audit/repair_tasks_20261108/BACKEND/evidence/full_pytest_after_i18n.log`）；CI/bench **exit0 7/7（基准为 2026-11-06：pytest 428、bench year 1.3596s<2.0）**；Node sim **95/95**、admin **30/30**；既有 23 E2E **23/23 PASS**；P1/P2/P3 专项全 PASS。旧 446/437/428/419/93/28 等口径均为历史。Windows 3.13 中文路径 283 passed 为历史数据（当时用例较少）。

资产 `assets_3d/**`（含 `plates/` 贴图、`lot_v2.json` 的 `props.lpr_cameras` 与 21 个 static_colliders/manifest hash `593617b6…`）和 `web/sim3d/assets/**` 按当前交付版本使用；固定场景每区最多20位，C25/自定义坐标仍 409，C25 预演/能力字段已删除（**P2-07 C25 仍暂缓/部分（需求降级）**，当前能力 80 位/每区20）。SQLite采用单服务进程；仿真每个会话仅一个调度所有者。3D 视觉尺寸按 GLB Box3 参与排队/跟车（OBB 碰撞语义不变，P2-17）；R-04 为视觉-only 偏移修复（视觉越墙 0.1097→0，OBB 始终 0）。Windows原生运行、中端GPU帧率/显存、两小时RSS检查仍有验收限制；P3-06 loopback RTSP 已实测、公网/鉴权未测；root E2E **23/23 PASS**（1173.5s，exit0）；P3-04/P3-03/P3-02 仍暂缓；**PR-2 不属于本轮演示/上传验收口径，不声称**，详见独立报告。







