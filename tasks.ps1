<#
.SYNOPSIS
    PowerShell task runner for this project (Windows equivalent of the Makefile).

.USAGE
    .\tasks.ps1 setup     # create .venv, install deps, copy .env.example -> .env
    .\tasks.ps1 check-db  # verify the DB_* connection settings in .env work
    .\tasks.ps1 embed     # introspect the live schema + build/refresh the Chroma index
    .\tasks.ps1 run       # check-db + embed + streamlit run
    .\tasks.ps1 test      # pytest
    .\tasks.ps1 lint      # ruff check + black --check + mypy
    .\tasks.ps1 format    # black + ruff --fix
    .\tasks.ps1 clean     # remove caches
#>

param(
    [Parameter(Position = 0)]
    [ValidateSet("setup", "check-db", "embed", "run", "test", "lint", "format", "clean")]
    [string]$Task = "run"
)

$ErrorActionPreference = "Stop"

$VenvDir = ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

function Invoke-Setup {
    if (-not (Test-Path $VenvDir)) {
        Write-Host "Creating virtual environment at $VenvDir ..."
        py -3.11 -m venv $VenvDir 2>$null
        if (-not (Test-Path $VenvPython)) {
            # Fall back to whatever `python` resolves to if 3.11 isn't installed
            # (see CLAUDE.md's Python-version note for this machine's setup).
            python -m venv $VenvDir
        }
    }
    & $VenvPython -m pip install --upgrade pip
    & $VenvPython -m pip install -r requirements.txt

    if (-not (Test-Path ".env")) {
        Copy-Item ".env.example" ".env"
        Write-Host "Created .env from .env.example"
    }
}

function Invoke-CheckDb {
    & $VenvPython "scripts\test_db_connection.py"
}

function Invoke-Embed {
    & $VenvPython "scripts\build_embeddings.py"
}

function Invoke-Run {
    Invoke-CheckDb
    Invoke-Embed
    & $VenvPython -m streamlit run "ui\app.py"
}

function Invoke-Test {
    & $VenvPython -m pytest
}

function Invoke-Lint {
    & $VenvPython -m ruff check .
    & $VenvPython -m black --check .
    & $VenvPython -m mypy .
}

function Invoke-Format {
    & $VenvPython -m black .
    & $VenvPython -m ruff check --fix .
}

function Invoke-Clean {
    Get-ChildItem -Path . -Recurse -Directory -Filter "__pycache__" |
        Remove-Item -Recurse -Force -Confirm:$false
    foreach ($dir in @(".pytest_cache", ".ruff_cache", ".mypy_cache")) {
        if (Test-Path $dir) { Remove-Item $dir -Recurse -Force -Confirm:$false }
    }
}

switch ($Task) {
    "setup"    { Invoke-Setup }
    "check-db" { Invoke-CheckDb }
    "embed"    { Invoke-Embed }
    "run"      { Invoke-Run }
    "test"     { Invoke-Test }
    "lint"     { Invoke-Lint }
    "format"   { Invoke-Format }
    "clean"    { Invoke-Clean }
}
