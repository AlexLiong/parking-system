$ErrorActionPreference = 'Stop'
$TaskRoot = Split-Path -Parent $PSScriptRoot
$TaskPython = Join-Path $TaskRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $TaskPython)) { $TaskPython = 'python' }
& $TaskPython (Join-Path $PSScriptRoot 'ci.py') @args
exit $LASTEXITCODE
