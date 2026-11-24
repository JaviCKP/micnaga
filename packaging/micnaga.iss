; Inno Setup 6 - instalador de Micnaga: UN CLIC, por usuario, sin administrador.
;
; Experiencia: se abre el instalador, copia los ficheros con una barra de progreso y arranca
; Micnaga solo al terminar. Sin paginas de bienvenida, carpeta, grupo de programas, resumen,
; tareas ni final. El arranque con Windows queda SIEMPRE activado (clave HKCU Run); se puede
; apagar luego en Ajustes. La actualizacion sustituye los ficheros tras el cierre de la app.
;
; Compilar (despues de `scripts\build.ps1`, que genera dist\micnaga):
;   iscc /DAppVersion=0.1.0 packaging\micnaga.iss       -> dist\installer\Micnaga-Setup-<version>.exe
; Firmado (lo hace build.ps1 si hay certificado; ver sus notas):
;   iscc /DSign=1 "/Smicnaga=$qC:\...\signtool.exe$q sign /fd sha256 /tr <tsa> /td sha256 /sha1 <huella> $f" ...
; Sin firma, Windows/SmartScreen/antivirus muestran «editor desconocido» y pueden bloquearlo.
;
; Instalacion silenciosa (despliegue): Micnaga-Setup-<version>.exe /VERYSILENT

#ifndef AppVersion
  #define AppVersion "0.1.1"
#endif

#define AppName "Micnaga"
#define AppExe "micnaga.exe"
; Mismo AppUserModelID que micnaga/ui/toast.py (AUMID): Windows atribuye y activa los toasts de
; la app a traves de este acceso directo del menu Inicio.
#define AppUserModelId "es.micnaga.Micnaga"
#define RunKey "Software\Microsoft\Windows\CurrentVersion\Run"
#define UninstallKey "Software\Microsoft\Windows\CurrentVersion\Uninstall\{C0D17A64-EEEB-4A5C-90DB-DB3776EEFF2A}_is1"
; Host de native messaging de la extension de Meet (DECISIONS §15): nombre e ID fijo de la extension
; (clave publica en extension/manifest.json). Mismo manifiesto que scripts\register_native_host.ps1.
#define HostName "es.micnaga.host"
#define ExtensionId "npfamnnohhilpnfalmlamccajpdandca"

; Un unico producto (incluye la diarizacion con torch CPU).
#define SourceDir "..\dist\micnaga"
#define OutputName "Micnaga-Setup-" + AppVersion
; Runtime de WebView2 (Evergreen), segun la guia de Microsoft: valor "pv" con la version instalada.
#define WebView2Key "Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

[Setup]
; GUID propio de Micnaga: no cambiarlo entre versiones (permite actualizar/desinstalar).
AppId={{C0D17A64-EEEB-4A5C-90DB-DB3776EEFF2A}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Micnaga contributors
DefaultDirName={localappdata}\Programs\Micnaga
; Una actualizacion reutiliza la carpeta de la instalacion anterior.
UsePreviousAppDir=yes
; Instalacion por usuario: sin elevacion (no toca HKLM ni Program Files).
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; ---- un clic: ninguna pagina salvo el progreso
DisableWelcomePage=yes
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
DisableReadyMemo=yes
DisableFinishedPage=yes
ShowLanguageDialog=no
LanguageDetectionMethod=none
; Un segundo instalador a la vez no tiene sentido.
SetupMutex=MicnagaSetup
; Micnaga se cierra de forma ordenada. El instalador espera sin forzar el cierre.
CloseApplications=no
RestartApplications=no
OutputDir=..\dist\installer
OutputBaseFilename={#OutputName}
SetupIconFile=..\micnaga\ui\web\assets\micnaga.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
MinVersion=10.0.19045
#ifdef Sign
; Firma el instalador y el desinstalador con la orden "micnaga" que pasa build.ps1 (/Smicnaga=...).
SignTool=micnaga
SignedUninstaller=yes
#endif

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[UninstallDelete]
; El manifiesto del host lo escribe [Code] (Inno no lo registra como fichero instalado).
Type: files; Name: "{app}\{#HostName}.json"

[InstallDelete]
; Retira solo binarios obsoletos conocidos. Una biblioteca personalizada puede vivir en _internal.
Type: files; Name: "{app}\_internal\pyaec\aec.dll"

[Files]
; La carpeta congelada completa. Hay que conservar la estructura onedir (DLL, .pyd y pystray/*.py
; sueltos): asi los componentes LGPL siguen siendo sustituibles. Ver THIRD_PARTY_LICENSES.md.
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
; El acceso directo del menu Inicio lleva el AppUserModelID (toasts). El argumento "app" abre la bandeja.
Name: "{userprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "app"; AppUserModelID: "{#AppUserModelId}"; IconFilename: "{app}\{#AppExe}"

[Registry]
; Arranque con Windows SIEMPRE activo: mismo valor y mismo comando que micnaga/ui/startup.py.
; Al ACTUALIZAR solo se reescribe si ya existia (asi se respeta quien lo apago en Ajustes).
Root: HKCU; Subkey: "{#RunKey}"; ValueType: string; ValueName: "{#AppName}"; ValueData: """{app}\{#AppExe}"" app"; Check: ShouldWriteRunKey
; Host de native messaging (por usuario, sin administrador): cada navegador lee el manifiesto que
; escribe [Code] en {app}. El navegador solo lanza el host si la extension de Micnaga se conecta.
Root: HKCU; Subkey: "Software\Google\Chrome\NativeMessagingHosts\{#HostName}"; ValueType: string; ValueData: "{app}\{#HostName}.json"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Microsoft\Edge\NativeMessagingHosts\{#HostName}"; ValueType: string; ValueData: "{app}\{#HostName}.json"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\{#HostName}"; ValueType: string; ValueData: "{app}\{#HostName}.json"; Flags: uninsdeletekey
; Citas del conector de Claude, dentro de la biblioteca del usuario.
Root: HKCU; Subkey: "Software\Classes\micnaga"; ValueType: string; ValueData: "URL:Micnaga"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\micnaga"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\micnaga\shell\open\command"; ValueType: string; ValueData: """{app}\{#AppExe}"" app --open-url ""%1"""

[Run]
; Arranca Micnaga al terminar, TAMBIEN en instalaciones silenciosas: es lo que relanza la app tras
; una actualizacion automatica (micnaga/updates.py ejecuta el instalador con /VERYSILENT
; /SUPPRESSMSGBOXES /NORESTART /NOCLOSEAPPLICATIONS). Sin "postinstall" (exige pagina final y con
; /VERYSILENT no habria nada que marcar) ni "skipifsilent": una entrada asi se ejecuta siempre.
; runasoriginaluser = con la cuenta del usuario.
Filename: "{app}\{#AppExe}"; Parameters: "app"; Flags: nowait runasoriginaluser

[UninstallRun]
; El registro de desinstalacion conserva ordenes de versiones anteriores. Sustituye sus cierres
; forzados usando los mismos RunOnceId para que solo se ejecuten estas ordenes sin efectos.
Filename: "{sys}\cmd.exe"; Parameters: "/D /C exit /B 0"; Flags: runhidden; RunOnceId: "StopMicnaga"
Filename: "{sys}\cmd.exe"; Parameters: "/D /C exit /B 0"; Flags: runhidden; RunOnceId: "StopMicnagaHost"

[Code]
// ---------------------------------------------------------------------------------------------
// Actualizaciones y ejecutables en uso
// ---------------------------------------------------------------------------------------------

function IsUpgrade(): Boolean;
begin
  Result := RegKeyExists(HKCU, '{#UninstallKey}');
end;

function ShouldWriteRunKey(): Boolean;
begin
  Result := (not IsUpgrade()) or RegValueExists(HKCU, '{#RunKey}', '{#AppName}');
end;

// Windows rechaza el acceso de escritura si hay una imagen ejecutable cargada.
// Se comprueba la ruta de esta instalacion, tambien para el worker y el host de Meet.
function ExecutableInUse(const Name: String): Boolean;
var
  FileName: String;
  Stream: TFileStream;
begin
  Result := False;
  FileName := ExpandConstant('{app}\') + Name;
  if not FileExists(FileName) then
    Exit;
  try
    Stream := TFileStream.Create(FileName, fmOpenReadWrite or fmShareDenyNone);
    Stream.Free;
  except
    Result := True;
  end;
end;

function MicnagaInUse(): Boolean;
begin
  Result := ExecutableInUse('{#AppExe}') or ExecutableInUse('micnaga-cli.exe') or
            ExecutableInUse('micnaga-host.exe');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  I: Integer;
begin
  Result := '';
  for I := 1 to 300 do
  begin
    if not MicnagaInUse() then
      Exit;
    Sleep(100);
  end;
  Result := 'Espera a que Micnaga termine de grabar o procesar. Cierra Micnaga y las pestanas ' +
            'de Meet y vuelve a ejecutar la instalacion.';
end;

function InitializeUninstall(): Boolean;
begin
  Result := not MicnagaInUse();
  if not Result and not UninstallSilent then
    MsgBox('Cierra Micnaga y las pestanas de Meet antes de desinstalar.', mbError, MB_OK);
end;

// ---------------------------------------------------------------------------------------------
// WebView2: las ventanas de Micnaga son WebView2. Viene con Windows 11 y Edge lo instala en casi
// todos los Windows 10; si falta, la app solo tendria la bandeja. No bloquea la instalacion: se
// avisa (nunca en una instalacion silenciosa: solo queda en el log) y se ofrece la descarga.
// ---------------------------------------------------------------------------------------------

function HasWebView2Key(RootKey: Integer; SubKey: String): Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(RootKey, SubKey, 'pv', Version) and (Version <> '') and
            (Version <> '0.0.0.0');
end;

function HasWebView2(): Boolean;
begin
  Result := HasWebView2Key(HKLM32, 'SOFTWARE\{#WebView2Key}') or
            HasWebView2Key(HKLM64, 'SOFTWARE\{#WebView2Key}') or
            HasWebView2Key(HKCU, 'Software\{#WebView2Key}');
end;

// Manifiesto del host de native messaging (el [Registry] apunta aqui). Se borra con {app}.
procedure WriteHostManifest();
var
  Json: String;
begin
  Json := '{' + #13#10 +
          '  "name": "{#HostName}",' + #13#10 +
          '  "description": "Micnaga: senal de llamada de Google Meet para el vigilante",' + #13#10 +
          '  "path": "micnaga-host.exe",' + #13#10 +
          '  "type": "stdio",' + #13#10 +
          '  "allowed_origins": ["chrome-extension://{#ExtensionId}/"]' + #13#10 +
          '}' + #13#10;
  if not SaveStringToFile(ExpandConstant('{app}\{#HostName}.json'), Json, False) then
    Log('could not write the native messaging host manifest');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
begin
  if CurStep = ssPostInstall then
    WriteHostManifest();
  if (CurStep <> ssPostInstall) or HasWebView2() then
    Exit;
  Log('WebView2 Runtime not found');
  if WizardSilent then
    Exit;
  if MsgBox('Micnaga necesita Microsoft Edge WebView2 Runtime para mostrar sus ventanas y no' + #13#10 +
            'esta instalado en este equipo.' + #13#10#13#10 +
            'Quieres abrir la pagina de descarga de Microsoft?', mbConfirmation, MB_YESNO) = IDYES then
    ShellExec('open', 'https://go.microsoft.com/fwlink/p/?LinkId=2124703', '', '', SW_SHOWNORMAL,
              ewNoWait, Code);
end;

// ---------------------------------------------------------------------------------------------
// Desinstalacion: quita la clave Run y el registro del host de la extension SIEMPRE; los datos del
// usuario (configuracion, huellas de voz, logs, modelos y las reuniones) solo si el usuario lo pide.
// ---------------------------------------------------------------------------------------------

procedure RemoveAutostartAndHostKeys();
begin
  // Aunque el usuario lo activase desde Ajustes (no desde el instalador), el valor lleva el
  // mismo nombre y se borra igualmente.
  RegDeleteValue(HKCU, '{#RunKey}', '{#AppName}');
  // Registro opcional del host de native messaging (scripts\register_native_host.ps1).
  RegDeleteKeyIncludingSubkeys(HKCU, 'Software\Google\Chrome\NativeMessagingHosts\es.micnaga.host');
  RegDeleteKeyIncludingSubkeys(HKCU, 'Software\Microsoft\Edge\NativeMessagingHosts\es.micnaga.host');
  RegDeleteKeyIncludingSubkeys(HKCU, 'Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\es.micnaga.host');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir, MeetingsDir, Msg: String;
begin
  if CurUninstallStep <> usUninstall then
    Exit;
  RemoveAutostartAndHostKeys();
  // Una desinstalacion silenciosa nunca borra datos del usuario.
  if UninstallSilent then
    Exit;
  DataDir := ExpandConstant('{userappdata}\Micnaga');
  MeetingsDir := ExpandConstant('{userdocs}\Micnaga');
  Msg := 'Micnaga se ha desinstalado.' + #13#10#13#10 +
         'Quieres borrar tambien tus datos? Esto elimina PARA SIEMPRE:' + #13#10 +
         '  - ' + DataDir + #13#10 +
         '    (configuracion, huellas de voz, registros y modelos descargados)' + #13#10 +
         '  - ' + MeetingsDir + #13#10 +
         '    (tus reuniones: audio y transcripciones)' + #13#10#13#10 +
         'Si cambiaste la carpeta de reuniones en Ajustes, esa carpeta no se borra.' + #13#10 +
         'Pulsa "No" para conservarlo todo.';
  if MsgBox(Msg, mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
  begin
    if DirExists(DataDir) then
      DelTree(DataDir, True, True, True);
    if DirExists(MeetingsDir) then
      DelTree(MeetingsDir, True, True, True);
  end;
end;
