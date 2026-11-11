"""PyInstaller native launcher; build separately on each target OS/architecture."""
from __future__ import annotations
import argparse,os,sys
from pathlib import Path

def main():
    root=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[1]
    os.chdir(root)
    # Native delivery defaults to production; the source launcher keeps its
    # explicit course configuration. Set this before importing any app module.
    if not os.environ.get('PARKING_CONFIG'):
        os.environ['PARKING_CONFIG']=str(root/'config.production.yaml')
    parser=argparse.ArgumentParser();parser.add_argument('--host',default='127.0.0.1');parser.add_argument('--port',type=int,default=8000);parser.add_argument('--check',action='store_true');args=parser.parse_args()
    from tools.preflight import check
    print(check(),flush=True)
    if args.check:return
    import uvicorn
    from server.main import app
    uvicorn.run(app,host=args.host,port=args.port,workers=1)
if __name__=='__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    main()
