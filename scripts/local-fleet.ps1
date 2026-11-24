<#
.SYNOPSIS
    Run N local CLI agents against one orchestra repository.

.DESCRIPTION
    Starts N independent `orchestra loop` supervisors in the background, each with
    its own holder name and its own agent process. The ledger, the SQLite lease
    file and the git claims are the only things they share; a worker that dies
    takes nothing else down.

    Every worker calls scripts/agent_run.py, which turns the task brief into a
    prompt for your CLI agent and runs it inside the task worktree. The
    supervisor then runs the impl gate; a passing round lands in `review`, never
    in `main`.

.EXAMPLE
    .\scripts\local-fleet.ps1 -Agents 3 -Rounds 2

.EXAMPLE
    .\scripts\local-fleet.ps1 -Status

.EXAMPLE
    .\scripts\local-fleet.ps1 -Stop

.EXAMPLE
    .\scripts\local-fleet.ps1 -Agents 2 -Agent opencode -Model opencode-go/deepseek-v4-pro
#>
[CmdletBinding()]
param(
    [int]$Agents = 3,
    [int]$Rounds = 1,
    [string]$Agent = "",
    [string]$Model = "",
    [string]$Approval = "auto_edit",
    [int]$Timeout = 1800,
    [string]$Repo = "",
    [switch]$Status,
    [switch]$Stop,
    [switch]$Logs
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Repo) { $Repo = (Resolve-Path (Join-Path $scriptDir "..")).Path }
$Repo = (Resolve-Path $Repo).Path

$python = (Get-Command python).Source
$adapter = Join-Path $scriptDir "agent_run.py"
$logDir = Join-Path $Repo ".orchestra\logs"
$stateFile = Join-Path $Repo ".orchestra\fleet.json"
# The orchestra package lives next to this script, not inside the target repo.
$orchSrc = Join-Path (Split-Path -Parent $scriptDir) "src"
if (Test-Path (Join-Path $orchSrc "orchestra")) { $env:PYTHONPATH = $orchSrc }
& $python -c "import orchestra" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Error "cannot import the orchestra package - point PYTHONPATH at the directory holding it"
}

function Invoke-Orchestra {
    param([string[]]$Arguments)
    & $python -m orchestra.cli --repo $Repo @Arguments
}

function Resolve-Agent {
    param([string]$Requested)
    if ($Requested) { return $Requested }
    $known = Join-Path $env:LOCALAPPDATA "Programs\ai.opencode.desktop\resources\opencode-cli.exe"
    if (Test-Path $known) { return $known }
    foreach ($name in @("opencode", "claude", "codex", "gemini")) {
        $found = Get-Command $name -ErrorAction SilentlyContinue
        if ($found) { return $name }
    }
    Write-Error "no CLI agent found - pass -Agent <path to opencode/claude/codex>"
}

if (-not (Test-Path (Join-Path $Repo ".git"))) {
    Write-Error "$Repo is not a git repository - run 'orchestra init' inside the project first."
}

if ($Status) {
    "=== status ==="
    Invoke-Orchestra @("status")
    ""
    "=== tasks ==="
    Invoke-Orchestra @("task", "list")
    ""
    "=== leases ==="
    Invoke-Orchestra @("lease", "list")
    if (Test-Path $stateFile) {
        ""
        "=== workers ==="
        $state = Get-Content $stateFile -Raw | ConvertFrom-Json
        foreach ($worker in $state.workers) {
            $alive = Get-Process -Id $worker.pid -ErrorAction SilentlyContinue
            $verdict = if ($alive) { "running" } else { "exited" }
            "{0,-12} pid {1,-7} {2,-8} log {3}" -f $worker.holder, $worker.pid, $verdict, $worker.log
        }
    }
    exit 0
}

if ($Stop) {
    Invoke-Orchestra @("stop")
    "Kill switch written. Workers finish the round they are in, then exit."
    if (Test-Path $stateFile) {
        $state = Get-Content $stateFile -Raw | ConvertFrom-Json
        foreach ($worker in $state.workers) {
            $alive = Get-Process -Id $worker.pid -ErrorAction SilentlyContinue
            if ($alive) { "  {0}: pid {1} still in a round" -f $worker.holder, $worker.pid }
        }
    }
    exit 0
}

if ($Logs) {
    if (-not (Test-Path $logDir)) { "no logs yet: $logDir"; exit 0 }
    Get-ChildItem $logDir -Filter "worker-*.log" | ForEach-Object {
        "=== $($_.Name) ==="
        Get-Content $_.FullName -Tail 15
        ""
    }
    exit 0
}

# --- start ---------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$Agent = Resolve-Agent $Agent

$agentCmd = "$python `"$adapter`" {brief} --agent `"$Agent`" --approval $Approval"
if ($Model) { $agentCmd = "$agentCmd -m $Model" }

$workers = @()
for ($i = 1; $i -le $Agents; $i++) {
    $holder = "worker-$i"
    $log = Join-Path $logDir "$holder.log"
    $errLog = Join-Path $logDir "$holder.err.log"
    $argLine = "-m orchestra.cli --repo `"$Repo`" loop --max-rounds $Rounds --holder $holder --timeout $Timeout --agent-cmd `"$agentCmd`""
    $proc = Start-Process -FilePath $python -ArgumentList $argLine -WorkingDirectory $Repo `
        -RedirectStandardOutput $log -RedirectStandardError $errLog `
        -WindowStyle Hidden -PassThru
    $workers += [pscustomobject]@{ holder = $holder; pid = $proc.Id; log = $log; rounds = $Rounds }
    "started {0,-10} pid {1,-7} max-rounds {2}" -f $holder, $proc.Id, $Rounds
}

[pscustomobject]@{ repo = $Repo; agent = $Agent; started = (Get-Date).ToString("s"); workers = $workers } |
    ConvertTo-Json -Depth 5 | Set-Content -Path $stateFile -Encoding utf8

""
"agent:   $Agent"
"Watch:   Get-Content '$logDir\worker-1.log' -Wait"
"State:   .\scripts\local-fleet.ps1 -Status"
"Stop:    .\scripts\local-fleet.ps1 -Stop"
