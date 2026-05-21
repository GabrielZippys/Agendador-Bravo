; =============================================================================
;  AgendadorBravo — Inno Setup 6
;  Gera: AgendadorBravo-Setup-<versão>.exe
;
;  Para compilar:
;    iscc agendador-setup.iss
;  ou abra no Inno Setup IDE e pressione F9.
;
;  Pré-requisito: o executável já deve estar em dist\AgendadorBravo.exe
;  (rode build_exe.py antes)
; =============================================================================

#define MyAppName      "AgendadorBravo"
#define MyAppVersion   "2025.10.11.14"
#define MyAppPublisher "Bravo Logística"
#define MyAppExeName   "AgendadorBravo.exe"
#define MyAppURL       "https://github.com/GabrielZippys/Agendador-Bravo"
; v2025.10.11.14 — agora apontamos para a PASTA onedir, não para um único exe
#define MySrcDir       "dist\AgendadorBravo"

; ---------------------------------------------------------------------------
[Setup]
AppId={{7D0C3A64-AB00-45A0-8E22-AGENDADORBRAVO}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases

; v2025.10.11.10 — Por DEFAULT instalamos em %LOCALAPPDATA% (sem admin).
; Isso permite que o auto-update funcione SEM precisar de UAC a cada release.
; Usuário pode mudar para Program Files no Wizard se quiser.
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=no

; Saída
OutputDir=dist
OutputBaseFilename=AgendadorBravo-Setup-{#MyAppVersion}

; Ícone do instalador
SetupIconFile=Logo.ico

; Compressão
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes

; Arquitetura
ArchitecturesInstallIn64BitMode=x64compatible

; v2025.10.11.10 — Privilégios: NÃO requer admin por default. O Inno Setup
; vai elevar automaticamente se o usuário escolher Program Files manualmente.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; Visual
WizardStyle=modern
; WizardResizable removido (obsoleto no Inno 6.x)
DisableWelcomePage=no
LicenseFile=

; Log de instalação
SetupLogging=yes

; Não reinicia o Windows após instalação
RestartIfNeededByRun=no
AlwaysRestart=no

; Uninstall
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppVersion}

; ---------------------------------------------------------------------------
[Languages]
Name: "ptbr"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

; ---------------------------------------------------------------------------
[Tasks]
Name: "desktopicon";  Description: "Criar atalho na &Área de Trabalho"; \
      GroupDescription: "Atalhos adicionais:"; Flags: unchecked
Name: "autostart";    Description: "Iniciar &automaticamente com o Windows"; \
      GroupDescription: "Inicialização:"; Flags: unchecked

; ---------------------------------------------------------------------------
[Files]
; v2025.10.11.14 — Modo ONEDIR: instala a PASTA inteira do build
; (AgendadorBravo.exe + _internal/ com todos os DLLs e libs).
; Resolve definitivamente o erro 'Failed to load python313.dll' que ocorria
; com --onefile (extração em runtime conflitava com antivírus).
Source: "{#MySrcDir}\*"; DestDir: "{app}"; \
        Flags: ignoreversion recursesubdirs createallsubdirs

; Ícone (para atalhos e painel de controle)
Source: "Logo.ico"; DestDir: "{app}"; Flags: ignoreversion

; Script WhatsApp (se existir)
Source: "wa\wa_send.js"; DestDir: "{app}\wa"; Flags: ignoreversion skipifsourcedoesntexist

; ---------------------------------------------------------------------------
[Dirs]
; Pasta de dados compartilhada — escrita para todos os usuários
Name: "{commonappdata}\AgendadorBravo";           Permissions: users-full
Name: "{commonappdata}\AgendadorBravo\logs";      Permissions: users-full
Name: "{commonappdata}\AgendadorBravo\wa_data";   Permissions: users-full

; v2025.10.11.14: a pasta 'rt' nao e mais necessaria (modo onedir nao extrai),
; mas mantemos pra limpar instalacoes antigas/orfas.
Name: "{localappdata}\AgendadorBravo\rt";         Permissions: users-full

; ---------------------------------------------------------------------------
[Icons]
; Menu Iniciar
Name: "{group}\{#MyAppName}";             Filename: "{app}\{#MyAppExeName}"; \
      IconFilename: "{app}\Logo.ico"
Name: "{group}\Desinstalar {#MyAppName}"; Filename: "{uninstallexe}"

; Área de Trabalho (opcional)
Name: "{autodesktop}\{#MyAppName}";       Filename: "{app}\{#MyAppExeName}"; \
      IconFilename: "{app}\Logo.ico"; \
      Tasks: desktopicon

; ---------------------------------------------------------------------------
[Registry]
; Autostart opcional
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
     ValueType: string; ValueName: "AgendadorBravo"; \
     ValueData: """{app}\{#MyAppExeName}"""; \
     Flags: uninsdeletevalue; Tasks: autostart

; Garante que o update automático do app também reconheça o caminho do exe
Root: HKCU; Subkey: "Software\AgendadorBravo"; \
     ValueType: string; ValueName: "InstallDir"; \
     ValueData: "{app}"; \
     Flags: uninsdeletekey createvalueifdoesntexist

; ---------------------------------------------------------------------------
[Run]
; Abre o aplicativo após instalação (checkbox na tela final)
Filename: "{app}\{#MyAppExeName}"; \
          Description: "Abrir {#MyAppName} agora"; \
          Flags: nowait postinstall skipifsilent unchecked; \
          WorkingDir: "{app}"

; ---------------------------------------------------------------------------
[UninstallRun]
; Garante que o app esteja fechado antes de desinstalar
Filename: "{sys}\taskkill.exe"; Parameters: "/IM {#MyAppExeName} /F"; \
          Flags: runhidden skipifdoesntexist; RunOnceId: "KillBeforeUninstall"

; ---------------------------------------------------------------------------
[Code]

{ -------------------------------------------------------------------------- }
{ Verifica instalação existente e oferece atualização silenciosa             }
{ -------------------------------------------------------------------------- }
function GetUninstallString(): string;
var
  sUnInstPath, sUnInstallString: string;
begin
  { O AppId do [Setup] sem as chaves é usado como chave de registro }
  sUnInstPath := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{7D0C3A64-AB00-45A0-8E22-AGENDADORBRAVO}_is1';
  sUnInstallString := '';
  if not RegQueryStringValue(HKLM, sUnInstPath, 'UninstallString', sUnInstallString) then
    RegQueryStringValue(HKCU, sUnInstPath, 'UninstallString', sUnInstallString);
  Result := sUnInstallString;
end;

function IsUpgrade(): Boolean;
begin
  Result := (GetUninstallString() <> '');
end;

function UnInstallOldVersion(): Integer;
var
  sUnInstallString: string;
  iResultCode: integer;
begin
  Result := 0;
  sUnInstallString := GetUninstallString();
  if sUnInstallString <> '' then begin
    sUnInstallString := RemoveQuotes(sUnInstallString);
    if Exec(sUnInstallString, '/SILENT /NORESTART /SUPPRESSMSGBOXES', '',
            SW_HIDE, ewWaitUntilTerminated, iResultCode) then
      Result := 3
    else
      Result := 2;
  end else
    Result := 1;
end;

{ -------------------------------------------------------------------------- }
{ Cria config padrão em %ProgramData%\AgendadorBravo\ se não existir         }
{ -------------------------------------------------------------------------- }
function NodeExePath(): string;
begin
  Result := '';
  if FileExists(ExpandConstant('{pf}\nodejs\node.exe')) then
    Result := ExpandConstant('{pf}\nodejs\node.exe')
  else if FileExists(ExpandConstant('{pf32}\nodejs\node.exe')) then
    Result := ExpandConstant('{pf32}\nodejs\node.exe');
end;

procedure EscapeBackslash(var S: string);
begin
  StringChangeEx(S, '\', '\\', True);
end;

procedure CreateDefaultConfig();
var
  pdi, nodeExe, jsPath, cfgFile, json: string;
begin
  cfgFile := ExpandConstant('{commonappdata}\AgendadorBravo\agendador_data.json');
  if FileExists(cfgFile) then Exit;   { não sobrescreve dados existentes }

  pdi     := 'C:\Pentaho\data-integration';
  nodeExe := NodeExePath();
  if nodeExe = '' then
    nodeExe := 'C:\Program Files\nodejs\node.exe';
  jsPath := ExpandConstant('{app}\wa\wa_send.js');

  EscapeBackslash(pdi);
  EscapeBackslash(nodeExe);
  EscapeBackslash(jsPath);

  json :=
    '{' + #13#10 +
    '  "settings": {' + #13#10 +
    '    "pdi_home": "' + pdi + '",' + #13#10 +
    '    "email": {' + #13#10 +
    '      "enabled": false,' + #13#10 +
    '      "smtp_host": "smtp.gmail.com",' + #13#10 +
    '      "smtp_port": 587,' + #13#10 +
    '      "username": "",' + #13#10 +
    '      "password": "",' + #13#10 +
    '      "from_email": "",' + #13#10 +
    '      "to_emails": []' + #13#10 +
    '    },' + #13#10 +
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

  SaveStringToFile(cfgFile, json, False);
end;

{ -------------------------------------------------------------------------- }
{ Antes de instalar: mata versão rodando + desinstala versão antiga silenc.  }
{ -------------------------------------------------------------------------- }
procedure CurStepChanged(CurStep: TSetupStep);
var
  RC: Integer;
begin
  if CurStep = ssInstall then begin
    { Mata qualquer instância rodando }
    Exec(ExpandConstant('{sys}\taskkill.exe'),
         '/IM {#MyAppExeName} /F', '', SW_HIDE, ewWaitUntilTerminated, RC);

    { Cria config padrão se necessário }
    CreateDefaultConfig();
  end;
end;

{ Pergunta se deve desinstalar versão anterior }
function InitializeSetup(): Boolean;
begin
  Result := True;
  if IsUpgrade() then begin
    if MsgBox(
      'Uma versão anterior do ' + '{#MyAppName}' + ' foi encontrada.' + #13#10 + #13#10 +
      'Deseja atualizar para a versão {#MyAppVersion}?',
      mbConfirmation, MB_YESNO) = IDNO then
    begin
      Result := False;
      Exit;
    end;
    UnInstallOldVersion();
  end;
end;
