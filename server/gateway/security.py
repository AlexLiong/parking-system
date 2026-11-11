"""HTTP identity/origin enforcement and request-scoped SQLAlchemy cleanup."""
from http.cookies import SimpleCookie
from uuid import uuid4

from starlette.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..core.errors import AuthError
from ..repository.db import request_scope
from .auth import validate_token

PUBLIC = {'/api/auth/login', '/api/health', '/api/public-config',
          '/api/auth/demo-session',
          # N2：报表下载公开（浏览器直链下载，与旧 export 行为一致）
          '/api/reports/export', '/api/reports/export.csv'}


class SecurityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        key = request_scope.set(uuid4().hex)
        container = getattr(scope['app'].state, 'container', None)
        try:
            if scope['type'] == 'http' and container is not None:
                path = scope['path']
                protected = ((path.startswith('/api/') and path not in PUBLIC)
                    or path.startswith('/sim/events/') or path in ('/sim/state', '/sim/scenario')
                    or path.startswith('/media/') or path.startswith('/datasets/'))
                if protected and scope['method'] != 'OPTIONS':
                    headers = {k.decode().lower():v.decode() for k,v in scope.get('headers',[])}
                    authorization = headers.get('authorization', '')
                    if authorization.lower().startswith('bearer '):
                        credential = authorization.split(' ',1)[1]
                    else:
                        cookies = SimpleCookie();cookies.load(headers.get('cookie',''))
                        credential = cookies['parking_session'].value if 'parking_session' in cookies else ''
                    try:
                        claims = await run_in_threadpool(validate_token, credential, container)
                    except (AuthError, ValueError):
                        return await JSONResponse({'error':'unauthorized','message':'请先登录'},status_code=401)(scope,receive,send)
                    if claims.get('role') not in ('admin','operator'):
                        return await JSONResponse({'error':'forbidden'},status_code=403)(scope,receive,send)
                    origin = headers.get('origin')
                    host_origin = f"{scope.get('scheme','http')}://{headers.get('host','')}"
                    if origin and origin != host_origin and origin not in container.cfg.server.cors_origins:
                        return await JSONResponse({'error':'origin_denied'},status_code=403)(scope,receive,send)
                    admin_only = (path.startswith('/api/admin/layout') or path.startswith('/api/lists')
                        or (path.startswith('/api/fee-rules') and scope['method'] != 'GET')
                        or path == '/api/barrier/actions/export'
                        or (path.startswith('/api/admin/scenario') and scope['method'] != 'GET')
                        or path in ('/api/demo/reset', '/api/demo/session'))
                    if admin_only and claims['role'] != 'admin':
                        # 3D 演示端使用 simulator 身份做清场/重置；普通 operator 账号不允许。
                        if not (path == '/api/demo/reset' and claims.get('sub') == 'simulator'):
                            return await JSONResponse({'error':'forbidden'},status_code=403)(scope,receive,send)
            await self.app(scope,receive,send)
        finally:
            if container is not None:
                container.db.remove()
            request_scope.reset(key)
