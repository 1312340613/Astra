param([ValidateSet('debug', 'release')][string]$Configuration = 'debug')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'windows_swift_environment.ps1')
$swiftExecutable = Initialize-AstraSwiftEnvironment
$packagePath = Join-Path $PSScriptRoot '..\native\appshot-core'
& $swiftExecutable test --package-path $packagePath -c $Configuration
if ($LASTEXITCODE -ne 0) { throw "Appshot core tests failed ($LASTEXITCODE)." }
