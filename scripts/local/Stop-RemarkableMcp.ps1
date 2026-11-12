[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$stateFile = Join-Path $PSScriptRoot 'server-state.json'
if (-not (Test-Path -LiteralPath $stateFile)) { throw 'No saved server process was found.' }
$state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
$process = Get-CimInstance Win32_Process -Filter "ProcessId = $($state.server_pid)"
if (-not $process) { Write-Output 'The saved server process has already stopped.'; return }
if ($process.Name -ne 'python.exe' -or $process.CommandLine -notmatch 'remarkable_mcp\.cli.*--usb.*--read-only.*--no-cloud-fallback') {
    throw 'The saved PID does not match the read-only USB server. Nothing was stopped.'
}
Stop-Process -Id $state.server_pid
Write-Output 'Stopped the reMarkable USB MCP server.'
