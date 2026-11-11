"""Build a platform-specific onedir bundle, copying unmodified sibling assets."""
import argparse,subprocess,sys,shutil
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,default=ROOT.parent/'native-dist');args=p.parse_args()
    out=args.output.resolve()
    if out.exists():p.error('输出目录必须不存在，避免覆盖现有交付')
    subprocess.run([sys.executable,'tools/preflight.py'],cwd=ROOT,check=True)
    subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--distpath',str(out),'--workpath',str(ROOT/'build/pyinstaller'),'parking.spec'],cwd=ROOT,check=True)
    shutil.copytree(ROOT.parent/'assets_3d',out/'assets_3d')
    print(out)
