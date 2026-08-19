[CmdletBinding()]
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet("setup", "test", "lint", "build", "run")]
    [string]$Action,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Command
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = (Resolve-Path $PSScriptRoot).Path
$uv = Get-Command ($env:UV_BIN ?? "uv") -ErrorAction SilentlyContinue
if (-not $uv) {
    Write-Error "uv is required; install it as a device bootstrap prerequisite."
    exit 127
}

# Do not let legacy process state bypass centralized project environments.
Remove-Item Env:UV_PROJECT_ENVIRONMENT -ErrorAction SilentlyContinue
$features = @($env:UV_PREVIEW_FEATURES -split ',' | Where-Object { $_ })
if ($features -notcontains "centralized-project-envs") {
    $env:UV_PREVIEW_FEATURES = ($features + "centralized-project-envs") -join ','
}

Push-Location $projectRoot
try {
    & $uv.Source sync --locked --extra dev
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    switch ($Action) {
        "setup" { if ($Command.Count -gt 0) { throw "setup accepts no arguments" } }
        "test" {
            if ($Command.Count -gt 0) { throw "test accepts no arguments" }
            & $uv.Source run --locked --extra dev python -m pytest
        }
        "lint" {
            if ($Command.Count -gt 0) { throw "lint accepts no arguments" }
            & $uv.Source run --locked --extra dev ruff check .
            if ($LASTEXITCODE -eq 0) { & $uv.Source run --locked --extra dev mypy src }
        }
        "build" {
            if ($Command.Count -gt 0) { throw "build accepts no arguments" }
            & $uv.Source build
        }
        "run" {
            if ($Command.Count -eq 0) { throw "run requires a command" }
            & $uv.Source run --locked --extra dev @Command
        }
    }
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
