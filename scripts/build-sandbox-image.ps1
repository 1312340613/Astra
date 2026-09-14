param(
    [string]$Tag = "astra-sandbox:agent-system-dev"
)

$repoRoot = Split-Path -Parent $PSScriptRoot
$dockerfile = Join-Path $repoRoot "docker\sandbox\Dockerfile"

docker build --file $dockerfile --tag $Tag $repoRoot
exit $LASTEXITCODE
