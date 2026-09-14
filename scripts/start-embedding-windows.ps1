param(
    [string]$Server = 'D:\llama-cpp\llama-server.exe',
    [string]$Model = 'D:\llama-cpp\models\Qwen3-Embedding-4B-Q4_K_M.gguf',
    [int]$Port = 8088,
    [ValidateSet('start', 'stop', 'status')]
    [string]$Action = 'start'
)
$ErrorActionPreference = 'Stop'
$serverPath = [IO.Path]::GetFullPath($Server)
$modelPattern = [regex]::Escape($Model)
$portPattern = '--port\s+' + $Port + '(\s|$)'
$owned = @(Get-CimInstance Win32_Process -Filter "Name = 'llama-server.exe'" | Where-Object {
    $_.ExecutablePath -ieq $serverPath -and
    $_.CommandLine -match $modelPattern -and
    $_.CommandLine -match $portPattern -and
    $_.CommandLine -match '--embedding(\s|$)' -and
    $_.CommandLine -match '--alias\s+"?Qwen3-Embedding-4B-Q4_K_M"?(\s|$)'
})
if ($Action -eq 'stop') {
    if (!$owned.Count) { Write-Host 'Embedding service is not running.'; return }
    foreach ($service in $owned) {
        # Keep the process handle bound to the inspected instance (avoid PID reuse).
        $process = Get-Process -Id $service.ProcessId -ErrorAction SilentlyContinue
        if (!$process) { continue }
        if ($process.Path -ine $serverPath -or
            [Math]::Abs(($process.StartTime - $service.CreationDate).TotalSeconds) -gt 1) {
            throw 'Process identity changed; refusing to stop it.'
        }
        $process.Kill()
        if (!$process.WaitForExit(10000)) { throw 'Embedding process did not exit in time.' }
        Write-Host "Stopped embedding service (PID $($service.ProcessId))."
    }
    return
}
if ($Action -eq 'status') {
    if ($owned.Count) {
        Write-Host "Embedding process running: PID $($owned.ProcessId -join ', '), port $Port"
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
            Write-Host "Health: $($health.status)"
        } catch { Write-Host 'Health: loading or unavailable.' }
    } else { Write-Host 'Embedding service is not running.' }
    return
}
if ($owned.Count) { Write-Host 'Embedding service is already running.'; return }
if (!(Test-Path -LiteralPath $Server -PathType Leaf)) { throw "Missing llama-server: $Server" }
if (!(Test-Path -LiteralPath $Model -PathType Leaf)) { throw "Missing GGUF: $Model" }
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "Port $Port is occupied; inspect its owner before starting another server."
}
$logDir = Join-Path $PSScriptRoot '..\.logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$arguments = @('-m', ('"' + $Model + '"'), '--host', '127.0.0.1', '--port', $Port,
    '--embedding', '--pooling', 'last', '-ngl', '99', '-c', '8192', '-b', '8192', '-ub', '8192',
    '--alias', 'Qwen3-Embedding-4B-Q4_K_M')
Start-Process -FilePath $Server -ArgumentList $arguments -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $logDir 'embedding-stdout.log') `
    -RedirectStandardError (Join-Path $logDir 'embedding-stderr.log') |
    Select-Object Id, ProcessName
