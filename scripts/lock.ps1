# Re-resolve all dependencies to their latest versions and scan for known vulnerabilities.
# Usage (from repo root, venv active):  .\scripts\lock.ps1
$ErrorActionPreference = 'Stop'

python -m piptools compile --upgrade --generate-hashes --strip-extras --quiet `
    -o requirements.txt pyproject.toml
python -m piptools compile --upgrade --generate-hashes --strip-extras --allow-unsafe --quiet `
    --extra dev -o requirements-dev.txt pyproject.toml

python -m pip_audit --strict --require-hashes --disable-pip -r requirements.txt
python -m pip_audit --strict --require-hashes --disable-pip -r requirements-dev.txt

Write-Host "Lock files updated and audited. Run: pip-sync requirements-dev.txt; pip install --no-deps -e ."
