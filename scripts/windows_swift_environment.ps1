# Build-time environment only; dot-source from the helper build/test scripts.
Set-StrictMode -Version Latest

function Initialize-AstraSwiftEnvironment {
    $swiftVersion = '6.3.3'
    $swiftBase = Join-Path $env:LOCALAPPDATA 'Programs\Swift'
    $swiftBin = Join-Path $swiftBase "Toolchains\$swiftVersion+Asserts\usr\bin"
    $swiftExecutable = Join-Path $swiftBin 'swift.exe'
    if (-not (Test-Path -LiteralPath $swiftExecutable -PathType Leaf)) {
        throw "Swift $swiftVersion is required. Install Swift.Toolchain with winget."
    }
    $env:SDKROOT = Join-Path $swiftBase "Platforms\$swiftVersion\Windows.platform\Developer\SDKs\Windows.sdk"
    if (-not (Test-Path -LiteralPath $env:SDKROOT -PathType Container)) {
        throw 'Swift Windows SDK is missing; repair the Swift installation.'
    }
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$swiftBin;$userPath;$env:Path"
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (-not (Test-Path -LiteralPath $vswhere -PathType Leaf)) {
        throw 'Visual Studio 2022 C++ Build Tools and Windows SDK 22621 are required.'
    }
    $vsInstall = & $vswhere -latest -products '*' -version '[17.0,18.0)' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if ($LASTEXITCODE -ne 0 -or -not $vsInstall) { throw 'MSVC v143 x64 tools are missing.' }
    Import-Module (Join-Path $vsInstall 'Common7\Tools\Microsoft.VisualStudio.DevShell.dll')
    Enter-VsDevShell -VsInstallPath $vsInstall -SkipAutomaticLocation -DevCmdArguments '-arch=x64 -host_arch=x64' | Out-Null
    return $swiftExecutable
}

# App-local runtime deployment, also used by release packaging. Running the
# resulting helper/storage DLL must not depend on a compiler terminal's PATH.
function Copy-AstraSwiftRuntime([string]$Destination) {
    $runtime = Join-Path $env:LOCALAPPDATA 'Programs\Swift\Runtimes\6.3.3\usr\bin'
    if (-not (Test-Path -LiteralPath (Join-Path $runtime 'swiftCore.dll') -PathType Leaf)) {
        throw 'Swift 6.3.3 runtime is missing.'
    }
    $target = Get-Item -LiteralPath $Destination
    if (-not $target.PSIsContainer -or ($target.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Runtime destination must be an ordinary build/package directory.'
    }
    foreach ($library in Get-ChildItem -LiteralPath $runtime -Filter '*.dll' -File) {
        Copy-Item -LiteralPath $library.FullName -Destination (Join-Path $target.FullName $library.Name)
    }
}
