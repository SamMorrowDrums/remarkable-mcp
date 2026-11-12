[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
$tokenFile = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.rmapi'
$endpoint = 'http://127.0.0.1:8000/mcp'
if (-not (Test-Path -LiteralPath $python)) { throw 'The installed MCP Python environment is missing.' }
if (-not (Test-Path -LiteralPath $tokenFile) -or (Get-Item -LiteralPath $tokenFile).Length -eq 0) {
    throw 'Register cloud access first with Register-RemarkableCloud.ps1.'
}
if (Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match '^(cloudflared|tunnel-client|tunnel-runtime|harpoon.*)$' }) {
    throw 'A tunnel runtime is active. Stop it before starting the local cloud write prototype.'
}
$probe = [Net.Sockets.TcpClient]::new()
try {
    $occupied = $false
    try { $probe.Connect('127.0.0.1', 8000); $occupied = $true } catch { }
    if ($occupied) { throw 'Port 8000 is already in use; no existing process was changed.' }
} finally { $probe.Dispose() }
$runId = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
$logDir = Join-Path $PSScriptRoot "logs\cloud-$runId"
New-Item -ItemType Directory -Path $logDir | Out-Null
$settings = @{
    REMARKABLE_USE_USB_WEB = '0'
    REMARKABLE_USE_SSH = '0'
    REMARKABLE_USE_LOCAL_DIR = '0'
    REMARKABLE_LOCAL_DIR = ''
    REMARKABLE_READ_ONLY = '0'
    REMARKABLE_DISABLE_CLOUD_FALLBACK = '1'
    REMARKABLE_ROOT_PATH = ''
    REMARKABLE_TOKEN = ''
    REMARKABLE_SKIP_CONFIRM = '0'
    REMARKABLE_OCR_BACKEND = 'tesseract'
    REMARKABLE_CACHE_DIR = (Join-Path $PSScriptRoot '.cloud-cache')
    PYTHONUNBUFFERED = '1'
    NO_PROXY = ((@($env:NO_PROXY, '127.0.0.1', 'localhost') | Where-Object { $_ }) -join ',')
}
$previous = @{}
$process = $null
try {
    foreach ($key in $settings.Keys) {
        $previous[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, $settings[$key], 'Process')
    }
    $serverArgs = @('-m', 'remarkable_mcp.cli', '--http', '--host', '127.0.0.1', '--port', '8000')
    $process = Start-Process -FilePath $python -ArgumentList $serverArgs -WorkingDirectory $repoRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'stdout.log') -RedirectStandardError (Join-Path $logDir 'stderr.log') -PassThru
} finally {
    foreach ($key in $previous.Keys) { [Environment]::SetEnvironmentVariable($key, $previous[$key], 'Process') }
}
$ready = $false
for ($attempt = 0; $attempt -lt 120; $attempt++) {
    $socket = [Net.Sockets.TcpClient]::new()
    try {
        $socket.Connect('127.0.0.1', 8000)
        $ready = $true
        break
    } catch {
        $process.Refresh()
        if ($process.HasExited) { throw "Cloud server exited. Inspect $logDir." }
        Start-Sleep -Milliseconds 250
    } finally { $socket.Dispose() }
}
if (-not $ready) {
    & taskkill.exe /PID $process.Id /T /F 2>$null | Out-Null
    throw "Cloud server did not become ready. Inspect $logDir."
}
$listeners = @(Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction Stop)
if (-not $listeners.Count -or @($listeners | Where-Object { $_.LocalAddress -ne '127.0.0.1' }).Count) {
    & taskkill.exe /PID $process.Id /T /F 2>$null | Out-Null
    throw 'Unexpected HTTP binding; the cloud process tree was stopped.'
}
@{
    launcher_pid = $process.Id
    server_pid = $listeners[0].OwningProcess
    started_utc = [DateTime]::UtcNow.ToString('o')
    endpoint = $endpoint
    mode = 'cloud'
    writes_requested = $true
    verified = $false
    arguments = $serverArgs
    log_directory = $logDir
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'cloud-server-state.json') -Encoding utf8
Write-Output "Cloud MCP started at $endpoint. Run verify_cloud_mcp.py to confirm authentication and writes."
