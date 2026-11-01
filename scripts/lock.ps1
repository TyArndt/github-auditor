# Re-resolve all dependencies to their latest versions, block disallowed packages,
# and scan for known vulnerabilities.
# Usage (from repo root, venv active):  .\scripts\lock.ps1
$ErrorActionPreference = 'Stop'

python -m piptools compile --upgrade --generate-hashes --strip-extras --quiet `
    -o requirements.txt pyproject.toml
python -m piptools compile --upgrade --generate-hashes --strip-extras --allow-unsafe --quiet `
    --extra dev -o requirements-dev.txt pyproject.toml

# Fail if any blocked package (blocked-packages.txt) got in, even transitively.
$blocked = Get-Content blocked-packages.txt |
    Where-Object { $_ -notmatch '^\s*(#|$)' } |
    ForEach-Object { ($_ -split '\s+')[0] }
$pattern = '^(' + ($blocked -join '|') + ')=='
$hits = Select-String -Path requirements.txt, requirements-dev.txt -Pattern $pattern
if ($hits) {
    $hits | ForEach-Object { Write-Host "BLOCKED: $($_.Line) in $($_.Filename)" -ForegroundColor Red }
    Write-Host "Find what pulls it in: look for '# via' under it in the requirements file." -ForegroundColor Red
    exit 1
}

python -m pip_audit --strict --require-hashes --disable-pip -r requirements.txt
python -m pip_audit --strict --require-hashes --disable-pip -r requirements-dev.txt

Write-Host "Lock files updated and audited. Run: pip-sync requirements-dev.txt; pip install --no-deps -e ."
