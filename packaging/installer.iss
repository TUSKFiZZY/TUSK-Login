; Inno Setup recipe for TUSKFiZZY's installer. Run by build-installer.ps1 with /DAppVersion=<version>, after
; shell\build\package.mjs has packed the Electron shell, and the core in its resources, into shell\out\TUSKFiZZY-win32-x64.
;
; It installs for the one Windows user who runs it, with no administrator rights, into
; %LOCALAPPDATA%\Programs\TUSKFiZZY, and adds TUSKFiZZY to the Start menu and, if wanted, the desktop.
; The player's own data, %LOCALAPPDATA%\TUSKFiZZY, is never touched: an update and an uninstall both leave it.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define Root ".."
#define Built Root + "\shell\out\TUSKFiZZY-win32-x64"

[Setup]
AppId={{B71D2F4A-8C63-4D91-9A12-5F7E3C8B6D20}
AppName=TUSKFiZZY
AppVersion={#AppVersion}
AppVerName=TUSKFiZZY {#AppVersion}
AppPublisher=TUSKFiZZY
LicenseFile={#Root}\LICENSE
DefaultDirName={localappdata}\Programs\TUSKFiZZY
DefaultGroupName=TUSKFiZZY
DisableProgramGroupPage=yes
DisableDirPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
SetupIconFile={#Root}\shell\assets\icon.ico
UninstallDisplayIcon={app}\tuskfizzy-app.exe
UninstallDisplayName=TUSKFiZZY
OutputDir={#Root}\dist-installer
OutputBaseFilename=TUSKFiZZY Setup {#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=no

[Tasks]
Name: "desktop"; Description: "Put a TUSKFiZZY shortcut on the desktop"

[Files]
Source: "{#Built}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; The packed folder already carries Electron's own LICENSE and LICENSES.chromium.html beside the program, and they stay (the
; notices say so). TUSKFiZZY's licence therefore goes beside them under another name, with the notices of the software inside
; it and the privacy notice, where the player can find them.
Source: "{#Root}\LICENSE"; DestDir: "{app}"; DestName: "LICENSE-TUSKFiZZY.txt"; Flags: ignoreversion
Source: "{#Root}\THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\PRIVACY.md"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; An update replaces the whole program, so files an older version had and this one has not do not linger.
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\guildboard-tray.exe"
; The core's folder is the installer's own, and a new version's core replaces it whole (app.asar is replaced by [Files]). Only
; this one folder is named: resources is too common a name to delete, if the program is installed into a shared folder.
Type: filesandordirs; Name: "{app}\resources\core"

[Icons]
Name: "{userprograms}\TUSKFiZZY"; Filename: "{app}\tuskfizzy-app.exe"
Name: "{userdesktop}\TUSKFiZZY"; Filename: "{app}\tuskfizzy-app.exe"; Tasks: desktop

[Registry]
; tuskfizzy:// opens TUSKFiZZY, for an invite link. The current user's own classes, removed on uninstall. This is Guild
; Board's own key; nothing in the program itself touches the registry. The -- keeps whatever a link holds from being an option.
Root: HKCU; Subkey: "Software\Classes\guildboard"; ValueType: string; ValueName: ""; ValueData: "URL:TUSKFiZZY"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\guildboard"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\guildboard\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\tuskfizzy-app.exe,0"
Root: HKCU; Subkey: "Software\Classes\guildboard\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\tuskfizzy-app.exe"" -- ""%1"""

[Run]
Filename: "{app}\tuskfizzy-app.exe"; Description: "Open TUSKFiZZY"; Flags: postinstall nowait

[Code]
// Start with Windows, if it was turned on in TUSKFiZZY's settings, goes with the program; a shortcut of the same
// name that starts another copy of TUSKFiZZY stays.
procedure RemoveStartupShortcut();
var
  Shell, Link: Variant;
  Path: String;
begin
  Path := ExpandConstant('{userstartup}\TUSKFiZZY.lnk');
  if not FileExists(Path) then
    exit;
  try
    Shell := CreateOleObject('WScript.Shell');
    Link := Shell.CreateShortcut(Path);
    if CompareText(ExtractFilePath(Link.TargetPath), AddBackslash(ExpandConstant('{app}'))) = 0 then
      DeleteFile(Path);
  except
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveStartupShortcut();
end;

// A running TUSKFiZZY is asked to quit before its files are replaced or removed; it waits until it has.
function QuitTUSKFiZZY(Exe: String): Boolean;
var
  Code: Integer;
begin
  Result := True;
  if FileExists(Exe) then
    Result := Exec(Exe, '--quit', '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not QuitTUSKFiZZY(ExpandConstant('{app}\tuskfizzy-app.exe')) then
    Result := 'TUSKFiZZY could not be closed. Quit it from its tray icon and run the setup again.';
end;

function InitializeUninstall(): Boolean;
begin
  QuitTUSKFiZZY(ExpandConstant('{app}\tuskfizzy-app.exe'));
  Result := True;  // the uninstall goes ahead: a TUSKFiZZY still running keeps only the files it holds
end;
