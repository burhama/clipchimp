; ClipChimp installer (Inno Setup 6). Built by .github/workflows/release.yml from the PyInstaller folder.
; One approval: it installs to Program Files and registers a sign-in task that starts ClipChimp with
; administrator rights, so ClipChimp can clip over windows that run as administrator.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6E3C2B1A-6F7D-4C59-9B57-2E8D1C0F4A11}
AppName=ClipChimp
AppVersion={#AppVersion}
AppPublisher=ClipChimp contributors
DefaultDirName={autopf}\ClipChimp
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=ClipChimp-Setup-{#AppVersion}
SetupIconFile=..\clipchimp.ico
UninstallDisplayIcon={app}\ClipChimp.exe
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
CloseApplications=no

[Files]
Source: "..\dist\ClipChimp\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\ClipChimp"; Filename: "{sys}\schtasks.exe"; Parameters: "/Run /TN ""ClipChimp"""; \
  IconFilename: "{app}\ClipChimp.exe"; Flags: runminimized

[Run]
Filename: "{sys}\schtasks.exe"; Parameters: "/Create /TN ""ClipChimp"" /XML ""{tmp}\clipchimp-task.xml"" /F"; \
  Flags: runhidden; StatusMsg: "Starting ClipChimp with Windows..."
Filename: "{sys}\schtasks.exe"; Parameters: "/Run /TN ""ClipChimp"""; Flags: runhidden

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/IM ClipChimp.exe /F"; Flags: runhidden; RunOnceId: "StopClipChimp"
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN ""ClipChimp"" /F"; Flags: runhidden; RunOnceId: "RemoveTask"

[Code]
function XmlEscape(S: String): String;
begin
  StringChangeEx(S, '&', '&amp;', True);
  StringChangeEx(S, '<', '&lt;', True);
  StringChangeEx(S, '>', '&gt;', True);
  Result := S;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  { an older copy must not hold its files open while they are replaced }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/IM ClipChimp.exe /F', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  User, Xml: String;
  Lines: TArrayOfString;
begin
  if CurStep = ssPostInstall then
  begin
    User := XmlEscape(GetEnv('USERDOMAIN') + '\' + GetUserNameString);
    Xml :=
      '<?xml version="1.0" encoding="UTF-8"?>' + #13#10 +
      '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">' +
      '<RegistrationInfo><Description>Starts ClipChimp at sign-in so it can clip over every window.</Description></RegistrationInfo>' +
      '<Triggers><LogonTrigger><Enabled>true</Enabled><UserId>' + User + '</UserId></LogonTrigger></Triggers>' +
      '<Principals><Principal id="Author"><UserId>' + User + '</UserId><LogonType>InteractiveToken</LogonType>' +
      '<RunLevel>HighestAvailable</RunLevel></Principal></Principals>' +
      '<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>' +
      '<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>' +
      '<AllowHardTerminate>true</AllowHardTerminate><StartWhenAvailable>false</StartWhenAvailable>' +
      '<RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>' +
      '<IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>' +
      '<AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled><Hidden>false</Hidden>' +
      '<RunOnlyIfIdle>false</RunOnlyIfIdle><WakeToRun>false</WakeToRun>' +
      '<ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Priority>4</Priority></Settings>' +
      '<Actions Context="Author"><Exec><Command>"' + XmlEscape(ExpandConstant('{app}\ClipChimp.exe')) + '"</Command></Exec></Actions>' +
      '</Task>';
    SetArrayLength(Lines, 1);
    Lines[0] := Xml;
    SaveStringsToUTF8File(ExpandConstant('{tmp}\clipchimp-task.xml'), Lines, False);
  end;
end;
