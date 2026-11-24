#requires -Version 5.1
# Run the bundled test suite against the bundled engine.
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$env:PYTHONPATH = Join-Path $root 'src'
$python = (Get-Command python).Source
Push-Location $root
try {
    & $python -m unittest discover -s (Join-Path $root 'tests') -t $root
    if ($LASTEXITCODE -ne 0) { throw 'tests failed' }
    Write-Host 'bundle is healthy' -ForegroundColor Green
}
finally { Pop-Location }