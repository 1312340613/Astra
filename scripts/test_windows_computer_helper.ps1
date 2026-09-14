param(
    [ValidateSet('debug', 'release')][string]$Configuration = 'debug',
    [switch]$Capture,
    [switch]$Files,
    [switch]$IPC,
    [switch]$Hotkey,
    [switch]$Broker,
    [switch]$Consumer,
    [switch]$Daemon,
    [switch]$SkipBuild
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'windows_swift_environment.ps1')
$swiftExecutable = Initialize-AstraSwiftEnvironment
$packagePath = Join-Path $PSScriptRoot '..\native\windows-computer-helper'
if (-not $SkipBuild) {
    & $swiftExecutable build --package-path $packagePath -c $Configuration --product AstraWindowsComputerHelper
    if ($LASTEXITCODE -ne 0) { throw "Windows helper build failed ($LASTEXITCODE)." }
    & $swiftExecutable build --package-path $packagePath -c $Configuration --product AstraWindowsNative
    if ($LASTEXITCODE -ne 0) { throw "Windows storage library build failed ($LASTEXITCODE)." }
}
$binPath = & $swiftExecutable build --package-path $packagePath -c $Configuration --show-bin-path
if ($LASTEXITCODE -ne 0) { throw 'Unable to locate helper build output.' }
$helperPath = Join-Path $binPath 'AstraWindowsComputerHelper.exe'
Copy-AstraSwiftRuntime -Destination $binPath
$modes = @('--self-test')
if ($IPC) { $modes += '--self-test-ipc' }
if ($Hotkey) { $modes += '--self-test-hotkey' }
if ($Capture -and -not $Files -and -not $Broker) { $modes += '--self-test-capture' }
$fileTestRoot = $null
if ($Files -or $Broker) {
    $repository = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
    $fileTestRoot = Join-Path $repository ('.test-appshot-files-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $fileTestRoot | Out-Null
    if ($Files) { $modes += '--self-test-files' }
    if ($Capture -and $Files -and -not $Broker) { $modes += '--self-test-artifacts' }
    if ($Broker) { $modes += '--self-test-broker' }
    if ($Broker -and $Capture) { $modes += '--self-test-broker-capture' }
}
foreach ($mode in $modes) {
    $interactiveFixture = $mode -in @('--self-test-capture', '--self-test-artifacts', '--self-test-broker-capture')
    if ($interactiveFixture) {
        Write-Host 'Click Start test in Astra Appshot controlled test (within 60 seconds); keep it in front until it closes.'
    }
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $helperPath
    $startInfo.Arguments = $mode
    if ($mode -in @('--self-test-files', '--self-test-artifacts', '--self-test-broker', '--self-test-broker-capture')) {
        $startInfo.Arguments += ' "' + (Join-Path $fileTestRoot 'private') + '"'
    }
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) { throw 'Unable to start helper test.' }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        $timeout = if ($interactiveFixture) { 80000 } else { 15000 }
        $elapsed = [Diagnostics.Stopwatch]::StartNew()
        while (-not $process.WaitForExit(200) -and $elapsed.ElapsedMilliseconds -lt $timeout) {}
        if (-not $process.HasExited) {
            # This exact Process instance belongs to this test; never enumerate/kill by name.
            $process.Kill()
            $process.WaitForExit()
            throw "Windows helper test exceeded $timeout milliseconds."
        }
        if ($process.ExitCode -ne 0) { throw "Windows helper test failed: $($stderr.Result.Trim()) $($stdout.Result.Trim()). Retained fixture: $fileTestRoot" }
        $result = $stdout.Result | ConvertFrom-Json
        if (-not $result.ok) { throw "Windows helper fixture failed: $($stdout.Result.Trim()). Retained fixture: $fileTestRoot" }
        $stdout.Result.Trim()
    } finally {
        $process.Dispose()
    }
}
if ($fileTestRoot) {
    $resolved = (Resolve-Path -LiteralPath $fileTestRoot).Path
    if (-not $resolved.StartsWith($repository + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Unexpected fixture directory; cleanup refused.'
    }
    $private = Join-Path $resolved 'private'
    foreach ($folder in @($private, $resolved)) {
        $item = Get-Item -LiteralPath $folder
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Fixture cleanup refused a reparse point.' }
        if (@(Get-ChildItem -LiteralPath $folder -Force).Count -ne 0) { throw "Fixture was not empty: $folder" }
        # Only empty directories created by this test; no recursive deletion.
        [IO.Directory]::Delete($folder, $false)
    }
}
if ($Consumer) {
    $repository = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
    Push-Location (Join-Path $repository 'ui-tui')
    try {
        & node --import tsx (Join-Path $repository 'tests\fixtures\appshot_windows_consumer.mts') $helperPath $repository
        if ($LASTEXITCODE -ne 0) { throw 'Windows native/Node consumer test failed.' }
    } finally { Pop-Location }
}
if ($Daemon) {
    $repository = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
    Push-Location (Join-Path $repository 'ui-tui')
    try {
        & node --import tsx (Join-Path $repository 'tests\fixtures\appshot_windows_daemon.mts') $helperPath $repository
        if ($LASTEXITCODE -ne 0) { throw 'Windows production daemon test failed.' }
    } finally { Pop-Location }
}
