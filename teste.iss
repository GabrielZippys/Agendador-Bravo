; ============================
; AgendadorBravo — Inno Setup
; ============================

#define MyAppName      "AgendadorBravo"
#define MyAppVersion   "1.0.0"
#define MyAppExeName   "AgendadorBravo.exe"

; --- Se você instalar o Inno Download Plugin (IDP), o include abaixo será usado automaticamente.
;     Baixe o plugin em: https://mitrichsoftware.wordpress.com/inno-download-plugin/
;     Depois, copie o arquivo "idp.iss" para a pasta do Inno (ex.: C:\Program Files (x86)\Inno Setup 6\)
#ifexist "idp.iss"
  #define HAVE_IDP
  #include "idp.iss"
#endif

[Setup]
AppId={{7D0C3A64-AB00-45A0-8E22-AB-AGENDADORBRAVO}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={pf}\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir=.
OutputBaseFilename=AgendadorBravo-Setup
ArchitecturesInstallIn64BitMode=x64
Compression=lzma
SolidCompression=yes
PrivilegesRequired=admin
WizardStyle=modern
SetupLogging=yes

[Languages]
Name: "portuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "Criar atalho na &Área de Trabalho"; GroupDescription: "Atalhos:"; Flags: unchecked

[Files]
; --- seu executável
Source: "AgendadorBravo.exe"; DestDir: "{app}"; Flags: ignoreversion
; --- logo (opcional)
Source: "Logo.ico"; DestDir: "{app}"; Flags: ignoreversion; Attribs: readonly; Check: FileExists(ExpandConstant('{src}\logo.png'))
; --- script do WhatsApp (vai em {app}\wa\)
Source: "wa\wa_send.js"; DestDir: "{app}\wa"; Flags: ignoreversion

; --- (OPCIONAL) instalação offline do Node.js:
;     Se você colocar os MSIs nas paths abaixo, o instalador usará eles sem baixar nada.
;     Caso contrário, se o IDP estiver instalado, baixaremos do site oficial.
;Source: "redist\node-v18.18.2-x64.msi"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: Is64BitInstallMode
;Source: "redist\node-v18.18.2-x86.msi"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: not Is64BitInstallMode

[Dirs]
; Pastas de dados em ProgramData com permissão de escrita para usuários
Name: "{commonappdata}\AgendadorBravo";            Permissions: users-full
Name: "{commonappdata}\AgendadorBravo\logs";       Permissions: users-full
Name: "{commonappdata}\AgendadorBravo\wa_data";    Permissions: users-full

[Icons]
Name: "{group}\{#MyAppName}";               Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}";         Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{group}\Desinstalar {#MyAppName}";   Filename: "{uninstallexe}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Abrir {#MyAppName}"; Flags: nowait postinstall skipifsilent

; =====================================================
;                       CODE
; =====================================================
[Code]

function NodeExePath(): string;
begin
  if Is64BitInstallMode then begin
    if FileExists(ExpandConstant('{pf}\nodejs\node.exe')) then
      Result := ExpandConstant('{pf}\nodejs\node.exe')
    else if FileExists(ExpandConstant('{pf32}\nodejs\node.exe')) then
      Result := ExpandConstant('{pf32}\nodejs\node.exe')
    else
      Result := '';
  end else begin
    if FileExists(ExpandConstant('{pf32}\nodejs\node.exe')) then
      Result := ExpandConstant('{pf32}\nodejs\node.exe')
    else
      Result := '';
  end;
end;

function IsNodeInstalled(): Boolean;
begin
  Result := (NodeExePath() <> '');
end;

procedure JsonEscapeBackslashes(var S: string);
begin
  StringChangeEx(S, '\', '\\', True);
end;

procedure CreateDefaultConfig();
var
  pdi, nodeExe, jsPath, f, j: string;
begin
  pdi     := 'C:\Pentaho\data-integration';
  nodeExe := NodeExePath();
  if nodeExe = '' then
    nodeExe := 'C:\Program Files\nodejs\node.exe';

  jsPath  := ExpandConstant('{app}\wa\wa_send.js');

  JsonEscapeBackslashes(pdi);
  JsonEscapeBackslashes(nodeExe);
  JsonEscapeBackslashes(jsPath);

  j :=
    '{' + #13#10 +
    '  "settings": {' + #13#10 +
    '    "pdi_home": "' + pdi + '",' + #13#10 +
    '    "email": { "enabled": false, "smtp_host": "smtp.gmail.com", "smtp_port": 587,' + #13#10 +
    '               "username": "", "password": "", "from_email": "", "to_emails": [] },' + #13#10 +
    '    "whatsapp": {' + #13#10 +
    '      "enabled": false,' + #13#10 +
    '      "mode": "webjs",' + #13#10 +
    '      "node_path": "' + nodeExe + '",' + #13#10 +
    '      "webjs_script": "' + jsPath + '",' + #13#10 +
    '      "to_targets": [],' + #13#10 +
    '      "my_number": ""' + #13#10 +
    '    }' + #13#10 +
    '  },' + #13#10 +
    '  "tasks": [],' + #13#10 +
    '  "history": {}' + #13#10 +
    '}';

  f := ExpandConstant('{commonappdata}\AgendadorBravo\config.json');
  SaveStringToFile(f, j, False);
end;

procedure InstallNode();
var
  MSI, URL, FN: string;
  RC: Integer;
begin
  { 1) Tenta instalador offline (se foi incluído em [Files]) }
  if Is64BitInstallMode then
    MSI := ExpandConstant('{tmp}\node-v18.18.2-x64.msi')
  else
    MSI := ExpandConstant('{tmp}\node-v18.18.2-x86.msi');

  if FileExists(MSI) then begin
    if not Exec('msiexec.exe', '/i "' + MSI + '" /qn', '', SW_SHOW, ewWaitUntilTerminated, RC) then
      RaiseException('Falha ao instalar Node.js (offline). Código: ' + IntToStr(RC));
    Exit;
  end;

  { 2) Sem offline: baixa com o IDP (se disponível) }
  #ifdef HAVE_IDP
    if Is64BitInstallMode then begin
      URL := 'https://nodejs.org/dist/v18.18.2/node-v18.18.2-x64.msi';
      FN  := 'node-x64.msi';
    end else begin
      URL := 'https://nodejs.org/dist/v18.18.2/node-v18.18.2-x86.msi';
      FN  := 'node-x86.msi';
    end;

    idpAddFile(URL, FN);
    if not idpDownloadFiles() then
      RaiseException('Falha ao baixar o instalador do Node.js.');

    MSI := ExpandConstant('{tmp}\') + FN;
    if not Exec('msiexec.exe', '/i "' + MSI + '" /qn', '', SW_SHOW, ewWaitUntilTerminated, RC) then
      RaiseException('Falha ao instalar Node.js (download). Código: ' + IntToStr(RC));
  #else
    
    SuppressibleMsgBox(
      'Node.js não encontrado e não foi possível instalar automaticamente.'#13#10 +
      'Você pode instalar depois a partir de https://nodejs.org/ (LTS).',
      mbInformation, MB_OK, IDOK);
  #endif
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  cfg: string;
begin
  if CurStep = ssInstall then begin
    { 1) Garante Node.js }
    if not IsNodeInstalled() then
    try
      InstallNode();
    except
      MsgBox(GetExceptionMessage, mbError, MB_OK);
    end;

    { 2) Cria config.json padrão (se ainda não existir) }
    cfg := ExpandConstant('{commonappdata}\AgendadorBravo\config.json');
    if not FileExists(cfg) then
      CreateDefaultConfig();
  end;
end;
