[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$state = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'cloud-server-state.json') -Raw | ConvertFrom-Json
$process = Get-Process -Id $state.launcher_pid -ErrorAction SilentlyContinue
if (-not $process) { Write-Output 'The saved cloud server has already stopped.'; return }
$expected = (Resolve-Path -LiteralPath (Join-Path $repoRoot '.venv\Scripts\python.exe')).Path
if ($process.Path -ne $expected) { throw 'The saved PID no longer matches the cloud launcher. Nothing was stopped.' }
$started = [DateTime]::Parse($state.started_utc).ToUniversalTime()
if ([Math]::Abs(($process.StartTime.ToUniversalTime() - $started).TotalSeconds) -gt 120) {
    throw 'The saved PID has a different start time. Nothing was stopped.'
}
# Windows venv launchers can create a child process. Taskkill /T closes only this saved process tree.
& taskkill.exe /PID $process.Id /T /F | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'The cloud process tree could not be stopped.' }
Write-Output 'Stopped the local reMarkable cloud MCP process tree.'
