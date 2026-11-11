# T4 契约

POST auth/login正常Bearer，新增auth/logout撤销当前令牌；GET api/public-config只返回demo_mode/timezone；POST auth/demo-session仅显式demo模式可用，否则403。读取与sim事件需Bearer或登录时HttpOnly SameSite Strict cookie（供同源图片/PDF），令牌不可通过URL。WS协议v2或legacy + token.<JWT>，无token1008、非白名单Origin1008，过期关闭。Unauthorized401、角色403、失败过多429；登录cookie按HTTPS secure设置。

server.demo_mode默认false，config.yaml为课程显式true；PARKING_JWT_SECRET/PARKING_ADMIN_PASSWORD覆盖，生产弱默认不被使用。server.cors_origins为明确白名单。数据库索引失败不启服务，先tools/audit_consistency.py DB --out JSON，人工处理冲突后重启；脚本默认只读。
