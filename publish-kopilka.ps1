$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

$RepositoryUrl = "https://github.com/medvedushechka/Kopilka.git"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git не найден. Установите Git for Windows и повторите запуск."
}

$requiredFiles = @(
    "README.md",
    "requirements.txt",
    "run.py",
    "app\templates\base.html",
    "app\static\css\style.css",
    "tests\test_regressions.py",
    "docs\screenshots\index.webp"
)

foreach ($file in $requiredFiles) {
    if (-not (Test-Path (Join-Path $ProjectRoot $file))) {
        throw "Не найден обязательный файл: $file. Запускайте скрипт из распакованной GitHub Ready версии проекта."
    }
}

Write-Host "[Kopilka] Проверка локальных файлов..." -ForegroundColor Cyan

$forbidden = @(
    ".venv",
    "__pycache__",
    "instance",
    "app.db",
    "kopilka.db",
    ".payload",
    ".bootstrap"
)

foreach ($item in $forbidden) {
    if (Test-Path (Join-Path $ProjectRoot $item)) {
        throw "В проекте найден лишний локальный объект: $item. Удалите его перед публикацией."
    }
}

if (-not (Test-Path (Join-Path $ProjectRoot ".git"))) {
    git init
    if ($LASTEXITCODE -ne 0) {
        throw "Не удалось инициализировать Git-репозиторий."
    }
}

git branch -M main
if ($LASTEXITCODE -ne 0) {
    throw "Не удалось подготовить ветку main."
}

$remoteNames = @(git remote)
if ($remoteNames -contains "origin") {
    git remote set-url origin $RepositoryUrl
}
else {
    git remote add origin $RepositoryUrl
}

if ($LASTEXITCODE -ne 0) {
    throw "Не удалось настроить удалённый репозиторий origin."
}

git add -A
if ($LASTEXITCODE -ne 0) {
    throw "Не удалось добавить файлы в индекс Git."
}

$hasChanges = git status --porcelain
if ($hasChanges) {
    git commit -m "Подготовлен проект Kopilka"
    if ($LASTEXITCODE -ne 0) {
        throw "Не удалось создать коммит. Проверьте настройки git user.name и user.email."
    }
}
else {
    Write-Host "[Kopilka] Новых локальных изменений нет." -ForegroundColor Yellow
}

Write-Host "[Kopilka] Публикация чистой ветки main..." -ForegroundColor Cyan
git push -u origin main --force
if ($LASTEXITCODE -ne 0) {
    throw "GitHub отклонил отправку. Завершите авторизацию в открывшемся окне и запустите скрипт повторно."
}

Write-Host ""
Write-Host "[Kopilka] Проект опубликован:" -ForegroundColor Green
Write-Host "https://github.com/medvedushechka/Kopilka"
