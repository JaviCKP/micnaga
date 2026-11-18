; Inno Setup 6 - instalador de Micnaga: UN CLIC, por usuario, sin administrador.
;
; Experiencia: se abre el instalador, copia los ficheros con una barra de progreso y arranca
; Micnaga solo al terminar. Sin paginas de bienvenida, carpeta, grupo de programas, resumen,
; tareas ni final. El arranque con Windows queda SIEMPRE activado (clave HKCU Run); se puede
; apagar luego en Ajustes. Actualizar = ejecutar el instalador nuevo encima: cierra la app en
; marcha, sustituye los ficheros y la vuelve a abrir.
;
; Compilar (despues de `scripts\build.ps1`, que genera dist\micnaga):
;   iscc /DAppVersion=0.1.0 packaging\micnaga.iss       -> dist\installer\Micnaga-Setup-<version>.exe
;   iscc /DVariant=torch   packaging\micnaga.iss        -> solo desarrollo (diarize + torch), no se distribuye
;
; Instalacion silenciosa (despliegue): Micnaga-Setup-<version>.exe /VERYSILENT

#ifndef Variant
  #define Variant "standard"
#endif
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

#define AppName "Micnaga"
#define AppExe "micnaga.exe"
; Mismo AppUserModelID que micnaga/ui/toast.py (AUMID): Windows atribuye y activa los toasts de
; la app a traves de este acceso directo del menu Inicio.
#define AppUserModelId "es.micnaga.Micnaga"
#define RunKey "Software\Microsoft\Windows\CurrentVersion\Run"
#define UninstallKey "Software\Microsoft\Windows\CurrentVersion\Uninstall\{C0D17A64-EEEB-4A5C-90DB-DB3776EEFF2A}_is1"

#if Variant == "torch"
  #define SourceDir "..\dist\micnaga-torch"
  #define OutputName "Micnaga-Setup-" + AppVersion + "-torch"
#else
  #define SourceDir "..\dist\micnaga"
  #define OutputName "Micnaga-Setup-" + AppVersion
#endif

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
; La app se cierra en PrepareToInstall (mas abajo); no se usa el Restart Manager (pediria permiso).
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

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

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

[Run]
; Arranca Micnaga al terminar, tambien en instalaciones silenciosas. Sin "postinstall": no hay
; pagina final, asi que se ejecuta siempre. runasoriginaluser = con la cuenta del usuario.
Filename: "{app}\{#AppExe}"; Parameters: "app"; Flags: nowait runasoriginaluser

[UninstallRun]
; Micnaga y su worker son el mismo ejecutable: se cierran antes de borrar la carpeta.
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#AppExe}"; Flags: runhidden; RunOnceId: "StopMicnaga"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM micnaga-host.exe"; Flags: runhidden; RunOnceId: "StopMicnagaHost"

[Code]
// ---------------------------------------------------------------------------------------------
// Actualizaciones y mutex de la app
// ---------------------------------------------------------------------------------------------

function IsUpgrade(): Boolean;
begin
  Result := RegKeyExists(HKCU, '{#UninstallKey}');
end;

function ShouldWriteRunKey(): Boolean;
begin
  Result := (not IsUpgrade()) or RegValueExists(HKCU, '{#RunKey}', '{#AppName}');
end;

// Mismo nombre que micnaga/app.py (SingleInstance): "Local\micnaga-app-" + sha1(ruta de datos en
// minusculas)[:10]. GetSHA1OfString hace lo mismo que hashlib en rutas ASCII (comprobado); con
// nombres de usuario no ASCII el hash difiere y solo se pierde la espera: la app se cierra igual.
function AppMutexName(): String;
var
  DataDir: String;
begin
  DataDir := GetEnv('MICNAGA_HOME');
  if DataDir = '' then
    DataDir := ExpandConstant('{userappdata}\Micnaga');
  Result := 'Local\micnaga-app-' + Copy(GetSHA1OfString(Lowercase(DataDir)), 1, 10);
end;

procedure StopMicnaga();
var
  Code, I: Integer;
begin
  // La app, su worker y el host de la extension. Una parada forzada es segura: el audio ya
  // escrito se recupera al arrancar y los trabajos a medias vuelven a la cola (spec: nunca
  // perder una grabacion).
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM micnaga-host.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
  for I := 1 to 50 do
  begin
    if not CheckForMutexes(AppMutexName()) then
      Break;
    Sleep(100);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopMicnaga();
  Result := '';
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
