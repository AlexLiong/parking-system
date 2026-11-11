$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
Set-Location $PSScriptRoot
python tools/run.py @args
exit $LASTEXITCODE
