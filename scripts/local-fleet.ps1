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

    MODEL IS REQUIRED. A fleet never starts on an implicit default: without
    -Model (or an explicit -Model auto) this script refuses and lists what is
    available. The chosen model is recorded in .orchestra/fleet.json and shown by
    -Status, so "which model is running" is always answerable.

.PARAMETER Model
    Model id passed to the agent (e.g. opencode-go/deepseek-v4-pro). Use
    `-Model auto` to deliberately accept the agent's own default.

.EXAMPLE
    .\scripts\local-fleet.ps1 -Agents 3 -Rounds 2 -Model opencode-go/deepseek-v4-pro

.EXAMPLE
    .\scripts\local-fleet.ps1 -Agents 2 -Model opencode-go/deepseek-v4-pro -DryRun

.EXAMPLE
    .\scripts\local-fleet.ps1 -Status

.EXAMPLE
    .\scripts\local-fleet.ps1 -Stop
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
    [switch]$Yes,
    [switch]$DryRun,
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
    foreach ($name in @("opencode", "claude", "codex")) {
        $found = Get-Command $name -ErrorAction SilentlyContinue
        if ($found) { return $name }
    }
    Write-Error "no CLI agent found - pass -Agent <path to opencode/claude/codex>"
}

function Get-AvailableModels {
    param([string]$AgentPath)
    try {
        $listed = & $AgentPath models 2>$null
    } catch {
        return @()
    }
    if ($LASTEXITCODE -ne 0 -or -not $listed) { return @() }
    return @($listed | Where-Object { $_ -match "/" })
}

function Resolve-Model {
    param([string]$Requested, [string]$AgentPath)
    $available = Get-AvailableModels $AgentPath

    if ($Requested -eq "auto") {
        Write-Warning "-Model auto: the agent default will be used and NOT pinned. Prefer an explicit id for anything you plan to review."
        return ""
    }
    if (-not $Requested) {
        $sample = if ($available.Count -gt 0) {
            ($available | Select-Object -First 12) -join "`n  "
        } else { "(run: `"$AgentPath`" models)" }
        Write-Error @"
-Model is required: a local fleet never starts on an implicit model.

Available (first 12 of $($available.Count)):
  $sample

Pass one of them, e.g.  -Model <id>
Or pass -Model auto if you really mean "whatever the agent defaults to".
"@
    }
    if ($available.Count -gt 0 -and $available -notcontains $Requested) {
        Write-Error "model '$Requested' is not in the list this agent reports ($($available.Count) models). Run: `"$AgentPath`" models"
    }
    return $Requested
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
            "{0,-12} pid {1,-7} {2,-8} model {3,-32} log {4}" -f `
                $worker.holder, $worker.pid, $verdict, $(if ($worker.model) { $worker.model } else { "(agent default)" }), $worker.log
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
$Agent = Resolve-Agent $Agent
$Model = Resolve-Model $Model $Agent

$agentCmd = "$python `"$adapter`" {brief} --agent `"$Agent`" --approval $Approval"
if ($Model) { $agentCmd = "$agentCmd -m $Model" }

$display = if ($Model) { "$Agent / $Model" } else { "$Agent / (agent default, not pinned)" }

if ($DryRun) {
    "agent:  $Agent"
    "model:  $(if ($Model) { $Model } else { '(agent default)' })"
    "agents: $Agents   rounds: $Rounds   timeout: ${Timeout}s"
    "agent-cmd: $agentCmd"
    exit 0
}

if (-not $Yes) {
    $answer = Read-Host "Start $Agents worker(s) x $Rounds round(s) with $display ? [Y/n]"
    if ($answer -and $answer -match "^(n|no)$") { "aborted"; exit 1 }
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$workers = @()
for ($i = 1; $i -le $Agents; $i++) {
    $holder = "worker-$i"
    $log = Join-Path $logDir "$holder.log"
    $errLog = Join-Path $logDir "$holder.err.log"
    $argLine = "-m orchestra.cli --repo `"$Repo`" loop --max-rounds $Rounds --holder $holder --timeout $Timeout --agent-cmd `"$agentCmd`""
    $proc = Start-Process -FilePath $python -ArgumentList $argLine -WorkingDirectory $Repo `
        -RedirectStandardOutput $log -RedirectStandardError $errLog `
        -WindowStyle Hidden -PassThru
    $workers += [pscustomobject]@{ holder = $holder; pid = $proc.Id; log = $log; rounds = $Rounds; model = $Model }
    "started {0,-10} pid {1,-7} max-rounds {2} model {3}" -f $holder, $proc.Id, $Rounds, $(if ($Model) { $Model } else { "(agent default)" })
}

[pscustomobject]@{
    repo = $Repo; agent = $Agent; model = $Model; started = (Get-Date).ToString("s"); workers = $workers
} | ConvertTo-Json -Depth 5 | Set-Content -Path $stateFile -Encoding utf8

""
"agent:   $display"
"Watch:   Get-Content '$logDir\worker-1.log' -Wait"
"State:   .\scripts\local-fleet.ps1 -Status"
"Stop:    .\scripts\local-fleet.ps1 -Stop"
