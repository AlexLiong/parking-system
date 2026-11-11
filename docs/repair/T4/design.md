# T4 并发/安全方案

基线c4ad9c7。未授权records/overview/识别日志与sim事件均200；旧WS无认证，默认凭据/secret明文，scoped_session无HTTP线程清理。SQLite重复进场/占位仅先读后写。方案采用请求ContextVar作用域会话+ASGI完整生命周期finally remove，外层写scope持有进程RLock、BEGIN IMMEDIATE并在提交后发送outbox；同牌in/同位in/同车位占用牌号部分唯一索引、既有付款唯一约束。迁移先备份，发现冲突报告后中止、不自动删除；独立audit/dry-run脚本。Access修改与支付事件以同一锁序列化，读接口不持写锁。仅单worker承诺，多个独立控制器不部署。

统一HTTP和WS身份检查；除health/login/public-config与显式demo-session外的API/sim事件均需令牌，源路径限制到演示数据和photos，上传大小/类型限制。全部WS强制角色/有效期/Origin，凭据仅子协议传递。默认demo_mode=false，项目课程config显式true；生产无已知管理员，生成本地一次性bootstrap凭据，JWT优先环境变量否则本地0600随机密钥，禁止旧默认密钥。CORS只接受明确同源清单，禁'*'+credentials；登录失败有界限流；日志不输出token/密码。

UI移除密码预填与静态SIM_ADMIN，demo模式后端签临时模拟终端会话，关闭模式需管理端正常登录后复用同源token。报表链接/图片使用有权限的浏览器会话或Bearer。新增会话登出撤销jti（撤销jti持久落库，重启仍有效）；权限矩阵operator允许业务控制，admin允许配置/名单/费率/日志导出。

独立提交；白名单内改动，所有迁移备份/索引回退。平台密钥不触碰；凭据写本项目data受限文件。定向/20及50并发/全量/浏览器后闭环。
