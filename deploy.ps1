#requires -Version 5.1
<#
.SYNOPSIS
    CD-скрипт PartyManagementSystem (Windows / PowerShell).

.DESCRIPTION
    Порядок действий:
      1. git fetch / checkout / pull --ff-only нужной ветки;
      2. docker compose build;
      3. проверки (gate, останавливает деплой при ошибке):
           - python manage.py check
           - python manage.py makemigrations
           - python manage.py collectstatic --noinput
      4. применение миграций (default + archive);
      5. docker compose up -d --build — запуск web / scheduler / worker;
      6. пост-проверка: python manage.py check в running-контейнере.

.PARAMETER Branch
    Ветка для деплоя. По умолчанию — текущая ветка.

.PARAMETER CheckOnly
    Только проверки (git pull + build + gate), без миграций и запуска.

.PARAMETER NoPull
    Не выполнять git fetch/checkout/pull.

.PARAMETER NoBuild
    Не пересобирать образ.

.EXAMPLE
    .\deploy.ps1
    .\deploy.ps1 -Branch main
    .\deploy.ps1 -Branch main -CheckOnly
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Branch,

    [switch]$CheckOnly,
    [switch]$NoPull,
    [switch]$NoBuild,

    [string]$Service = 'web',
    [string]$DbService = 'db',
    [int]$HealthTimeout = 90
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = [System.Text.Encoding]::UTF8
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$env:PYTHONIOENCODING = 'utf-8'

Set-Location -LiteralPath $PSScriptRoot

function Write-Log  { param([string]$Message) Write-Host "[deploy] $Message" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Message) Write-Host "[  ok  ] $Message" -ForegroundColor Green }
function Write-Warn { param([string]$Message) Write-Host "[ warn ] $Message" -ForegroundColor Yellow }
function Fail       { param([string]$Message) Write-Host "[ fail ] $Message" -ForegroundColor Red; exit 1 }

function Assert-Exit {
    param([string]$Step)
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ fail ] $Step (exit code $LASTEXITCODE)" -ForegroundColor Red
        exit 1
    }
}

function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    & git @Args
    Assert-Exit "git $($Args -join ' ')"
}

# --- 0. Проверка окружения -------------------------------------------------
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail 'git не найден в PATH' }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail 'docker не найден в PATH' }

& docker compose version *> $null
Assert-Exit 'docker compose version'

& git rev-parse --git-dir *> $null
if ($LASTEXITCODE -ne 0) { Fail "Каталог $PSScriptRoot не является git-репозиторием" }

if ([string]::IsNullOrWhiteSpace($Branch)) {
    $Branch = (& git rev-parse --abbrev-ref HEAD).Trim()
}
if ($Branch -eq 'HEAD') { Fail 'Обнаружен detached HEAD. Укажите ветку: .\deploy.ps1 -Branch <branch>' }

Write-Log "Ветка деплоя: $Branch"

# --- 1. Обновление кода ----------------------------------------------------
if (-not $NoPull) {
    $dirty = & git status --porcelain --untracked-files=no
    if ($dirty) {
        Write-Warn 'Есть незакоммиченные изменения tracked-файлов:'
        $dirty | ForEach-Object { Write-Host "        $_" }
        Write-Warn 'Продолжаю, но git pull может упасть.'
    }

    Write-Log "git fetch origin $Branch"
    Invoke-Git fetch origin $Branch

    $current = (& git rev-parse --abbrev-ref HEAD).Trim()
    if ($current -ne $Branch) {
        Write-Log "git checkout $Branch"
        Invoke-Git checkout $Branch
    }

    Write-Log "git pull --ff-only origin $Branch"
    Invoke-Git pull --ff-only origin $Branch

    $sha = (& git rev-parse --short HEAD).Trim()
    $subject = (& git log -1 --pretty=%s).Trim()
    Write-Ok "Код обновлён: $sha ($subject)"
} else {
    Write-Warn 'Пропускаю git pull (-NoPull)'
}

# --- 2. Сборка образа ------------------------------------------------------
if (-not $NoBuild) {
    Write-Log "docker compose build $Service"
    & docker compose build $Service
    Assert-Exit 'docker compose build'
    Write-Ok 'Образ собран'
} else {
    Write-Warn 'Пропускаю сборку (-NoBuild)'
}

# --- 3. Запуск БД и ожидание готовности ------------------------------------
Write-Log "Запуск сервиса БД '$DbService'"
& docker compose up -d $DbService
Assert-Exit 'docker compose up db'

Write-Log "Ожидание готовности БД (до ${HealthTimeout}s)"
$dbCid = (& docker compose ps -q $DbService).Trim()
if ([string]::IsNullOrWhiteSpace($dbCid)) { Fail "Не удалось найти контейнер сервиса '$DbService'" }

$waited = 0
while ($true) {
    $health = (& docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' $dbCid 2>$null).Trim()
    if ($health -eq 'healthy' -or $health -eq 'running') {
        Write-Ok "БД готова ($health)"
        break
    }
    if ($health -eq 'unhealthy') { Fail 'БД перешла в состояние unhealthy' }
    if ($waited -ge $HealthTimeout) { Fail "БД не готова за ${HealthTimeout}s (status: $health)" }
    Start-Sleep -Seconds 2
    $waited += 2
}

# --- 4. Gate: проверка проекта --------------------------------------------
Write-Log 'Проверки проекта (gate)'

Write-Log '>>> python manage.py check'
& docker compose run --rm $Service python manage.py check
Assert-Exit 'manage.py check'

Write-Log '>>> python manage.py makemigrations'
& docker compose run --rm $Service python manage.py makemigrations
Assert-Exit 'manage.py makemigrations'

Write-Log '>>> python manage.py collectstatic --noinput'
& docker compose run --rm $Service python manage.py collectstatic --noinput
Assert-Exit 'manage.py collectstatic'

Write-Ok 'Проверки пройдены (check / makemigrations / collectstatic)'

# --- 5. Применение миграций ------------------------------------------------
if (-not $CheckOnly) {
    Write-Log 'Применение миграций'

    Write-Log '>>> python manage.py migrate'
    & docker compose run --rm $Service python manage.py migrate
    Assert-Exit 'manage.py migrate'

    Write-Log '>>> python manage.py migrate --database archive'
    & docker compose run --rm $Service python manage.py migrate --database archive
    Assert-Exit 'manage.py migrate --database archive'

    Write-Ok 'Миграции применены'
} else {
    Write-Warn 'Пропускаю миграции и запуск (-CheckOnly)'
}

# --- 6. Запуск сервисов ----------------------------------------------------
if (-not $CheckOnly) {
    Write-Log 'docker compose up -d --build'
    & docker compose up -d --build
    Assert-Exit 'docker compose up'
    Write-Ok 'Сервисы запущены'

    Write-Log "Ожидание запуска контейнера '$Service'"
    $waited = 0
    while ($true) {
        $webCid = (& docker compose ps -q $Service).Trim()
        $running = (& docker inspect -f '{{.State.Running}}' $webCid 2>$null).Trim()
        if ($running -eq 'true') { break }
        if ($waited -ge $HealthTimeout) { Fail "Контейнер '$Service' не запустился за ${HealthTimeout}s" }
        Start-Sleep -Seconds 2
        $waited += 2
    }

    Write-Log 'Пост-проверка running-контейнера'
    & docker compose exec -T $Service python manage.py check
    Assert-Exit 'post-up manage.py check'
    Write-Ok 'Пост-проверка пройдена'
}

Write-Host ''
$sha = (& git rev-parse --short HEAD).Trim()
Write-Ok "Деплой завершён: ветка $Branch, коммит $sha"
if (-not $CheckOnly) {
    & docker compose ps
}
