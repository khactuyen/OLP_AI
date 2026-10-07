param([switch]$Demo, [int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$appRoot = $PSScriptRoot
$pythonPath = Join-Path $appRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Virtualenv not found. See README.md for setup.'
}
$env:OLP_DEMO = if ($Demo) { '1' } else { '0' }
Set-Location -LiteralPath $appRoot
& $pythonPath -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port $Port --workers 1 --no-access-log
