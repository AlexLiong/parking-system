# Build from parking-system using the active target-platform Python.
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules,collect_data_files
root=Path(SPECPATH)
datas=[]
for name in ['web','server/lpr/models','datasets/demo-lpr','datasets/plates']:
    datas.append((str(root/name),name))
datas += [(str(root/'config.yaml'),'.'),(str(root/'config.production.yaml'),'.'),(str(root/'hyperlpr3/LICENSE'),'hyperlpr3'),(str(root/'hyperlpr3/NOTICE.md'),'hyperlpr3')]
hidden=collect_submodules('server')+collect_submodules('hyperlpr3')+collect_submodules('tools.preflight')
a=Analysis([str(root/'tools/frozen_entry.py')],pathex=[str(root)],datas=datas,hiddenimports=hidden,excludes=['torch','MNN','pytest','tkinter'])
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,[],exclude_binaries=True,name='parking',console=True,contents_directory='.')
coll=COLLECT(exe,a.binaries,a.datas,strip=False,upx=False,name='parking-system')
