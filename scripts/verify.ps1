#requires -Version 5.1
<#
  One command does two things:
    1. runs the whole unit test suite;
    2. builds a throwaway repo and drives a real task through claim -> gate -> scope rejection.
  Nothing is left behind except .tmp\demo-repo (gitignored).
#>
[CmdletBinding()]
param([switch]$SkipDemo)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $root 'src'
$python = (Get-Command python).Source

Write-Host '== unit tests ==' -ForegroundColor Cyan
Push-Location $root
try {
    & $python -m unittest discover -s (Join-Path $root 'tests') -t $root
    if ($LASTEXITCODE -ne 0) { throw 'unit tests failed' }
}
finally { Pop-Location }

if ($SkipDemo) { Write-Host 'demo skipped' -ForegroundColor Yellow; exit 0 }

Write-Host '== demo: one task, real gates ==' -ForegroundColor Cyan
$demo = Join-Path $root '.tmp\demo-repo'
Remove-Item -Recurse -Force $demo -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path (Join-Path $demo 'src\core') | Out-Null
Set-Content -Path (Join-Path $demo 'src\core\app.py') -NoNewline -Value "def add(a, b):`n    return a + b`n"
git -C $demo init -q -b main
git -C $demo add -A
git -C $demo -c user.name=verify -c user.email=verify@example.com commit -q -m 'chore: init'

function Orch { & $python -m orchestra.cli --repo $demo @args }

Orch init | Out-Null
# In practice a human signs the intent; the file is the signature.
Set-Content -Path (Join-Path $demo 'ledger\intents\I-0001.md') -NoNewline `
    -Value "---`nsigned: true`nauthor: kai`n---`n`n# demo intent: make add order-independent`n"

# Commit what init produced: coordination config is reviewed like any other file.
# Until it is committed, the first gate would see it as an out-of-lease change.
git -C $demo add -A
git -C $demo -c user.name=verify -c user.email=verify@example.com commit -q -m 'chore: orchestra init'

$taskId = (Orch task new --title 'demo fix' --role patch --lineage lineage-a --intent I-0001 --paths src/core --json | ConvertFrom-Json).id
Write-Host "task: $taskId"
Orch task claim --task $taskId --holder agent-1 | Out-Null
Orch task transition --task $taskId --to in_progress --actor agent-1 | Out-Null
git -C $demo checkout -q -b "agent/lineage-a/$taskId"

Set-Content -Path (Join-Path $demo 'src\core\app.py') -NoNewline -Value "def add(a, b):`n    return b + a`n"
git -C $demo add -A
git -C $demo -c user.name=verify -c user.email=verify@example.com commit -q -m "fix: swap order`n`nTask-Id: $taskId"

Orch gate run --task $taskId --stage impl
if ($LASTEXITCODE -ne 0) { throw 'impl gate should pass for an in-lease change' }

Write-Host '-- an out-of-lease change must be rejected --' -ForegroundColor Yellow
New-Item -ItemType Directory -Force -Path (Join-Path $demo 'src\other') | Out-Null
Set-Content -Path (Join-Path $demo 'src\other\x.py') -NoNewline -Value 'x = 1'
git -C $demo add -A
git -C $demo -c user.name=verify -c user.email=verify@example.com commit -q -m "chore: out of scope`n`nTask-Id: $taskId"
Orch gate run --task $taskId --stage impl
if ($LASTEXITCODE -ne 1) { throw 'scope guard must reject an out-of-lease change with exit 1' }

Write-Host '-- admission control: a claim beyond the envelope exits 5 and leaks nothing --' -ForegroundColor Yellow
Orch budget set --scope intent:I-0001 --ceiling 0.5 | Out-Null
$secondId = (Orch task new --title 'second task' --paths src/other --intent I-0001 --json | ConvertFrom-Json).id
Orch task claim --task $secondId --holder agent-2
if ($LASTEXITCODE -ne 5) { throw "a claim beyond the envelope must exit 5, got $LASTEXITCODE" }
$leases = (Orch lease list --json | ConvertFrom-Json).leases
if ($leases | Where-Object { $_.task -eq $secondId }) { throw 'a refused claim must not keep a lease' }
Write-Host 'refused with exit 5, no lease left behind' -ForegroundColor Green

Write-Host '-- evidence: JSONL snapshots and trend --' -ForegroundColor Yellow
Orch status --append | Out-Null
Start-Sleep -Milliseconds 100
Orch status --append | Out-Null
Orch trend

Write-Host '-- reference adapter: dry run resolves the brief --' -ForegroundColor Yellow
Orch worktree create --task $secondId | Out-Null
& $python (Join-Path $root 'tools\agent_adapter.py') --repo $demo --task $secondId --dry-run
if ($LASTEXITCODE -ne 0) { throw 'adapter dry run failed' }

Write-Host '== all green: tests, gate, scope guard, admission refusal, metrics, adapter ==' -ForegroundColor Green