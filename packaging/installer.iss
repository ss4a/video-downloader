; Inno Setup script for Video Downloader
; Build:  ISCC.exe packaging\installer.iss   (after running PyInstaller)

#define AppName      "Video Downloader"
#define AppVersion   "1.0.0"
#define AppExe       "VideoDownloader.exe"
#define AppPublisher "Video Downloader"

[Setup]
AppId={{6E2B7C1D-4F3A-4E8B-9C2D-8A1F5B3E7D40}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName} Setup

; Installs per-user by default (no admin prompt); the user can pick
; "Install for all users" on the first page instead.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no
DisableWelcomePage=no
UsePreviousAppDir=yes

; 64-bit Windows 10 / 11 only
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

WizardStyle=modern
SetupIconFile=app.ico
WizardSmallImageFile=wizard-small.bmp,wizard-small-2x.bmp
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}

OutputDir=..\Release
OutputBaseFilename=VideoDownloader_Setup
Compression=lzma2/ultra64
SolidCompression=yes
; Compress in a separate (64-bit capable) process to avoid the compiler running out of memory
LZMAUseSeparateProcess=yes

; Close a running copy before upgrading / uninstalling
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel2=This will install [name/ver] on your computer.%n%nPaste a video link, preview it, and download it in one click. Everything it needs is included — no other software required.%n%nIt is recommended that you close all other applications before continuing.

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\VideoDownloader\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "THIRD_PARTY_NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\vendor\licenses\*"; DestDir: "{app}\licenses"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Only the app's own log/cache. Downloaded videos (in Downloads\Video Downloader)
; are never touched.
Type: filesandordirs; Name: "{localappdata}\{#AppName}"
Type: dirifempty; Name: "{app}"

[Code]
// The app window uses the Microsoft Edge WebView2 Runtime. It ships with
// Windows 11 and up-to-date Windows 10; install it if it's missing.
// (If this fails, the app still works by opening in the default browser.)
const
  WV2Key = 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function HasWebView2: Boolean;
var v: String;
begin
  Result :=
    (RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', v) and (v <> '') and (v <> '0.0.0.0')) or
    (RegQueryStringValue(HKLM, WV2Key, 'pv', v) and (v <> '') and (v <> '0.0.0.0')) or
    (RegQueryStringValue(HKCU, WV2Key, 'pv', v) and (v <> '') and (v <> '0.0.0.0'));
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if (CurStep = ssPostInstall) and (not HasWebView2) then
  begin
    WizardForm.StatusLabel.Caption := 'Installing Microsoft Edge WebView2 Runtime...';
    try
      DownloadTemporaryFile('https://go.microsoft.com/fwlink/p/?LinkId=2124703',
                            'MicrosoftEdgeWebview2Setup.exe', '', nil);
      Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'), '/silent /install', '',
           SW_HIDE, ewWaitUntilTerminated, ResultCode);
    except
      Log('WebView2 install skipped: ' + GetExceptionMessage);
    end;
  end;
end;
