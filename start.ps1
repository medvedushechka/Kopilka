$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
Set-Location $ProjectRoot
. (Join-Path $ProjectRoot "load-env.ps1")

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Окружение не найдено. Сначала выполните .\setup.ps1"
}

& $Python run.py
