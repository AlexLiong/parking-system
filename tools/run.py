#!/usr/bin/env python3
"""Portable source launcher. Keeps existing environments/data and fails on missing resources."""
from __future__ import annotations
import argparse,json,os,subprocess,sys,threading,time,webbrowser
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
# Windows 中文 locale 下 pip/preflight 会按 GBK 解码 UTF-8 文件；统一强制 UTF-8。
os.environ.setdefault("PYTHONUTF8","1")
os.environ.setdefault("PYTHONIOENCODING","utf-8")
MODULES=['fastapi','uvicorn','cv2','sqlalchemy','yaml','reportlab','matplotlib','multipart','jwt','numpy','PIL','onnxruntime','loguru','tzdata','hyperlpr3']

def run(command,**kw):
    print('[run] '+' '.join(map(str,command)),flush=True)
    kw.setdefault('env', {**os.environ, 'PYTHONUTF8':'1', 'PYTHONIOENCODING':'utf-8'})
    return subprocess.run(list(map(str,command)),cwd=ROOT,check=True,**kw)

def main():
    ap=argparse.ArgumentParser(description='停车场收费系统源码启动（Python 3.11–3.14）')
    ap.add_argument('--host',default='127.0.0.1');ap.add_argument('--port',type=int)
    ap.add_argument('--reload',action='store_true');ap.add_argument('--skip-deps',action='store_true')
    ap.add_argument('--compatible-deps',action='store_true',help='使用兼容范围清单代替精确锁')
    ap.add_argument('--venv',type=Path,default=ROOT/'.venv')
    ap.add_argument('--check',action='store_true',help='校验依赖和资源后退出')
    ap.add_argument('--no-browser',action='store_true')
    ap.add_argument('--gen-assets',action='store_true');ap.add_argument('--gen-dataset',action='store_true')
    ap.add_argument('--seed',action='store_true',help='添加演示历史（不清空数据库）')
    ap.add_argument('--reset',action='store_true',
                    help='启动前只清开放记录/运行时状态（保留已完成历史与场景文件）')
    ap.add_argument('--resetdata',action='store_true',
                    help='启动前备份并完整重建数据库+重灌演示数据（保留备份）')
    args=ap.parse_args()
    if sys.version_info<(3,11):ap.error('需要 Python 3.11 或以上；建议使用 3.14')
    folder=args.venv.resolve();py=folder/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    if not py.exists():run([sys.executable,'-m','venv',folder])
    probe='import importlib.util,json;print(json.dumps([m for m in '+repr(MODULES)+' if importlib.util.find_spec(m) is None]))'
    missing=json.loads(run([py,'-c',probe],capture_output=True,text=True).stdout)
    if missing:
        if args.skip_deps:raise RuntimeError('缺少依赖: '+', '.join(missing))
        req='requirements.txt' if args.compatible_deps else 'requirements.lock.txt'
        run([py,'-m','pip','install','--no-input','-r',req])
    run([py,'tools/preflight.py'])
    if args.check:return 0
    if args.reset or args.resetdata:
        run([py,'tools/reset_runtime.py','--resetdata' if args.resetdata else '--reset'])
    for flag,script in [(args.gen_assets,'gen_templates.py'),(args.gen_dataset,'gen_synthetic_plates.py'),(args.seed,'seed_demo.py')]:
        if flag:run([py,'tools/'+script])
    code='from server.core.config import load_config;print(load_config().server.port)'
    port=args.port or int(run([py,'-c',code],capture_output=True,text=True).stdout.strip())
    url=f'http://{args.host if args.host not in ("0.0.0.0","::") else "127.0.0.1"}:{port}'
    if not args.no_browser:
        def browse():
            import urllib.request
            for _ in range(60):
                try:
                    urllib.request.urlopen(url+'/api/health',timeout=1).close();webbrowser.open(url+'/admin/');return
                except OSError:time.sleep(.5)
        threading.Thread(target=browse,daemon=True).start()
    print(f'管理端 {url}/admin/   仿真端 {url}/sim/',flush=True)
    command=[py,'-m','uvicorn','server.main:app','--host',args.host,'--port',str(port)]
    if args.reload:command.append('--reload')
    run(command);return 0
if __name__=='__main__':
    try:raise SystemExit(main())
    except KeyboardInterrupt:raise SystemExit(0)
    except (subprocess.CalledProcessError,RuntimeError) as exc:print(str(exc),file=sys.stderr);raise SystemExit(1)
