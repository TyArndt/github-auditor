# Run the same checks as CI (lint, format, types, tests). Usage: .\scripts\check.ps1
# Tests that need PostgreSQL are skipped if `docker compose up -d db` isn't running.
$ErrorActionPreference = 'Stop'
$py = if (Test-Path .venv\Scripts\python.exe) { '.venv\Scripts\python.exe' } else { 'python' }

foreach ($step in @(
        @('-m', 'ruff', 'check', '.'),
        @('-m', 'ruff', 'format', '--check', '.'),
        @('-m', 'mypy', 'src', 'tests'),
        @('-m', 'pytest', '-q')
    )) {
    Write-Host "> $($step -join ' ')" -ForegroundColor Cyan
    & $py @step
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
Write-Host "All checks passed." -ForegroundColor Green
