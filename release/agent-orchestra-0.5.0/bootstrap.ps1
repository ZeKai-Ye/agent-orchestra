#requires -Version 5.1
<#
  Install the engine into a target repository (this file lives in the bundle).

    .\bootstrap.ps1 -Target D:\work\myrepo            # copy engine + init + register handles dir
    .\bootstrap.ps1 -Target D:\work\myrepo -NoInit    # only copy files

  It never commits: you review the diff and commit yourself.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Target,
    [switch]$NoInit
)

$ErrorActionPreference = 'Stop'
$bundle = $PSScriptRoot
$target = (Resolve-Path $Target).Path
$python = (Get-Command python).Source

Copy-Item (Join-Path $bundle 'src\orchestra') (Join-Path $target 'src\orchestra') -Recurse -Force
Copy-Item (Join-Path $bundle 'join.txt') $target -Force
Copy-Item (Join-Path $bundle 'join.json') $target -Force
New-Item -ItemType Directory -Force -Path (Join-Path $target 'handles') | Out-Null

if (-not $NoInit) {
    $env:PYTHONPATH = Join-Path $target 'src'
    & $python -m orchestra.cli --repo $target init | Out-Null
    Write-Host 'initialised: config/orchestra.toml, ledger/, .orchestra/' -ForegroundColor Green
}

Write-Host ''
Write-Host 'next steps:' -ForegroundColor Cyan
Write-Host "  1) review and commit the copied files (config/ must be committed before the first gate)"
Write-Host "  2) sign a direction:        ledger/intents/I-0001.md   (---  signed: true  ---)"
Write-Host "  3) create a task:           PYTHONPATH=src python -m orchestra.cli --repo . task new --title '...' --paths src/core --intent I-0001"
Write-Host "  4) hand join.txt to an agent, or run: PYTHONPATH=src python -m orchestra.cli --repo . loop --once --agent-cmd '...'"