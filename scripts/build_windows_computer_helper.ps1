param([ValidateSet('debug', 'release')][string]$Configuration = 'release', [switch]$Install)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'windows_swift_environment.ps1')
$swiftExecutable = Initialize-AstraSwiftEnvironment
$packagePath = Join-Path $PSScriptRoot '..\native\windows-computer-helper'
& $swiftExecutable build --package-path $packagePath -c $Configuration --product AstraWindowsComputerHelper
if ($LASTEXITCODE -ne 0) { throw "Windows helper build failed ($LASTEXITCODE)." }
& $swiftExecutable build --package-path $packagePath -c $Configuration --product AstraWindowsNative
if ($LASTEXITCODE -ne 0) { throw "Windows storage library build failed ($LASTEXITCODE)." }
# Building never starts a broker or changes the user's settings.
$binPath = & $swiftExecutable build --package-path $packagePath -c $Configuration --show-bin-path
if ($LASTEXITCODE -ne 0) { throw 'Unable to locate Windows helper output.' }
Copy-AstraSwiftRuntime -Destination $binPath
Write-Output $binPath
if ($Install) {
    if ($Configuration -ne 'release') { throw 'Only a release helper can be installed.' }
    $repository = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
    $binRoot = Join-Path $repository '.astra\bin'
    $destination = [IO.Path]::GetFullPath((Join-Path $binRoot 'AstraWindowsComputerHelper'))
    if (-not $destination.StartsWith($repository + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Helper destination escaped the installation root.'
    }
    foreach ($path in @((Join-Path $repository '.astra'), $binRoot, $destination)) {
        if (Test-Path -LiteralPath $path) {
            $item = Get-Item -LiteralPath $path -Force
            if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw 'Helper installation refuses redirected or non-directory paths.'
            }
        }
    }
    New-Item -ItemType Directory -Path $binRoot -Force | Out-Null
    $staging = Join-Path $binRoot ('AstraWindowsComputerHelper.pending-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $staging | Out-Null
    Copy-Item -LiteralPath (Join-Path $binPath 'AstraWindowsComputerHelper.exe') -Destination $staging
    foreach ($library in Get-ChildItem -LiteralPath $binPath -Filter '*.dll' -File) {
        Copy-Item -LiteralPath $library.FullName -Destination $staging
    }
    $info = & (Join-Path $staging 'AstraWindowsComputerHelper.exe') --service-info | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or -not $info.production_ready -or $info.protocol_version -ne 2) {
        throw "Helper verification failed; staging retained: $staging"
    }
    # Same-parent directory publication. Preserve the previous bundle for rollback;
    # never delete a running installation or alter another platform's helper.
    $previous = $null
    if (Test-Path -LiteralPath $destination) {
        $previous = Join-Path $binRoot ('AstraWindowsComputerHelper.previous-' + [Guid]::NewGuid().ToString('N'))
        Rename-Item -LiteralPath $destination -NewName ([IO.Path]::GetFileName($previous))
    }
    try { Rename-Item -LiteralPath $staging -NewName ([IO.Path]::GetFileName($destination)) }
    catch {
        if ($previous -and -not (Test-Path -LiteralPath $destination)) {
            Rename-Item -LiteralPath $previous -NewName ([IO.Path]::GetFileName($destination))
        }
        throw
    }
    Write-Output "Installed Windows Appshot helper: $destination"
    if ($previous) { Write-Output "Previous bundle retained: $previous" }
}
