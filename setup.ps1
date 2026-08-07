$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
Set-Location $ProjectRoot
. (Join-Path $ProjectRoot "load-env.ps1")

function Test-PythonCandidate {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,

        [string[]]$Arguments = @()
    )

    $PreviousPreference = $ErrorActionPreference
    try {
        # Python Launcher пишет сообщение об отсутствующей версии в stderr.
        # На Windows PowerShell 5.1 при ErrorActionPreference=Stop это раньше
        # прерывало цикл до проверки остальных установленных версий.
        $ErrorActionPreference = "SilentlyContinue"
        & $Executable @Arguments -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) else 1)" *> $null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
    finally {
        $ErrorActionPreference = $PreviousPreference
    }
}

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [Parameter(Mandatory = $true)]
        [string]$FailureMessage
    )

    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw $FailureMessage
    }
}

$Candidates = @()

$PyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if ($PyLauncher) {
    foreach ($Version in @("-3.13", "-3.12", "-3.11")) {
        $Candidates += [PSCustomObject]@{
            Executable = $PyLauncher.Source
            Arguments  = @($Version)
        }
    }

    # Последним пробуем версию Python, выбранную лаунчером по умолчанию.
    $Candidates += [PSCustomObject]@{
        Executable = $PyLauncher.Source
        Arguments  = @()
    }
}

foreach ($CommandName in @("python.exe", "python3.exe")) {
    $PythonCommand = Get-Command $CommandName -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($PythonCommand) {
        $Candidates += [PSCustomObject]@{
            Executable = $PythonCommand.Source
            Arguments  = @()
        }
    }
}

$SelectedPython = $null
foreach ($Candidate in $Candidates) {
    if (Test-PythonCandidate -Executable $Candidate.Executable -Arguments $Candidate.Arguments) {
        $SelectedPython = $Candidate
        break
    }
}

if (-not $SelectedPython) {
    throw @"
Не найден совместимый Python 3.11, 3.12 или 3.13.

Проверьте установленные версии:
  py -0p
  python --version

Если Python не установлен, установите Python 3.12 с python.org и включите опции
'Add python.exe to PATH' и 'Install launcher for all users'.
"@
}

$Launcher = $SelectedPython.Executable
$LauncherArguments = @($SelectedPython.Arguments)
$VersionText = & $Launcher @LauncherArguments --version 2>&1
Write-Host "[Kopilka] Используется $VersionText"

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if ((Test-Path ".venv") -and -not (Test-Path $VenvPython)) {
    Write-Host "[Kopilka] Удаляется повреждённое виртуальное окружение."
    Remove-Item ".venv" -Recurse -Force
}

if (-not (Test-Path $VenvPython)) {
    Write-Host "[Kopilka] Создание виртуального окружения..."
    Invoke-CheckedCommand `
        -Executable $Launcher `
        -Arguments ($LauncherArguments + @("-m", "venv", ".venv")) `
        -FailureMessage "Не удалось создать виртуальное окружение."
}

Write-Host "[Kopilka] Обновление pip..."
Invoke-CheckedCommand `
    -Executable $VenvPython `
    -Arguments @("-m", "pip", "install", "--upgrade", "pip") `
    -FailureMessage "Не удалось обновить pip."

Write-Host "[Kopilka] Установка зависимостей..."
Invoke-CheckedCommand `
    -Executable $VenvPython `
    -Arguments @("-m", "pip", "install", "-r", "requirements.txt") `
    -FailureMessage "Не удалось установить зависимости из requirements.txt."

Write-Host "[Kopilka] Создание демонстрационной базы..."
Invoke-CheckedCommand `
    -Executable $VenvPython `
    -Arguments @("-m", "flask", "--app", "run.py", "reset-db") `
    -FailureMessage "Не удалось создать демонстрационную базу."

Write-Host "[Kopilka] Подготовка завершена. Запуск: .\start.ps1"
