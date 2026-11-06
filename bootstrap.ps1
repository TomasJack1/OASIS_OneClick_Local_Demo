$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$venv = Join-Path $root ".venv"
$python = Join-Path $venv "Scripts\python.exe"
$marker = Join-Path $root ".dependencies_ready"

try {
    if ((Test-Path $python) -and (Test-Path $marker)) {
        Write-Host "[OK] Local Python environment is ready."
        exit 0
    }

    $havePython311 = $false
    try {
        & py -3.11 --version *> $null
        $havePython311 = ($LASTEXITCODE -eq 0)
    } catch { $havePython311 = $false }

    if ($havePython311) {
        Write-Host "[SETUP] Python 3.11 detected. Creating isolated environment..."
        if (-not (Test-Path $python)) {
            & py -3.11 -m venv $venv
            if ($LASTEXITCODE -ne 0) { throw "Failed to create Python venv" }
        }
        Write-Host "[SETUP] Installing OASIS and Streamlit packages via pip..."
        & $python -m pip install --disable-pip-version-check -r (Join-Path $root "requirements.txt")
        if ($LASTEXITCODE -ne 0) { throw "pip failed" }
    } else {
        Write-Host "[SETUP] Python 3.11 not found. Downloading portable uv for this user..."
        $tools = Join-Path $root ".tools"
        New-Item -ItemType Directory -Path $tools -Force | Out-Null
        $uv = Join-Path $tools "uv.exe"
        if (-not (Test-Path $uv)) {
            $archive = Join-Path $tools "uv-win64.zip"
            $url = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
            Invoke-WebRequest -Uri $url -OutFile $archive -UseBasicParsing
            Expand-Archive -LiteralPath $archive -DestinationPath $tools -Force
            $found = Get-ChildItem -Path $tools -Filter uv.exe -Recurse -File | Select-Object -First 1
            if (-not $found) { throw "Downloaded archive does not contain uv.exe" }
            if ($found.FullName -ne $uv) { Copy-Item $found.FullName $uv -Force }
            Remove-Item $archive -Force
        }
        Write-Host "[SETUP] Downloading managed Python 3.11..."
        & $uv python install 3.11
        if ($LASTEXITCODE -ne 0) { throw "uv Python download failed" }
        if (-not (Test-Path $python)) {
            & $uv venv --python 3.11 $venv
            if ($LASTEXITCODE -ne 0) { throw "uv venv failed" }
        }
        Write-Host "[SETUP] Installing OASIS and Streamlit packages via uv..."
        & $uv pip install --python $python -r (Join-Path $root "requirements.txt")
        if ($LASTEXITCODE -ne 0) { throw "uv pip install failed" }
    }

    Write-Host "[CHECK] Checking imported modules..."
    & $python -c "import streamlit, oasis, camel, pandas, plotly, networkx; print('All core imports succeeded')"
    if ($LASTEXITCODE -ne 0) { throw "Python import smoke test failed" }
    Set-Content -LiteralPath $marker -Value "ready" -Encoding Ascii
    Write-Host "[OK] Local environment is installed."
    exit 0
} catch {
    Write-Host "[ERROR] First-run setup failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Internet access to PyPI and GitHub is required for the first run."
    Write-Host "If your network blocks these downloads, ask the project author for an offline prepared environment."
    exit 1
}
