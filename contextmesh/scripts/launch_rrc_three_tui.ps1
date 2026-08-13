<#
Starts the visible three-arm RRCv2 comparison:
  Raw | Raw + ContextMesh | ContextMesh + RRCv2 | live meter

The Ollama key must already be available to WSL as OLLAMA_API_KEY. It is never
written to this repository or passed on the command line.
#>
[CmdletBinding()]
param(
    [switch]$NoLaunch,
    [switch]$Resume
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if ($repo -notmatch '^(?<drive>[A-Za-z]):(?<rest>\\.*)$') {
    throw "Repository path '$repo' is not a drive-rooted Windows path (expected C:\...)."
}
$repoWsl = "/mnt/$($matches.drive.ToLowerInvariant())$($matches.rest -replace '\\', '/')"

$ollamaApiKey = (& wsl.exe -- bash -lc 'printenv OLLAMA_API_KEY') -join ''
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrEmpty($ollamaApiKey)) {
    throw "OLLAMA_API_KEY is not available inside WSL. Export it there, then rerun this launcher."
}
$env:OLLAMA_API_KEY = $ollamaApiKey
Clear-Variable -Name ollamaApiKey

$wslEnvEntries = if ([string]::IsNullOrEmpty($env:WSLENV)) { @() } else { @($env:WSLENV -split ':') }
if ($wslEnvEntries -notcontains 'OLLAMA_API_KEY/u') {
    $env:WSLENV = (($wslEnvEntries + 'OLLAMA_API_KEY/u') -join ':')
}

# A rerun replaces only the four windows owned by this launcher.  Keeping this
# here makes repeated demos safe without closing a user's unrelated terminals.
Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowTitle -like 'RRCv2 demo - *' } |
    Stop-Process -Force -ErrorAction SilentlyContinue

if (-not $Resume) {
    & wsl.exe -- bash -lc "cd '$repoWsl' && bash contextmesh/demo.sh prep"
    if ($LASTEXITCODE -ne 0) { throw "The three-arm demo preparation failed." }
} elseif (-not (Test-Path (Join-Path $repo 'contextmesh\runs\demo-tui\round'))) {
    throw "No prepared three-arm round exists to resume. Run without -Resume first."
}
if ($NoLaunch) {
    Write-Host "Three-arm demo preparation completed; child windows were not opened."
    return
}

function Start-Sequence {
    # The shared provider serializes Pro streams.  Running one arm at a time
    # avoids measuring queue time as if it were a ContextMesh token result.
    $command = "`$Host.UI.RawUI.WindowTitle = 'RRCv2 demo - Raw'; wsl.exe -- bash -lc 'cd `"$repoWsl`" && exec bash contextmesh/demo.sh raw'; " +
        "`$Host.UI.RawUI.WindowTitle = 'RRCv2 demo - Raw + ContextMesh'; wsl.exe -- bash -lc 'cd `"$repoWsl`" && exec bash contextmesh/demo.sh contextmesh'; " +
        "`$Host.UI.RawUI.WindowTitle = 'RRCv2 demo - ContextMesh + RRCv2'; wsl.exe -- bash -lc 'cd `"$repoWsl`" && exec bash contextmesh/demo.sh full'"
    Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoExit", "-Command", $command) | Out-Null
}

Start-Sequence
$meterCommand = "`$Host.UI.RawUI.WindowTitle = 'RRCv2 demo - live meter'; wsl.exe -- bash -lc 'cd `"$repoWsl`" && exec bash contextmesh/demo.sh meter3'"
Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoExit", "-Command", $meterCommand) | Out-Null

Write-Host "Opened one sequential Raw -> ContextMesh -> RRCv2 TUI and the live meter."
