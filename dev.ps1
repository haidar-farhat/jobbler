<#
.SYNOPSIS
    Task runner for LocalApply. Run from the repo root.

.DESCRIPTION
    Wraps the venv interpreter so tools are always invoked as
    `.venv\Scripts\python.exe -m <tool>`. That needs no activation, no PATH entry, and is
    unaffected by PowerShell's execution policy -- the three things that make a half-set-up
    shell install packages into the global interpreter and then fail to find them.

.EXAMPLE
    .\dev.ps1 setup      # containers, venv, deps, chromium, schema, seed
    .\dev.ps1 api        # start the API on :8000
    .\dev.ps1 test       # full suite
    .\dev.ps1 status     # what is running
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('setup', 'up', 'down', 'api', 'test', 'unit', 'lint', 'seed',
                 'migrate', 'status', 'psql', 'reset-runs')]
    [string]$Task = 'status',

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = 'Stop'
$Root    = $PSScriptRoot
$Api     = Join-Path $Root 'services\api'
$Py      = Join-Path $Api  '.venv\Scripts\python.exe'
$Compose = Join-Path $Root 'infrastructure\docker\docker-compose.yml'

function Assert-Venv {
    if (-not (Test-Path $Py)) {
        throw "No virtualenv at $Py. Run: .\dev.ps1 setup"
    }
}

function Invoke-Py {
    param([string[]]$Arguments, [string]$In = $Api)
    Push-Location $In
    try {
        & $Py @Arguments
        if ($LASTEXITCODE -ne 0) { throw "python $($Arguments -join ' ') exited $LASTEXITCODE" }
    } finally { Pop-Location }
}

function Wait-Postgres {
    foreach ($i in 1..60) {
        $state = docker inspect --format='{{.State.Health.Status}}' localapply-postgres 2>$null
        if ($state -eq 'healthy') { Write-Host "postgres healthy" -ForegroundColor Green; return }
        Start-Sleep -Seconds 1
    }
    throw "postgres did not become healthy within 60s"
}

switch ($Task) {

    'setup' {
        Write-Host "`n[1/5] containers" -ForegroundColor Cyan
        docker compose -f $Compose up -d
        Wait-Postgres

        Write-Host "`n[2/5] virtualenv + dependencies" -ForegroundColor Cyan
        if (-not (Test-Path $Py)) { python -m venv (Join-Path $Api '.venv') }
        Invoke-Py @('-m', 'pip', 'install', '--upgrade', 'pip', '-q')
        Invoke-Py @('-m', 'pip', 'install', '-e', '.[dev]', '-q')

        Write-Host "`n[3/5] chromium" -ForegroundColor Cyan
        Invoke-Py @('-m', 'playwright', 'install', 'chromium')

        Write-Host "`n[4/5] schema" -ForegroundColor Cyan
        $envFile = Join-Path $Root '.env'
        if (-not (Test-Path $envFile)) {
            Copy-Item (Join-Path $Root '.env.example') $envFile
            Write-Host "  created .env"
        }
        # Autogenerate the first revision only when none exists yet.
        $versions = Join-Path $Api 'migrations\versions'
        if (-not (Get-ChildItem $versions -Filter '*.py' -ErrorAction SilentlyContinue)) {
            Invoke-Py @('-m', 'alembic', 'revision', '--autogenerate', '-m', 'initial schema')
        }
        Invoke-Py @('-m', 'alembic', 'upgrade', 'head')

        Write-Host "`n[5/5] profile" -ForegroundColor Cyan
        Invoke-Py @('scripts\dev_bootstrap.py')

        Write-Host "`nReady. Next: .\dev.ps1 api" -ForegroundColor Green
    }

    'up'      { docker compose -f $Compose up -d; Wait-Postgres }
    'down'    { docker compose -f $Compose down }

    'api' {
        # NOT --reload. On Windows uvicorn's reload mode runs on a SelectorEventLoop, which
        # cannot spawn subprocesses, so Playwright cannot start its driver and every run
        # fails with a bare NotImplementedError. Restart manually after code changes.
        Assert-Venv
        Invoke-Py (@('-m','uvicorn','localapply.main:app','--port','8000') + $Rest)
    }
    'seed'    { Assert-Venv; Invoke-Py @('scripts\dev_bootstrap.py') }
    'migrate' { Assert-Venv; Invoke-Py (@('-m','alembic') + $(if ($Rest) { $Rest } else { @('upgrade','head') })) }

    'test'    { Assert-Venv; Invoke-Py (@('-m','pytest') + $Rest) -In $Root }
    'unit'    { Assert-Venv; Invoke-Py (@('-m','pytest','-m','not browser') + $Rest) -In $Root }
    'lint'    { Assert-Venv; Invoke-Py @('-m','ruff','check','localapply') }

    'psql'       { docker exec -it localapply-postgres psql -U localapply -d localapply @Rest }
    'reset-runs' {
        docker exec localapply-postgres psql -U localapply -d localapply -c `
            "TRUNCATE agent_events, browser_actions, screenshots, browser_sessions, approvals, audit_logs, agent_runs, applications, jobs CASCADE;"
        Write-Host "run history cleared; profile kept" -ForegroundColor Green
    }

    'status' {
        Write-Host "`ncontainers" -ForegroundColor Cyan
        docker compose -f $Compose ps --format "  {{.Name}}  {{.Status}}"

        Write-Host "`ntoolchain" -ForegroundColor Cyan
        Write-Host ("  venv    " + $(if (Test-Path $Py) { "ok" } else { "MISSING - run .\dev.ps1 setup" }))
        $node = Get-Command node -ErrorAction SilentlyContinue
        Write-Host ("  node    " + $(if ($node) { node -v } else { "MISSING - dashboard only" }))

        Write-Host "`napi" -ForegroundColor Cyan
        try {
            $h = Invoke-RestMethod -Uri 'http://localhost:8000/health' -TimeoutSec 2
            Write-Host "  status     $($h.status)"
            Write-Host "  dry_run    $($h.safety.dry_run)"
            Write-Host "  kill switch $(if ($h.safety.kill_switch.engaged) { 'ENGAGED' } else { 'armed' })"
        } catch {
            Write-Host "  not running - start with .\dev.ps1 api"
        }
        Write-Host ""
    }
}
