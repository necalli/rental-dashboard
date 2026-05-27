param(
    [Parameter(Mandatory = $true)]
    [string]$Destination,

    [switch]$Force
)

$source = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$target = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Destination)

if ((Test-Path -LiteralPath $target) -and -not $Force) {
    throw "Destination already exists. Pass -Force to replace it: $target"
}

if (Test-Path -LiteralPath $target) {
    Remove-Item -Recurse -Force -LiteralPath $target
}

New-Item -ItemType Directory -Force -Path $target | Out-Null

$excludeDirs = @(
    (Join-Path $source ".git"),
    (Join-Path $source ".claude"),
    (Join-Path $source ".venv"),
    (Join-Path $source "venv"),
    (Join-Path $source "backend\data"),
    (Join-Path $source "backend\raw"),
    (Join-Path $source "frontend\node_modules"),
    (Join-Path $source "frontend\dist"),
    "__pycache__",
    ".pytest_cache"
)

$excludeFiles = @(
    ".env",
    ".env.local",
    "*.log",
    "*.docx",
    "*.pyc",
    "*.pyo",
    "chatlog_*.txt",
    ".codex_write_probe",
    "_write_test.txt",
    "python_write_test.txt"
)

robocopy $source $target /E /XD $excludeDirs /XF $excludeFiles | Out-Host

Write-Host ""
Write-Host "Public release copy created at: $target"
Write-Host "Review the destination before running git init or git add."
