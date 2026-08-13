[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')]
    [string]$SessionKey,

    [Parameter(Mandatory, Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Task
)

# Durable launcher for ordinary delegated project work.  A key identifies one
# independent work slice; choose a new key for a new worker rather than sharing
# or replacing an existing session.
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$profilePath = Join-Path $repoRoot '.codex\delegates\deepseek.toml'
$sessionDirectory = Join-Path $repoRoot '.codex\delegates\sessions'
$sessionPath = Join-Path $sessionDirectory "$SessionKey.json"
$streamPath = Join-Path $sessionDirectory "$SessionKey.jsonl"
$finalPath = Join-Path $sessionDirectory "$SessionKey.final.md"

function Get-Profile {
    param([string]$Path)

    $settings = @{}
    foreach ($rawLine in Get-Content -LiteralPath $Path) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith('#')) { continue }
        if ($line -notmatch '^(?<key>[A-Za-z0-9_]+)\s*=\s*(?<value>.+?)\s*$') {
            throw "Unsupported profile line in $Path`: $rawLine"
        }
        $key = $Matches.key
        $value = $Matches.value.Trim()
        if ($value -match '^"(?<text>(?:[^"\\]|\\.)*)"$') {
            $settings[$key] = $Matches.text -replace '\\"', '"' -replace '\\\\', '\\'
        }
        elseif ($value -match '^\d+$') {
            $settings[$key] = [long]$value
        }
        elseif ($value -match '^(true|false)$') {
            $settings[$key] = [bool]::Parse($value)
        }
        else {
            throw "Unsupported TOML value for '$key' in $Path"
        }
    }
    return $settings
}

function Save-Session {
    param([string]$SessionId)
    [ordered]@{
        session_key = $SessionKey
        runner = 'qwen-code'
        session_id = $SessionId
        model = $profile.model
        updated_at = (Get-Date).ToString('o')
    } | ConvertTo-Json | Set-Content -LiteralPath $sessionPath -Encoding utf8
}

if (-not (Test-Path -LiteralPath $profilePath)) {
    throw "DeepSeek delegate profile not found: $profilePath"
}
if (-not (Get-Command qwen -ErrorAction SilentlyContinue)) {
    throw 'Qwen Code was not found on PATH.'
}

$profile = Get-Profile -Path $profilePath
foreach ($required in @('model', 'model_reasoning_effort', 'model_context_window', 'model_auto_compact_token_limit', 'model_auto_compact_token_limit_scope', 'local_provider', 'model_catalog_json', 'developer_instructions')) {
    if (-not $profile.ContainsKey($required)) { throw "DeepSeek delegate profile lacks '$required'." }
}
if ($profile.model -ne 'deepseek-v4-flash:0731-cloud' -or $profile.model_auto_compact_token_limit -ne 230000) {
    throw 'The ordinary worker workflow is pinned to DeepSeek V4 Flash 0731-cloud with a 230000-token compaction threshold.'
}

$catalogPath = Join-Path (Split-Path -Parent $profilePath) $profile.model_catalog_json
if (-not (Test-Path -LiteralPath $catalogPath)) {
    throw "DeepSeek model catalog not found: $catalogPath"
}
New-Item -ItemType Directory -Force -Path $sessionDirectory | Out-Null
Set-Location -LiteralPath $repoRoot
$python = Join-Path $repoRoot '.codex\dev\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw "Harness Python was not found: $python"
}

$session = $null
if (Test-Path -LiteralPath $sessionPath) {
    try { $session = Get-Content -Raw -LiteralPath $sessionPath | ConvertFrom-Json }
    catch { throw "Invalid delegate session record: $sessionPath" }
    if ($session.runner -ne 'qwen-code' -or -not $session.session_id) {
        throw "Delegate session record is not a resumable Qwen session: $sessionPath"
    }
}

$workerArgs = @(
    '-m', 'harness.qwen_delegate',
    '--repo', $repoRoot,
    '--final', $finalPath,
    '--qwen-bin', 'qwen'
)

if ($session) {
    $mode = 'resuming'
    $workerArgs += @('--resume-session-id', $session.session_id)
} else {
    $mode = 'starting'
}

Write-Host "$($mode.Substring(0, 1).ToUpper() + $mode.Substring(1)) DeepSeek worker '$SessionKey' ($($profile.model), 230000-token auto-compaction, full access)."
$prompt = @($profile.developer_instructions, ($Task -join ' ')) -join "`n`n"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$stream = New-Object System.IO.StreamWriter($streamPath, $true, $utf8NoBom)
try {
    $prompt | & $python @workerArgs 2>&1 | ForEach-Object {
        $line = [string]$_
        $stream.WriteLine($line)
        $stream.Flush()
        try { $event = $line | ConvertFrom-Json -ErrorAction Stop }
        catch { Write-Output $line; return }
        if ($event.type -eq 'system' -and $event.session_id) {
            Save-Session -SessionId $event.session_id
            Write-Host "Delegate session: $($event.session_id)"
        }
        elseif ($event.type -eq 'result' -and $event.result) {
            Write-Output $event.result
        }
        elseif ($event.type -eq 'result' -and $event.is_error) {
            Write-Warning $event.result
        }
    }
    $workerExitCode = $LASTEXITCODE
} finally {
    $stream.Dispose()
}
exit $workerExitCode
