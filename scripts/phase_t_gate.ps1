param(
    [switch]$ProviderSmoke,
    [string]$ModelKey = ""
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Arguments = @("scripts\phase_t_gate.py")
if ($ProviderSmoke) {
    $Arguments += "--provider-smoke"
}
if ($ModelKey) {
    $Arguments += @("--model-key", $ModelKey)
}

Push-Location $ProjectRoot
try {
    & $Python @Arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
