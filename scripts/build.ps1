<#
.SYNOPSIS
  Construye Micnaga: comprobacion de licencias, PyInstaller (onedir), firma de codigo (opcional),
  prueba del congelado, medida de tamano e instalador Inno Setup (si hay iscc).

  Un unico producto: incluye la diarizacion `diarize` (torch CPU). Ya no hay variantes.

.PARAMETER SkipInstaller
  No compila el instalador (aunque exista iscc).

.PARAMETER SkipSelfTest
  No ejecuta `micnaga-cli.exe --selftest` sobre el resultado.

.PARAMETER Python
  Interprete con las dependencias instaladas. Por defecto `<repo>\.venv\Scripts\python.exe` (o la
  variable MICNAGA_PYTHON). Util en un worktree sin `.venv` propio.

.NOTES
  FIRMA DE CODIGO (opcional; sin ella Windows/SmartScreen/antivirus muestran «editor desconocido»
  y pueden bloquear la instalacion o DLL sueltas como paso con `aec.dll`). Se activa si existe:
    MICNAGA_SIGN_THUMBPRINT   huella SHA-1 de un certificado de firma de codigo del almacen del
                              usuario/equipo (p. ej. un token EV), o
    MICNAGA_SIGN_PFX          ruta a un .pfx (+ MICNAGA_SIGN_PFX_PASSWORD si lo tiene).
  Opcionales: MICNAGA_SIGNTOOL (ruta a signtool.exe; si no, se busca en el Windows SDK) y
  MICNAGA_SIGN_TIMESTAMP (servidor RFC 3161; por defecto http://timestamp.digicert.com).
  Se firman micnaga*.exe, toda DLL/.pyd de dist\micnaga que NO venga ya firmada (las de Microsoft,
  Intel... se dejan como estan) y el instalador y su desinstalador (directiva SignTool de Inno).
  Sin certificado: aviso y se sigue SIN firmar.

.EXAMPLE
  .\scripts\build.ps1
  .\scripts\build.ps1 -SkipInstaller
  $env:MICNAGA_SIGN_THUMBPRINT = "0123...ABCD"; .\scripts\build.ps1

  Salida: dist\micnaga e instalador en dist\installer. `build\` y `dist\` estan en .gitignore.
#>
[CmdletBinding()]
param(
    [switch]$SkipInstaller,
    [switch]$SkipSelfTest,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$py = if ($Python) { $Python } elseif ($env:MICNAGA_PYTHON) { $env:MICNAGA_PYTHON } else { Join-Path $repo ".venv\Scripts\python.exe" }
if (-not (Test-Path $py)) { throw "No existe $py; ver README (uv venv + uv pip install -e .[asr,diar,aec,ui,dev]) o usar -Python" }
# El codigo que se congela es SIEMPRE el de este arbol (un -Python de otro checkout con instalacion
# editable apuntaria a su propio micnaga).
$env:PYTHONPATH = $repo

function Invoke-Step([string]$Title, [scriptblock]$Action) {
    Write-Host "==> $Title" -ForegroundColor Cyan
    # PowerShell 5.1 convierte el stderr de un ejecutable nativo en error: PyInstaller escribe ahi.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Action } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) { throw "Fallo en '$Title' (codigo $LASTEXITCODE)" }
}

# ------------------------------------------------------------------------------- firma de codigo
function Find-SignTool {
    if ($env:MICNAGA_SIGNTOOL -and (Test-Path $env:MICNAGA_SIGNTOOL)) { return $env:MICNAGA_SIGNTOOL }
    $cmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $kits = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    if (Test-Path $kits) {
        $found = Get-ChildItem $kits -Filter signtool.exe -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match "\\x64\\" } | Sort-Object FullName -Descending | Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    return $null
}

function Get-SignArguments {
    # Argumentos de `signtool sign` (sin los ficheros); $null si no hay certificado configurado.
    $timestamp = if ($env:MICNAGA_SIGN_TIMESTAMP) { $env:MICNAGA_SIGN_TIMESTAMP } else { "http://timestamp.digicert.com" }
    $common = @("sign", "/fd", "sha256", "/tr", $timestamp, "/td", "sha256", "/d", "Micnaga")
    if ($env:MICNAGA_SIGN_THUMBPRINT) {
        return $common + @("/sha1", $env:MICNAGA_SIGN_THUMBPRINT)
    }
    if ($env:MICNAGA_SIGN_PFX) {
        if (-not (Test-Path $env:MICNAGA_SIGN_PFX)) { throw "MICNAGA_SIGN_PFX no existe: $env:MICNAGA_SIGN_PFX" }
        $pfxArgs = $common + @("/f", $env:MICNAGA_SIGN_PFX)
        if ($env:MICNAGA_SIGN_PFX_PASSWORD) { $pfxArgs += @("/p", $env:MICNAGA_SIGN_PFX_PASSWORD) }
        return $pfxArgs
    }
    return $null
}

$signTool = $null
$signArgs = Get-SignArguments
if ($signArgs) {
    $signTool = Find-SignTool
    if (-not $signTool) { throw "Hay certificado (MICNAGA_SIGN_*) pero no se encuentra signtool.exe (Windows SDK o MICNAGA_SIGNTOOL)" }
} else {
    Write-Warning ("Sin certificado de firma (MICNAGA_SIGN_THUMBPRINT o MICNAGA_SIGN_PFX): el programa y el instalador " +
        "saldran SIN FIRMAR y Windows/antivirus los mostraran como de editor desconocido.")
}

function Invoke-Sign([string[]]$Files) {
    # En tandas: la linea de comandos de Windows tiene un limite de longitud.
    for ($i = 0; $i -lt $Files.Count; $i += 40) {
        $batch = $Files[$i..([Math]::Min($i + 39, $Files.Count - 1))]
        Invoke-Step "Firma ($($i + 1)-$($i + $batch.Count) de $($Files.Count))" { & $signTool @signArgs @batch }
    }
}

# 1. Licencias: bloquea la build si hay una dependencia incompatible con MIT. Regenera el fichero
#    que el instalador incluye.
Invoke-Step "Licencias (gen_licenses.py --write)" { & $py scripts\gen_licenses.py --write }

# 2. PyInstaller
Remove-Item Env:MICNAGA_VARIANT -ErrorAction SilentlyContinue
Invoke-Step "PyInstaller" {
    & $py -m PyInstaller --noconfirm --distpath dist --workpath build\work-standard packaging\micnaga.spec
}
$outDir = "dist\micnaga"

# 3. Prohibidos en el instalador (x264/x265, cuDNN, aec.dll...).
Invoke-Step "Binarios prohibidos en $outDir" { & $py scripts\gen_licenses.py --check --dist $outDir }

# 4. Firma (si hay certificado): ejecutables y DLL/.pyd que no traigan ya una firma valida.
if ($signArgs) {
    $unsigned = Get-ChildItem $outDir -Recurse -File -Include *.exe, *.dll, *.pyd |
        Where-Object { (Get-AuthenticodeSignature $_.FullName).Status -ne "Valid" } |
        ForEach-Object { $_.FullName }
    Invoke-Sign @($unsigned)
}

# 5. Prueba del ejecutable congelado (despues de firmar: valida lo que se distribuye).
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

# 6. Tamano (la estimacion LZMA aproxima el instalador; el real lo da iscc).
Invoke-Step "Tamano" { & $py scripts\measure_dist.py $outDir }

# 7. Instalador (opcional: necesita Inno Setup 6)
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
        $isccArgs = @("/DAppVersion=$version")
        if ($signArgs) {
            # Inno firma el instalador y el desinstalador con esta orden ($f = fichero, $q = comilla).
            $quoted = ($signArgs | ForEach-Object { if ($_ -match "\s") { "`$q$_`$q" } else { $_ } }) -join " "
            $isccArgs += @("/DSign=1", "/Smicnaga=`$q$signTool`$q $quoted `$f")
        }
        Invoke-Step "Inno Setup" { & $iscc @isccArgs packaging\micnaga.iss }
        Get-ChildItem dist\installer -Filter *.exe | ForEach-Object {
            "{0}: {1:N1} MiB" -f $_.Name, ($_.Length / 1MB)
        }
        # Sumas para las actualizaciones automaticas (micnaga/updates.py): formato sha256sum,
        # "<sha256 en minusculas>  <fichero>", ASCII y saltos LF.
        $sums = Get-ChildItem dist\installer -Filter *.exe | Sort-Object Name | ForEach-Object {
            "{0}  {1}" -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant(), $_.Name
        }
        $sumsPath = Join-Path $repo "dist\installer\SHA256SUMS.txt"
        [System.IO.File]::WriteAllText($sumsPath, (($sums -join "`n") + "`n"), (New-Object System.Text.ASCIIEncoding))
        Write-Host "SHA256SUMS.txt: $sumsPath"
    } else {
        Write-Warning "Inno Setup no esta instalado (iscc.exe): se omite el instalador. packaging\micnaga.iss queda listo."
    }
}
Write-Host "Listo: $outDir" -ForegroundColor Green
