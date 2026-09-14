param([ValidateSet('start','stop','pause','resume','status','clear','browser')][string]$Action = 'status')
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$python = Join-Path $repo '.venv\Scripts\python.exe'
$state = Join-Path $repo '.astra\windows-activity'
$processes = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object {
    $_.CommandLine -match '(?:^|\s)-m\s+agent\.runtime\.activity_recorder\.windows(?:\s|$)' -and
    $_.CommandLine -like ('*' + $python + '*')
})
New-Item -ItemType Directory -Path $state -Force | Out-Null
switch ($Action) {
    'browser' {
        $token = Join-Path $state 'browser-token.txt'
        if (!(Test-Path -LiteralPath $token)) { throw 'Start the recorder once to generate a browser pairing code.' }
        Get-Content -LiteralPath $token -Raw | Set-Clipboard
        Write-Host 'Pairing code copied. Load the browser-extension folder at edge://extensions or chrome://extensions.'
        Write-Host (Join-Path $repo 'browser-extension')
        Write-Host 'Open extension Options, paste the code, enable recording, and Save.'
    }
    'clear' {
        if ($processes.Count) { throw 'Stop the recorder before clearing activity history.' }
        $confirm = Read-Host 'Permanently delete local activity history? Type CLEAR to confirm'
        if ($confirm -cne 'CLEAR') { Write-Host 'Cancelled.'; break }
        Push-Location $repo
        try { & $python -m agent.runtime.activity_recorder.windows --clear-history }
        finally { Pop-Location }
    }
    'start' {
        if ($processes.Count) { Write-Host 'Recorder already running.'; break }
        if (!(Test-Path -LiteralPath $python)) { throw "Missing Python: $python" }
        Start-Process -FilePath $python -ArgumentList '-m agent.runtime.activity_recorder.windows' `
            -WorkingDirectory $repo -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $state 'stdout.log') `
            -RedirectStandardError (Join-Path $state 'stderr.log') | Select-Object Id, ProcessName
        Write-Host 'Recording foreground app/title only. Summaries use the configured LLM. Retention: 30 days.'
    }
    'stop' {
        if (!$processes.Count) { Write-Host 'Recorder is not running.'; break }
        New-Item -ItemType File -Path (Join-Path $state 'stop') -Force | Out-Null
        foreach ($process in $processes) {
            Wait-Process -Id $process.ProcessId -Timeout 20 -ErrorAction SilentlyContinue
        }
        $remaining = @($processes | Where-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue })
        if ($remaining.Count) { throw 'Stop requested; recorder is still shutting down. Check status again.' }
        Write-Host 'Recorder stopped.'
    }
    'pause' {
        New-Item -ItemType File -Path (Join-Path $state 'pause') -Force | Out-Null
        Write-Host 'Pause requested (applies within 5 seconds).'
    }
    'resume' {
        Remove-Item -LiteralPath (Join-Path $state 'pause') -Force -ErrorAction SilentlyContinue
        Write-Host 'Pause cleared. Use Start if the recorder is stopped.'
    }
    'status' {
        if (!$processes.Count) { Write-Host 'Recorder is not running.' } else { Write-Host 'Recorder process is running.' }
        $file = Join-Path $state 'status.json'
        if (Test-Path -LiteralPath $file) {
            $status = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
            $status | Format-List
            if ($processes.Count -and [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - $status.updated_at -gt 30) {
                Write-Host 'WARNING: status heartbeat is stale.'
            }
        }
    }
}
