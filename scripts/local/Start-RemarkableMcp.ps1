[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
$logDir = Join-Path $PSScriptRoot 'logs'
$stateFile = Join-Path $PSScriptRoot 'server-state.json'
$endpoint = 'http://127.0.0.1:8000/mcp'
if (-not (Test-Path -LiteralPath $python)) { throw 'The project Python environment is missing.' }
if (Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue) {
    throw "Port 8000 is already in use. Check $endpoint before starting another server."
}
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

$settings = @{
    REMARKABLE_USE_USB_WEB = '1'
    REMARKABLE_USE_LOCAL_DIR = '0'
    REMARKABLE_LOCAL_DIR = ''
    REMARKABLE_USE_SSH = '0'
    REMARKABLE_USB_HOST = 'http://10.11.99.1'
    REMARKABLE_USB_TIMEOUT = '30'
    REMARKABLE_USB_MAX_CONCURRENCY = '2'
    REMARKABLE_READ_ONLY = '1'
    REMARKABLE_DISABLE_CLOUD_FALLBACK = '1'
    REMARKABLE_OCR_BACKEND = 'tesseract'
    REMARKABLE_ROOT_PATH = ''
    REMARKABLE_TOKEN = ''
    NO_PROXY = ((@($env:NO_PROXY, '127.0.0.1', 'localhost', '10.11.99.1') | Where-Object { $_ }) -join ',')
}
$tesseract = Join-Path $PSScriptRoot '.tools\tesseract\tesseract.exe'
if (Test-Path -LiteralPath $tesseract) {
    $settings['PATH'] = (Split-Path -Parent $tesseract) + ';' + $env:PATH
    $settings['TESSDATA_PREFIX'] = Join-Path $PSScriptRoot '.tools\tesseract\tessdata'
}
$previous = @{}
try {
    foreach ($key in $settings.Keys) {
        $previous[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, $settings[$key], 'Process')
    }
    $serverArgs = @('-m', 'remarkable_mcp.cli', '--usb', '--read-only', '--no-cloud-fallback', '--http', '--host', '127.0.0.1', '--port', '8000')
    $process = Start-Process -FilePath $python -ArgumentList $serverArgs -WorkingDirectory $repoRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'server.stdout.log') -RedirectStandardError (Join-Path $logDir 'server.stderr.log') -PassThru
} finally {
    foreach ($key in $previous.Keys) {
        [Environment]::SetEnvironmentVariable($key, $previous[$key], 'Process')
    }
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
        if ($process.HasExited) { throw "Server exited. See $logDir\server.stderr.log" }
        Start-Sleep -Milliseconds 250
    } finally { $socket.Dispose() }
}
if (-not $ready) { throw "Server did not become ready. See $logDir\server.stderr.log" }
$listener = Get-NetTCPConnection -State Listen -LocalPort 8000
if (@($listener | Where-Object { $_.LocalAddress -ne '127.0.0.1' }).Count) {
    throw 'Unexpected non-loopback listener; inspect the server before using it.'
}
@{
    launcher_pid = $process.Id
    server_pid = @($listener)[0].OwningProcess
    endpoint = $endpoint
    started_utc = [DateTime]::UtcNow.ToString('o')
    arguments = $serverArgs
} | ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding UTF8
Write-Output "Read-only USB MCP server is running at $endpoint"
Write-Output "Logs: $logDir"
