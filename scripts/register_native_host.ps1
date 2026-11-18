<#
.SYNOPSIS
  Registra (o quita) el host de native messaging de Micnaga ("es.micnaga.host") para el usuario actual.

.DESCRIPTION
  1. Escribe el manifiesto del host (JSON) en %APPDATA%\Micnaga\extension\es.micnaga.host.json a
     partir de extension\native-host\es.micnaga.host.json.template.
  2. Crea, en HKCU (no hace falta administrador), la clave NativeMessagingHosts\es.micnaga.host de
     Chrome, Edge y/o Brave apuntando a ese manifiesto.

  NO se ejecuta nunca de forma automatica: lo lanza el usuario a mano. Usa -WhatIf para ver, sin
  escribir nada, que haria. El instalador NO llama a este script.

  La extension se instala aparte (chrome://extensions > Modo desarrollador > Cargar descomprimida >
  carpeta extension\). Su ID es fijo (campo "key" de manifest.json) y se calcula aqui mismo.

.PARAMETER HostPath
  Ruta del host. Instalado: <carpeta de Micnaga>\micnaga-host.exe. Desarrollo:
  extension\native-host\micnaga-host-dev.bat (valor por defecto).

.PARAMETER Browsers
  chrome, edge, brave (por defecto los tres).

.PARAMETER Unregister
  Borra las claves del registro y el manifiesto en vez de crearlos.

.EXAMPLE
  .\scripts\register_native_host.ps1 -WhatIf
  .\scripts\register_native_host.ps1 -HostPath "$env:LOCALAPPDATA\Programs\Micnaga\micnaga-host.exe"
  .\scripts\register_native_host.ps1 -Unregister
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$HostPath = "",
    [ValidateSet("chrome", "edge", "brave")]
    [string[]]$Browsers = @("chrome", "edge", "brave"),
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$hostName = "es.micnaga.host"
$keys = @{
    chrome = "HKCU:\Software\Google\Chrome\NativeMessagingHosts\$hostName"
    edge   = "HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\$hostName"
    brave  = "HKCU:\Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\$hostName"
}
$dataDir = if ($env:MICNAGA_HOME) { $env:MICNAGA_HOME } else { Join-Path $env:APPDATA "Micnaga" }
$manifestDir = Join-Path $dataDir "extension"
$manifestPath = Join-Path $manifestDir "$hostName.json"

function Get-ExtensionId([string]$KeyBase64) {
    # Algoritmo de Chrome: SHA-256 de la clave publica (DER), 16 primeros bytes, nibbles -> a..p.
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $hash = $sha.ComputeHash([Convert]::FromBase64String($KeyBase64))
    -join ($hash[0..15] | ForEach-Object {
        [char](97 + ($_ -shr 4)) + [char](97 + ($_ -band 15))
    })
}

if ($Unregister) {
    foreach ($b in $Browsers) {
        if (Test-Path $keys[$b]) {
            if ($PSCmdlet.ShouldProcess($keys[$b], "Remove-Item")) { Remove-Item $keys[$b] -Recurse }
        }
    }
    if (Test-Path $manifestPath) {
        if ($PSCmdlet.ShouldProcess($manifestPath, "Remove-Item")) { Remove-Item $manifestPath }
    }
    return
}

if (-not $HostPath) { $HostPath = Join-Path $repo "extension\native-host\micnaga-host-dev.bat" }
$HostPath = [System.IO.Path]::GetFullPath($HostPath)
if (-not (Test-Path $HostPath)) { throw "No existe el host: $HostPath" }

$extManifest = Get-Content (Join-Path $repo "extension\manifest.json") -Raw | ConvertFrom-Json
$extensionId = Get-ExtensionId $extManifest.key
$template = Get-Content (Join-Path $repo "extension\native-host\$hostName.json.template") -Raw
# JSON necesita las barras invertidas de la ruta escapadas
$json = $template.Replace("@HOST_PATH@", $HostPath.Replace("\", "\\")).Replace("@EXTENSION_ID@", $extensionId)
$null = $json | ConvertFrom-Json  # valida

Write-Host "Extension ID : $extensionId"
Write-Host "Host         : $HostPath"
Write-Host "Manifiesto   : $manifestPath"

if ($PSCmdlet.ShouldProcess($manifestPath, "Escribir manifiesto del host")) {
    New-Item -ItemType Directory -Force -Path $manifestDir | Out-Null
    [System.IO.File]::WriteAllText($manifestPath, $json, (New-Object System.Text.UTF8Encoding($false)))
}
foreach ($b in $Browsers) {
    if ($PSCmdlet.ShouldProcess($keys[$b], "Crear clave de registro (valor por defecto = ruta del manifiesto)")) {
        New-Item -Path $keys[$b] -Force | Out-Null
        Set-ItemProperty -Path $keys[$b] -Name "(default)" -Value $manifestPath
    }
}
