$ErrorActionPreference = 'Stop'
$candidate = $env:AWARDLINE_PYTHON
if (-not $candidate) {
    $installed = Get-Command python -ErrorAction SilentlyContinue
    if ($installed) { $candidate = $installed.Source }
}
if (-not $candidate) {
    $candidate = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
}
if (-not (Test-Path -LiteralPath $candidate)) {
    throw "Python 3.12+ not found. Set AWARDLINE_PYTHON to its executable path."
}
Push-Location (Join-Path $PSScriptRoot '..')
try {
    & $candidate -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $candidate -m compileall -q awardline tests
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}
