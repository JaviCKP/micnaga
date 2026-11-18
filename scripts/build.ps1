<#
.SYNOPSIS
  Construye Micnaga: comprobacion de licencias, PyInstaller (onedir), prueba del congelado,
  medida de tamano e instalador Inno Setup (si hay iscc).

.PARAMETER Variant
  standard (por defecto): EL producto. Sin torch; diarizacion ONNX (processing.diarization=onnx).
  torch: respaldo solo para desarrollo (diarize + torch); no se distribuye.

.PARAMETER SkipInstaller
  No compila el instalador (aunque exista iscc).

.PARAMETER SkipSelfTest
  No ejecuta `micnaga-cli.exe --selftest` sobre el resultado.

.EXAMPLE
  .\scripts\build.ps1                     # standard
  .\scripts\build.ps1 -Variant torch
  .\scripts\build.ps1 -SkipInstaller

  Salida: dist\micnaga (standard) o dist\micnaga-torch, e instaladores en dist\installer.
  `build\` y `dist\` estan en .gitignore.
#>
[CmdletBinding()]
param(
    [ValidateSet("standard", "torch")]
    [string]$Variant = "standard",
    [switch]$SkipInstaller,
    [switch]$SkipSelfTest
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$py = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "No existe .venv; ver README (uv venv + uv pip install -e .[asr,diar,aec,ui,dev])" }

function Invoke-Step([string]$Title, [scriptblock]$Action) {
    Write-Host "==> $Title" -ForegroundColor Cyan
    # PowerShell 5.1 convierte el stderr de un ejecutable nativo en error: PyInstaller escribe ahi.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Action } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) { throw "Fallo en '$Title' (codigo $LASTEXITCODE)" }
}

# 1. Licencias: bloquea la build si hay una dependencia incompatible con MIT. Regenera el fichero
#    que el instalador incluye.
Invoke-Step "Licencias (gen_licenses.py --write)" { & $py scripts\gen_licenses.py --write }

# 2. PyInstaller
$env:MICNAGA_VARIANT = $Variant
$workDir = "build\work-$Variant"
Invoke-Step "PyInstaller ($Variant)" {
    & $py -m PyInstaller --noconfirm --distpath dist --workpath $workDir packaging\micnaga.spec
}
$outDir = if ($Variant -eq "torch") { "dist\micnaga-torch" } else { "dist\micnaga" }

# 3. Prohibidos en el instalador (x264/x265, cuDNN...) y prueba del ejecutable congelado.
Invoke-Step "Binarios prohibidos en $outDir" { & $py scripts\gen_licenses.py --check --dist $outDir }
if (-not $SkipSelfTest) {
    # Las pruebas del congelado NUNCA deben tocar los datos reales del usuario.
    $scratch = Join-Path $repo "build\selftest-home"
    New-Item -ItemType Directory -Force -Path $scratch | Out-Null
    $env:MICNAGA_HOME = $scratch
    $env:MICNAGA_STORAGE = Join-Path $scratch "meetings"
    $env:MICNAGA_MODELS = Join-Path $scratch "models"
    try {
        Invoke-Step "Selftest del ejecutable congelado" { & "$outDir\micnaga-cli.exe" --selftest }
    } finally {
        Remove-Item Env:MICNAGA_HOME, Env:MICNAGA_STORAGE, Env:MICNAGA_MODELS -ErrorAction SilentlyContinue
    }
}

# 4. Tamano (la estimacion LZMA aproxima el instalador; el limite es 150 MiB, spec 2).
Invoke-Step "Tamano" { & $py scripts\measure_dist.py $outDir }

# 5. Instalador (opcional: necesita Inno Setup 6)
if (-not $SkipInstaller) {
    $iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
    if (-not $iscc) {
        foreach ($c in @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
                         "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
                         "$env:ProgramFiles\Inno Setup 6\ISCC.exe")) {
            if (Test-Path $c) { $iscc = $c; break }
        }
    }
    if ($iscc) {
        $version = (& $py -c "import micnaga; print(micnaga.__version__)").Trim()
        Invoke-Step "Inno Setup ($Variant)" {
            & $iscc "/DVariant=$Variant" "/DAppVersion=$version" packaging\micnaga.iss
        }
        Get-ChildItem dist\installer -Filter *.exe | ForEach-Object {
            "{0}: {1:N1} MiB" -f $_.Name, ($_.Length / 1MB)
        }
    } else {
        Write-Warning "Inno Setup no esta instalado (iscc.exe): se omite el instalador. packaging\micnaga.iss queda listo."
    }
}
Write-Host "Listo: $outDir" -ForegroundColor Green
