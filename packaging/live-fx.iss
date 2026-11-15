#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif

#define MyAppName "LIVE FX"
#define MyAppExeName "Live FX.exe"

[Setup]
AppId={{93B7862E-3A6C-4F86-9FA7-D20115705B83}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=LIVE FX
DefaultDirName={autopf}\LIVE FX
DefaultGroupName=LIVE FX
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile=..\assets\live-fx.ico
OutputDir=..\dist
OutputBaseFilename=LIVE-FX-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
CloseApplications=yes
RestartApplications=no
VersionInfoVersion={#MyAppVersion}.0
VersionInfoProductName={#MyAppName}
VersionInfoDescription=LIVE FX installer
VersionInfoCompany=LIVE FX

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\LIVE FX"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\LIVE FX"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch LIVE FX"; WorkingDir: "{app}"; Flags: postinstall nowait skipifsilent
