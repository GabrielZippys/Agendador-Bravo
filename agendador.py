# agendador_pro.py

import os, sys, json, subprocess, traceback, time, smtplib, ssl, threading, shlex
import zipfile, base64, calendar
from email.mime.text import MIMEText
from email.utils import formatdate
from datetime import datetime, timedelta, date
from pathlib import Path
import re
from tkinter import ttk, filedialog, messagebox, simpledialog


# --- AUTOUPDATE (com aviso na UI) -------------------------------------------
import urllib.request, urllib.error, hashlib, tempfile

# YAML para export/import de jobs (lazy: importa só quando precisar)
def _lazy_yaml():
    try:
        import yaml  # PyYAML
        return yaml
    except Exception:
        return None

# DPAPI (Windows) para criptografia de credenciais
def _lazy_dpapi():
    try:
        import win32crypt  # pywin32
        return win32crypt
    except Exception:
        return None

# --- CONECTIVIDADE / FILA DE ATUALIZAÇÕES ----------------------------------
NET_CHECK_EVERY_SEC = int(os.getenv("AGENDADOR_NET_EVERY_SEC", "30"))  # Reduzido para 30s (menos overhead)
NET_FLAP_STABLE = int(os.getenv("AGENDADOR_NET_STABLE", "2"))
UPDATE_QUEUE_MAX = 50

# Cache para verificação de rede
_net_cache = {"status": None, "timestamp": 0}
_NET_CACHE_TTL = 10  # Cache de 10 segundos

def is_online(timeout=2, use_cache=True) -> bool:
    """Verifica conectividade com cache para reduzir overhead."""
    if use_cache:
        now = time.time()
        if now - _net_cache["timestamp"] < _NET_CACHE_TTL:
            if _net_cache["status"] is not None:
                return _net_cache["status"]
    
    try:
        urllib.request.urlopen("https://www.gstatic.com/generate_204", timeout=timeout)
        status = True
    except Exception:
        status = False
    
    # Atualiza cache
    _net_cache["status"] = status
    _net_cache["timestamp"] = time.time()
    return status

def start_net_monitor(app_ref, interval=NET_CHECK_EVERY_SEC, stable=NET_FLAP_STABLE):
    def worker():
        last = None
        same = 0
        while True:
            ok = is_online()
            if ok == last:
                same += 1
            else:
                same = 0
                last = ok
            if same >= stable and app_ref and app_ref.winfo_exists():
                try:
                    app_ref.after(0, lambda s=ok: app_ref.on_net_status_change(s))
                except Exception:
                    pass
            time.sleep(max(2, int(interval)))
    threading.Thread(target=worker, daemon=True).start()
# --- /CONECTIVIDADE ----------------------------------------------------------


APP_VERSION = "2025.10.11.17"   # << aumente em cada build
UPDATE_MANIFEST_URL = os.getenv(
    "AGENDADOR_UPDATE_MANIFEST",
    "https://raw.githubusercontent.com/GabrielZippys/Agendador-Bravo/main/update/manifest.json"
)
UPDATE_CHECK_EVERY_MIN = int(os.getenv("AGENDADOR_UPDATE_EVERY_MIN", "480"))  # 8h (reduzido overhead)

def _is_frozen():
    return getattr(sys, "frozen", False)

def _exe_path():
    return Path(sys.executable if _is_frozen() else __file__).resolve()

def _ver_tuple(v: str):
    return tuple(int(x) for x in re.findall(r"\d+", v or "0"))

def _http_get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))
    
def find_logo_ico() -> Path | None:
    candidates = [
        APP_DIR / "Logo.ico",
        APP_DIR / "logo.ico",
        APP_DIR / "Logo" / "Logo.ico",
        APP_DIR / "Logo" / "logo.ico",
        resource_path("Logo.ico"),
        resource_path("logo.ico"),
        resource_path("Logo", "Logo.ico"),
        resource_path("Logo", "logo.ico"),
    ]
    for p in candidates:
        try:
            if p.exists():
                return p
        except Exception:
            pass
    return None

def _download(url: str, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
        f.write(r.read())

def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1<<20), b""):
            h.update(chunk)
    return h.hexdigest()

def expand_start_repeat(start_hhmm: str, every_value: int, every_unit: str, repeat_times: int) -> list[str]:
    """
    Gera lista de horários HH:MM para 'início + repetição'.
    Ex.: 14:25, a cada 5 minutos, por 10 vezes  ->  ['14:25','14:30',...]
    - 'repeat_times' = número de repetições APÓS a primeira (total = repeat_times + 1).
    - Corta horários que ultrapassem 23:59 (não atravessa para o dia seguinte).
    """
    try:
        hh, mm = map(int, start_hhmm.strip().split(":"))
        assert 0 <= hh <= 23 and 0 <= mm <= 59
    except Exception:
        hh, mm = 6, 0  # fallback 06:00

    step_min = int(every_value) * (60 if (every_unit or "minutes").lower() == "hours" else 1)
    if step_min <= 0:
        step_min = 1

    t0 = hh * 60 + mm
    out = []
    for k in range(int(repeat_times) + 1):
        t = t0 + k * step_min
        if t >= 24 * 60:
            break
        out.append(f"{t // 60:02d}:{t % 60:02d}")
    return out


def _can_write_to_dir(dir_path: Path) -> bool:
    """v2025.10.11.10 — Testa se temos permissão de escrita no diretório."""
    try:
        test = dir_path / f".write_test_{os.getpid()}_{int(time.time())}.tmp"
        test.write_text("x", encoding="utf-8")
        test.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _write_update_scripts(pid: int, src_new: Path, dst_exe: Path, sha256_hex: str = "") -> Path:
    """
    v2025.10.11.10 — Updater BLINDADO. Resolve para sempre o ciclo de falha
    "Não foi possível concluir a atualização":

      1) VBS gravado como UTF-16 LE com BOM → acentos sempre corretos
      2) Mata processo via taskkill /F /T (com filhos)
      3) Limpa _MEI* em múltiplos locais
      4) Backup do exe atual
      5) Cópia BLINDADA do novo exe:
         - 30 tentativas × 1,5 s (45 s totais — tempo de scan do AV)
         - Captura Err.Description em cada falha pro log
         - Fallback A: MoveFile (renomeia ao invés de copiar)
         - Fallback B: deleta destino antes de copiar
      6) PRÉ-AQUECIMENTO AV (--pre-extract)
      7) Inicia o exe (até 3 tentativas)
      8) Em qualquer falha: MsgBox com 3 botões (Ver log / Abrir Releases /
         Abrir pasta) → nunca deixa o usuário sem saída

    Rodado via wscript.exe sob UAC quando o destino exige (Program Files).
    """
    exe_name = dst_exe.name
    backup = dst_exe.with_suffix(".exe.bak")
    log = Path(tempfile.gettempdir()) / f"agendador_update_{pid}.log"

    # Escapa aspas para VBS
    def q(s: str) -> str:
        return str(s).replace('"', '""')

    SRC = q(src_new)
    DST = q(dst_exe)
    BAK = q(backup)
    LOG = q(log)
    EXE = q(exe_name)
    DST_DIR = q(str(Path(dst_exe).parent))

    RELEASES_URL = "https://github.com/GabrielZippys/Agendador-Bravo/releases/latest"

    vbs = f'''Option Explicit

Dim sh, fso, wmi
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
On Error Resume Next
Set wmi = GetObject("winmgmts:\\\\.\\root\\cimv2")
On Error Goto 0

Const SRC_PATH = "{SRC}"
Const DST_PATH = "{DST}"
Const DST_DIR  = "{DST_DIR}"
Const BAK_PATH = "{BAK}"
Const LOG_PATH = "{LOG}"
Const EXE_NAME = "{EXE}"
Const APP_TITLE = "Agendador-Bravo"
Const RELEASES_URL = "{RELEASES_URL}"

Sub LogLine(msg)
    On Error Resume Next
    Dim ts, f
    ts = Now
    Set f = fso.OpenTextFile(LOG_PATH, 8, True, -1)  ' True=create, -1=Unicode
    f.WriteLine "[" & ts & "] " & msg
    f.Close
    On Error Goto 0
End Sub

Function HasWriteAccess(dirPath)
    HasWriteAccess = False
    On Error Resume Next
    Dim test, f
    test = dirPath & "\\.agendador_write_test.tmp"
    Set f = fso.OpenTextFile(test, 2, True)
    If Err.Number = 0 Then
        f.Close
        fso.DeleteFile test, True
        HasWriteAccess = True
    End If
    Err.Clear
    On Error Goto 0
End Function

Function AlreadyElevated()
    Dim i
    AlreadyElevated = False
    For i = 0 To WScript.Arguments.Count - 1
        If LCase(WScript.Arguments(i)) = "/elevated" Then
            AlreadyElevated = True
            Exit Function
        End If
    Next
End Function

Sub MaybeElevate()
    ' Se nao temos permissao, relanca o proprio VBS via UAC (uma vez).
    If HasWriteAccess(DST_DIR) Then Exit Sub
    If AlreadyElevated() Then
        ' Ja relancou elevado e ainda assim nao tem perm: segue e falha
        ' com erro claro depois — evita loop infinito.
        LogLine "still no write access even after elevation request"
        Exit Sub
    End If
    LogLine "no write access; requesting UAC elevation"
    Dim args
    args = Chr(34) & WScript.ScriptFullName & Chr(34) & " /elevated"
    On Error Resume Next
    Dim sa
    Set sa = CreateObject("Shell.Application")
    sa.ShellExecute "wscript.exe", args, "", "runas", 0
    On Error Goto 0
    WScript.Quit 0   ' o re-launch elevado faz o resto
End Sub

Sub KillApp()
    On Error Resume Next
    ' /T mata processos filhos tambem; /F forca
    sh.Run "taskkill /F /T /IM " & Chr(34) & EXE_NAME & Chr(34), 0, True
    On Error Goto 0
End Sub

Function IsAppRunning()
    IsAppRunning = False
    If IsEmpty(wmi) Or IsNull(wmi) Then Exit Function
    On Error Resume Next
    Dim procs, p
    Set procs = wmi.ExecQuery("Select * from Win32_Process Where Name='" & EXE_NAME & "'")
    If Err.Number = 0 Then
        For Each p In procs
            IsAppRunning = True
            Exit Function
        Next
    End If
    On Error Goto 0
End Function

Sub CleanMEIIn(dirPath)
    On Error Resume Next
    Dim base, f
    If fso.FolderExists(dirPath) Then
        Set base = fso.GetFolder(dirPath)
        For Each f In base.SubFolders
            If LCase(Left(f.Name, 4)) = "_mei" Then
                fso.DeleteFolder f.Path, True
            End If
        Next
    End If
    On Error Goto 0
End Sub

Sub CleanMEI()
    CleanMEIIn sh.ExpandEnvironmentStrings("%TEMP%")
    CleanMEIIn sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\\AgendadorBravo\\rt")
    CleanMEIIn sh.ExpandEnvironmentStrings("%TEMP%\\2")
End Sub

Function TryReplaceExe(src, dst)
    ' Estrategia 1: CopyFile com sobrescrita (30 retries x 1.5s = 45s).
    Dim i, lastErr
    lastErr = ""
    For i = 1 To 30
        On Error Resume Next
        fso.CopyFile src, dst, True
        If Err.Number = 0 Then
            TryReplaceExe = ""
            On Error Goto 0
            Exit Function
        End If
        lastErr = "Copy " & i & ": #" & Err.Number & " " & Err.Description
        LogLine lastErr
        Err.Clear
        On Error Goto 0
        WScript.Sleep 1500
    Next

    ' Estrategia 2: deleta destino e tenta copiar
    LogLine "fallback A: delete + copy"
    On Error Resume Next
    fso.DeleteFile dst, True
    Err.Clear
    fso.CopyFile src, dst, True
    If Err.Number = 0 Then
        TryReplaceExe = ""
        On Error Goto 0
        Exit Function
    End If
    lastErr = "Delete+Copy: #" & Err.Number & " " & Err.Description
    LogLine lastErr
    Err.Clear
    On Error Goto 0

    ' Estrategia 3: MoveFile (rename do .new para o destino)
    LogLine "fallback B: move"
    On Error Resume Next
    fso.MoveFile src, dst
    If Err.Number = 0 Then
        TryReplaceExe = ""
        On Error Goto 0
        Exit Function
    End If
    lastErr = "Move: #" & Err.Number & " " & Err.Description
    LogLine lastErr
    Err.Clear
    On Error Goto 0

    TryReplaceExe = lastErr
End Function

Sub ShowFailureDialog(reason)
    ' Dialogo com 3 botoes (Sim/Nao/Cancelar mapeados):
    '   Sim     -> abre o log no Notepad
    '   Nao     -> abre Releases no navegador
    '   Cancelar-> apenas fecha
    Dim msg, resp
    msg = "Nao foi possivel concluir a atualizacao automatica." & vbCrLf & vbCrLf
    msg = msg & "Causa: " & reason & vbCrLf & vbCrLf
    msg = msg & "A versao anterior foi mantida e o app esta funcional." & vbCrLf & vbCrLf
    msg = msg & "Deseja abrir o log de atualizacao agora?" & vbCrLf
    msg = msg & "(Sim = abrir log    Nao = abrir Releases no navegador    Cancelar = fechar)"
    resp = MsgBox(msg, 51, APP_TITLE)   ' 51 = YesNoCancel + Warning
    If resp = 6 Then
        ' Yes -> abre o log no Notepad
        On Error Resume Next
        sh.Run "notepad.exe " & Chr(34) & LOG_PATH & Chr(34), 1, False
        On Error Goto 0
    ElseIf resp = 7 Then
        ' No -> abre Releases no navegador
        On Error Resume Next
        sh.Run RELEASES_URL, 1, False
        On Error Goto 0
    End If
End Sub

' =====================================================================
LogLine "updater start pid={pid}"
LogLine "src=" & SRC_PATH
LogLine "dst=" & DST_PATH
LogLine "dst_dir=" & DST_DIR

' ── 0) Auto-eleva se nao temos permissao no destino ─────────────────────
MaybeElevate

' ── 1) Mata app antigo (mais agressivo agora) ───────────────────────────
KillApp
WScript.Sleep 1500
KillApp
WScript.Sleep 1000
KillApp
WScript.Sleep 500

' ── 2) Limpa _MEI antigos ───────────────────────────────────────────────
CleanMEI

' ── 3) Backup ───────────────────────────────────────────────────────────
If fso.FileExists(DST_PATH) Then
    On Error Resume Next
    fso.CopyFile DST_PATH, BAK_PATH, True
    LogLine "backup created"
    On Error Goto 0
End If

' ── 4) Substitui o exe (multiplas estrategias) ──────────────────────────
Dim replaceErr
replaceErr = TryReplaceExe(SRC_PATH, DST_PATH)
If replaceErr <> "" Then
    LogLine "ALL strategies failed: " & replaceErr
    ' Restaura backup
    On Error Resume Next
    If fso.FileExists(BAK_PATH) Then fso.CopyFile BAK_PATH, DST_PATH, True
    On Error Goto 0
    ShowFailureDialog replaceErr
    On Error Resume Next
    fso.DeleteFile SRC_PATH, True
    fso.DeleteFile BAK_PATH, True
    fso.DeleteFile WScript.ScriptFullName, True
    On Error Goto 0
    WScript.Quit 1
End If
LogLine "replace ok"

' ── 5) PRE-AQUECIMENTO DO ANTIVIRUS ─────────────────────────────────────
LogLine "pre-extracting for AV warm-up..."
On Error Resume Next
sh.Run Chr(34) & DST_PATH & Chr(34) & " --pre-extract", 0, False
On Error Goto 0
WScript.Sleep 12000
sh.Run "taskkill /F /T /IM " & Chr(34) & EXE_NAME & Chr(34), 0, True
WScript.Sleep 1000
LogLine "pre-extract done"

' ── 6) Dialogo de sucesso ───────────────────────────────────────────────
MsgBox "Atualizacao concluida com sucesso!" & vbCrLf & vbCrLf & _
       "Clique em OK para abrir o Agendador-Bravo.", 64, APP_TITLE

' ── 7) Inicia o exe de verdade (3 tentativas) ───────────────────────────
Dim attempt, launched, waitMs
launched = False
For attempt = 1 To 3
    LogLine "launch attempt " & attempt
    On Error Resume Next
    sh.Run Chr(34) & DST_PATH & Chr(34), 1, False
    On Error Goto 0
    waitMs = 3000 + attempt * 2000
    WScript.Sleep waitMs
    If IsAppRunning() Then
        launched = True
        LogLine "launch ok (attempt " & attempt & ")"
        Exit For
    End If
    LogLine "launch attempt " & attempt & " failed"
    KillApp
    CleanMEI
    WScript.Sleep 1500
Next

If Not launched Then
    LogLine "all launches failed"
    MsgBox "A atualizacao foi instalada, mas o aplicativo nao iniciou." & vbCrLf & vbCrLf & _
           "Abra o Agendador-Bravo pelo atalho no Menu Iniciar.", 48, APP_TITLE
End If

' ── 8) Cleanup ──────────────────────────────────────────────────────────
On Error Resume Next
fso.DeleteFile SRC_PATH, True
fso.DeleteFile BAK_PATH, True
fso.DeleteFile WScript.ScriptFullName, True
On Error Goto 0
'''

    vbs_path = Path(tempfile.gettempdir()) / f"agendador_update_{pid}.vbs"
    # v2025.10.11.10 — GRAVAR COMO UTF-16 LE COM BOM
    # Esta é a única forma confiável de o wscript renderizar acentos
    # corretamente no Brasil (cp1252 pode falhar dependendo do locale).
    try:
        vbs_bytes = b"\xff\xfe" + vbs.encode("utf-16-le")
        vbs_path.write_bytes(vbs_bytes)
    except Exception:
        # fallback: ASCII puro (vbs já evita a maioria dos acentos críticos)
        vbs_path.write_text(vbs, encoding="ascii", errors="replace")
    return vbs_path


def _apply_folder_update_and_restart(new_folder: Path, sha256_hex: str = ""):
    """v2025.10.11.14 — Updater FOLDER-based (modo --onedir).

    Mais robusto que o updater legado de .exe único:
    - Não depende de extração runtime do _MEI (que conflita com AV).
    - Usa robocopy /MIR para mirror da pasta — robocopy lida bem com
      arquivos em uso, faz retries internamente e suporta paths longos.
    - Mata o app atual, espera 2s, faz o mirror, lança o novo exe.

    A "atomicidade" não é perfeita (alguns arquivos podem ficar mistos
    durante o copy), mas robocopy minimiza isso e o backup .bak é mantido
    pra rollback automático em caso de falha.
    """
    install_dir = _exe_path().parent  # onde o app está instalado
    exe_name = _exe_path().name
    log_path = Path(tempfile.gettempdir()) / f"agendador_update_{os.getpid()}.log"

    # Script CMD que faz o trabalho. Roda invisível via wscript launcher.
    # /MIR mirror, /R:30 30 retries por arquivo, /W:1 1s entre retries,
    # /NJH /NJS sem header/summary, /NP sem progresso.
    cmd_path = Path(tempfile.gettempdir()) / f"agendador_update_{os.getpid()}.cmd"
    cmd_content = f"""@echo off
chcp 65001 > nul
echo [%date% %time%] === update folder-based start === >> "{log_path}"
echo [src] {new_folder} >> "{log_path}"
echo [dst] {install_dir} >> "{log_path}"

rem 1) Kill app
taskkill /F /T /IM "{exe_name}" >> "{log_path}" 2>&1
ping 127.0.0.1 -n 3 > nul

rem 2) Robocopy mirror (resiliente a arquivos travados)
robocopy "{new_folder}" "{install_dir}" /MIR /R:30 /W:1 /NJH /NJS /NDL /NP /XD logs pids wa_data rt >> "{log_path}" 2>&1
set ROBO_RC=%ERRORLEVEL%
echo [robocopy exitcode] %ROBO_RC% >> "{log_path}"

rem Robocopy considera 0-7 sucesso, >=8 falha
if %ROBO_RC% LSS 8 (
  echo [ok] copy succeeded >> "{log_path}"
  rem 3) Launch new app
  start "" "{install_dir}\\{exe_name}"
  echo [launched] new exe >> "{log_path}"
) else (
  echo [FAIL] robocopy failed; old install preserved >> "{log_path}"
  msg * "Falha na atualizacao automatica. Veja {log_path}. Abra Releases pra baixar o instalador manualmente."
)

rem 4) Cleanup
rmdir /s /q "{new_folder}" 2>nul
del /q "{cmd_path}" 2>nul
"""
    cmd_path.write_text(cmd_content, encoding="utf-8")

    # Verifica permissão. Se não pode escrever no install_dir, vai pedir UAC.
    needs_elevation = (os.name == "nt") and not _can_write_to_dir(install_dir)

    try:
        if needs_elevation:
            import ctypes
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", "cmd.exe", f'/c "{cmd_path}"', None, 0
            )
            if int(rc) <= 32:
                print(f"[update] ShellExecute runas falhou (rc={rc}); tentando sem elevação")
                CREATE_NO_WINDOW = 0x08000000
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 0
                subprocess.Popen(["cmd.exe", "/c", str(cmd_path)],
                                 creationflags=CREATE_NO_WINDOW,
                                 startupinfo=si, close_fds=True)
        else:
            CREATE_NO_WINDOW = 0x08000000
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            subprocess.Popen(["cmd.exe", "/c", str(cmd_path)],
                             creationflags=CREATE_NO_WINDOW,
                             startupinfo=si, close_fds=True)
    except Exception as e:
        print(f"[update] falha ao disparar updater folder-based: {e}")

    # Encerra o app pra liberar handles
    try:
        import ctypes
        ctypes.windll.kernel32.ExitProcess(0)
    except Exception:
        os._exit(0)


def _apply_update_and_restart(new_exe: Path, sha256_hex: str = ""):
    """Dispara o updater invisível e encerra o processo atual.

    v2025.10.11.10 — pré-detecta falta de permissão e usa ShellExecute com
    verbo 'runas' (UAC) quando o destino está em Program Files. Belt and
    suspenders: o próprio VBS também faz auto-elevação se necessário.
    """
    vbs = _write_update_scripts(os.getpid(), new_exe, _exe_path(), sha256_hex)
    dst_dir = _exe_path().parent
    needs_elevation = (os.name == "nt") and not _can_write_to_dir(dst_dir)

    try:
        if needs_elevation:
            # UAC prompt → wscript elevado
            import ctypes
            params = f'"{vbs}"'
            # ShellExecuteW(hwnd, verb, file, params, dir, nShow)
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", "wscript.exe", params, None, 0
            )
            # Valores <= 32 indicam erro (1223 = ERROR_CANCELLED quando user nega UAC)
            if int(rc) <= 32:
                print(f"[update] ShellExecute retornou {rc}; fallback sem elevação")
                CREATE_NO_WINDOW = 0x08000000
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 0
                subprocess.Popen(
                    ["wscript.exe", str(vbs)],
                    creationflags=CREATE_NO_WINDOW,
                    startupinfo=si, close_fds=True,
                )
        else:
            CREATE_NO_WINDOW = 0x08000000
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            subprocess.Popen(
                ["wscript.exe", str(vbs)],
                creationflags=CREATE_NO_WINDOW,
                startupinfo=si, close_fds=True,
            )
    except Exception as e:
        print(f"[update] falha ao disparar updater: {e}")

    # Encerra de forma dura pra liberar o handle do .exe
    try:
        import ctypes
        ctypes.windll.kernel32.ExitProcess(0)
    except Exception:
        os._exit(0)


# --- AUTOSTART com Windows (HKCU\...\Run) ----------------------------------
AUTOSTART_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_REG_NAME = "AgendadorBravo"

def is_autostart_enabled() -> bool:
    """Verifica se o app está configurado para iniciar com o Windows."""
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_REG_PATH, 0, winreg.KEY_READ) as k:
            val, _ = winreg.QueryValueEx(k, AUTOSTART_REG_NAME)
            return bool(val)
    except FileNotFoundError:
        return False
    except Exception:
        return False

def set_autostart(enable: bool) -> tuple[bool, str]:
    """Cria/remove a entrada de autostart no registro do Windows."""
    if os.name != "nt":
        return (False, "Somente Windows.")
    try:
        import winreg
        if enable:
            exe = _exe_path()
            # Evita configurar autostart em modo dev (script .py)
            if not _is_frozen():
                return (False, "Modo dev: autostart não configurado.")
            cmd = f'"{exe}"'
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_REG_PATH, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, AUTOSTART_REG_NAME, 0, winreg.REG_SZ, cmd)
            return (True, "Autostart habilitado.")
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_REG_PATH, 0, winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, AUTOSTART_REG_NAME)
            except FileNotFoundError:
                pass
            return (True, "Autostart desabilitado.")
    except Exception as e:
        return (False, f"Falha: {e}")
# --- /AUTOSTART --------------------------------------------------------------


def fetch_update_info() -> tuple[bool, dict | str]:
    """
    Apenas verifica se há versão nova.
    Retorna (True, {version, exe_url, sha256}) OU (False, mensagem).
    """
    try:
        mf = _http_get_json(UPDATE_MANIFEST_URL)
        remote_v = mf.get("version", "")
        if not remote_v:
            return (False, "Manifesto sem 'version'.")
        if _ver_tuple(remote_v) <= _ver_tuple(APP_VERSION):
            return (False, "Já está na última versão.")
        # v2025.10.11.14 — suporta tanto exe_url (legado --onefile) quanto
        # zip_url (novo --onedir). zip_url tem prioridade quando ambos existem.
        info = {
            "version": remote_v,
            "exe_url": mf.get("exe_url") or mf.get("url") or "",
            "zip_url": mf.get("zip_url") or "",
            "sha256":  (mf.get("sha256") or "").lower(),
        }
        if not info["exe_url"] and not info["zip_url"]:
            return (False, "Manifesto sem 'exe_url' nem 'zip_url'.")

        return (True, info)
    except Exception as e:
        return (False, f"Falha ao checar: {e}")


def apply_update_now(info: dict) -> tuple[bool, str]:
    """v2025.10.11.14 — Baixa e aplica update.

    Detecta automaticamente o formato:
    - zip_url presente → update FOLDER-based (descompacta o ZIP, swap da pasta)
    - exe_url presente → update LEGACY (troca o .exe único — pode falhar com
                          AV bloqueando python313.dll)
    """
    if not _is_frozen():
        return (False, f"Nova versão {info.get('version')} disponível (modo dev: não aplica).")
    sha_expected = (info.get("sha256") or "").lower()

    # === Caminho NOVO: ZIP-based (onedir) ===
    if info.get("zip_url"):
        try:
            tmp_zip = Path(tempfile.gettempdir()) / f"{APP_BASENAME}.new.zip"
            _download(info["zip_url"], tmp_zip)
            if sha_expected:
                got = _sha256(tmp_zip).lower()
                if got != sha_expected:
                    tmp_zip.unlink(missing_ok=True)
                    return (False, f"SHA256 divergente (esperado {sha_expected}, obtido {got}).")
            # Extrai o ZIP para temp/AgendadorBravo_new/
            extract_dir = Path(tempfile.gettempdir()) / f"{APP_BASENAME}_new"
            if extract_dir.exists():
                import shutil as _sh
                _sh.rmtree(extract_dir, ignore_errors=True)
            extract_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(tmp_zip, "r") as zf:
                zf.extractall(extract_dir)
            # O ZIP contém uma pasta AgendadorBravo/... — pegamos esse caminho
            roots = [p for p in extract_dir.iterdir() if p.is_dir()]
            if not roots:
                return (False, "ZIP não tem a pasta esperada.")
            new_folder = roots[0]
            _apply_folder_update_and_restart(new_folder, sha_expected)
            return (True, f"Atualizando para {info.get('version')} (folder-based)...")
        except Exception as e:
            return (False, f"Falha ao aplicar (ZIP): {e}")

    # === Caminho LEGADO: troca de .exe único ===
    try:
        tmp_new = Path(tempfile.gettempdir()) / f"{APP_BASENAME}.new.exe"
        _download(info["exe_url"], tmp_new)
        if sha_expected:
            got = _sha256(tmp_new).lower()
            if got != sha_expected:
                tmp_new.unlink(missing_ok=True)
                return (False, f"SHA256 divergente (esperado {sha_expected}, obtido {got}).")
        _apply_update_and_restart(tmp_new, sha_expected)
        return (True, f"Atualizando para {info.get('version')}...")
    except Exception as e:
        return (False, f"Falha ao aplicar: {e}")

def start_auto_update_thread(app_ref):
    """Checa no início e depois periodicamente; se offline, enfileira a checagem."""
    def worker():
        time.sleep(20)  # deixa a UI abrir
        def _enqueue_check():
            try:
                if app_ref and app_ref.winfo_exists():
                    app_ref.enqueue_update("check")  # dedupe automático
            except Exception:
                pass

        # tentativa imediata (ou fila se offline)
        if app_ref and getattr(app_ref, "net_online", True):
            has, data = fetch_update_info()
            if has and app_ref and app_ref.winfo_exists():
                app_ref.after(0, lambda d=data: app_ref.on_update_available(d))
        else:
            _enqueue_check()

        # ciclo
        while True:
            time.sleep(max(60, UPDATE_CHECK_EVERY_MIN * 60))
            if app_ref and getattr(app_ref, "net_online", True):
                has, data = fetch_update_info()
                if has and app_ref and app_ref.winfo_exists():
                    app_ref.after(0, lambda d=data: app_ref.on_update_available(d))
            else:
                _enqueue_check()
    threading.Thread(target=worker, daemon=True).start()

# --- /AUTOUPDATE -------------------------------------------------------------

# Tema opcional
try:
    import sv_ttk  # pip install sv-ttk
except Exception:
    sv_ttk = None

import tkinter as tk
import tkinter.font as tkfont

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.executors.pool import ThreadPoolExecutor as APSThreadPoolExecutor
from apscheduler.events import EVENT_JOB_MISSED, EVENT_JOB_ERROR, EVENT_JOB_MAX_INSTANCES


# Logos/ícones (opcional)
try:
    from PIL import Image, ImageTk  # pip install pillow
except Exception:
    Image = ImageTk = None

# Para checar processos em execução (opcional, mas recomendado)
try:
 import psutil  # pip install psutil
except Exception:
    psutil = None

# ======================================================================================
#  v2025.10.11.15 — Sistema de Tema Unificado MDW Bravo TI
# ======================================================================================
def _bravo_theme(dark: bool = False) -> dict:
    """Paleta Bravo TI completa (light/dark). Centraliza todas as cores
    para que SettingsDialog, HelpPanel, TaskDialog e a janela principal
    fiquem consistentes em ambos os modos.
    """
    if dark:
        return {
            # backgrounds
            'bg_app':       '#0f1419',  # fundo da janela principal
            'bg_surface':   '#1a1f2e',  # cards, painéis, conteúdo
            'bg_sidebar':   '#0d121a',  # sidebar mais escura que o app
            'bg_hover':     '#252b3b',  # hover state
            'bg_selected':  '#1e3a8a',  # seleção (azul Bravo escuro)
            'bg_code':      '#0b0f17',  # blocos de código no Help
            'bg_input':     '#1a1f2e',  # campos de entrada
            # borders
            'border':       '#2d3748',
            'border_strong':'#475569',
            # text
            'text':         '#e2e8f0',  # texto primário
            'text_strong':  '#f8fafc',  # títulos
            'subtext':      '#94a3b8',  # texto secundário
            'text_muted':   '#64748b',  # placeholders, labels desabilitados
            # accent (azul Bravo — variante mais clara para contraste)
            'accent':       '#3b82f6',
            'accent_dark':  '#1d4ed8',
            'accent_light': '#60a5fa',
            'accent_bg':    '#1e3a8a',  # bg para destaque sutil
            # status
            'success':      '#10b981',
            'success_bg':   '#064e3b',
            'warning':      '#fbbf24',
            'warning_bg':   '#78350f',
            'error':        '#ef4444',
            'error_bg':     '#7f1d1d',
            'info':         '#06b6d4',
            'info_bg':      '#164e63',
            # outras
            'shadow':       '#000000',
        }
    else:
        return {
            # backgrounds
            'bg_app':       '#f1f3f5',  # fundo um pouco mais leve que o BravoForm
            'bg_surface':   '#ffffff',
            'bg_sidebar':   '#e9ecef',
            'bg_hover':     '#e9ecef',
            'bg_selected':  '#cfe2ff',  # bg azul claro pra seleção
            'bg_code':      '#f5f7fa',
            'bg_input':     '#ffffff',
            # borders
            'border':       '#dee2e6',
            'border_strong':'#adb5bd',
            # text
            'text':         '#212529',
            'text_strong':  '#0d1117',
            'subtext':      '#6c757d',
            'text_muted':   '#adb5bd',
            # accent (Bravo blue oficial)
            'accent':       '#007bff',
            'accent_dark':  '#0056b3',
            'accent_light': '#66b3ff',
            'accent_bg':    '#e7f1ff',
            # status
            'success':      '#198754',
            'success_bg':   '#d1e7dd',
            'warning':      '#ffc107',
            'warning_bg':   '#fff3cd',
            'error':        '#dc3545',
            'error_bg':     '#f8d7da',
            'info':         '#0dcaf0',
            'info_bg':      '#cff4fc',
            # outras
            'shadow':       '#00000020',
        }

def _is_dark_mode(master) -> bool:
    """Detecta se o tema escuro está ativo no app principal."""
    try:
        return bool(master.var_dark.get())
    except Exception:
        return False

def _resolve_fonts():
    """Detecta as fontes Bravo (Montserrat / Roboto). Fallback Segoe UI."""
    try:
        import tkinter.font as _tkf
        avail = set(_tkf.families())
    except Exception:
        avail = set()
    title = "Montserrat" if "Montserrat" in avail else \
            "Segoe UI Variable" if "Segoe UI Variable" in avail else "Segoe UI"
    body = "Roboto" if "Roboto" in avail else \
           "Segoe UI Variable" if "Segoe UI Variable" in avail else "Segoe UI"
    return title, body

# ======================================================================================
#  Caminhos / Pastas (resistente a Program Files)
# ======================================================================================

APP_NAME = "Agendador-Bravo"
APP_BASENAME = "AgendadorBravo"  # nome de pasta em ProgramData

def resource_path(*parts):
    """Retorna caminho para recurso empacotado (PyInstaller) ou ao lado do .py.
    Tolerante quando __file__ não existe (p.ex. durante shutdown)."""
    try:
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    except NameError:
        base = Path.cwd()
    return base.joinpath(*parts)

def pick_base_dir():
    """Escolhe uma pasta gravável do usuário/sistema (ProgramData > LocalAppData > AppData > Home)."""
    for env in ("PROGRAMDATA", "LOCALAPPDATA", "APPDATA"):
        base = os.environ.get(env)
        if base:
            p = Path(base) / APP_BASENAME
            try:
                p.mkdir(parents=True, exist_ok=True)
                return p
            except Exception:
                pass
    p = Path.home() / APP_BASENAME
    p.mkdir(parents=True, exist_ok=True)
    return p

APP_DIR = pick_base_dir()            # ex.: C:\ProgramData\AgendadorBravo
DATA_FILE = APP_DIR / "config.json"  # config.json gravável
LOG_DIR  = APP_DIR / "logs"          # logs graváveis
WA_DIR   = APP_DIR / "wa"            # cache do WhatsApp WebJS (gravável)
PID_DIR  = APP_DIR / "pids"          # pids para modo spawn/watchdog

def ensure_dirs():
    for d in (APP_DIR, LOG_DIR, WA_DIR, PID_DIR):
        d.mkdir(parents=True, exist_ok=True)

def _safe_name(name: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in name)

# ======================================================================================
#  Utilitários / Persistência
# ======================================================================================

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def format_days_bool(days_list):
    labels = ["seg","ter","qua","qui","sex","sab","dom"]
    return ",".join([labels[i] for i,v in enumerate(days_list) if v])

# Cache de dados para evitar leituras repetidas
_data_cache = {"data": None, "mtime": 0}

def _migrate_data_schema(data: dict) -> dict:
    """Adiciona keys novas (v2025.10.11.8) em config.json antigos, sem destruir nada."""
    settings = data.setdefault("settings", {})
    # webhooks
    settings.setdefault("webhooks", {
        "enabled": False, "urls": [],
        "notify_success": False, "notify_failure": True,
    })
    # maintenance_windows
    settings.setdefault("maintenance_windows", [])
    # digest_email
    settings.setdefault("digest_email", {
        "enabled": False, "time": "08:00",
        "days": [True]*5 + [False, False],
        "include_success": True, "include_failures": True, "include_top_slow": True,
    })
    # rest_api
    settings.setdefault("rest_api", {
        "enabled": False, "port": 17654, "token": "", "bind_host": "127.0.0.1",
    })
    # rollback
    settings.setdefault("rollback", {"enabled": True, "crash_threshold": 3})
    # secrets_encrypted flag
    settings.setdefault("secrets_encrypted", False)
    # tags universe
    data.setdefault("tags", [])
    # garante que cada task tenha defaults dos campos novos
    for t in data.get("tasks", []):
        t.setdefault("tags", [])
        t.setdefault("depends_on", [])
        t.setdefault("retry", {"enabled": False, "max_attempts": 3, "backoff_seconds": [60, 300, 900]})
        t.setdefault("respect_maintenance", True)
        t.setdefault("dynamic_params", True)
        t.setdefault("variables", {})  # {"VAR_NAME": "valor"} usado em ${VAR_NAME}
    return data

def load_data():
    """Carrega config.json com cache; cria defaults se não existir/corrompido."""
    ensure_dirs()
    
    # Verifica cache
    if DATA_FILE.exists():
        try:
            current_mtime = DATA_FILE.stat().st_mtime
            if _data_cache["data"] and _data_cache["mtime"] == current_mtime:
                return _data_cache["data"].copy()  # Retorna cópia para evitar mutações
            
            # Carrega e atualiza cache
            data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            data = _migrate_data_schema(data)
            _data_cache["data"] = data
            _data_cache["mtime"] = current_mtime
            return data
        except Exception:
            pass
    
    # Dados padrão
    default_data = {
        "settings": {
            "pdi_home": r"C:\Pentaho\data-integration",
            "email": {
                "enabled": False, "smtp_host": "smtp.gmail.com", "smtp_port": 587,
                "username": "", "password": "", "from_email": "", "to_emails": []
            },
            "whatsapp": {
                "enabled": False,
                "mode": "webjs",
                "node_path": r"C:\Program Files\nodejs\node.exe",
                "webjs_script": str(resource_path("wa", "wa_send.js")),
                "to_targets": [],
                "my_number": ""
            },
            "log_cleanup": {
                "enabled": True,
                "keep_days": 7,
                "schedule_day": 6,  # 0=segunda, 6=domingo
                "schedule_time": "02:00"
            },
            # v2025.10.11.8 — webhooks outbound (Discord/Slack/Teams)
            "webhooks": {
                "enabled": False,
                "urls": [],          # lista de URLs (auto-detectadas Discord/Slack/Teams)
                "notify_success": False,
                "notify_failure": True,
            },
            # v2025.10.11.8 — janelas de manutenção (blackout windows)
            "maintenance_windows": [
                # {"name":"Madrugada","start":"22:00","end":"06:00","days":[True]*7}
            ],
            # v2025.10.11.8 — digest diário por e-mail
            "digest_email": {
                "enabled": False,
                "time": "08:00",         # HH:MM
                "days": [True]*5 + [False, False],   # seg-sex por padrão
                "include_success": True,
                "include_failures": True,
                "include_top_slow": True,
            },
            # v2025.10.11.8 — API REST local
            "rest_api": {
                "enabled": False,
                "port": 17654,
                "token": "",              # vazio = sem auth (só localhost)
                "bind_host": "127.0.0.1",
            },
            # v2025.10.11.8 — rollback automático após N crashes seguidos
            "rollback": {
                "enabled": True,
                "crash_threshold": 3,
            },
            # v2025.10.11.8 — flag indicando que segredos estão criptografados (DPAPI)
            "secrets_encrypted": False,
        },
        "tasks": [],
        "history": {},
        "tags": []  # v2025.10.11.8 — universo de tags disponíveis
    }
    default_data = _migrate_data_schema(default_data)
    _data_cache["data"] = default_data
    return default_data

def save_data(data):
    """Salva dados e atualiza cache."""
    ensure_dirs()
    DATA_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    # Atualiza cache
    _data_cache["data"] = data.copy()
    _data_cache["mtime"] = DATA_FILE.stat().st_mtime

def append_history(data, task_name, rc, dur):
    hist = data.setdefault("history", {}).setdefault(task_name, [])
    hist.append({"ts": now_str(), "rc": int(rc), "dur": float(dur)})
    if len(hist) > 50:
        del hist[:-50]
    save_data(data)

# ======================================================================================
#  Limpeza automática de logs
# ======================================================================================

def cleanup_logs(settings):
    """Remove logs antigos baseado na configuração de limpeza."""
    print(f"[CLEANUP] Iniciando limpeza de logs @ {now_str()}")
    
    cleanup_cfg = settings.get("log_cleanup", {})
    if not cleanup_cfg.get("enabled", True):
        print("[CLEANUP] Limpeza desabilitada nas configurações")
        return
    
    keep_days = int(cleanup_cfg.get("keep_days", 7))
    if keep_days <= 0:
        print(f"[CLEANUP] keep_days inválido: {keep_days}")
        return
    
    print(f"[CLEANUP] Configuração: manter logs dos últimos {keep_days} dias")
    
    ensure_dirs()
    if not LOG_DIR.exists():
        print(f"[CLEANUP] Diretório de logs não existe: {LOG_DIR}")
        return
    
    print(f"[CLEANUP] Diretório de logs: {LOG_DIR}")
    
    # Calcula data limite (logs mais antigos que isso serão removidos)
    from datetime import timedelta
    cutoff_date = datetime.now() - timedelta(days=keep_days)
    print(f"[CLEANUP] Data de corte: {cutoff_date.strftime('%Y-%m-%d %H:%M:%S')}")
    
    removed_count = 0
    total_size = 0
    skipped_count = 0
    
    try:
        # Lista todos os arquivos .log na pasta de logs
        log_files = list(LOG_DIR.glob("*.log"))
        print(f"[CLEANUP] Encontrados {len(log_files)} arquivos .log")
        
        for log_file in log_files:
            try:
                # Verifica a data de modificação do arquivo
                file_mtime = datetime.fromtimestamp(log_file.stat().st_mtime)
                file_age_days = (datetime.now() - file_mtime).days
                
                print(f"[CLEANUP] Arquivo: {log_file.name} | Idade: {file_age_days} dias | Data: {file_mtime.strftime('%Y-%m-%d')}")
                
                if file_mtime < cutoff_date:
                    file_size = log_file.stat().st_size
                    log_file.unlink()
                    removed_count += 1
                    total_size += file_size
                    print(f"[CLEANUP] ✓ Removido: {log_file.name} ({file_size} bytes)")
                else:
                    skipped_count += 1
                    print(f"[CLEANUP] ○ Mantido: {log_file.name}")
            except Exception as e:
                print(f"[CLEANUP] ✗ Erro ao processar {log_file.name}: {e}")
                continue
    except Exception as e:
        print(f"[CLEANUP] ✗ Erro geral ao listar logs: {e}")
        pass
    
    print(f"[CLEANUP] Resumo: {removed_count} removidos, {skipped_count} mantidos")
    
    # Log da limpeza (sempre cria, mesmo se não removeu nada)
    cleanup_log = LOG_DIR / f"cleanup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    try:
        size_mb = total_size / (1024 * 1024)
        with open(cleanup_log, "w", encoding="utf-8") as f:
            f.write(f"# Limpeza automática de logs @ {now_str()}\n")
            f.write(f"Arquivos analisados: {removed_count + skipped_count}\n")
            f.write(f"Arquivos removidos: {removed_count}\n")
            f.write(f"Arquivos mantidos: {skipped_count}\n")
            f.write(f"Espaço liberado: {size_mb:.2f} MB\n")
            f.write(f"Critério: logs mais antigos que {keep_days} dias\n")
            f.write(f"Data de corte: {cutoff_date.strftime('%Y-%m-%d %H:%M:%S')}\n")
        print(f"[CLEANUP] Log de limpeza criado: {cleanup_log}")
    except Exception as e:
        print(f"[CLEANUP] Erro ao criar log de limpeza: {e}")

# ======================================================================================
#  v2025.10.11.8 — Utilidades novas (DPAPI, webhooks, dyn params, KTR parser,
#                  maintenance, digest, backup, YAML, rollback, REST API)
# ======================================================================================

# ---- DPAPI (Windows) -------------------------------------------------------
_DPAPI_PREFIX = "dpapi::"

def dpapi_encrypt(plaintext: str) -> str:
    """Criptografa string com DPAPI do Windows (escopo CurrentUser).
    Retorna string com prefixo 'dpapi::' + base64. Se DPAPI não disponível,
    retorna o próprio plaintext sem alterar."""
    if not plaintext:
        return plaintext
    if plaintext.startswith(_DPAPI_PREFIX):
        return plaintext  # já criptografado
    w = _lazy_dpapi()
    if not w or os.name != "nt":
        return plaintext
    try:
        blob = w.CryptProtectData(plaintext.encode("utf-8"), "AgendadorBravo", None, None, None, 0)
        return _DPAPI_PREFIX + base64.b64encode(blob).decode("ascii")
    except Exception:
        return plaintext

def dpapi_decrypt(value: str) -> str:
    """Decifra string DPAPI. Se não tiver prefixo, retorna como veio (plain)."""
    if not value or not isinstance(value, str):
        return value or ""
    if not value.startswith(_DPAPI_PREFIX):
        return value
    w = _lazy_dpapi()
    if not w or os.name != "nt":
        return ""  # impossível decifrar
    try:
        blob = base64.b64decode(value[len(_DPAPI_PREFIX):].encode("ascii"))
        desc, data = w.CryptUnprotectData(blob, None, None, None, 0)
        return data.decode("utf-8")
    except Exception:
        return ""

def migrate_secrets_to_dpapi(data: dict) -> bool:
    """Migra senhas plain text em settings para DPAPI. Retorna True se algo foi migrado."""
    if os.name != "nt" or not _lazy_dpapi():
        return False
    settings = data.get("settings", {})
    changed = False
    em = settings.get("email", {})
    if em.get("password") and not str(em["password"]).startswith(_DPAPI_PREFIX):
        em["password"] = dpapi_encrypt(em["password"]); changed = True
    wa = settings.get("whatsapp", {})
    if wa.get("auth_token") and not str(wa.get("auth_token","")).startswith(_DPAPI_PREFIX):
        wa["auth_token"] = dpapi_encrypt(wa["auth_token"]); changed = True
    api = settings.get("rest_api", {})
    if api.get("token") and not str(api.get("token","")).startswith(_DPAPI_PREFIX):
        api["token"] = dpapi_encrypt(api["token"]); changed = True
    if changed:
        settings["secrets_encrypted"] = True
    return changed

# ---- Webhooks outbound (Discord/Slack/Teams) -------------------------------
def _detect_webhook_kind(url: str) -> str:
    u = (url or "").lower()
    if "discord.com/api/webhooks" in u or "discordapp.com/api/webhooks" in u:
        return "discord"
    if "hooks.slack.com" in u:
        return "slack"
    if "outlook.office.com/webhook" in u or "webhook.office.com" in u:
        return "teams"
    return "generic"

def _webhook_payload(kind: str, subject: str, body: str, ok: bool) -> dict:
    icon = "✅" if ok else "❌"
    color = 0x22c55e if ok else 0xef4444  # decimal
    text = f"{icon} **{subject}**\n```\n{body[:1800]}\n```"
    if kind == "discord":
        return {
            "username": "Agendador-Bravo",
            "embeds": [{
                "title": f"{icon} {subject}"[:256],
                "description": body[:4000],
                "color": color,
                "timestamp": datetime.now().isoformat(),
            }]
        }
    if kind == "slack":
        return {
            "text": f"{icon} {subject}",
            "attachments": [{
                "color": "#22c55e" if ok else "#ef4444",
                "text": body[:2000],
                "footer": "Agendador-Bravo",
                "ts": int(time.time()),
            }]
        }
    if kind == "teams":
        return {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "themeColor": "22c55e" if ok else "ef4444",
            "summary": subject[:140],
            "title": f"{icon} {subject}"[:120],
            "text": body[:4000].replace("\n", "  \n"),
        }
    # generic
    return {"subject": subject, "body": body, "ok": ok, "ts": now_str()}

def send_webhook(settings, subject, body, ok: bool):
    cfg = (settings or {}).get("webhooks", {})
    if not cfg.get("enabled"):
        return
    if ok and not cfg.get("notify_success", False):
        return
    if (not ok) and not cfg.get("notify_failure", True):
        return
    urls = cfg.get("urls", []) or []
    if isinstance(urls, str):
        urls = [u.strip() for u in urls.split(",") if u.strip()]
    errors = []
    for url in urls:
        kind = _detect_webhook_kind(url)
        payload = _webhook_payload(kind, subject, body, ok)
        try:
            data_bytes = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=data_bytes,
                headers={"Content-Type": "application/json", "User-Agent": "AgendadorBravo/1.0"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                _ = r.read()
        except Exception as e:
            errors.append(f"{kind}: {e}")
    if errors:
        raise RuntimeError("Webhook: " + " | ".join(errors))

# ---- Parâmetros dinâmicos --------------------------------------------------
_DYN_PARAM_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

def _last_business_day(ref: date) -> date:
    """Retorna o último dia útil estritamente anterior a ref (pula sábado/domingo)."""
    d = ref - timedelta(days=1)
    while d.weekday() >= 5:  # 5=sab, 6=dom
        d -= timedelta(days=1)
    return d

def _dynamic_param_values(today: date | None = None, custom: dict | None = None) -> dict:
    """Constrói o dicionário de valores para substituição de ${VAR}."""
    today = today or date.today()
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)
    first_day = today.replace(day=1)
    last_day = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    prev_month_last = first_day - timedelta(days=1)
    prev_month_first = prev_month_last.replace(day=1)
    util = _last_business_day(today)
    vals = {
        "data_atual": today.strftime("%Y-%m-%d"),
        "hoje": today.strftime("%Y-%m-%d"),
        "ontem": yesterday.strftime("%Y-%m-%d"),
        "amanha": tomorrow.strftime("%Y-%m-%d"),
        "primeiro_dia_mes": first_day.strftime("%Y-%m-%d"),
        "ultimo_dia_mes": last_day.strftime("%Y-%m-%d"),
        "ultimo_dia_util": util.strftime("%Y-%m-%d"),
        "mes_anterior": prev_month_first.strftime("%Y-%m"),
        "ano_atual": today.strftime("%Y"),
        "mes_atual": today.strftime("%m"),
        "data_atual_br": today.strftime("%d/%m/%Y"),
        "hoje_br": today.strftime("%d/%m/%Y"),
        "ontem_br": yesterday.strftime("%d/%m/%Y"),
        "timestamp": datetime.now().strftime("%Y%m%d_%H%M%S"),
    }
    if custom:
        for k, v in custom.items():
            if k and v is not None:
                vals[str(k)] = str(v)
    return vals

def resolve_dynamic(text: str, custom: dict | None = None) -> str:
    """Substitui ${var} pelos valores conhecidos. Variáveis não encontradas
    são preservadas como ${var} para o subprocess decidir."""
    if not text or "${" not in text:
        return text or ""
    vals = _dynamic_param_values(custom=custom)
    def _sub(m):
        key = m.group(1)
        return vals.get(key, m.group(0))
    return _DYN_PARAM_PATTERN.sub(_sub, text)

# ---- Pentaho .ktr/.kjb parser ---------------------------------------------
def parse_ktr_variables(file_path: str) -> list[str]:
    """Extrai nomes de variáveis ${...} de um arquivo .ktr/.kjb. Lê XML."""
    try:
        if not file_path or not os.path.exists(file_path):
            return []
        # Lê como texto bruto e procura ${...} — é mais robusto que XML parsing
        # porque cobre variáveis em values, defaults, descriptions, etc.
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        found = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_\.\-]*)\}", content))
        # Tenta também via XML parsing para pegar <parameter name="..."> declarados
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(content)
            for param in root.iter("parameter"):
                nm = (param.findtext("name") or "").strip()
                if nm:
                    found.add(nm)
        except Exception:
            pass
        # Remove variáveis built-in conhecidas que o usuário não precisa setar
        BUILTINS = {"Internal.Transformation.Filename.Directory",
                    "Internal.Job.Filename.Directory",
                    "Internal.Entry.Current.Directory"}
        found = {f for f in found if f not in BUILTINS}
        return sorted(found)
    except Exception:
        return []

# ---- Maintenance windows --------------------------------------------------
def _hhmm_to_minutes(s: str) -> int:
    try:
        hh, mm = s.split(":")
        return int(hh) * 60 + int(mm)
    except Exception:
        return -1

def in_maintenance_window(settings, now: datetime | None = None) -> tuple[bool, str]:
    """Retorna (in_window, window_name) se 'now' cair dentro de alguma janela."""
    now = now or datetime.now()
    cur_min = now.hour * 60 + now.minute
    weekday = now.weekday()  # 0=seg, 6=dom
    for w in (settings or {}).get("maintenance_windows", []) or []:
        if not w:
            continue
        days = w.get("days") or [True]*7
        if not (0 <= weekday < len(days) and days[weekday]):
            continue
        a = _hhmm_to_minutes(w.get("start", ""))
        b = _hhmm_to_minutes(w.get("end", ""))
        if a < 0 or b < 0:
            continue
        if a <= b:
            inside = (a <= cur_min < b)
        else:
            # janela atravessa meia-noite
            inside = (cur_min >= a) or (cur_min < b)
        if inside:
            return True, w.get("name", "manutenção")
    return False, ""

# ---- Backup / Restore ZIP --------------------------------------------------
def backup_to_zip(dest_zip: Path) -> Path:
    """Empacota config.json + logs/ + pids/ + tags + history em um ZIP."""
    dest_zip = Path(dest_zip)
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        if DATA_FILE.exists():
            zf.write(DATA_FILE, arcname="config.json")
        for d_name in ("logs", "pids"):
            d = APP_DIR / d_name
            if d.exists():
                for p in d.rglob("*"):
                    if p.is_file():
                        zf.write(p, arcname=str(p.relative_to(APP_DIR)))
        meta = {
            "backup_at": now_str(),
            "version": APP_VERSION,
            "app_dir": str(APP_DIR),
        }
        zf.writestr("backup_meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
    return dest_zip

def restore_from_zip(src_zip: Path) -> tuple[bool, str]:
    """Restaura backup. Retorna (ok, msg). Não toca em logs/pids do app rodando."""
    src_zip = Path(src_zip)
    if not src_zip.exists():
        return False, f"Arquivo não encontrado: {src_zip}"
    try:
        with zipfile.ZipFile(src_zip, "r") as zf:
            names = zf.namelist()
            if "config.json" not in names:
                return False, "Backup inválido: falta config.json"
            ensure_dirs()
            zf.extract("config.json", path=APP_DIR)
        # invalida cache
        _data_cache["data"] = None
        _data_cache["mtime"] = 0
        return True, "Backup restaurado. Reinicie o Agendador para aplicar."
    except Exception as e:
        return False, f"Falha ao restaurar: {e}"

# ---- Export/Import YAML ---------------------------------------------------
def export_tasks_yaml(dest_path: Path, tasks: list) -> tuple[bool, str]:
    y = _lazy_yaml()
    if not y:
        # fallback JSON com extensão .yaml
        try:
            Path(dest_path).write_text(
                "# Fallback JSON (PyYAML não instalado)\n" +
                json.dumps({"tasks": tasks}, ensure_ascii=False, indent=2),
                encoding="utf-8")
            return True, "Exportado (JSON, sem PyYAML)."
        except Exception as e:
            return False, str(e)
    try:
        Path(dest_path).write_text(
            y.safe_dump({"tasks": tasks}, sort_keys=False, allow_unicode=True),
            encoding="utf-8")
        return True, "Exportado em YAML."
    except Exception as e:
        return False, str(e)

def import_tasks_yaml(src_path: Path) -> tuple[bool, list, str]:
    try:
        text = Path(src_path).read_text(encoding="utf-8")
    except Exception as e:
        return False, [], f"Falha ao ler: {e}"
    parsed = None
    y = _lazy_yaml()
    if y:
        try:
            parsed = y.safe_load(text)
        except Exception:
            parsed = None
    if parsed is None:
        try:
            parsed = json.loads(text)
        except Exception as e:
            return False, [], f"Conteúdo inválido (não é YAML/JSON): {e}"
    tasks = []
    if isinstance(parsed, dict):
        tasks = parsed.get("tasks", []) or []
    elif isinstance(parsed, list):
        tasks = parsed
    if not isinstance(tasks, list):
        return False, [], "Estrutura inválida: 'tasks' deve ser uma lista."
    return True, tasks, f"{len(tasks)} job(s) lidos."

# ---- Rollback automático (counter de crashes no startup) ------------------
_CRASH_COUNTER_FILE = lambda: APP_DIR / "startup_counter.json"

def _read_crash_counter() -> int:
    try:
        p = _CRASH_COUNTER_FILE()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8") or "{}")
            return int(data.get("count", 0) or 0)
    except Exception:
        pass
    return 0

def _write_crash_counter(count: int):
    try:
        ensure_dirs()
        _CRASH_COUNTER_FILE().write_text(
            json.dumps({"count": int(count), "ts": now_str()}, ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass

def startup_increment_and_check() -> tuple[int, bool]:
    """Incrementa contador de crash e retorna (novo_valor, should_rollback).
    Deve ser chamado no __main__ antes do App() iniciar."""
    c = _read_crash_counter() + 1
    _write_crash_counter(c)
    threshold = 3
    try:
        d = load_data()
        threshold = int(d.get("settings", {}).get("rollback", {}).get("crash_threshold", 3) or 3)
    except Exception:
        pass
    return c, c >= threshold

def startup_mark_success():
    """Chamado quando a UI aparece com sucesso — zera o contador."""
    _write_crash_counter(0)

def try_rollback_exe() -> tuple[bool, str]:
    """Tenta restaurar o backup .exe.bak (criado pelo updater) e re-lançar."""
    if not _is_frozen():
        return False, "Modo dev: rollback ignorado"
    exe = _exe_path()
    bak = exe.with_suffix(".exe.bak")
    if not bak.exists():
        return False, "Sem backup (.exe.bak) para fazer rollback"
    try:
        # Não dá pra sobrescrever o próprio exe enquanto roda → grava VBS pra fazer
        ensure_dirs()
        vbs = APP_DIR / "_rollback.vbs"
        log = APP_DIR / "logs" / f"rollback_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        script = f'''Option Explicit
Dim sh, fso
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
On Error Resume Next
sh.Run "taskkill /IM """ & "{exe.name}" & """ /F", 0, True
WScript.Sleep 1500
fso.CopyFile "{str(bak).replace(chr(92), chr(92)+chr(92))}", "{str(exe).replace(chr(92), chr(92)+chr(92))}", True
Dim f
Set f = fso.OpenTextFile("{str(log).replace(chr(92), chr(92)+chr(92))}", 8, True)
f.WriteLine "[" & Now & "] rollback executado: " & "{exe.name}" & " <- .exe.bak"
f.Close
sh.Run """" & "{str(exe).replace(chr(92), chr(92)+chr(92))}" & """", 1, False
'''
        vbs.write_text(script, encoding="utf-8")
        subprocess.Popen(["wscript.exe", str(vbs)], shell=False)
        return True, f"Rollback acionado; veja {log}"
    except Exception as e:
        return False, f"Falha no rollback: {e}"

# ---- REST API local (http.server stdlib) ----------------------------------
class _RestAPIHandler:
    """Compactado em closure dentro de start_rest_api para acessar o app."""
    pass

def start_rest_api(app_ref):
    """Inicia (uma vez) o servidor HTTP local. Lê settings em runtime."""
    import http.server, socketserver

    cfg = app_ref.data.get("settings", {}).get("rest_api", {}) or {}
    if not cfg.get("enabled"):
        return None
    port = int(cfg.get("port", 17654) or 17654)
    host = cfg.get("bind_host", "127.0.0.1") or "127.0.0.1"
    token_enc = cfg.get("token", "")
    token = dpapi_decrypt(token_enc) if token_enc else ""

    def _ok():
        return token  # cache

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # silencia stdout
            pass

        def _auth(self):
            if not token:
                return True
            got = self.headers.get("X-Auth-Token") or ""
            return got == token

        def _send(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._auth():
                return self._send(401, {"error": "unauthorized"})
            path = self.path.split("?", 1)[0].rstrip("/")
            if path in ("", "/"):
                return self._send(200, {
                    "app": APP_NAME, "version": APP_VERSION,
                    "tasks_count": len(app_ref.data.get("tasks", [])),
                    "endpoints": ["GET /jobs", "GET /jobs/{name}", "POST /jobs/{name}/run", "GET /health"]
                })
            if path == "/health":
                return self._send(200, {"ok": True, "ts": now_str()})
            if path == "/jobs":
                tasks = app_ref.data.get("tasks", [])
                return self._send(200, {"jobs": [
                    {"name": t["name"], "enabled": t.get("enabled", True),
                     "schedule_type": t.get("schedule_type", "cron"),
                     "tags": t.get("tags", []),
                     "path": t.get("path", "")}
                    for t in tasks]})
            if path.startswith("/jobs/"):
                name = urllib.parse.unquote(path.split("/jobs/", 1)[1])
                t = next((x for x in app_ref.data.get("tasks", []) if x.get("name") == name), None)
                if not t:
                    return self._send(404, {"error": "not_found"})
                return self._send(200, t)
            return self._send(404, {"error": "not_found"})

        def do_POST(self):
            if not self._auth():
                return self._send(401, {"error": "unauthorized"})
            path = self.path.split("?", 1)[0].rstrip("/")
            if path.startswith("/jobs/") and path.endswith("/run"):
                name = urllib.parse.unquote(path[len("/jobs/"):-len("/run")])
                t = next((x for x in app_ref.data.get("tasks", []) if x.get("name") == name), None)
                if not t:
                    return self._send(404, {"error": "not_found"})
                try:
                    threading.Thread(target=lambda: app_ref._job_wrapper(t), daemon=True).start()
                    return self._send(202, {"queued": name})
                except Exception as e:
                    return self._send(500, {"error": str(e)})
            return self._send(404, {"error": "not_found"})

    try:
        server = http.server.ThreadingHTTPServer((host, port), Handler)
    except Exception as e:
        print(f"[REST API] falha ao iniciar em {host}:{port}: {e}")
        return None

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"[REST API] escutando em http://{host}:{port}")
    return server

import urllib.parse  # usado na REST API

# ======================================================================================
#  Notificações
# ======================================================================================

def send_email(settings, subject, body):
    cfg = settings.get("email", {})
    if not cfg.get("enabled"):
        return
    to_emails = cfg.get("to_emails", [])
    if isinstance(to_emails, str):
        to_emails = [e.strip() for e in to_emails.split(",") if e.strip()]
    if not to_emails:
        return

    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = cfg["from_email"] or cfg["username"]
    msg["To"] = ", ".join(to_emails)
    msg["Date"] = formatdate(localtime=True)

    context = ssl.create_default_context()
    pwd = dpapi_decrypt(cfg.get("password", "")) if cfg.get("password") else ""
    with smtplib.SMTP(cfg["smtp_host"], int(cfg.get("smtp_port", 587)), timeout=30) as server:
        server.ehlo()
        server.starttls(context=context)
        server.login(cfg["username"], pwd)
        server.sendmail(msg["From"], to_emails, msg.as_string())

def send_whatsapp(settings, subject, body, timeout_sec=45):
    cfg = settings.get("whatsapp", {})
    if not cfg.get("enabled"):
        return

    mode = cfg.get("mode", "webjs")
    if mode == "twilio":
        try:
            from twilio.rest import Client
        except Exception:
            return
        auth_token = dpapi_decrypt(cfg.get("auth_token","")) if cfg.get("auth_token") else ""
        client = Client(cfg.get("account_sid",""), auth_token)
        to_numbers = cfg.get("to_numbers") or cfg.get("to_targets") or []
        if isinstance(to_numbers, str):
            to_numbers = [n.strip() for n in to_numbers.split(",") if n.strip()]
        text = f"{subject}\n\n{body[:1500]}"
        for to in to_numbers:
            client.messages.create(from_=cfg.get("from_number",""), to=to, body=text)
        return

    # WebJS (QR)
    node   = cfg.get("node_path", r"C:\Program Files\nodejs\node.exe")
    script = cfg.get("webjs_script", str(resource_path("wa", "wa_send.js")))
    tos    = cfg.get("to_targets", [])
    if isinstance(tos, str):
        tos = [t.strip() for t in tos.split(",") if t.strip()]

    if not tos or not os.path.exists(node) or not os.path.exists(script):
        raise RuntimeError("WhatsApp (QR) não configurado corretamente.")

    ensure_dirs()
    msg = f"{subject}\n\n{body}"
    cmd = [node, script, "--to", ",".join(tos), "--message", msg]

    proc = subprocess.run(
    cmd,
    cwd=str(WA_DIR),
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    encoding="utf-8",
    errors="ignore",   # <— evita UnicodeDecodeError
    timeout=timeout_sec
)

    out = (proc.stdout or "")[-800:]
    if proc.returncode != 0:
        raise RuntimeError(f"wa_send.js RC={proc.returncode}\n{out}")

# ======================================================================================
#  Execução de tarefas / logs
# ======================================================================================

def build_command(task, pdi_home):
    path = task["path"]
    args = task.get("args","").strip()
    # v2025.10.11.8 — parâmetros dinâmicos: ${data_atual}, ${ultimo_dia_util}, etc.
    # Também substitui vars custom definidas em task["variables"].
    if task.get("dynamic_params", True):
        custom = task.get("variables", {}) or {}
        args = resolve_dynamic(args, custom=custom)
        path = resolve_dynamic(path, custom=custom)
    arg_list = shlex.split(args, posix=False) if args else []
    ext = Path(path).suffix.lower()

    if ext == ".exe":
        return [path] + arg_list
    if ext in (".bat", ".cmd"):
        return ["cmd", "/c", path] + arg_list
    if ext == ".ps1":
       # Executa PowerShell oculto
        return ["powershell", "-NoLogo", "-NonInteractive",
                "-WindowStyle", "Hidden",
                "-ExecutionPolicy", "Bypass", "-File", path] + arg_list
    if ext == ".py":
        # Para scripts Python, usa flags que garantem encerramento limpo
        py = sys.executable
        # -u: unbuffered output (para ver logs em tempo real)
        # -B: não cria arquivos .pyc
        return [py, "-u", "-B", path] + arg_list
    if ext == ".ktr":
        # Otimizações para Pan (Kettle Transformation)
        pan_path = str(Path(pdi_home)/"Pan.bat")
        optimization_flags = [
            f"/file:{path}",
            "/level:Basic",  # Nível de log básico (menos overhead)
            "/norep",        # Não usa repositório (mais rápido)
        ]
        return [pan_path] + optimization_flags + arg_list
    if ext == ".kjb":
        # Otimizações para Kitchen (Kettle Job)
        kitchen_path = str(Path(pdi_home)/"Kitchen.bat")
        optimization_flags = [
            f"/file:{path}",
            "/level:Basic",  # Nível de log básico
            "/norep",        # Não usa repositório
        ]
        return [kitchen_path] + optimization_flags + arg_list
    return [path] + arg_list

def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if psutil:
        try:
            return psutil.pid_exists(pid)
        except Exception:
            pass
    # Fallback simples no Windows via tasklist
    if os.name == "nt":
        try:
            res = subprocess.run(
    ["cmd", "/c", f'tasklist /FI "PID eq {pid}"'],
    capture_output=True, text=True, encoding="utf-8",
    errors="ignore",   # <— evita UnicodeDecodeError
    timeout=5
)
            out = (res.stdout or "")
            return "INFO: No tasks" not in out and str(pid) in out
        except Exception:
            return False
    return False

def _already_running_by_pidfile(task) -> bool:
    """Usa arquivo PID para verificar se o processo anterior (spawn) ainda vive."""
    pidfile = PID_DIR / (_safe_name(task["name"]) + ".pid")
    if not pidfile.exists():
        return False
    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip())
    except Exception:
        return False
    alive = _pid_alive(pid)
    if not alive:
        # limpa pid antigo
        try: pidfile.unlink(missing_ok=True)
        except Exception: pass
    return alive

def _write_pid(task, pid: int):
    try:
        (PID_DIR / (_safe_name(task["name"]) + ".pid")).write_text(str(pid), encoding="utf-8")
    except Exception:
        pass

def cleanup_orphaned_python_processes():
    """Limpa processos Python órfãos que podem ter ficado em execução."""
    if not psutil:
        return
    
    try:
        current_pid = os.getpid()
        python_exe = os.path.basename(sys.executable).lower()
        
        print(f"[CLEANUP] Verificando processos Python órfãos...")
        
        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time']):
            try:
                # Verifica se é um processo Python
                if proc.info['name'] and python_exe in proc.info['name'].lower():
                    # Não mata o processo atual
                    if proc.info['pid'] == current_pid:
                        continue
                    
                    # Verifica se é um processo antigo (mais de 1 hora)
                    age_hours = (time.time() - proc.info['create_time']) / 3600
                    
                    # Se for muito antigo, pode ser órfão
                    if age_hours > 1:
                        cmdline = proc.info.get('cmdline', [])
                        # Verifica se está executando um script (não é o agendador)
                        if cmdline and len(cmdline) > 1 and cmdline[1].endswith('.py'):
                            script_name = os.path.basename(cmdline[1])
                            if 'agendador' not in script_name.lower():
                                print(f"[CLEANUP] Processo órfão encontrado: PID {proc.info['pid']} - {script_name} (idade: {age_hours:.1f}h)")
                                try:
                                    proc.terminate()
                                    proc.wait(timeout=3)
                                    print(f"[CLEANUP] ✓ Processo {proc.info['pid']} encerrado")
                                except Exception as e:
                                    print(f"[CLEANUP] ✗ Erro ao encerrar processo {proc.info['pid']}: {e}")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception as e:
                print(f"[CLEANUP] Erro ao verificar processo: {e}")
                continue
                
        print(f"[CLEANUP] Verificação de processos órfãos concluída")
    except Exception as e:
        print(f"[CLEANUP] Erro geral na limpeza de processos: {e}")

def run_task(task, settings, progress_cb=None, process_callback=None):
    ensure_dirs()
    name = task["name"]
    log_file = LOG_DIR / f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    pdi_home = settings.get("pdi_home", r"C:\Pentaho\data-integration")
    cmd = build_command(task, pdi_home)
    # workdir resolvido também (caso o path use ${variáveis})
    custom = task.get("variables", {}) or {}
    raw_path = task.get("path", "")
    resolved_path = resolve_dynamic(raw_path, custom=custom) if task.get("dynamic_params", True) else raw_path
    raw_workdir = task.get("working_dir") or str(Path(resolved_path).parent)
    workdir = resolve_dynamic(raw_workdir, custom=custom) if task.get("dynamic_params", True) else raw_workdir
    # timeout pode ser numero (segundos) ou string vazia
    timeout = int(task.get("timeout", "0") or 0) or None
    spawn = bool(task.get("spawn", False))

        # --- Janelas ocultas no Windows ---
    si = None
    
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE

        CREATE_NEW_PROCESS_GROUP = 0x00000200
        DETACHED_PROCESS        = 0x00000008
        CREATE_NO_WINDOW        = 0x08000000


    # Otimizações de ambiente
    env = os.environ.copy()
    # Python
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")  # Não cria .pyc
    # Pentaho/Java (otimizações de memória)
    env.setdefault("PENTAHO_DI_JAVA_OPTIONS", "-Xms512m -Xmx2048m -XX:+UseG1GC")
    # Desabilita verificações desnecessárias
    env.setdefault("KETTLE_DISABLE_CONSOLE_LOGGING", "Y")

    # se for spawn e já tem processo vivo, só loga e sai
    if spawn and _already_running_by_pidfile(task):
        try:
            with open(log_file, "a", encoding="utf-8", errors="ignore") as f:
                f.write(f"# {name} @ {now_str()} (spawn)\n")
                f.write("Processo já está em execução. Nada a fazer.\n")
        except Exception:
            pass
        return 0, 0.0, str(log_file)

    start = time.time()

    if spawn:
        # inicia DETACHED escrevendo direto no log e retorna
        try:
            log_fh = open(log_file, "a", encoding="utf-8", errors="ignore")
            log_fh.write(f"# {name} @ {now_str()} (spawn)\nCMD: {' '.join(cmd)}\n\n")
            popen_kwargs = dict(
                cwd=workdir, stdout=log_fh, stderr=subprocess.STDOUT, env=env
            )
            if os.name == "nt":
                popen_kwargs["creationflags"] = (CREATE_NEW_PROCESS_GROUP |
                                                 DETACHED_PROCESS | CREATE_NO_WINDOW)
                popen_kwargs["startupinfo"] = si
            else:
                import os as _os
                popen_kwargs["preexec_fn"] = _os.setpgrp


            proc = subprocess.Popen(cmd, **popen_kwargs)
            _write_pid(task, proc.pid)
            if process_callback:
                process_callback(proc, task['name'])
            try:
                log_fh.flush(); log_fh.close()
            except Exception:
                pass
            return 0, time.time() - start, str(log_file)
        except Exception as e:
            with open(log_file, "a", encoding="utf-8", errors="ignore") as f:
                f.write("\n### ERRO ao iniciar em modo spawn:\n" + "".join(traceback.format_exception(e)))
            return -1, time.time() - start, str(log_file)

    # modo tradicional: stream da saída para o log, aguardando terminar
    with open(log_file, "w", encoding="utf-8", errors="ignore") as f:
        f.write(f"# {name} @ {now_str()}\nCMD: {' '.join(cmd)}\n\n")
        try:
            proc = subprocess.Popen(
                cmd, cwd=workdir,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="ignore", bufsize=1, env=env,
                **({"startupinfo": si, "creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {})
            )
            
            # Registra o processo no callback, se fornecido
            if process_callback and callable(process_callback):
                process_callback(proc, task['name'])

            while True:
                line = proc.stdout.readline()
                if not line and proc.poll() is not None:
                    break
                if line:
                    f.write(line)
                    if progress_cb:
                        progress_cb(line.strip()[:140])
            
            # Aguarda o processo terminar completamente
            proc.wait()
            
            if timeout and (time.time() - start) > timeout:
                try: 
                    proc.kill()
                    proc.wait(timeout=2)  # Aguarda até 2 segundos para o processo morrer
                except Exception: 
                    pass
                rc = -9
                f.write("\n### TIMEOUT atingido.\n")
            else:
                rc = proc.returncode
            
            # Garante que o processo e seus filhos sejam encerrados (especialmente Python)
            try:
                if psutil and proc.pid:
                    try:
                        parent = psutil.Process(proc.pid)
                        # Encerra processos filhos primeiro
                        for child in parent.children(recursive=True):
                            try:
                                child.terminate()
                            except Exception:
                                pass
                        # Aguarda um pouco
                        psutil.wait_procs(parent.children(recursive=True), timeout=1)
                        # Força o encerramento se ainda estiver vivo
                        if parent.is_running():
                            parent.terminate()
                            parent.wait(timeout=1)
                    except psutil.NoSuchProcess:
                        pass  # Processo já morreu
                    except Exception as e:
                        f.write(f"\n### Aviso ao limpar processos: {e}\n")
            except Exception:
                pass
                
        except Exception as e:
            rc = -1
            f.write("\n### ERRO ao iniciar/executar:\n" + "".join(traceback.format_exception(e)))
            # Tenta matar o processo se ainda existir
            try:
                if 'proc' in locals() and proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=2)
            except Exception:
                pass

    return rc, time.time() - start, str(log_file)

# ======================================================================================
#  Diálogos
# ======================================================================================

def parse_times(text: str):
    """
    Converte '13:30, 14:16;18:00' -> ['13:30','14:16','18:00'].
    Aceita separadores: vírgula, ponto e vírgula ou espaços.
    """
    parts = [p for p in re.split(r"[,\s;]+", text.strip()) if p]
    out = []
    for p in parts:
        h, m = p.split(":")
        h = int(h); m = int(m)
        assert 0 <= h <= 23 and 0 <= m <= 59
        norm = f"{h:02d}:{m:02d}"
        if norm not in out:
            out.append(norm)
    return out or ["06:00"]

# ===== classes =====
class ToolTip:
    """
    Cria uma dica de ferramenta para um widget.
    """
    def __init__(self, widget, text='widget info'):
        self.waittime = 500     # milissegundos
        self.wraplength = 300   # pixels
        self.widget = widget
        self.text = text
        self.widget.bind("<Enter>", self.enter)
        self.widget.bind("<Leave>", self.leave)
        self.widget.bind("<ButtonPress>", self.leave)
        self.id = None
        self.tw = None

    def enter(self, event=None):
        self.schedule()
        
    def schedule(self):
        self.unschedule()
        self.id = self.widget.after(self.waittime, self.showtip)
        
    def unschedule(self):
        id = self.id
        self.id = None
        if id:
            self.widget.after_cancel(id)
            
    def leave(self, event=None):
        self.unschedule()
        self.hidetip()
        
    def showtip(self):
        """Exibe a dica de ferramenta com o texto."""
        x = y = 0
        x, y, _, _ = self.widget.bbox("insert")
        x += self.widget.winfo_rootx() + 25
        y += self.widget.winfo_rooty() + 25
        
        # Cria a janela de dica
        self.tw = tk.Toplevel(self.widget)
        self.tw.wm_overrideredirect(True)
        self.tw.wm_geometry(f"+{x}+{y}")
        
        # Estilo da dica
        label = ttk.Label(
            self.tw, 
            text=self.text, 
            justify='left',
            background="#ffffe0", 
            relief='solid', 
            borderwidth=1,
            padding=5,
            wraplength=self.wraplength
        )
        label.pack(ipadx=1)
        
        # Garante que a dica fique acima de outras janelas
        self.tw.attributes('-topmost', True)
        
        # Remove o foco da janela de dica
        self.tw.focus_set()
        
        # Posiciona a dica para não sair da tela
        self.tw.update_idletasks()
        width = self.tw.winfo_width()
        height = self.tw.winfo_height()
        screen_width = self.tw.winfo_screenwidth()
        screen_height = self.tw.winfo_screenheight()
        
        if x + width > screen_width - 10:
            x = screen_width - width - 10
        if y + height > screen_height - 10:
            y = screen_height - height - 10
            
        self.tw.wm_geometry(f"+{x}+{y}")

    def hidetip(self):
        """Esconde a dica de ferramenta."""
        if self.tw:
            self.tw.destroy()
            self.tw = None

class TaskDialog(tk.Toplevel):
    def __init__(self, master, task=None, existing_jobs=None, all_tags=None):
        super().__init__(master)
        self.title("Tarefa")
        self.resizable(True, True)   # v2025.10.11.8 — agora tem mais campos
        self.result = None
        # Lista de outros jobs (para depends_on) e universo de tags (para combobox)
        self._existing_jobs = list(existing_jobs or [])
        self._all_tags = list(all_tags or [])

        # ---------- Vars ----------
        self.var_name = tk.StringVar(value=(task or {}).get("name", ""))
        self.var_path = tk.StringVar(value=(task or {}).get("path", ""))
        self.var_args = tk.StringVar(value=(task or {}).get("args", ""))
        self.var_work = tk.StringVar(value=(task or {}).get("working_dir", ""))
        # v2025.10.11.8 — campos novos
        self.var_tags = tk.StringVar(value=", ".join((task or {}).get("tags", []) or []))
        self.var_dyn = tk.BooleanVar(value=(task or {}).get("dynamic_params", True))
        self.var_resp_mw = tk.BooleanVar(value=(task or {}).get("respect_maintenance", True))
        _retry = (task or {}).get("retry", {}) or {}
        self.var_retry_on = tk.BooleanVar(value=_retry.get("enabled", False))
        self.var_retry_max = tk.StringVar(value=str(_retry.get("max_attempts", 3)))
        backoff_list = _retry.get("backoff_seconds", [60, 300, 900]) or [60, 300, 900]
        self.var_retry_backoff = tk.StringVar(value=",".join(str(x) for x in backoff_list))
        self._depends_on = list((task or {}).get("depends_on", []) or [])
        self._variables = dict((task or {}).get("variables", {}) or {})
        # ---- "Início + repetição" (start_repeat) ----
        self.var_sr_start      = tk.StringVar(value=(task or {}).get("sr_start", (task or {}).get("time", "06:00")))
        self.var_sr_every_val  = tk.StringVar(value=str((task or {}).get("sr_every_value", (task or {}).get("every_value", 5))))
        self.var_sr_every_unit = tk.StringVar(value=(task or {}).get("sr_every_unit", (task or {}).get("every_unit", "minutes")))
        self.var_sr_count      = tk.StringVar(value=str((task or {}).get("sr_count", 5)))


        # horários (lista interna) + string para exibir
        times_seed = (task or {}).get("times")
        if not times_seed:
            # compatibilidade com versões antigas: pegar 'time' único
            t0 = (task or {}).get("time", "06:00")
            try:
                times_seed = parse_times(t0)
            except Exception:
                times_seed = ["06:00"]
        self.times = list(dict.fromkeys(times_seed))  # únicos, mantém ordem
        self.var_times_str = tk.StringVar(value=self._fmt_times())

        self.var_timeout = tk.StringVar(value=str((task or {}).get("timeout", "0")))
        self.var_notify_fail = tk.BooleanVar(value=((task or {}).get("notify_fail", True)))
        self.var_schedule = tk.StringVar(value=(task or {}).get("schedule_type", "cron"))
        self.var_every_val = tk.StringVar(value=str((task or {}).get("every_value", "30")))
        self.var_every_unit = tk.StringVar(value=(task or {}).get("every_unit", "minutes"))
        self.var_spawn = tk.BooleanVar(value=(task or {}).get("spawn", True))

        days = (task or {}).get("days", [True] * 7)
        self.days_vars = [tk.BooleanVar(value=days[i]) for i in range(7)]

        # ---------- Layout ----------
        # v2025.10.11.12 — mais padding interno
        frm = ttk.Frame(self, padding=18)
        frm.grid(sticky="nsew")
        frm.columnconfigure(1, weight=1)

        # default pad entre linhas
        rowpad = {"pady": 4}

        row = 0
        ttk.Label(frm, text="Nome:").grid(row=row, column=0, sticky="w", **rowpad)
        ttk.Entry(frm, textvariable=self.var_name, width=42)\
            .grid(row=row, column=1, columnspan=3, sticky="we", **rowpad)
        row += 1

        ttk.Label(frm, text="Arquivo/Comando:").grid(row=row, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.var_path, width=42)\
            .grid(row=row, column=1, columnspan=2, sticky="we")
        ttk.Button(frm, text="Procurar...", command=self.pick_file)\
            .grid(row=row, column=3, sticky="we")
        row += 1

        ttk.Label(frm, text="Argumentos (opcional):").grid(row=row, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.var_args, width=42)\
            .grid(row=row, column=1, columnspan=3, sticky="we")
        row += 1

        ttk.Label(frm, text="Pasta de trabalho:").grid(row=row, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.var_work, width=42)\
            .grid(row=row, column=1, columnspan=2, sticky="we")
        ttk.Button(frm, text="Escolher...", command=self.pick_dir)\
            .grid(row=row, column=3, sticky="we")
        row += 1

        # ---- Linha Horários + Timeout ----
        self.lbl_time_title = ttk.Label(frm, text="Horário(s):")
        self.lbl_time_title.grid(row=row, column=0, sticky="w")

        times_box = ttk.Frame(frm)
        times_box.grid(row=row, column=1, sticky="we", padx=(0, 6))
        times_box.columnconfigure(0, weight=1)

        self.lbl_times = ttk.Label(times_box, textvariable=self.var_times_str, anchor="w")
        self.lbl_times.grid(row=0, column=0, sticky="we")

        self.btn_times = ttk.Button(times_box, text="Horários…", width=12, command=self.edit_times)
        self.btn_times.grid(row=0, column=1, padx=(8, 0))

        ttk.Label(frm, text="Timeout (s, 0=sem):").grid(row=row, column=2, sticky="e")
        ttk.Entry(frm, textvariable=self.var_timeout, width=10)\
            .grid(row=row, column=3, sticky="w")
        row += 1

        # ---- Dias da semana ----
        days_row = ttk.Frame(frm)
        days_row.grid(row=row, column=0, columnspan=4, sticky="w", pady=(4, 0))
        for i, lab in enumerate(["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]):
            ttk.Checkbutton(days_row, text=lab, variable=self.days_vars[i]).grid(row=0, column=i, padx=2)
        row += 1

        # ---- Agendamento ----
        sched = ttk.LabelFrame(frm, text="Agendamento", padding=(6, 6))
        sched.grid(row=row, column=0, columnspan=4, sticky="we", pady=(6, 0))
        row += 1

        type_row = ttk.Frame(sched)
        type_row.grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Radiobutton(type_row, text="Horário fixo", value="cron",
                        variable=self.var_schedule).grid(row=0, column=0, padx=(0, 10))
        ttk.Radiobutton(type_row, text="Intervalo", value="interval",
                        variable=self.var_schedule).grid(row=0, column=1)
        ttk.Radiobutton(type_row, text="Início + repetição", value="start_repeat",
                variable=self.var_schedule).grid(row=0, column=2, padx=(10, 0))


        self.int_row = ttk.Frame(sched)
        self.int_row.grid(row=1, column=0, columnspan=4, pady=(6, 0), sticky="w")
        ttk.Label(self.int_row, text="A cada").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.int_row, textvariable=self.var_every_val, width=6)\
            .grid(row=0, column=1, padx=(4, 6))
        ttk.Combobox(self.int_row, textvariable=self.var_every_unit,
                     values=("minutes", "hours"), width=10, state="readonly")\
            .grid(row=0, column=2)
        self.sr_row = ttk.Frame(sched)  # Início + repetição
        self.sr_row.grid(row=2, column=0, columnspan=4, pady=(6, 0), sticky="w")

        ttk.Label(self.sr_row, text="Início (HH:MM)").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.sr_row, textvariable=self.var_sr_start, width=8).grid(row=0, column=1, padx=(4, 12))

        ttk.Label(self.sr_row, text="Repetir a cada").grid(row=0, column=2, sticky="w")
        ttk.Entry(self.sr_row, textvariable=self.var_sr_every_val, width=6).grid(row=0, column=3, padx=(4, 6))
        ttk.Combobox(self.sr_row, textvariable=self.var_sr_every_unit,
             values=("minutes", "hours"), width=10, state="readonly").grid(row=0, column=4)

        ttk.Label(self.sr_row, text="por").grid(row=0, column=5, padx=(10, 2))
        ttk.Entry(self.sr_row, textvariable=self.var_sr_count, width=6).grid(row=0, column=6)
        ttk.Label(self.sr_row, text="vezes").grid(row=0, column=7, padx=(4, 0))


        # alterna UI conforme o tipo
        self.var_schedule.trace_add("write", lambda *_: self._toggle_schedule_ui())
        self._toggle_schedule_ui()

        # ---- Opções ----
        ttk.Checkbutton(frm, text="Notificar ao falhar", variable=self.var_notify_fail)\
            .grid(row=row, column=0, columnspan=4, sticky="w", pady=(6, 0))
        row += 1

        ttk.Checkbutton(frm,
                        text="Executar em segundo plano (não aguardar término)",
                        variable=self.var_spawn)\
            .grid(row=row, column=0, columnspan=4, sticky="w")
        row += 1

        # ---- v2025.10.11.8 — Avançado ----
        adv = ttk.LabelFrame(frm, text="Avançado", padding=(6, 6))
        adv.grid(row=row, column=0, columnspan=4, sticky="we", pady=(8, 0))
        adv.columnconfigure(1, weight=1)
        row += 1

        # Tags
        ttk.Label(adv, text="Tags (vírgula):").grid(row=0, column=0, sticky="w")
        tags_combo = ttk.Combobox(adv, textvariable=self.var_tags,
                                   values=self._all_tags, width=42)
        tags_combo.grid(row=0, column=1, columnspan=3, sticky="we", padx=(4, 0))
        ToolTip(tags_combo, "Etiquetas separadas por vírgula. Ex.: ETL, Diário")

        # Retry
        ttk.Checkbutton(adv, text="Retry automático em falha",
                        variable=self.var_retry_on)\
            .grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(adv, text="Tentativas:").grid(row=2, column=0, sticky="w")
        ttk.Entry(adv, textvariable=self.var_retry_max, width=6).grid(row=2, column=1, sticky="w")
        ttk.Label(adv, text="Backoff (s, vírgula):").grid(row=2, column=2, sticky="e")
        ttk.Entry(adv, textvariable=self.var_retry_backoff, width=24).grid(row=2, column=3, sticky="we")

        # Dependências
        dep_frame = ttk.Frame(adv)
        dep_frame.grid(row=3, column=0, columnspan=4, sticky="we", pady=(6, 0))
        dep_frame.columnconfigure(1, weight=1)
        ttk.Label(dep_frame, text="Depende de:").grid(row=0, column=0, sticky="w")
        self._lbl_depends = ttk.Label(dep_frame, text=self._fmt_depends_label())
        self._lbl_depends.grid(row=0, column=1, sticky="we", padx=(4, 4))
        ttk.Button(dep_frame, text="Editar…", width=10,
                   command=self._edit_depends).grid(row=0, column=2, sticky="e")

        # Variáveis customizadas + Pentaho detector
        var_frame = ttk.Frame(adv)
        var_frame.grid(row=4, column=0, columnspan=4, sticky="we", pady=(6, 0))
        var_frame.columnconfigure(1, weight=1)
        ttk.Label(var_frame, text="Variáveis:").grid(row=0, column=0, sticky="w")
        self._lbl_vars = ttk.Label(var_frame, text=self._fmt_vars_label())
        self._lbl_vars.grid(row=0, column=1, sticky="we", padx=(4, 4))
        ttk.Button(var_frame, text="Editar…", width=10,
                   command=self._edit_vars).grid(row=0, column=2, sticky="e")
        ttk.Button(var_frame, text="🔍 Detectar Pentaho", width=18,
                   command=self._detect_pentaho_vars).grid(row=0, column=3, padx=(4, 0))

        # Toggles
        ttk.Checkbutton(adv,
                        text="Resolver ${data_atual}, ${ultimo_dia_util}, etc. nos args/path",
                        variable=self.var_dyn)\
            .grid(row=5, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Checkbutton(adv,
                        text="Respeitar janelas de manutenção",
                        variable=self.var_resp_mw)\
            .grid(row=6, column=0, columnspan=4, sticky="w")

        # ---- Botões ----
        btns = ttk.Frame(frm)
        btns.grid(row=row, column=0, columnspan=4, pady=(10, 0))
        ttk.Button(btns, text="Salvar", command=self.on_save).grid(row=0, column=0, padx=6)
        ttk.Button(btns, text="Cancelar", command=self.destroy).grid(row=0, column=1, padx=6)

        self.grab_set()
        self.wait_visibility()
        self.focus_set()

    # ---------- Helpers UI ----------
    def _fmt_times(self):
        return ", ".join(self.times) if self.times else "— nenhum —"

    def _fmt_depends_label(self):
        if not self._depends_on:
            return "— nenhum —"
        return ", ".join(self._depends_on[:3]) + (f" (+{len(self._depends_on)-3})" if len(self._depends_on) > 3 else "")

    def _fmt_vars_label(self):
        if not self._variables:
            return "— nenhuma —"
        items = list(self._variables.items())[:3]
        s = ", ".join(f"{k}={v}" for k, v in items)
        if len(self._variables) > 3:
            s += f" (+{len(self._variables)-3})"
        return s

    def _edit_depends(self):
        """Multi-select de jobs existentes para depends_on."""
        dlg = tk.Toplevel(self)
        dlg.title("Dependências")
        dlg.resizable(False, False)
        dlg.grab_set()
        frm = ttk.Frame(dlg, padding=10); frm.grid(sticky="nsew")
        ttk.Label(frm, text="Marque os jobs que precisam rodar com SUCESSO antes deste:").grid(
            row=0, column=0, columnspan=2, sticky="w")
        lb = tk.Listbox(frm, selectmode="multiple", height=10, width=42, exportselection=False)
        lb.grid(row=1, column=0, columnspan=2, sticky="we", pady=(6, 0))
        # popula com jobs (exceto este mesmo)
        my_name = self.var_name.get().strip()
        for n in self._existing_jobs:
            if n == my_name:
                continue
            lb.insert("end", n)
        # pré-seleciona
        for i in range(lb.size()):
            if lb.get(i) in self._depends_on:
                lb.selection_set(i)
        def _save():
            self._depends_on = [lb.get(i) for i in lb.curselection()]
            self._lbl_depends.config(text=self._fmt_depends_label())
            dlg.destroy()
        ttk.Button(frm, text="OK", command=_save).grid(row=2, column=0, pady=(8, 0), sticky="e", padx=4)
        ttk.Button(frm, text="Cancelar", command=dlg.destroy).grid(row=2, column=1, pady=(8, 0), sticky="w", padx=4)

    def _edit_vars(self):
        """Editor simples de variáveis NAME=valor."""
        dlg = tk.Toplevel(self)
        dlg.title("Variáveis customizadas")
        dlg.resizable(True, True)
        dlg.grab_set()
        frm = ttk.Frame(dlg, padding=10); frm.grid(sticky="nsew")
        ttk.Label(frm, text="Uma variável por linha: NOME=VALOR\nUse ${NOME} nos args/path.",
                  foreground="#666").grid(row=0, column=0, columnspan=2, sticky="w")
        txt = tk.Text(frm, height=12, width=48)
        txt.grid(row=1, column=0, columnspan=2, sticky="we", pady=(6, 0))
        for k, v in self._variables.items():
            txt.insert("end", f"{k}={v}\n")
        def _save():
            new = {}
            for line in txt.get("1.0", "end").splitlines():
                line = line.strip()
                if not line or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k:
                    new[k] = v.strip()
            self._variables = new
            self._lbl_vars.config(text=self._fmt_vars_label())
            dlg.destroy()
        ttk.Button(frm, text="OK", command=_save).grid(row=2, column=0, pady=(8, 0), sticky="e", padx=4)
        ttk.Button(frm, text="Cancelar", command=dlg.destroy).grid(row=2, column=1, pady=(8, 0), sticky="w", padx=4)

    def _detect_pentaho_vars(self):
        """Lê o .ktr/.kjb e popula variáveis automaticamente."""
        path = self.var_path.get().strip()
        if not path:
            messagebox.showwarning("Detectar Pentaho", "Defina o caminho do arquivo primeiro.", parent=self)
            return
        if not path.lower().endswith((".ktr", ".kjb")):
            messagebox.showwarning("Detectar Pentaho",
                                   "Funciona apenas para .ktr / .kjb.", parent=self)
            return
        found = parse_ktr_variables(path)
        if not found:
            messagebox.showinfo("Detectar Pentaho",
                                "Nenhuma variável detectada.\n"
                                "Use ${VAR} no .ktr/.kjb para que apareça aqui.", parent=self)
            return
        added = 0
        for v in found:
            if v not in self._variables:
                self._variables[v] = ""
                added += 1
        self._lbl_vars.config(text=self._fmt_vars_label())
        messagebox.showinfo("Detectar Pentaho",
                            f"{len(found)} variável(is) detectada(s) ({added} nova(s)).\n"
                            "Clique em 'Editar…' para preencher os valores.", parent=self)

    def _toggle_schedule_ui(self):
     mode = (self.var_schedule.get() or "cron").lower()
     is_interval = (mode == "interval")
     is_sr = (mode == "start_repeat")

     # Botão/label de horários só ficam ativos no modo "cron"
     state = ("normal" if mode == "cron" else "disabled")
     try:
        self.btn_times.configure(state=state)
        self.lbl_times.configure(foreground=("" if mode == "cron" else "#888"))
        self.lbl_time_title.configure(foreground=("" if mode == "cron" else "#888"))
     except Exception:
        pass

     # Mostra/oculta linhas específicas
     if is_interval:
        self.int_row.grid()
     else:
        self.int_row.grid_remove()

     if is_sr:
        self.sr_row.grid()
     else:
        self.sr_row.grid_remove()


    # ---------- Ações ----------
    def on_save(self):
        # validações básicas
        if not self.var_name.get().strip():
            messagebox.showerror("Erro", "Informe o nome da tarefa.")
            return
        if not self.var_path.get().strip():
            messagebox.showerror("Erro", "Escolha o arquivo/comando.")
            return

        mode = (self.var_schedule.get() or "cron").lower()

        # calcula a lista de horários conforme o modo
        if mode == "cron":
            if not self.times:
                messagebox.showerror("Erro", "Adicione pelo menos um horário.")
                return
            times_list = list(self.times)

        elif mode == "interval":
            # intervalo puro não usa lista de horários
            try:
                ev = int(self.var_every_val.get())
                assert ev > 0
            except Exception:
                messagebox.showerror("Erro", "Informe um intervalo válido (>0).")
                return
            times_list = []

        elif mode == "start_repeat":
            # início + repetição
            try:
                start = parse_times(self.var_sr_start.get().strip())[0]
                ev = int(self.var_sr_every_val.get());   assert ev > 0
                cnt = int(self.var_sr_count.get());      assert cnt >= 0
                unit = (self.var_sr_every_unit.get() or "minutes").lower()
            except Exception:
                messagebox.showerror("Erro", "Preencha Início (HH:MM), intervalo (>0) e vezes (>=0).")
                return
            times_list = expand_start_repeat(start, ev, unit, cnt)
        else:
            # fallback seguro
            times_list = ["06:00"]

        # v2025.10.11.8 — backoff list
        try:
            backoff = [int(x.strip()) for x in self.var_retry_backoff.get().split(",") if x.strip()]
        except Exception:
            backoff = [60, 300, 900]
        try:
            max_attempts = int(self.var_retry_max.get())
        except Exception:
            max_attempts = 3
        tags_list = [t.strip() for t in self.var_tags.get().split(",") if t.strip()]

        # monta o payload final
        self.result = {
            "name": self.var_name.get().strip(),
            "path": self.var_path.get().strip(),
            "args": self.var_args.get().strip(),
            "working_dir": self.var_work.get().strip(),

            # horários (primeiro para compatibilidade + lista completa)
            "time": (times_list[0] if times_list else "06:00"),
            "times": times_list,

            "days": [v.get() for v in self.days_vars],
            "timeout": self.var_timeout.get().strip(),
            "notify_fail": self.var_notify_fail.get(),
            "schedule_type": mode,

            # campos do modo "interval"
            "every_value": int(self.var_every_val.get() or 0),
            "every_unit": self.var_every_unit.get(),

            # campos do modo "início + repetição"
            "sr_start": self.var_sr_start.get().strip(),
            "sr_every_value": int(self.var_sr_every_val.get() or 0),
            "sr_every_unit": self.var_sr_every_unit.get(),
            "sr_count": int(self.var_sr_count.get() or 0),

            "spawn": self.var_spawn.get(),

            # v2025.10.11.8 — campos novos
            "tags": tags_list,
            "depends_on": list(self._depends_on),
            "retry": {
                "enabled": self.var_retry_on.get(),
                "max_attempts": max_attempts,
                "backoff_seconds": backoff,
            },
            "respect_maintenance": self.var_resp_mw.get(),
            "dynamic_params": self.var_dyn.get(),
            "variables": dict(self._variables),
        }
        self.destroy()


    def pick_file(self):
        path = filedialog.askopenfilename(title="Escolha o arquivo")
        if path:
            self.var_path.set(path)

    def pick_dir(self):
        d = filedialog.askdirectory(title="Escolha a pasta")
        if d:
            self.var_work.set(d)

    # ---------- Editor de horários ----------
    def edit_times(self):
        """Abre um editor (listbox) para gerenciar os horários."""
        dlg = tk.Toplevel(self)
        dlg.title("Horários")
        dlg.resizable(False, False)
        dlg.grab_set()

        frm = ttk.Frame(dlg, padding=10)
        frm.grid(sticky="nsew")

        lb = tk.Listbox(frm, height=8, width=16, exportselection=False)
        for t in self.times:
            lb.insert("end", t)
        lb.grid(row=0, column=0, rowspan=6, sticky="nsw")

        def _validate_hhmm(s):
            try:
                arr = parse_times(s.strip())
                return len(arr) == 1
            except Exception:
                return False

        def _add():
            s = simpledialog.askstring("Novo horário", "Informe um horário (HH:MM):", parent=dlg)
            if not s:
                return
            if not _validate_hhmm(s):
                messagebox.showerror("Inválido", "Use o formato HH:MM.")
                return
            t = parse_times(s)[0]
            if t in self.times:
                return
            self.times.append(t)
            lb.insert("end", t)

        def _edit():
            i = lb.curselection()
            if not i:
                return
            idx = i[0]
            cur = lb.get(idx)
            s = simpledialog.askstring("Editar horário", "Novo valor (HH:MM):", initialvalue=cur, parent=dlg)
            if not s:
                return
            if not _validate_hhmm(s):
                messagebox.showerror("Inválido", "Use o formato HH:MM.")
                return
            t = parse_times(s)[0]
            if t in self.times and t != cur:
                messagebox.showwarning("Duplicado", "Esse horário já existe.")
                return
            self.times[idx] = t
            lb.delete(idx); lb.insert(idx, t)
            lb.selection_set(idx)

        def _remove():
            i = lb.curselection()
            if not i:
                return
            idx = i[0]
            lb.delete(idx)
            del self.times[idx]

        def _up():
            i = lb.curselection()
            if not i or i[0] == 0:
                return
            idx = i[0]
            self.times[idx-1], self.times[idx] = self.times[idx], self.times[idx-1]
            tmp = lb.get(idx)
            lb.delete(idx); lb.insert(idx-1, tmp)
            lb.selection_set(idx-1)

        def _down():
            i = lb.curselection()
            if not i or i[0] == lb.size()-1:
                return
            idx = i[0]
            self.times[idx+1], self.times[idx] = self.times[idx], self.times[idx+1]
            tmp = lb.get(idx)
            lb.delete(idx); lb.insert(idx+1, tmp)
            lb.selection_set(idx+1)

        btns = ttk.Frame(frm)
        btns.grid(row=0, column=1, padx=(8, 0), sticky="n")
        ttk.Button(btns, text="Adicionar", width=12, command=_add).pack(pady=2, fill="x")
        ttk.Button(btns, text="Editar", width=12, command=_edit).pack(pady=2, fill="x")
        ttk.Button(btns, text="Remover", width=12, command=_remove).pack(pady=2, fill="x")
        ttk.Button(btns, text="↑", width=12, command=_up).pack(pady=2, fill="x")
        ttk.Button(btns, text="↓", width=12, command=_down).pack(pady=2, fill="x")

        def _ok():
            # normaliza e remove duplicados mantendo ordem
            seen, norm = set(), []
            for t in self.times:
                if t not in seen:
                    seen.add(t); norm.append(t)
            self.times = norm
            self.var_times_str.set(self._fmt_times())
            dlg.destroy()

        def _cancel():
            dlg.destroy()

        footer = ttk.Frame(frm)
        footer.grid(row=6, column=0, columnspan=2, pady=(8, 0))
        ttk.Button(footer, text="OK", width=10, command=_ok).pack(side="left", padx=4)
        ttk.Button(footer, text="Cancelar", width=10, command=_cancel).pack(side="left", padx=4)


class AssistantDialog(tk.Toplevel):
    """
    Assistente rápido para sugerir uma nova tarefa.
    Ele monta um dict básico (schedule_type=cron, 06:00, todos os dias)
    que depois é aberto no TaskDialog para ajustes finos.
    """
    def __init__(self, master):
        super().__init__(master)
        self.title("Assistente")
        self.resizable(False, False)
        self.result = None

        self.var_name = tk.StringVar(value="NovaTarefa")
        self.var_path = tk.StringVar(value="")
        self.var_args = tk.StringVar(value="")
        self.var_work = tk.StringVar(value="")

        frm = ttk.Frame(self, padding=10)
        frm.grid(sticky="nsew")
        frm.columnconfigure(1, weight=1)

        row = 0
        ttk.Label(frm, text="Nome:").grid(row=row, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.var_name, width=42)\
            .grid(row=row, column=1, columnspan=2, sticky="we"); row += 1

        ttk.Label(frm, text="Arquivo/Comando:").grid(row=row, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.var_path, width=42)\
            .grid(row=row, column=1, sticky="we")
        ttk.Button(frm, text="Procurar...", command=self._pick_file)\
            .grid(row=row, column=2, sticky="we"); row += 1

        ttk.Label(frm, text="Argumentos (opcional):").grid(row=row, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.var_args, width=42)\
            .grid(row=row, column=1, columnspan=2, sticky="we"); row += 1

        ttk.Label(frm, text="Pasta de trabalho:").grid(row=row, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.var_work, width=42)\
            .grid(row=row, column=1, sticky="we")
        ttk.Button(frm, text="Escolher...", command=self._pick_dir)\
            .grid(row=row, column=2, sticky="we"); row += 1

        btns = ttk.Frame(frm); btns.grid(row=row, column=0, columnspan=3, pady=(10,0))
        ttk.Button(btns, text="Gerar", command=self._on_ok).grid(row=0, column=0, padx=6)
        ttk.Button(btns, text="Cancelar", command=self.destroy).grid(row=0, column=1, padx=6)

        self.grab_set()
        self.wait_visibility()
        self.focus_set()

    # ------- helpers -------
    def _pick_file(self):
        path = filedialog.askopenfilename(title="Escolha o arquivo")
        if not path:
            return
        self.var_path.set(path)
        # sugere nome e pasta
        try:
            p = Path(path)
            if not self.var_work.get().strip():
                self.var_work.set(str(p.parent))
            if not self.var_name.get().strip() or self.var_name.get() == "NovaTarefa":
                self.var_name.set(p.stem)
        except Exception:
            pass

    def _pick_dir(self):
        d = filedialog.askdirectory(title="Escolha a pasta de trabalho")
        if d:
            self.var_work.set(d)

    # ------- ação principal -------
    def _on_ok(self):
        name = self.var_name.get().strip()
        path = self.var_path.get().strip()
        if not name:
            messagebox.showerror("Erro", "Informe o nome da tarefa."); return
        if not path:
            messagebox.showerror("Erro", "Escolha o arquivo/comando."); return

        work = self.var_work.get().strip() or str(Path(path).parent)

        # payload básico para o TaskDialog refinar
        self.result = {
            "name": name,
            "path": path,
            "args": self.var_args.get().strip(),
            "working_dir": work,

            "time": "06:00",
            "times": ["06:00"],
            "days": [True] * 7,

            "timeout": "0",
            "notify_fail": True,
            "schedule_type": "cron",

            "every_value": 30,
            "every_unit": "minutes",

            "sr_start": "06:00",
            "sr_every_value": 5,
            "sr_every_unit": "minutes",
            "sr_count": 5,

            "spawn": True,
        }
        self.destroy()



class HelpPanel(tk.Toplevel):
    """v2025.10.11.9 — Painel de Ajuda lateral com tutoriais passo-a-passo."""

    TOPICS = [
        ("🚀  Início rápido (3 passos)", "intro"),
        ("➕  Criar meu primeiro job", "first_job"),
        ("📅  Tipos de agendamento", "schedule_types"),
        ("✉️  Configurar e-mail (SMTP)", "email"),
        ("🪝  Configurar webhook (Discord/Slack/Teams)", "webhook"),
        ("💬  Configurar WhatsApp", "whatsapp"),
        ("✨  Variáveis dinâmicas (${data_atual} etc.)", "dyn_vars"),
        ("🔗  Dependências entre jobs", "depends"),
        ("🔁  Retry automático em falha", "retry"),
        ("🕓  Janelas de manutenção", "maintenance"),
        ("🏷️  Tags e filtros", "tags"),
        ("📊  Digest diário", "digest"),
        ("🤖  Detectar variáveis Pentaho", "pentaho"),
        ("💾  Backup, restauração e portabilidade", "backup"),
        ("🔌  API REST local", "rest_api"),
        ("⌨️  Atalhos de teclado", "shortcuts"),
        ("🐛  Problemas comuns (troubleshooting)", "troubleshoot"),
    ]

    def __init__(self, master):
        super().__init__(master)
        self.title("Ajuda — Agendador-Bravo")
        self.geometry("980x660")
        self.minsize(840, 540)
        try:
            self.transient(master)
        except Exception:
            pass

        # v2025.10.11.15 — Detecta tema do app pai
        self._dark = _is_dark_mode(master)
        self._theme = _bravo_theme(self._dark)
        self._font_title, self._font_body = _resolve_fonts()
        try:
            _avail = set(tkfont.families())
        except Exception:
            _avail = set()
        if "Cascadia Mono" in _avail:
            self._font_code = "Cascadia Mono"
        elif "Consolas" in _avail:
            self._font_code = "Consolas"
        else:
            self._font_code = "Courier New"

        # ícone + fundo geral
        try:
            ico = find_logo_ico()
            if ico:
                self.iconbitmap(default=str(ico))
        except Exception:
            pass
        self.configure(bg=self._theme['bg_app'])

        T = self._theme

        # Header com cor de fundo Bravo
        header = tk.Frame(self, bg=T['bg_surface'])
        header.pack(side="top", fill="x")
        header_inner = tk.Frame(header, bg=T['bg_surface'])
        header_inner.pack(fill="x", padx=18, pady=14)
        tk.Label(header_inner, text="📚  Central de Ajuda",
                 bg=T['bg_surface'], fg=T['accent'],
                 font=(self._font_title, 15, "bold")).pack(side="left")
        tk.Label(header_inner,
                 text=f"v{APP_VERSION}  ·  pressione F1 para reabrir",
                 bg=T['bg_surface'], fg=T['subtext'],
                 font=(self._font_body, 9)).pack(side="right")

        # separador
        tk.Frame(self, bg=T['border'], height=1).pack(side="top", fill="x")

        # Conteúdo principal: paned com sidebar à esquerda + Text à direita
        main = tk.Frame(self, bg=T['bg_app'])
        main.pack(fill="both", expand=True)

        # ── Sidebar (listbox de tópicos) ─────────────────────────────────
        sidebar = tk.Frame(main, bg=T['bg_sidebar'], width=240)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        self.listbox = tk.Listbox(
            sidebar,
            font=(self._font_body, 10),
            bg=T['bg_sidebar'], fg=T['text'],
            selectbackground=T['accent'], selectforeground='#ffffff',
            activestyle='none',
            borderwidth=0, highlightthickness=0,
            relief="flat",
            selectmode="single", exportselection=False,
        )
        for title, _key in self.TOPICS:
            self.listbox.insert("end", f"  {title}")
        self.listbox.pack(fill="both", expand=True, padx=(8, 0), pady=12)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        # separador vertical
        tk.Frame(main, bg=T['border'], width=1).pack(side="left", fill="y")

        # ── Conteúdo (Text widget) ──────────────────────────────────────
        right = tk.Frame(main, bg=T['bg_surface'])
        right.pack(side="left", fill="both", expand=True)

        self._text = tk.Text(
            right, wrap="word",
            padx=24, pady=20,
            font=(self._font_body, 10),
            bg=T['bg_surface'], fg=T['text'],
            insertbackground=T['text'],
            relief="flat", borderwidth=0,
            highlightthickness=0,
        )
        self._text.pack(side="left", fill="both", expand=True)
        tsb = ttk.Scrollbar(right, orient="vertical", command=self._text.yview)
        self._text.configure(yscrollcommand=tsb.set)
        tsb.pack(side="right", fill="y")

        # Tags de estilo — TODAS respeitam o tema agora
        self._text.tag_configure("h1",
                                 font=(self._font_title, 18, "bold"),
                                 foreground=T['text_strong'],
                                 spacing1=4, spacing3=12)
        self._text.tag_configure("h2",
                                 font=(self._font_title, 13, "bold"),
                                 foreground=T['accent'],
                                 spacing1=14, spacing3=8)
        self._text.tag_configure("p",
                                 foreground=T['text'],
                                 spacing3=8, lmargin1=0, lmargin2=0)
        self._text.tag_configure("code",
                                 font=(self._font_code, 9),
                                 background=T['bg_code'],
                                 foreground=T['text_strong'],
                                 lmargin1=16, lmargin2=16, rmargin=16,
                                 spacing1=6, spacing3=6,
                                 borderwidth=0)
        self._text.tag_configure("step",
                                 font=(self._font_body, 10, "bold"),
                                 foreground=T['accent'],
                                 spacing1=12, spacing3=4)
        self._text.tag_configure("tip",
                                 foreground=T['success'],
                                 font=(self._font_body, 10, "italic"))
        self._text.tag_configure("warn",
                                 foreground=T['error'],
                                 font=(self._font_body, 10, "bold"))
        self._text.tag_configure("muted",
                                 foreground=T['subtext'])

        # Footer
        tk.Frame(self, bg=T['border'], height=1).pack(side="top", fill="x")
        footer = tk.Frame(self, bg=T['bg_surface'])
        footer.pack(side="bottom", fill="x")
        footer_inner = tk.Frame(footer, bg=T['bg_surface'])
        footer_inner.pack(fill="x", padx=18, pady=10)
        tk.Label(footer_inner,
                 text="💡  Dica: este painel é não-modal — pode deixar aberto enquanto configura.",
                 bg=T['bg_surface'], fg=T['subtext'],
                 font=(self._font_body, 9)).pack(side="left")
        ttk.Button(footer_inner, text="Fechar",
                   command=self.destroy).pack(side="right")

        # Seleciona o primeiro tópico
        self.listbox.selection_set(0)
        self._render(self.TOPICS[0][1])

    def _on_select(self, _evt=None):
        sel = self.listbox.curselection()
        if not sel:
            return
        self._render(self.TOPICS[sel[0]][1])

    def _set(self, *blocks):
        """Helper: limpa e escreve uma lista de tuplas (texto, tag)."""
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        for text, tag in blocks:
            self._text.insert("end", text, tag)
        self._text.configure(state="disabled")

    def _render(self, key):
        renderer = getattr(self, f"_render_{key}", None)
        if renderer:
            renderer()

    # ── Tópicos ───────────────────────────────────────────────────────
    def _render_intro(self):
        self._set(
            ("Início rápido em 3 passos\n", "h1"),
            ("O Agendador-Bravo automatiza qualquer arquivo executável "
             "(.ktr, .kjb, .py, .bat, .exe, .ps1) em horários definidos. "
             "Veja como começar.\n", "p"),
            ("1️⃣  Configure o caminho do Pentaho\n", "step"),
            ("Abra ⚙️ Configurações → aba Geral e aponte para a pasta do PDI "
             "que contém Pan.bat e Kitchen.bat. Pule este passo se você não "
             "usa Pentaho.\n", "p"),
            ("2️⃣  Crie seu primeiro job\n", "step"),
            ("Clique em 🧙 Assistente (mais rápido) ou ➕ Nova. Escolha o "
             "arquivo, defina os horários e os dias da semana. O job já fica "
             "agendado e rodará automaticamente.\n", "p"),
            ("3️⃣  Configure como ser notificado de falhas\n", "step"),
            ("Em ⚙️ Configurações habilite e-mail OU webhooks "
             "(Discord/Slack/Teams). A cada job que falhar você recebe um aviso "
             "com o log anexado.\n", "p"),
            ("\n💡  Pronto. ", "tip"),
            ("A partir daí, use a tabela principal para acompanhar status, "
             "histórico e o dashboard de 7 dias (mostrado quando nada está "
             "selecionado).\n", "p"),
        )

    def _render_first_job(self):
        self._set(
            ("Criar meu primeiro job\n", "h1"),
            ("Caminho rápido: clique no botão 🧙 Assistente.\n", "p"),
            ("Passo a passo\n", "h2"),
            ("1.  Clique em ➕ Nova (ou Ctrl+N).\n", "p"),
            ("2.  Dê um nome (sem espaços ou caracteres especiais — é usado como id).\n", "p"),
            ("3.  Em Arquivo/Comando aponte para o .ktr/.kjb/.py/.bat/.exe.\n", "p"),
            ("4.  Em Argumentos coloque parâmetros, se precisar (ex.: /param:nome=valor).\n", "p"),
            ("5.  Escolha o tipo de agendamento:\n", "p"),
            ("    • Horário fixo: lista de horários (ex.: 06:00, 12:00, 18:00).\n", "p"),
            ("    • Intervalo: a cada N minutos/horas.\n", "p"),
            ("    • Início + repetição: começa em HH:MM e repete N vezes.\n", "p"),
            ("6.  Marque os dias da semana.\n", "p"),
            ("7.  (Opcional) Expanda Avançado para configurar tags, retry, dependências, variáveis.\n", "p"),
            ("8.  Salvar.\n", "p"),
            ("\n💡  ", "tip"),
            ("Você pode rodar imediatamente sem esperar o horário: selecione o job na tabela e clique ▶ Executar agora.\n", "p"),
        )

    def _render_schedule_types(self):
        self._set(
            ("Tipos de agendamento\n", "h1"),
            ("Horário fixo (cron)\n", "h2"),
            ("Dispara nos horários listados, nos dias marcados.\n", "p"),
            ("Exemplo: times=['06:00','12:00','18:00'], days=[seg-sex] → "
             "6 disparos por dia, 5 dias da semana.\n", "code"),
            ("Intervalo (interval)\n", "h2"),
            ("A cada N minutos ou horas, 24h por dia (filtrando dias da semana).\n", "p"),
            ("Exemplo: every_value=15, every_unit='minutes' → "
             "a cada 15 minutos.\n", "code"),
            ("Início + repetição (start_repeat)\n", "h2"),
            ("Começa num horário e repete N vezes.\n", "p"),
            ("Exemplo: sr_start='14:25', sr_every_value=5, sr_count=10 → "
             "14:25, 14:30, 14:35 ... até 15:15 (10 repetições + a inicial).\n", "code"),
        )

    def _render_email(self):
        self._set(
            ("Configurar notificações por e-mail\n", "h1"),
            ("Em ⚙️ Configurações → aba ✉️ E-mail:\n", "p"),
            ("1.  Marque Ativar notificações por e-mail.\n", "p"),
            ("2.  SMTP host: smtp.gmail.com (Gmail), smtp.office365.com (Outlook), etc.\n", "p"),
            ("3.  SMTP porta: geralmente 587 (TLS).\n", "p"),
            ("4.  Usuário: seu e-mail completo.\n", "p"),
            ("5.  Senha de app: NÃO use sua senha normal. ", "p"),
            ("Para Gmail: myaccount.google.com → Segurança → Senhas de app.\n", "p"),
            ("6.  De (from): geralmente o mesmo do usuário.\n", "p"),
            ("7.  Para (vírgula): lista de destinatários separados por vírgula.\n", "p"),
            ("8.  Clique Testar e-mail para validar.\n", "p"),
            ("\n🔒  ", "tip"),
            ("A senha é criptografada via Windows DPAPI no config.json — "
             "só o seu usuário Windows consegue decifrar.\n", "p"),
        )

    def _render_webhook(self):
        self._set(
            ("Webhooks Discord / Slack / Teams\n", "h1"),
            ("Webhooks são URLs que recebem mensagens via HTTP POST. "
             "Mais estáveis que WhatsApp-web e não dependem de Node.js.\n", "p"),
            ("Discord\n", "h2"),
            ("1.  Editar canal → Integrações → Webhooks → Novo Webhook.\n", "p"),
            ("2.  Copie o URL (começa com discord.com/api/webhooks/...).\n", "p"),
            ("Slack\n", "h2"),
            ("1.  api.slack.com → Apps → Your Apps → Create New App.\n", "p"),
            ("2.  Ative Incoming Webhooks, adicione ao workspace.\n", "p"),
            ("3.  Copie o URL (começa com hooks.slack.com/services/...).\n", "p"),
            ("Microsoft Teams\n", "h2"),
            ("1.  No canal: ⋮ → Connectors → Incoming Webhook → Configure.\n", "p"),
            ("2.  Copie o URL (outlook.office.com/webhook/...).\n", "p"),
            ("Configurar no Agendador\n", "h2"),
            ("Em ⚙️ Configurações → aba 🪝 Webhooks:\n", "p"),
            ("1.  Cole uma URL por linha (pode ter várias misturadas).\n", "p"),
            ("2.  Escolha notificar sucessos, falhas ou ambos.\n", "p"),
            ("3.  Testar webhooks → manda mensagem de teste em todas.\n", "p"),
            ("\n💡  ", "tip"),
            ("O tipo é detectado automaticamente pelo URL — você não precisa escolher.\n", "p"),
        )

    def _render_whatsapp(self):
        self._set(
            ("Configurar WhatsApp (modo QR)\n", "h1"),
            ("Usa whatsapp-web.js via Node.js. Precisa ler o QR Code "
             "uma vez por máquina. Para alta carga, prefira Webhooks.\n", "p"),
            ("Pré-requisitos\n", "h2"),
            ("•  Node.js instalado em C:\\Program Files\\nodejs\\.\n", "p"),
            ("•  Pasta wa/ no aplicativo com wa_send.js.\n", "p"),
            ("Configuração\n", "h2"),
            ("1.  ⚙️ Configurações → aba 💬 WhatsApp.\n", "p"),
            ("2.  Marque Ativar notificações por WhatsApp.\n", "p"),
            ("3.  Confirme caminhos do Node.exe e wa_send.js.\n", "p"),
            ("4.  Destinos (vírgula): números no formato 5511999999999@c.us "
             "ou grupos como group:NomeDoGrupo.\n", "p"),
            ("5.  Testar WhatsApp → abre uma janela com QR. Leia com seu celular.\n", "p"),
        )

    def _render_dyn_vars(self):
        self._set(
            ("Variáveis dinâmicas\n", "h1"),
            ("Use ${VARIAVEL} nos Argumentos do job. O Agendador resolve "
             "no momento da execução. Útil para passar datas em pipelines de ETL.\n", "p"),
            ("Variáveis built-in\n", "h2"),
            ("${data_atual}        →  2026-05-21 (YYYY-MM-DD)\n", "code"),
            ("${hoje}              →  2026-05-21\n", "code"),
            ("${ontem}             →  2026-05-20\n", "code"),
            ("${amanha}            →  2026-05-22\n", "code"),
            ("${ultimo_dia_util}   →  último dia útil (pula sáb/dom)\n", "code"),
            ("${primeiro_dia_mes}  →  2026-05-01\n", "code"),
            ("${ultimo_dia_mes}    →  2026-05-31\n", "code"),
            ("${mes_anterior}      →  2026-04 (YYYY-MM)\n", "code"),
            ("${ano_atual}         →  2026\n", "code"),
            ("${mes_atual}         →  05\n", "code"),
            ("${data_atual_br}     →  21/05/2026\n", "code"),
            ("${timestamp}         →  20260521_153045\n", "code"),
            ("Variáveis customizadas (por job)\n", "h2"),
            ("Em ➕ Nova → Avançado → Variáveis → Editar... defina pares NOME=VALOR. "
             "Use ${NOME} nos argumentos.\n", "p"),
            ("\n💡  ", "tip"),
            ("Para .ktr/.kjb com parâmetros do Pentaho, use 🔍 Detectar Pentaho: "
             "o app lê o XML e cria as variáveis automaticamente.\n", "p"),
        )

    def _render_depends(self):
        self._set(
            ("Dependências entre jobs\n", "h1"),
            ("Faça o job B só rodar se o job A teve sucesso na última execução.\n", "p"),
            ("Como configurar\n", "h2"),
            ("1.  Crie/edite o job dependente (B).\n", "p"),
            ("2.  Expanda Avançado → clique Editar... ao lado de Depende de.\n", "p"),
            ("3.  Marque os jobs A que precisam ter rodado com sucesso.\n", "p"),
            ("4.  Salvar.\n", "p"),
            ("Como funciona\n", "h2"),
            ("No momento em que B for disparado pelo scheduler, ele consulta "
             "o histórico: a última execução de cada A precisa ter RC=0. "
             "Se algum A nunca rodou ou falhou na última, B é pulado e o "
             "evento é registrado em logs/_scheduler.log.\n", "p"),
            ("\n⚠️  ", "warn"),
            ("Cuidado com ciclos: A→B→A não é detectado automaticamente. "
             "Você precisa garantir que o grafo de dependências seja acíclico.\n", "p"),
        )

    def _render_retry(self):
        self._set(
            ("Retry automático em falha\n", "h1"),
            ("Quando um job falha (RC ≠ 0), o Agendador pode reagendar "
             "tentativas com backoff exponencial.\n", "p"),
            ("Como configurar\n", "h2"),
            ("Em Avançado do job:\n", "p"),
            ("•  Marque Retry automático em falha.\n", "p"),
            ("•  Tentativas: número total (incluindo a primeira). 3 = 1 inicial + 2 retries.\n", "p"),
            ("•  Backoff (s, vírgula): segundos entre tentativas. "
             "Ex.: 60,300,900 → espera 1min, depois 5min, depois 15min.\n", "p"),
            ("Exemplo prático\n", "h2"),
            ("Job ETL roda às 06:00, falha por SQL Server estar fora do ar:\n", "p"),
            ("•  06:00 — falha, agenda retry 1 para 06:01.\n", "p"),
            ("•  06:01 — falha, agenda retry 2 para 06:06.\n", "p"),
            ("•  06:06 — falha, agenda retry 3 para 06:21.\n", "p"),
            ("•  06:21 — sucesso ✅\n", "p"),
            ("\n💡  ", "tip"),
            ("Cada retry é registrado em logs/_scheduler.log com timestamp e número da tentativa.\n", "p"),
        )

    def _render_maintenance(self):
        self._set(
            ("Janelas de manutenção (blackout)\n", "h1"),
            ("Períodos em que NENHUM job é disparado. Útil para reboot semanal "
             "do servidor, deploy do ERP, manutenção do banco.\n", "p"),
            ("Configurar\n", "h2"),
            ("⚙️ Configurações → aba 🕓 Janelas → ➕ Adicionar.\n", "p"),
            ("•  Nome: identificador (ex.: \"Manutenção banco\").\n", "p"),
            ("•  Início e Fim: HH:MM. Janelas que atravessam meia-noite são suportadas (22:00→06:00).\n", "p"),
            ("•  Dias: marque os dias da semana que se aplicam.\n", "p"),
            ("Comportamento\n", "h2"),
            ("Quando um job (com flag Respeitar janelas de manutenção ativada) "
             "for disparado dentro de uma janela, ele é pulado silenciosamente "
             "e o evento é gravado em logs/_scheduler.log.\n", "p"),
            ("\n💡  ", "tip"),
            ("Você pode desativar a respeitar por job. Edite o job → "
             "Avançado → desmarque Respeitar janelas de manutenção.\n", "p"),
        )

    def _render_tags(self):
        self._set(
            ("Tags e filtros\n", "h1"),
            ("Organize jobs em categorias e filtre a tabela.\n", "p"),
            ("Criar tags\n", "h2"),
            ("Em ➕ Nova/✏️ Editar → Avançado → Tags (vírgula): "
             "digite separadas por vírgula. Ex.: ETL, Diário, Crítico.\n", "p"),
            ("Filtrar a tabela\n", "h2"),
            ("•  Caixa 🔎 no topo: filtra por nome, tipo, tag ou arquivo em tempo real.\n", "p"),
            ("•  Combobox ao lado: filtra por uma tag específica.\n", "p"),
            ("•  Botão ✕: limpa todos os filtros.\n", "p"),
        )

    def _render_digest(self):
        self._set(
            ("Digest diário\n", "h1"),
            ("Resumo automático das últimas 24h enviado por e-mail e/ou webhook.\n", "p"),
            ("Configurar\n", "h2"),
            ("⚙️ Configurações → aba 📊 Digest.\n", "p"),
            ("•  Habilite e escolha horário (ex.: 08:00).\n", "p"),
            ("•  Dias da semana (padrão: seg-sex).\n", "p"),
            ("•  Conteúdo: sucessos, falhas, top 5 lentos.\n", "p"),
            ("•  Clique Enviar digest agora para testar.\n", "p"),
            ("Conteúdo gerado\n", "h2"),
            ("•  Total de execuções, sucessos, falhas, taxa de sucesso.\n", "p"),
            ("•  Lista detalhada (último horário, RC).\n", "p"),
            ("•  Top 5 jobs mais lentos.\n", "p"),
        )

    def _render_pentaho(self):
        self._set(
            ("Detectar variáveis Pentaho automaticamente\n", "h1"),
            ("Lê o XML do .ktr/.kjb e identifica todas as ${VARIAVEIS} declaradas, "
             "populando os campos automaticamente.\n", "p"),
            ("Como usar\n", "h2"),
            ("1.  Em ✏️ Editar/➕ Nova, defina o caminho do arquivo .ktr/.kjb.\n", "p"),
            ("2.  Em Avançado → 🔍 Detectar Pentaho.\n", "p"),
            ("3.  Uma caixa lista quantas variáveis foram encontradas.\n", "p"),
            ("4.  Clique Editar... ao lado de Variáveis para preencher os valores.\n", "p"),
            ("5.  Salvar.\n", "p"),
            ("Resultado\n", "h2"),
            ("O Pan/Kitchen recebe os valores via parâmetros na linha de comando. "
             "Você pode misturar com variáveis built-in: ${data_atual} no valor de "
             "uma var custom também funciona.\n", "p"),
        )

    def _render_backup(self):
        self._set(
            ("Backup, restauração e portabilidade\n", "h1"),
            ("Três cenários comuns:\n", "p"),
            ("1) Backup completo antes de mudanças grandes\n", "h2"),
            ("💾 Backup na toolbar → gera .zip com config.json + logs + pids. "
             "Guarde num pendrive ou OneDrive.\n", "p"),
            ("Para restaurar: ↩ Restaurar e aponte para o .zip.\n", "p"),
            ("2) Versionar jobs no Git\n", "h2"),
            ("📤 Export YAML → gera um arquivo .yaml legível com apenas os jobs.\n", "p"),
            ("Coloque no Git, ele segue convenções padrão de YAML "
             "(comentários, anchors, etc.).\n", "p"),
            ("📥 Import YAML para aplicar de volta — escolha mesclar ou substituir.\n", "p"),
            ("3) Migrar para outra máquina\n", "h2"),
            ("Opção A — backup completo: gere .zip aqui, restaure lá.\n", "p"),
            ("Opção B — só os jobs: export YAML aqui, import YAML lá. "
             "Configurações (e-mail, webhooks) ficam locais.\n", "p"),
        )

    def _render_rest_api(self):
        self._set(
            ("API REST local\n", "h1"),
            ("Sobe um servidor HTTP no localhost para outros sistemas dispararem jobs.\n", "p"),
            ("Configurar\n", "h2"),
            ("⚙️ Configurações → aba 🔌 API REST → marque Ativar servidor HTTP local. "
             "Reinicie o Agendador para entrar em vigor.\n", "p"),
            ("Endpoints\n", "h2"),
            ("GET  /            →  info do servidor\n", "code"),
            ("GET  /health      →  ok/timestamp\n", "code"),
            ("GET  /jobs        →  lista todos os jobs\n", "code"),
            ("GET  /jobs/{nome} →  detalhes de 1 job\n", "code"),
            ("POST /jobs/{nome}/run  →  dispara o job (assíncrono)\n", "code"),
            ("Autenticação\n", "h2"),
            ("Se você definir um Token, todas as requisições precisam do header:\n", "p"),
            ("X-Auth-Token: seu-token-aqui\n", "code"),
            ("Exemplo de chamada\n", "h2"),
            ("curl -X POST http://127.0.0.1:17654/jobs/RelatorioVendas/run \\\n"
             "     -H \"X-Auth-Token: meu-token\"\n", "code"),
            ("\n🔒  ", "tip"),
            ("Por padrão escuta apenas em 127.0.0.1 (localhost). "
             "Mudar para 0.0.0.0 expõe o serviço na rede — use token forte.\n", "p"),
        )

    def _render_shortcuts(self):
        self._set(
            ("Atalhos de teclado\n", "h1"),
            ("Ctrl+N        →  Novo job\n", "code"),
            ("Ctrl+E        →  Editar job selecionado\n", "code"),
            ("Del           →  Remover job selecionado\n", "code"),
            ("Ctrl+R        →  Executar agora (job selecionado)\n", "code"),
            ("Esc           →  Interromper job em execução\n", "code"),
            ("Ctrl+,        →  Abrir Configurações\n", "code"),
            ("F1            →  Abrir este painel de Ajuda\n", "code"),
            ("F5            →  Atualizar tabela\n", "code"),
            ("Ctrl+Click    →  Multi-seleção (para rodar vários em paralelo)\n", "code"),
        )

    def _render_troubleshoot(self):
        self._set(
            ("Problemas comuns\n", "h1"),
            ("Job não dispara no horário\n", "h2"),
            ("1.  Verifique se o job está ATIVO (coluna Status).\n", "p"),
            ("2.  Verifique se o dia da semana está marcado.\n", "p"),
            ("3.  Verifique se NÃO está em janela de manutenção.\n", "p"),
            ("4.  Veja logs/_scheduler.log — qualquer skip/erro fica lá.\n", "p"),
            ("E-mail de teste falha\n", "h2"),
            ("•  Para Gmail: precisa de senha de app (não a senha do Google).\n", "p"),
            ("•  Para Outlook: pode pedir token OAuth — use Gmail por simplicidade.\n", "p"),
            ("•  Confira firewall corporativo bloqueando porta 587.\n", "p"),
            ("Erro 'python313.dll não encontrado' após atualizar\n", "h2"),
            ("Já corrigido em v2025.10.11.7 via AV pre-warm. "
             "Se acontecer: aguarde 30s e reabra. Se persistir, restaure o "
             "backup e reabra a issue no GitHub.\n", "p"),
            ("API REST não responde\n", "h2"),
            ("1.  Reinicie o Agendador depois de habilitar.\n", "p"),
            ("2.  curl http://127.0.0.1:17654/health — se der refused, outra "
             "instância pode estar usando a porta.\n", "p"),
            ("3.  Mude a porta nas configurações e reinicie.\n", "p"),
            ("Onde achar os logs\n", "h2"),
            ("%PROGRAMDATA%\\AgendadorBravo\\logs\\\n", "code"),
            ("Ou clique 📂 Logs na toolbar.\n", "p"),
        )


class SettingsDialog(tk.Toplevel):
    def __init__(self, master, settings, on_check_updates=None, current_version=APP_VERSION):
        self._on_check_updates = on_check_updates
        self._current_version = current_version

        super().__init__(master)
        self.title("Configurações")
        self.resizable(True, True)
        self.geometry("1180x740")  # v2025.10.11.12 — sidebar à esquerda, content respirado
        self.minsize(960, 620)
        self.result = None

        # v2025.10.11.15 — Sidebar com cores Bravo TI DINÂMICAS (light + dark)
        self._dark = _is_dark_mode(master)
        T = _bravo_theme(self._dark)
        self._theme = T
        try:
            style = ttk.Style(self)
            style.configure("Sidebar.TNotebook",
                            tabposition="wn",
                            background=T['bg_sidebar'],
                            borderwidth=0,
                            padding=0)
            style.configure("Sidebar.TNotebook.Tab",
                            padding=[20, 14],
                            font=("Segoe UI", 10),
                            width=20,
                            background=T['bg_sidebar'],
                            foreground=T['text'],
                            focuscolor="",
                            borderwidth=0)
            style.map("Sidebar.TNotebook.Tab",
                      background=[("selected", T['bg_surface']),
                                  ("active", T['bg_hover']),
                                  ("!selected", T['bg_sidebar'])],
                      foreground=[("selected", T['accent']),
                                  ("active", T['text_strong']),
                                  ("!selected", T['text'])],
                      font=[("selected", ("Segoe UI", 10, "bold")),
                            ("!selected", ("Segoe UI", 10))])
            # LabelFrame com bg dinâmico
            style.configure("TLabelframe",
                            padding=12,
                            background=T['bg_surface'],
                            relief="solid", bordercolor=T['border'], borderwidth=1)
            style.configure("TLabelframe.Label",
                            font=("Segoe UI", 10, "bold"),
                            foreground=T['accent'],
                            background=T['bg_surface'])
            # Frames padrão
            style.configure("TFrame", background=T['bg_surface'])
            style.configure("TLabel", background=T['bg_surface'], foreground=T['text'])
            style.configure("TCheckbutton", background=T['bg_surface'],
                            foreground=T['text'])
            style.configure("TRadiobutton", background=T['bg_surface'],
                            foreground=T['text'])
            # Entry / Combobox (precisam de fieldbackground)
            style.configure("TEntry",
                            fieldbackground=T['bg_input'],
                            foreground=T['text'],
                            insertcolor=T['text'])
            style.configure("TCombobox",
                            fieldbackground=T['bg_input'],
                            foreground=T['text'])
        except Exception:
            pass
        # Janela com bg dinâmico
        self.configure(bg=T['bg_app'])

        # ----- PDI (Pentaho) -----
        self.var_pdi = tk.StringVar(value=settings.get("pdi_home", r"C:\Pentaho\data-integration"))

        # ----- E-mail (SMTP) -----
        email = settings.get("email", {})
        self.var_mail_on = tk.BooleanVar(value=email.get("enabled", False))
        self.var_host    = tk.StringVar(value=email.get("smtp_host", "smtp.gmail.com"))
        self.var_port    = tk.StringVar(value=str(email.get("smtp_port", 587)))
        self.var_user    = tk.StringVar(value=email.get("username", ""))
        # Mostra senha decifrada na UI (DPAPI transparente)
        self.var_pass    = tk.StringVar(value=dpapi_decrypt(email.get("password", "")) if email.get("password") else "")
        self.var_from    = tk.StringVar(value=email.get("from_email", ""))
        self.var_to      = tk.StringVar(value=",".join(email.get("to_emails", [])))

        # ----- WhatsApp (QR via wa_send.js) -----
        wa = settings.get("whatsapp", {})
        self.var_wa_on      = tk.BooleanVar(value=wa.get("enabled", False))
        self.var_node_path  = tk.StringVar(value=wa.get("node_path", r"C:\Program Files\nodejs\node.exe"))
        self.var_script     = tk.StringVar(value=wa.get("webjs_script", str(resource_path("wa","wa_send.js"))))
        self.var_my_number  = tk.StringVar(value=wa.get("my_number", ""))
        self.var_to_targets = tk.StringVar(value=",".join(wa.get("to_targets", [])))

        # ----- Limpeza de logs -----
        cleanup = settings.get("log_cleanup", {})
        self.var_cleanup_enabled = tk.BooleanVar(value=cleanup.get("enabled", True))
        self.var_cleanup_days = tk.StringVar(value=str(cleanup.get("keep_days", 7)))
        self.var_cleanup_day = tk.StringVar(value=str(cleanup.get("schedule_day", 6)))
        self.var_cleanup_time = tk.StringVar(value=cleanup.get("schedule_time", "02:00"))

        # ----- v2025.10.11.8 — Webhooks (Discord/Slack/Teams) -----
        wh = settings.get("webhooks", {}) or {}
        self.var_wh_on = tk.BooleanVar(value=wh.get("enabled", False))
        self.var_wh_urls = tk.StringVar(value="\n".join(wh.get("urls", []) or []))
        self.var_wh_success = tk.BooleanVar(value=wh.get("notify_success", False))
        self.var_wh_failure = tk.BooleanVar(value=wh.get("notify_failure", True))

        # ----- v2025.10.11.8 — Maintenance windows -----
        self._mw_list = list(settings.get("maintenance_windows", []) or [])

        # ----- v2025.10.11.8 — Digest diário -----
        dg = settings.get("digest_email", {}) or {}
        self.var_dg_on = tk.BooleanVar(value=dg.get("enabled", False))
        self.var_dg_time = tk.StringVar(value=dg.get("time", "08:00"))
        dg_days = dg.get("days") or [True]*5 + [False, False]
        self.var_dg_days = [tk.BooleanVar(value=bool(dg_days[i])) for i in range(7)]
        self.var_dg_success = tk.BooleanVar(value=dg.get("include_success", True))
        self.var_dg_failures = tk.BooleanVar(value=dg.get("include_failures", True))
        self.var_dg_top_slow = tk.BooleanVar(value=dg.get("include_top_slow", True))

        # ----- v2025.10.11.8 — REST API -----
        api = settings.get("rest_api", {}) or {}
        self.var_api_on = tk.BooleanVar(value=api.get("enabled", False))
        self.var_api_port = tk.StringVar(value=str(api.get("port", 17654)))
        self.var_api_host = tk.StringVar(value=api.get("bind_host", "127.0.0.1"))
        # token decifrado para UI
        self.var_api_token = tk.StringVar(value=dpapi_decrypt(api.get("token", "")) if api.get("token") else "")

        # ----- v2025.10.11.8 — Rollback -----
        rb = settings.get("rollback", {}) or {}
        self.var_rb_on = tk.BooleanVar(value=rb.get("enabled", True))
        self.var_rb_thr = tk.StringVar(value=str(rb.get("crash_threshold", 3)))

        # ---------- LAYOUT COM ABAS ----------
        main_frame = ttk.Frame(self, padding=10)
        main_frame.grid(sticky="nsew")
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(0, weight=1)

        # Notebook com tabs verticais à esquerda (sidebar)
        notebook = ttk.Notebook(main_frame, style="Sidebar.TNotebook")
        notebook.grid(row=0, column=0, sticky="nsew", pady=(0, 10))

        # === ABA 1: GERAL ===
        tab_geral = ttk.Frame(notebook, padding=24)
        notebook.add(tab_geral, text="  Geral  ")
        self._add_tab_header(tab_geral, "⚙️  Configurações gerais",
                             "Caminhos do Pentaho Data Integration usado para executar os arquivos `.ktr` / `.kjb`.")

        row = 1
        ttk.Label(tab_geral, text="PDI Home (.ktr/.kjb):").grid(row=row, column=0, sticky="w")
        ttk.Entry(tab_geral, textvariable=self.var_pdi, width=40).grid(row=row, column=1, sticky="we", padx=(5, 5))
        ttk.Button(tab_geral, text="...", command=self.pick_pdi, width=3).grid(row=row, column=2); row += 1
        ttk.Label(tab_geral, text="Aponte para a pasta do PDI que contém `Pan.bat` e `Kitchen.bat`.",
                  foreground="#888").grid(row=row, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # === ABA 2: E-MAIL ===
        tab_email = ttk.Frame(notebook, padding=24)
        notebook.add(tab_email, text="  E-mail  ")
        self._add_tab_header(tab_email, "✉️  Notificações por e-mail (SMTP)",
                             "Envia avisos quando um job falha. Para Gmail, gere uma 'Senha de app' em myaccount.google.com → Segurança.")
        
        row = 1
        ttk.Checkbutton(tab_email, text="Ativar notificações por e-mail (SMTP)", variable=self.var_mail_on)\
            .grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8)); row += 1

        for label, var in [
            ("SMTP host", self.var_host),
            ("SMTP porta", self.var_port),
            ("Usuário", self.var_user),
            ("Senha de app", self.var_pass),
            ("De (from)", self.var_from),
            ("Para (vírgula)", self.var_to),
        ]:
            ttk.Label(tab_email, text=label + ":").grid(row=row, column=0, sticky="w")
            ttk.Entry(tab_email, textvariable=var, width=40, show="*" if "Senha" in label else "")\
                .grid(row=row, column=1, columnspan=2, sticky="we", padx=(5, 0)); row += 1

        ttk.Button(tab_email, text="✉️ Testar e-mail", command=self.test_email)\
            .grid(row=row, column=1, sticky="w", pady=(8, 0))

        # === ABA 3: WHATSAPP ===
        tab_wa = ttk.Frame(notebook, padding=24)
        notebook.add(tab_wa, text="  WhatsApp  ")
        self._add_tab_header(tab_wa, "💬  Notificações por WhatsApp (whatsapp-web.js)",
                             "Envia avisos via WhatsApp Web (precisa do Node.js + ler QR Code uma vez). Para uso intenso, prefira Webhooks ou Twilio.")
        
        row = 1
        ttk.Checkbutton(tab_wa, text="Ativar notificações por WhatsApp", variable=self.var_wa_on)\
            .grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8)); row += 1

        ttk.Label(tab_wa, text="Node.exe:").grid(row=row, column=0, sticky="w")
        ttk.Entry(tab_wa, textvariable=self.var_node_path, width=40).grid(row=row, column=1, sticky="we", padx=(5, 5))
        ttk.Button(tab_wa, text="...", command=lambda: self._pick(self.var_node_path, True), width=3)\
            .grid(row=row, column=2); row += 1

        ttk.Label(tab_wa, text="Script wa_send.js:").grid(row=row, column=0, sticky="w")
        ttk.Entry(tab_wa, textvariable=self.var_script, width=40).grid(row=row, column=1, sticky="we", padx=(5, 5))
        ttk.Button(tab_wa, text="...", command=lambda: self._pick(self.var_script, True), width=3)\
            .grid(row=row, column=2); row += 1

        ttk.Label(tab_wa, text="Meu número:").grid(row=row, column=0, sticky="w")
        ttk.Entry(tab_wa, textvariable=self.var_my_number, width=40)\
            .grid(row=row, column=1, columnspan=2, sticky="we", padx=(5, 0)); row += 1

        ttk.Label(tab_wa, text="Destinos (vírgula):").grid(row=row, column=0, sticky="w")
        ttk.Entry(tab_wa, textvariable=self.var_to_targets, width=40)\
            .grid(row=row, column=1, columnspan=2, sticky="we", padx=(5, 0)); row += 1

        ttk.Button(tab_wa, text="Testar WhatsApp", command=self.test_whatsapp_qr)\
            .grid(row=row, column=1, sticky="w", pady=(8, 0))

        # === ABA: WEBHOOKS (v2025.10.11.8) ===
        tab_wh = ttk.Frame(notebook, padding=24)
        notebook.add(tab_wh, text="  Webhooks  ")
        self._add_tab_header(tab_wh, "🪝  Webhooks Discord / Slack / Teams",
                             "Cole o URL do webhook nos canais e o app já detecta o tipo. Crie em: Discord (Editar Canal → Integrações), Slack (Apps → Incoming Webhooks), Teams (Connectors).")
        row = 1
        ttk.Checkbutton(tab_wh, text="Ativar webhooks (Discord/Slack/Teams)", variable=self.var_wh_on)\
            .grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8)); row += 1
        ttk.Label(tab_wh, text="URLs (uma por linha):").grid(row=row, column=0, sticky="nw")
        self._wh_text = tk.Text(tab_wh, height=6, width=58)
        self._wh_text.grid(row=row, column=1, columnspan=2, sticky="we", padx=(5, 0))
        self._wh_text.insert("1.0", self.var_wh_urls.get())
        row += 1
        ttk.Label(tab_wh, text="").grid(row=row, column=0); row += 1
        ttk.Checkbutton(tab_wh, text="Notificar sucessos", variable=self.var_wh_success)\
            .grid(row=row, column=0, columnspan=3, sticky="w"); row += 1
        ttk.Checkbutton(tab_wh, text="Notificar falhas", variable=self.var_wh_failure)\
            .grid(row=row, column=0, columnspan=3, sticky="w"); row += 1
        ttk.Label(tab_wh, text="Tipo de webhook detectado automaticamente pelo URL.",
                  foreground="#888").grid(row=row, column=0, columnspan=3, sticky="w", pady=(8, 0)); row += 1
        ttk.Button(tab_wh, text="Testar webhooks", command=self.test_webhooks)\
            .grid(row=row, column=1, sticky="w", pady=(8, 0))

        # === ABA 4: LIMPEZA DE LOGS ===
        tab_logs = ttk.Frame(notebook, padding=24)
        notebook.add(tab_logs, text="  Logs  ")
        self._add_tab_header(tab_logs, "📂  Limpeza automática de logs",
                             "Os logs ficam em %PROGRAMDATA%\\AgendadorBravo\\logs\\. Em produção com muitos jobs, é fácil acumular GBs. Aqui você define a retenção.")

        row = 1
        ttk.Checkbutton(tab_logs, text="Ativar limpeza automática de logs", variable=self.var_cleanup_enabled)\
            .grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8)); row += 1

        # Linha 1: Dias para manter
        cleanup_frame1 = ttk.Frame(tab_logs)
        cleanup_frame1.grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8)); row += 1
        ttk.Label(cleanup_frame1, text="Manter logs por:").grid(row=0, column=0, sticky="w")
        ttk.Entry(cleanup_frame1, textvariable=self.var_cleanup_days, width=6)\
            .grid(row=0, column=1, padx=(5, 2), sticky="w")
        ttk.Label(cleanup_frame1, text="dias").grid(row=0, column=2, sticky="w")

        # Linha 2: Horário
        cleanup_frame2 = ttk.Frame(tab_logs)
        cleanup_frame2.grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8)); row += 1
        ttk.Label(cleanup_frame2, text="Executar às:").grid(row=0, column=0, sticky="w")
        ttk.Entry(cleanup_frame2, textvariable=self.var_cleanup_time, width=8)\
            .grid(row=0, column=1, padx=(5, 2), sticky="w")

        # Linha 3: Dia da semana
        cleanup_frame3 = ttk.Frame(tab_logs)
        cleanup_frame3.grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8)); row += 1
        ttk.Label(cleanup_frame3, text="Dia da semana:").grid(row=0, column=0, sticky="w")
        day_combo = ttk.Combobox(cleanup_frame3, textvariable=self.var_cleanup_day, width=15, state="readonly")
        day_combo["values"] = ("0 - Segunda", "1 - Terça", "2 - Quarta", "3 - Quinta", "4 - Sexta", "5 - Sábado", "6 - Domingo")
        day_combo.grid(row=0, column=1, padx=(5, 0), sticky="w")

        ttk.Button(tab_logs, text="🧹 Limpar logs agora", command=self.cleanup_logs_now)\
            .grid(row=row, column=0, sticky="w", pady=(8, 0))
        row += 1

        # v2025.10.11.15 — Painel de status com tema
        T = getattr(self, '_theme', _bravo_theme(False))
        status_frame = ttk.LabelFrame(tab_logs, text="📊 Status atual da pasta de logs",
                                       padding=14)
        status_frame.grid(row=row, column=0, columnspan=3, sticky="we", pady=(20, 0))
        status_frame.columnconfigure(1, weight=1)
        self._logs_status_lbl = ttk.Label(status_frame,
                                          text="Calculando…",
                                          font=("Segoe UI", 10),
                                          foreground=T['text'],
                                          justify="left")
        self._logs_status_lbl.grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Button(status_frame, text="🔄 Atualizar",
                   command=self._refresh_logs_status).grid(row=0, column=2, sticky="e")
        # popula na primeira vez
        self.after(150, self._refresh_logs_status)

        # === ABA: JANELAS DE MANUTENÇÃO (v2025.10.11.8) ===
        tab_mw = ttk.Frame(notebook, padding=24)
        notebook.add(tab_mw, text="  Janelas  ")
        self._add_tab_header(tab_mw, "🕓  Janelas de manutenção (blackout)",
                             "Períodos em que NENHUM job dispara. Útil para reboot semanal do servidor, deploy do ERP, backup. Jobs marcados com 'respeitar janelas' são pulados.")
        ttk.Label(tab_mw, text="Janelas configuradas:",
                  font=("Segoe UI", 9, "bold")).grid(row=1, column=0, sticky="w", pady=(0, 6))
        self._mw_listbox = tk.Listbox(tab_mw, height=8, width=72, exportselection=False)
        self._mw_listbox.grid(row=2, column=0, sticky="we")
        self._mw_refresh_list()
        mw_btns = ttk.Frame(tab_mw); mw_btns.grid(row=3, column=0, pady=(8, 0), sticky="w")
        ttk.Button(mw_btns, text="➕ Adicionar", command=self._mw_add).pack(side="left", padx=4)
        ttk.Button(mw_btns, text="✏️ Editar", command=self._mw_edit).pack(side="left", padx=4)
        ttk.Button(mw_btns, text="🗑️ Remover", command=self._mw_remove).pack(side="left", padx=4)
        ttk.Label(tab_mw, text="Ex.: 'Madrugada' 22:00→06:00 todos os dias · 'Deploy ERP' 03:00→05:00 dom.",
                  foreground="#888").grid(row=4, column=0, sticky="w", pady=(8, 0))

        # === ABA: DIGEST DIÁRIO (v2025.10.11.8) ===
        tab_dg = ttk.Frame(notebook, padding=24)
        notebook.add(tab_dg, text="  Digest  ")
        self._add_tab_header(tab_dg, "📊  Digest diário",
                             "Resumo automático no e-mail/webhook todo dia no horário escolhido: nº execuções, sucessos, falhas, taxa, top 5 lentos das últimas 24h.")
        row = 1
        ttk.Checkbutton(tab_dg, text="Enviar digest diário por e-mail",
                        variable=self.var_dg_on).grid(row=row, column=0, columnspan=3, sticky="w"); row += 1
        ttk.Label(tab_dg, text="Horário (HH:MM):").grid(row=row, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(tab_dg, textvariable=self.var_dg_time, width=10)\
            .grid(row=row, column=1, sticky="w", pady=(8, 0)); row += 1
        ttk.Label(tab_dg, text="Dias da semana:").grid(row=row, column=0, sticky="w", pady=(8, 0))
        dg_days_frame = ttk.Frame(tab_dg)
        dg_days_frame.grid(row=row, column=1, columnspan=2, sticky="w", pady=(8, 0))
        for i, lab in enumerate(["Seg","Ter","Qua","Qui","Sex","Sáb","Dom"]):
            ttk.Checkbutton(dg_days_frame, text=lab, variable=self.var_dg_days[i])\
                .pack(side="left", padx=2)
        row += 1
        ttk.Checkbutton(tab_dg, text="Incluir sucessos", variable=self.var_dg_success)\
            .grid(row=row, column=0, columnspan=3, sticky="w", pady=(8, 0)); row += 1
        ttk.Checkbutton(tab_dg, text="Incluir falhas", variable=self.var_dg_failures)\
            .grid(row=row, column=0, columnspan=3, sticky="w"); row += 1
        ttk.Checkbutton(tab_dg, text="Incluir top 5 mais lentos", variable=self.var_dg_top_slow)\
            .grid(row=row, column=0, columnspan=3, sticky="w"); row += 1
        ttk.Button(tab_dg, text="Enviar digest agora (teste)",
                   command=self._test_digest).grid(row=row, column=1, sticky="w", pady=(8, 0))

        # === ABA: API REST (v2025.10.11.8) ===
        tab_api = ttk.Frame(notebook, padding=24)
        notebook.add(tab_api, text="  API REST  ")
        self._add_tab_header(tab_api, "🔌  API REST local (HTTP)",
                             "Sobe um servidor leve no localhost para outros sistemas dispararem jobs via HTTP. Útil para integrar com webhooks de ERP/CRM. Token protege contra acesso indevido.")
        row = 1
        ttk.Checkbutton(tab_api, text="Ativar servidor HTTP local",
                        variable=self.var_api_on).grid(row=row, column=0, columnspan=3, sticky="w"); row += 1
        ttk.Label(tab_api, text="Host:").grid(row=row, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(tab_api, textvariable=self.var_api_host, width=18)\
            .grid(row=row, column=1, sticky="w", pady=(8, 0)); row += 1
        ttk.Label(tab_api, text="Porta:").grid(row=row, column=0, sticky="w")
        ttk.Entry(tab_api, textvariable=self.var_api_port, width=10)\
            .grid(row=row, column=1, sticky="w"); row += 1
        ttk.Label(tab_api, text="Token (opcional):").grid(row=row, column=0, sticky="w")
        ttk.Entry(tab_api, textvariable=self.var_api_token, width=42, show="*")\
            .grid(row=row, column=1, columnspan=2, sticky="we"); row += 1
        ttk.Label(tab_api,
                  text=("Endpoints: GET /, GET /health, GET /jobs, GET /jobs/{nome},\n"
                        "POST /jobs/{nome}/run\n"
                        "Token é enviado no header X-Auth-Token. Mudar requer reiniciar o app."),
                  foreground="#888").grid(row=row, column=0, columnspan=3, sticky="w", pady=(8, 0))

        # === ABA 5: SISTEMA ===
        tab_sistema = ttk.Frame(notebook, padding=24)
        notebook.add(tab_sistema, text="  Sistema  ")
        self._add_tab_header(tab_sistema, "🛡️  Sistema e segurança",
                             "Atualizações, autostart com Windows, backup, rollback automático e info sobre criptografia DPAPI.")

        row = 1
        # Atualizações
        up_frame = ttk.LabelFrame(tab_sistema, text="Atualizações", padding=(8, 8))
        up_frame.grid(row=row, column=0, columnspan=3, sticky="we", pady=(0, 10)); row += 1
        ttk.Label(up_frame, text=f"Versão instalada: v{self._current_version}").grid(row=0, column=0, sticky="w")
        ttk.Button(up_frame, text="Verificar atualização", command=self._check_updates)\
            .grid(row=0, column=1, padx=(10, 0), sticky="e")
        up_frame.columnconfigure(0, weight=1)

        # Inicializar com o Windows
        self.var_autostart = tk.BooleanVar(value=is_autostart_enabled())
        startup_frame = ttk.LabelFrame(tab_sistema, text="Inicialização", padding=(8, 8))
        startup_frame.grid(row=row, column=0, columnspan=3, sticky="we", pady=(0, 10)); row += 1
        ttk.Checkbutton(
            startup_frame,
            text="Iniciar com o Windows",
            variable=self.var_autostart,
            command=self._toggle_autostart,
        ).grid(row=0, column=0, sticky="w")
        startup_frame.columnconfigure(0, weight=1)

        # Backup e migração
        backup_frame = ttk.LabelFrame(tab_sistema, text="Backup e migração", padding=(8, 8))
        backup_frame.grid(row=row, column=0, columnspan=3, sticky="we"); row += 1
        ttk.Button(backup_frame, text="Exportar configurações", command=self.export_all)\
            .grid(row=0, column=0, padx=(0, 8), pady=2, sticky="w")
        ttk.Button(backup_frame, text="Importar configurações", command=self.import_all)\
            .grid(row=0, column=1, pady=2, sticky="w")

        # v2025.10.11.8 — Rollback automático
        rb_frame = ttk.LabelFrame(tab_sistema, text="Rollback automático", padding=(8, 8))
        rb_frame.grid(row=row, column=0, columnspan=3, sticky="we", pady=(10, 0)); row += 1
        ttk.Checkbutton(rb_frame, text="Voltar à versão anterior se travar N vezes seguidas no startup",
                        variable=self.var_rb_on).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(rb_frame, text="Threshold:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(rb_frame, textvariable=self.var_rb_thr, width=6)\
            .grid(row=1, column=1, sticky="w", pady=(6, 0))
        ttk.Label(rb_frame, text="crashes consecutivos").grid(row=1, column=2, sticky="w", pady=(6, 0))

        # v2025.10.11.8 — Segurança DPAPI
        sec_frame = ttk.LabelFrame(tab_sistema, text="Segurança", padding=(8, 8))
        sec_frame.grid(row=row, column=0, columnspan=3, sticky="we", pady=(10, 0)); row += 1
        ttk.Label(sec_frame,
                  text=("Senhas (SMTP, Twilio, API token) ficam criptografadas\n"
                        "via Windows DPAPI no config.json. Migração ocorre automaticamente\n"
                        "no primeiro uso. Só este usuário do Windows decifra."),
                  foreground="#666").grid(row=0, column=0, sticky="w")

        # Configurar expansão das colunas nas abas
        for tab in [tab_geral, tab_email, tab_wa, tab_wh, tab_logs, tab_mw, tab_dg, tab_api, tab_sistema]:
            tab.columnconfigure(1, weight=1)

        # Botões principais
        btns = ttk.Frame(main_frame)
        btns.grid(row=1, column=0, pady=(10, 0))
        ttk.Button(btns, text="Salvar", command=self.on_save).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(btns, text="Cancelar", command=self.destroy).grid(row=0, column=1)

        # Configurar responsividade da janela principal
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.grab_set(); self.wait_visibility()

    # ===== Ações / helpers =====
    def _check_updates(self):
        if self._on_check_updates:
            self._on_check_updates()
        else:
            messagebox.showinfo("Atualizações", "Função de verificação não disponível.")

    def _toggle_autostart(self):
        """Habilita/desabilita a inicialização automática com o Windows."""
        enable = bool(self.var_autostart.get())
        ok, msg = set_autostart(enable)
        if not ok:
            messagebox.showwarning("Inicialização", msg, parent=self)
            # Reverte o checkbox se falhou
            self.var_autostart.set(is_autostart_enabled())

    def pick_pdi(self):
        d = filedialog.askdirectory(title="Selecione a pasta do Pentaho (data-integration)")
        if d:
            self.var_pdi.set(d)

        # ===== Backup / Migração =====
    def export_all(self):
        """Exporta configurações + tarefas para um JSON."""
        try:
            # Garante que a prévia atual da tela seja usada
            settings_preview = self.get_result_preview()
            app = self.master  # App()
            payload = {
                "app_name": APP_NAME,
                "version": APP_VERSION,
                "exported_at": now_str(),
                "settings": settings_preview,
                "tasks": list(app.data.get("tasks", [])),
            }

            fname = filedialog.asksaveasfilename(
                title="Salvar backup",
                defaultextension=".json",
                initialfile=f"AgendadorBravo-backup-{datetime.now():%Y%m%d}.json",
                filetypes=[("JSON", "*.json")]
            )
            if not fname:
                return
            Path(fname).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            messagebox.showinfo("Backup", "Backup exportado com sucesso!")
        except Exception as e:
            messagebox.showerror("Backup", f"Falha ao exportar:\n{e}")

    def import_all(self):
        """Importa configurações + tarefas de um JSON (sobrescreve as atuais)."""
        try:
            fname = filedialog.askopenfilename(
                title="Abrir backup",
                filetypes=[("JSON", "*.json")]
            )
            if not fname:
                return
            data = json.loads(Path(fname).read_text(encoding="utf-8"))

            # Aceita tanto payload completo quanto parcial
            new_settings = data.get("settings", {})
            new_tasks = data.get("tasks", [])

            if not new_settings and not new_tasks:
                messagebox.showwarning("Importar", "Arquivo não contém 'settings' ou 'tasks'.")
                return

            if not messagebox.askyesno(
                "Confirmar importação",
                "Isto irá sobrescrever as configurações e/ou tarefas atuais. Deseja continuar?"
            ):
                return

            app = self.master  # App()

            # Aplica SETTINGS (e reflete nos campos da tela)
            if new_settings:
                app.data["settings"] = new_settings
                self._apply_settings_to_vars(new_settings)

            # Aplica TASKS (reescalona)
            if new_tasks:
                app.data["tasks"] = new_tasks

            app.save(silent=True)
            app.refresh_table()
            app.reschedule_all()
            app.update_status_indicators()

            messagebox.showinfo("Importar", "Importação concluída!")
        except Exception as e:
            messagebox.showerror("Importar", f"Falha ao importar:\n{e}")

    def _apply_settings_to_vars(self, s):
        """Atualiza os campos da UI a partir do dicionário de settings."""
        # PDI
        self.var_pdi.set(s.get("pdi_home", r"C:\Pentaho\data-integration"))

        # E-mail
        em = s.get("email", {})
        self.var_mail_on.set(bool(em.get("enabled", False)))
        self.var_host.set(em.get("smtp_host", "smtp.gmail.com"))
        self.var_port.set(str(em.get("smtp_port", 587)))
        self.var_user.set(em.get("username", ""))
        self.var_pass.set(em.get("password", ""))
        self.var_from.set(em.get("from_email", ""))
        self.var_to.set(",".join(em.get("to_emails", [])))

        # WhatsApp
        wa = s.get("whatsapp", {})
        self.var_wa_on.set(bool(wa.get("enabled", False)))
        self.var_node_path.set(wa.get("node_path", r"C:\Program Files\nodejs\node.exe"))
        self.var_script.set(wa.get("webjs_script", str(resource_path("wa", "wa_send.js"))))
        self.var_my_number.set(wa.get("my_number", ""))
        self.var_to_targets.set(",".join(wa.get("to_targets", [])))

        # Limpeza de logs
        cleanup = s.get("log_cleanup", {})
        self.var_cleanup_enabled.set(bool(cleanup.get("enabled", True)))
        self.var_cleanup_days.set(str(cleanup.get("keep_days", 7)))
        self.var_cleanup_day.set(str(cleanup.get("schedule_day", 6)))
        self.var_cleanup_time.set(cleanup.get("schedule_time", "02:00"))
        

    def _pick(self, var, file=True):
        p = filedialog.askopenfilename(title="Selecionar arquivo") if file \
            else filedialog.askdirectory(title="Selecionar pasta")
        if p:
            var.set(p)

    # ===== Testes =====
    def test_email(self):
        try:
            cfg = self.get_result_preview()["email"]
            send_email({"email": cfg}, "[Agendador-Bravo] Teste de e-mail", f"Teste enviado em {now_str()}.")
            messagebox.showinfo("OK", "E-mail enviado!")
        except Exception as e:
            messagebox.showerror("Falha", f"Não foi possível enviar e-mail:\n{e}")

    def test_whatsapp_qr(self):
        node   = self.var_node_path.get().strip()
        script = self.var_script.get().strip()
        tos    = self.var_to_targets.get().strip()
        msg    = "Teste do Agendador-Bravo (QR)."

        if not (os.path.exists(node) and os.path.exists(script)):
            messagebox.showerror("Falha", "Verifique os caminhos do Node.exe e do wa_send.js.")
            return
        if not tos:
            messagebox.showerror("Falha", "Preencha ao menos um destino (ex.: group:Meu Grupo).")
            return

        try:
            ensure_dirs()
            si = subprocess.STARTUPINFO(); si.dwFlags |= subprocess.STARTF_USESHOWWINDOW; si.wShowWindow = 0
            creationflags = 0x08000000  # CREATE_NO_WINDOW
            subprocess.Popen([node, script, "--to", tos, "--message", msg],
                 creationflags=creationflags, startupinfo=si, cwd=str(WA_DIR))
            
            messagebox.showinfo(
                "WhatsApp",
                "Janela aberta.\nSe for a primeira vez, leia o QR Code com o WhatsApp do número emissor."
            )
        except Exception as e:
            messagebox.showerror("Falha", f"Não foi possível abrir o teste do WhatsApp:\n{e}")

    # v2025.10.11.15 — header dinâmico (light/dark) -------------------------
    def _add_tab_header(self, tab, title, description):
        """Cria header com título grande em azul Bravo + descrição em cinza."""
        T = getattr(self, '_theme', _bravo_theme(False))
        header = ttk.Frame(tab)
        header.grid(row=0, column=0, columnspan=4, sticky="we", pady=(0, 16))
        header.columnconfigure(0, weight=1)
        # Título em azul Bravo, grande
        ttk.Label(header, text=title,
                  font=("Segoe UI", 13, "bold"),
                  foreground=T['accent']).grid(row=0, column=0, sticky="w")
        # Descrição em subtext
        ttk.Label(header, text=description,
                  font=("Segoe UI", 9), foreground=T['subtext'],
                  wraplength=900, justify="left")\
            .grid(row=1, column=0, sticky="we", pady=(4, 0))
        # Linha divisória
        sep = tk.Frame(header, bg=T['border'], height=1)
        sep.grid(row=2, column=0, sticky="we", pady=(10, 0))

    # v2025.10.11.8 — helpers das novas abas ---------------------------------
    def test_webhooks(self):
        try:
            wh_text = self._wh_text.get("1.0", "end").strip()
            urls = [u.strip() for u in re.split(r"[,\n]+", wh_text or "") if u.strip()]
            if not urls:
                messagebox.showwarning("Webhooks", "Adicione pelo menos 1 URL.")
                return
            test_settings = {"webhooks": {
                "enabled": True, "urls": urls,
                "notify_success": True, "notify_failure": True,
            }}
            send_webhook(test_settings,
                         "[Agendador-Bravo] Teste de webhook",
                         f"Teste enviado em {now_str()}.\nFuncionando corretamente.",
                         ok=True)
            messagebox.showinfo("Webhooks", "Teste enviado com sucesso!")
        except Exception as e:
            messagebox.showerror("Webhooks", f"Falha:\n{e}")

    def _mw_refresh_list(self):
        try:
            self._mw_listbox.delete(0, "end")
            for w in self._mw_list:
                days = w.get("days") or [True]*7
                day_lbls = "".join("STQQSSD"[i] if days[i] else "·" for i in range(7))
                self._mw_listbox.insert(
                    "end",
                    f"{w.get('name','sem-nome')}  {w.get('start','--:--')}→{w.get('end','--:--')}  [{day_lbls}]")
        except Exception:
            pass

    def _mw_edit_dialog(self, initial=None):
        dlg = tk.Toplevel(self)
        dlg.title("Janela de manutenção")
        dlg.resizable(False, False)
        dlg.grab_set()
        frm = ttk.Frame(dlg, padding=10); frm.grid(sticky="nsew")
        vname = tk.StringVar(value=(initial or {}).get("name", "Madrugada"))
        vstart = tk.StringVar(value=(initial or {}).get("start", "22:00"))
        vend = tk.StringVar(value=(initial or {}).get("end", "06:00"))
        days_init = (initial or {}).get("days", [True]*7)
        vdays = [tk.BooleanVar(value=bool(days_init[i])) for i in range(7)]
        ttk.Label(frm, text="Nome:").grid(row=0, column=0, sticky="w")
        ttk.Entry(frm, textvariable=vname, width=20).grid(row=0, column=1, columnspan=2, sticky="we")
        ttk.Label(frm, text="Início (HH:MM):").grid(row=1, column=0, sticky="w")
        ttk.Entry(frm, textvariable=vstart, width=10).grid(row=1, column=1, sticky="w")
        ttk.Label(frm, text="Fim (HH:MM):").grid(row=2, column=0, sticky="w")
        ttk.Entry(frm, textvariable=vend, width=10).grid(row=2, column=1, sticky="w")
        ttk.Label(frm, text="Dias:").grid(row=3, column=0, sticky="w", pady=(6, 0))
        df = ttk.Frame(frm); df.grid(row=3, column=1, columnspan=2, sticky="w", pady=(6, 0))
        for i, lab in enumerate(["Seg","Ter","Qua","Qui","Sex","Sáb","Dom"]):
            ttk.Checkbutton(df, text=lab, variable=vdays[i]).pack(side="left", padx=2)
        result = {"ok": False}
        def _save():
            try:
                a = _hhmm_to_minutes(vstart.get())
                b = _hhmm_to_minutes(vend.get())
                if a < 0 or b < 0:
                    messagebox.showerror("Inválido", "Horários inválidos (use HH:MM).", parent=dlg)
                    return
            except Exception:
                return
            result["ok"] = True
            result["data"] = {
                "name": vname.get().strip() or "Janela",
                "start": vstart.get().strip(),
                "end": vend.get().strip(),
                "days": [v.get() for v in vdays],
            }
            dlg.destroy()
        btns = ttk.Frame(frm); btns.grid(row=4, column=0, columnspan=3, pady=(8, 0))
        ttk.Button(btns, text="OK", command=_save).pack(side="left", padx=6)
        ttk.Button(btns, text="Cancelar", command=dlg.destroy).pack(side="left", padx=6)
        self.wait_window(dlg)
        return result.get("data") if result.get("ok") else None

    def _mw_add(self):
        new = self._mw_edit_dialog()
        if new:
            self._mw_list.append(new)
            self._mw_refresh_list()

    def _mw_edit(self):
        sel = self._mw_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        edited = self._mw_edit_dialog(initial=self._mw_list[idx])
        if edited:
            self._mw_list[idx] = edited
            self._mw_refresh_list()

    def _mw_remove(self):
        sel = self._mw_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        del self._mw_list[idx]
        self._mw_refresh_list()

    def _test_digest(self):
        try:
            app = self.master
            if hasattr(app, "_send_digest_email"):
                threading.Thread(target=app._send_digest_email, daemon=True).start()
                messagebox.showinfo("Digest", "Digest enviado em segundo plano (verifique a caixa de entrada).")
            else:
                messagebox.showwarning("Digest", "App não disponível.")
        except Exception as e:
            messagebox.showerror("Digest", f"Falha:\n{e}")

    def cleanup_logs_now(self):
        """v2025.10.11.12 — Limpeza com preview + confirmação + feedback real."""
        try:
            try:
                keep_days = int(self.var_cleanup_days.get() or 7)
            except Exception:
                keep_days = 7

            if not LOG_DIR.exists():
                messagebox.showinfo("Limpeza de logs",
                                    "A pasta de logs ainda não existe.\nNada para limpar.",
                                    parent=self)
                return

            # Preview: o que será deletado
            from datetime import timedelta as _td
            cutoff = datetime.now() - _td(days=keep_days)
            all_logs = list(LOG_DIR.glob("*.log"))
            to_remove = [p for p in all_logs
                         if datetime.fromtimestamp(p.stat().st_mtime) < cutoff]

            if not to_remove:
                messagebox.showinfo("Limpeza de logs",
                                    f"Nenhum log mais velho que {keep_days} dias encontrado.\n"
                                    f"Total de arquivos na pasta: {len(all_logs)}",
                                    parent=self)
                self._refresh_logs_status()
                return

            total_bytes = sum(p.stat().st_size for p in to_remove)
            mb = total_bytes / (1024 * 1024)

            if not messagebox.askyesno(
                "Confirmar limpeza",
                f"Encontrei {len(to_remove)} arquivos antigos (>{keep_days} dias).\n"
                f"Total: {mb:.2f} MB.\n\n"
                f"Deseja remover?",
                parent=self):
                return

            # Executa de fato — usamos os arquivos do preview
            actually_removed = 0
            errors = []
            for p in to_remove:
                try:
                    p.unlink()
                    actually_removed += 1
                except Exception as e:
                    errors.append(f"{p.name}: {e}")
            log_count_after = len(list(LOG_DIR.glob("*.log")))
            msg = (f"{actually_removed} arquivo(s) removido(s).\n"
                   f"Espaço liberado: ~{mb:.2f} MB.\n"
                   f"Restam: {log_count_after} arquivos.")
            if errors:
                msg += f"\n\n⚠️ {len(errors)} erro(s):\n" + "\n".join(errors[:5])
                if len(errors) > 5:
                    msg += f"\n... e mais {len(errors)-5}"
            messagebox.showinfo("Limpeza concluída", msg, parent=self)
            self._refresh_logs_status()
        except Exception as e:
            messagebox.showerror("Erro", f"Erro ao executar limpeza de logs:\n{e}",
                                 parent=self)

    def _refresh_logs_status(self):
        """v2025.10.11.12 — Mostra estado atual da pasta de logs."""
        try:
            if not LOG_DIR.exists():
                self._logs_status_lbl.config(text="A pasta de logs ainda não foi criada.")
                return
            files = list(LOG_DIR.glob("*.log"))
            n = len(files)
            if n == 0:
                self._logs_status_lbl.config(text="Pasta vazia. Nenhum log gerado ainda.")
                return
            total_bytes = sum(p.stat().st_size for p in files)
            mb = total_bytes / (1024 * 1024)
            # mais antigo
            oldest = min(files, key=lambda p: p.stat().st_mtime)
            oldest_dt = datetime.fromtimestamp(oldest.stat().st_mtime)
            age_days = (datetime.now() - oldest_dt).days

            try:
                keep_days = int(self.var_cleanup_days.get() or 7)
            except Exception:
                keep_days = 7
            from datetime import timedelta as _td
            cutoff = datetime.now() - _td(days=keep_days)
            to_remove = [p for p in files if datetime.fromtimestamp(p.stat().st_mtime) < cutoff]
            to_remove_mb = sum(p.stat().st_size for p in to_remove) / (1024 * 1024)

            # Próxima limpeza agendada
            try:
                day_name = ["Segunda","Terça","Quarta","Quinta","Sexta","Sábado","Domingo"][
                    int(self.var_cleanup_day.get().split(" ")[0]) % 7]
            except Exception:
                day_name = "Domingo"
            sched_time = self.var_cleanup_time.get() or "02:00"

            text = (
                f"📁 Pasta: {LOG_DIR}\n"
                f"📄 Arquivos: {n}    💾 Tamanho total: {mb:.2f} MB\n"
                f"🕐 Mais antigo: {oldest_dt.strftime('%d/%m/%Y %H:%M')} ({age_days} dias atrás)\n\n"
                f"🧹 Próxima limpeza automática: {day_name} às {sched_time}\n"
                f"   → {len(to_remove)} arquivo(s) com >{keep_days}d ({to_remove_mb:.2f} MB) serão removidos."
            )
            self._logs_status_lbl.config(text=text)
        except Exception as e:
            self._logs_status_lbl.config(text=f"Erro ao calcular status: {e}")

    # ===== Coleta / salvar =====
    def get_result_preview(self):
        try:
            port = int(self.var_port.get())
        except Exception:
            port = 587
        try:
            cleanup_days = int(self.var_cleanup_days.get())
        except Exception:
            cleanup_days = 7
        try:
            cleanup_day = int(self.var_cleanup_day.get().split(" ")[0])
        except Exception:
            cleanup_day = 6
        # v2025.10.11.8 — coleta URLs do widget Text de webhooks
        try:
            wh_text = self._wh_text.get("1.0", "end").strip()
        except Exception:
            wh_text = self.var_wh_urls.get()
        wh_urls = [u.strip() for u in re.split(r"[,\n]+", wh_text or "") if u.strip()]

        try:
            rb_thr = int(self.var_rb_thr.get())
        except Exception:
            rb_thr = 3
        try:
            api_port = int(self.var_api_port.get())
        except Exception:
            api_port = 17654

        # Senhas: criptografa no save (DPAPI no Windows; passthrough fora)
        encrypted_pwd = dpapi_encrypt(self.var_pass.get()) if self.var_pass.get() else ""
        encrypted_api_token = dpapi_encrypt(self.var_api_token.get()) if self.var_api_token.get() else ""

        return {
            "pdi_home": self.var_pdi.get().strip(),
            "email": {
                "enabled": self.var_mail_on.get(),
                "smtp_host": self.var_host.get().strip(),
                "smtp_port": port,
                "username": self.var_user.get().strip(),
                "password": encrypted_pwd,
                "from_email": self.var_from.get().strip(),
                "to_emails": [e.strip() for e in self.var_to.get().split(",") if e.strip()],
            },
            "whatsapp": {
                "enabled": self.var_wa_on.get(),
                "mode": "webjs",
                "node_path": self.var_node_path.get().strip(),
                "webjs_script": self.var_script.get().strip(),
                "my_number": self.var_my_number.get().strip(),
                "to_targets": [n.strip() for n in self.var_to_targets.get().split(",") if n.strip()],
            },
            "log_cleanup": {
                "enabled": self.var_cleanup_enabled.get(),
                "keep_days": cleanup_days,
                "schedule_day": cleanup_day,
                "schedule_time": self.var_cleanup_time.get().strip()
            },
            "webhooks": {
                "enabled": self.var_wh_on.get(),
                "urls": wh_urls,
                "notify_success": self.var_wh_success.get(),
                "notify_failure": self.var_wh_failure.get(),
            },
            "maintenance_windows": list(self._mw_list),
            "digest_email": {
                "enabled": self.var_dg_on.get(),
                "time": self.var_dg_time.get().strip() or "08:00",
                "days": [v.get() for v in self.var_dg_days],
                "include_success": self.var_dg_success.get(),
                "include_failures": self.var_dg_failures.get(),
                "include_top_slow": self.var_dg_top_slow.get(),
            },
            "rest_api": {
                "enabled": self.var_api_on.get(),
                "port": api_port,
                "bind_host": self.var_api_host.get().strip() or "127.0.0.1",
                "token": encrypted_api_token,
            },
            "rollback": {
                "enabled": self.var_rb_on.get(),
                "crash_threshold": rb_thr,
            },
            "secrets_encrypted": True,
        }

    def on_save(self):
        self.result = self.get_result_preview()
        self.destroy()

    
    def pick_file(self):
        path = filedialog.askopenfilename(title="Escolha o arquivo")
        if not path: return
        self.var_file.set(path)
        self.var_work.set(str(Path(path).parent))
        self._suggest(path)

    def pick_dir(self):
        d = filedialog.askdirectory(title="Escolha a pasta de trabalho")
        if d: self.var_work.set(d)

    def _suggest(self, path):
        ext = Path(path).suffix.lower()
        if ext == ".js":
            node = r"C:\Program Files\nodejs\node.exe"
            if not Path(node).exists():
                node = "node"
            self.var_cmd.set(node); self.var_args.set(Path(path).name)
            if self.var_name.get() == "NovaTarefa":
                self.var_name.set("NodeJS_" + Path(path).stem)
        elif ext == ".py":
            self.var_cmd.set(sys.executable); self.var_args.set(Path(path).name)
            self.var_name.set("Python_" + Path(path).stem)
        elif ext in (".ktr",".kjb"):
            self.var_cmd.set("[usar arquivo .ktr/.kjb na tela de Tarefa]")
            self.var_args.set("")
            self.var_name.set(("Pentaho_" if ext==".ktr" else "PentahoJob_") + Path(path).stem)
        elif ext in (".bat",".cmd",".exe",".ps1"):
            self.var_cmd.set(path); self.var_args.set(""); self.var_name.set(Path(path).stem)
        else:
            self.var_cmd.set(path); self.var_args.set(""); self.var_name.set(Path(path).stem)

    def on_create(self):
        file_path = self.var_file.get().strip()
        if not file_path:
            messagebox.showerror("Erro","Escolha um arquivo"); return
        work = self.var_work.get().strip() or str(Path(file_path).parent)
        name = self.var_name.get().strip() or Path(file_path).stem
        self.result = {
           "name": name,
           "path": file_path,
           "args": self.var_args.get().strip(),
           "working_dir": work,
           "time": "06:00",
           "days": [True]*7,
           "timeout": "0",
           "notify_fail": True,
           "schedule_type": "cron",
           "every_value": 30,
           "every_unit": "minutes",
           "spawn": True,
        }
        self.destroy()

# ======================================================================================
#  Splash screen (tela de abertura animada)
# ======================================================================================
import math, random as _rnd

class SplashScreen(tk.Toplevel):
    """
    Tela de abertura animada — logo + 'AGENDADOR BRAVO' + partículas de dados
    + barra de progresso. Sai em ~1.5s (ou quando set_done() for chamado).
    """
    W, H = 520, 320

    def __init__(self, master):
        super().__init__(master)
        self.overrideredirect(True)     # sem borda/titulo
        self.attributes("-topmost", True)
        try:
            self.attributes("-alpha", 0.0)
        except Exception:
            pass

        # centraliza
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = (sw - self.W) // 2
        y = (sh - self.H) // 2
        self.geometry(f"{self.W}x{self.H}+{x}+{y}")

        # Paleta
        self.bg_top    = "#0b1221"
        self.bg_bottom = "#141b2e"
        self.accent    = "#3b82f6"
        self.accent2   = "#06b6d4"
        self.text_main = "#f1f5f9"
        self.text_dim  = "#94a3b8"

        self.canvas = tk.Canvas(
            self, width=self.W, height=self.H,
            bg=self.bg_top, highlightthickness=0, bd=0,
        )
        self.canvas.pack(fill="both", expand=True)

        # Fundo em gradiente
        self._draw_gradient()

        # Grade de pontos (estética de dados)
        self._draw_dot_grid()

        # Partículas animadas (nós de dados)
        self._particles = []
        for _ in range(32):
            px = _rnd.randint(0, self.W)
            py = _rnd.randint(0, self.H)
            r  = _rnd.choice([1, 1, 2, 2, 3])
            spd = _rnd.uniform(0.15, 0.55)
            ang = _rnd.uniform(0, math.tau)
            col = _rnd.choice(["#1e3a8a", "#1e40af", "#0e7490", "#0891b2"])
            pid = self.canvas.create_oval(px-r, py-r, px+r, py+r, fill=col, outline="")
            self._particles.append({
                "id": pid, "x": px, "y": py, "r": r,
                "dx": math.cos(ang) * spd, "dy": math.sin(ang) * spd,
                "color": col,
            })
        # Linhas dinâmicas entre partículas próximas
        self._links = []

        # Moldura sutil
        self.canvas.create_rectangle(
            1, 1, self.W-1, self.H-1,
            outline="#1e293b", width=1,
        )
        # Barra de destaque no topo
        self.canvas.create_rectangle(
            0, 0, self.W, 3, fill=self.accent, outline="",
        )

        # Logo
        self._logo_img = None
        ico = find_logo_ico()
        if ico and Image and ImageTk:
            try:
                pil = Image.open(ico).convert("RGBA").resize((84, 84), Image.LANCZOS)
                self._logo_img = ImageTk.PhotoImage(pil)
                self.canvas.create_image(self.W // 2, 95, image=self._logo_img)
            except Exception:
                pass

        # Título
        self.canvas.create_text(
            self.W // 2, 168,
            text="AGENDADOR BRAVO",
            font=("Segoe UI", 22, "bold"),
            fill=self.text_main,
        )
        self.canvas.create_text(
            self.W // 2, 198,
            text="Automação de Dados  •  Pentaho / PDI",
            font=("Segoe UI", 10),
            fill=self.text_dim,
        )

        # Barra de progresso
        bx, by, bw, bh = 80, 240, self.W - 160, 4
        self._bar_bg = self.canvas.create_rectangle(
            bx, by, bx + bw, by + bh,
            fill="#1e293b", outline="",
        )
        self._bar = self.canvas.create_rectangle(
            bx, by, bx, by + bh,
            fill=self.accent, outline="",
        )
        self._bar_box = (bx, by, bw, bh)

        # Status
        self._status_id = self.canvas.create_text(
            self.W // 2, 268,
            text="Inicializando…",
            font=("Segoe UI", 9),
            fill=self.text_dim,
        )

        # Rodapé versão
        self.canvas.create_text(
            self.W // 2, 298,
            text=f"v{APP_VERSION}",
            font=("Segoe UI", 8),
            fill="#475569",
        )

        self._progress_value = 0.0
        self._done = False
        self._alive = True
        self._fade_alpha = 0.0

        self._fade_in()
        self._animate()

    def _draw_gradient(self):
        # Gradiente vertical do bg_top para bg_bottom
        r1, g1, b1 = int(self.bg_top[1:3], 16), int(self.bg_top[3:5], 16), int(self.bg_top[5:7], 16)
        r2, g2, b2 = int(self.bg_bottom[1:3], 16), int(self.bg_bottom[3:5], 16), int(self.bg_bottom[5:7], 16)
        steps = 60
        band = self.H / steps
        for i in range(steps):
            t = i / (steps - 1)
            r = int(r1 + (r2 - r1) * t)
            g = int(g1 + (g2 - g1) * t)
            b = int(b1 + (b2 - b1) * t)
            color = f"#{r:02x}{g:02x}{b:02x}"
            self.canvas.create_rectangle(
                0, int(i * band), self.W, int((i + 1) * band) + 1,
                fill=color, outline="",
            )

    def _draw_dot_grid(self):
        step = 24
        for gy in range(0, self.H, step):
            for gx in range(0, self.W, step):
                self.canvas.create_oval(
                    gx, gy, gx + 1, gy + 1,
                    fill="#1e293b", outline="",
                )

    def _fade_in(self):
        if not self._alive:
            return
        try:
            self._fade_alpha = min(1.0, self._fade_alpha + 0.08)
            self.attributes("-alpha", self._fade_alpha)
            if self._fade_alpha < 1.0:
                self.after(16, self._fade_in)
        except Exception:
            pass

    def _animate(self):
        if not self._alive or not self.winfo_exists():
            return

        # Atualiza partículas
        for p in self._particles:
            p["x"] += p["dx"]
            p["y"] += p["dy"]
            if p["x"] <= 2 or p["x"] >= self.W - 2:
                p["dx"] *= -1
            if p["y"] <= 2 or p["y"] >= self.H - 2:
                p["dy"] *= -1
            r = p["r"]
            try:
                self.canvas.coords(
                    p["id"],
                    p["x"] - r, p["y"] - r,
                    p["x"] + r, p["y"] + r,
                )
            except Exception:
                pass

        # Redesenha as linhas entre partículas próximas
        try:
            for lid in self._links:
                self.canvas.delete(lid)
            self._links.clear()
            pts = self._particles
            n = len(pts)
            for i in range(n):
                for j in range(i + 1, n):
                    dx = pts[i]["x"] - pts[j]["x"]
                    dy = pts[i]["y"] - pts[j]["y"]
                    d2 = dx * dx + dy * dy
                    if d2 < 80 * 80:
                        lid = self.canvas.create_line(
                            pts[i]["x"], pts[i]["y"],
                            pts[j]["x"], pts[j]["y"],
                            fill="#1e3a8a", width=1,
                        )
                        self._links.append(lid)
                        self.canvas.tag_lower(lid)
        except Exception:
            pass

        # Progresso
        target = 1.0 if self._done else 0.92
        if self._progress_value < target:
            self._progress_value = min(target, self._progress_value + 0.02)
        bx, by, bw, bh = self._bar_box
        pw = int(bw * self._progress_value)
        try:
            self.canvas.coords(self._bar, bx, by, bx + pw, by + bh)
        except Exception:
            pass

        self.after(30, self._animate)

    def set_status(self, text: str):
        try:
            if self.winfo_exists():
                self.canvas.itemconfig(self._status_id, text=text)
        except Exception:
            pass

    def set_done(self):
        self._done = True

    def finish(self, on_done=None):
        """Fecha a splash com fade-out."""
        def fade_out(alpha=1.0):
            if not self._alive:
                return
            alpha -= 0.12
            try:
                if alpha <= 0:
                    self._alive = False
                    self.destroy()
                    if on_done:
                        on_done()
                    return
                self.attributes("-alpha", alpha)
                self.after(16, lambda: fade_out(alpha))
            except Exception:
                self._alive = False
                try:
                    self.destroy()
                except Exception:
                    pass
                if on_done:
                    on_done()
        fade_out()


# ======================================================================================
#  Aplicação principal (GUI)

class App(tk.Tk):


    def _style_table(self):
        """Aplica estilo moderno à tabela com cores do tema atual"""
        dark = bool(self.var_dark.get())
        
        # Usa cores do tema se disponível, senão usa cores padrão
        if hasattr(self, '_theme_colors'):
            colors = self._theme_colors
            even = colors['surface']
            odd = colors['bg']
            sel_bg = colors['accent']  # Usa a cor de destaque sem transparência
            text_color = colors['text']
            heading_bg = colors['overlay']
        else:
            # Cores padrão melhoradas
            if dark:
                even = "#2a2a2a"
                odd = "#1e1e1e"
                sel_bg = "#0078d7"  # Azul mais forte para seleção
                text_color = "#ffffff"
                heading_bg = "#3a3a3a"
            else:
                even = "#f8f9fa"
                odd = "#ffffff"
                sel_bg = "#b3d7ff"  # Azul claro para seleção
                text_color = "#212529"
                heading_bg = "#e9ecef"

        style = ttk.Style(self)
        
        # Configuração do estilo do cabeçalho
        style.configure("Treeview.Heading", 
                       background=heading_bg,
                       foreground=text_color,
                       relief="flat",
                       font=("Segoe UI", 9, "bold"),
                       anchor="center")  # Centraliza o texto do cabeçalho
        
        # Configuração da tabela
        style.configure("Treeview",
                       rowheight=28,  # Altura maior para melhor legibilidade
                       background=odd,
                       foreground=text_color,
                       fieldbackground=odd,
                       borderwidth=0,
                       font=("Segoe UI", 9))
        
        # Configuração específica para a coluna Status
        style.configure("Treeview",
                       rowheight=32,  # Altura um pouco maior para melhor visualização
                       font=("Segoe UI", 9),
                       fieldbackground=odd,
                       background=odd,
                       foreground=text_color)
        
        # Estilo para o cabeçalho
        style.configure("Treeview.Heading", 
                      font=('Segoe UI', 9, 'bold'),
                      anchor='center',
                      background=heading_bg,
                      foreground=text_color,
                      relief='flat')
        
        # Estilo para as células
        style.configure("Treeview.Cell", 
                      anchor='center',
                      padding=0)
                      
        # Estilo para a coluna Status
        style.configure('Status.Treeview.Cell',
                      anchor='center',
                      font=('Segoe UI', 12, 'bold'))
        
        # Ajusta o alinhamento das colunas
        style.layout("Treeview", [
            ('Treeview.treearea', {'sticky': 'nswe'})
        ])
        
        # Cores alternadas modernas
        try:
            self.tree.tag_configure("evenrow", 
                                  background=even,
                                  foreground=text_color)
            self.tree.tag_configure("oddrow", 
                                  background=odd,
                                  foreground=text_color)
            
            # Cores para itens desabilitados
            self.tree.tag_configure("disabled", 
                                  foreground=text_color,
                                  background=odd,
                                  font=("Segoe UI", 9))
            
            # Configuração para itens selecionados
            self.tree.tag_configure('selected', 
                                  background=sel_bg, 
                                  foreground=text_color)
                                  
        except Exception as e:
            print(f"Erro ao configurar estilos: {e}")

        # Configuração do tema para seleção
        style.map('Treeview',
                 background=[('selected', sel_bg)],
                 foreground=[('selected', text_color)],
                 fieldbackground=[('!selected', odd if dark else even), ('selected', sel_bg)])
        
        # Ajusta o alinhamento das células
        self.tree.column("#0", width=0, stretch=tk.NO)  # Coluna fantasma
        self.tree.column("Status", anchor="center", width=90, stretch=tk.NO, minwidth=90)
        self.tree.heading("Status", text="Status", anchor="center")
        
        # Configura o estilo das células da coluna Status
        self.tree.tag_configure('status_cell', anchor='center')
        
        # Configuração específica para a coluna Status
        self.tree.column('Status', width=90, anchor='center', stretch=False)
        self.tree.heading('Status', text='Status', anchor='center')
        
        # Aplica o estilo a todas as células da coluna Status
        for item in self.tree.get_children():
            self.tree.set(item, 'Status', self.tree.set(item, 'Status'))
        
        # Ajusta o alinhamento das outras colunas
        for col in ["Nome", "Horário", "Tipo", "Dias", "Tags", "Arquivo"]:
            try:
                self.tree.column(col, anchor="w")
                self.tree.heading(col, text=col, anchor="w")
            except Exception:
                pass

     # ===== Conectividade / Fila de updates =====
    def on_net_status_change(self, online: bool):
        if online == getattr(self, "net_online", None):
            return
        self.net_online = online
        self.update_net_indicator()
        if online:
            self.set_status_line("Conexão restaurada. Processando fila de atualizações…")
            self.process_update_queue()
        else:
            self.set_status_line("Sem internet. Atualizações pausadas.")
        self.data["update_queue"] = self.update_queue
        save_data(self.data)

    def update_net_indicator(self):
        self.lbl_net.config(foreground=self._status_color(bool(self.net_online)))

    def enqueue_update(self, op: str, info: dict | None = None, dedup=True) -> bool:
        if dedup and op == "check" and any(x.get("op") == "check" for x in self.update_queue):
            return False
        self.update_queue.append({"op": op, "info": info or {}, "ts": now_str()})
        if len(self.update_queue) > UPDATE_QUEUE_MAX:
            del self.update_queue[:-UPDATE_QUEUE_MAX]
        self.data["update_queue"] = self.update_queue
        save_data(self.data)
        try:
            self.status_label.config(text=f"Atualizações enfileiradas: {len(self.update_queue)}")
        except Exception:
            pass
        return True

    def process_update_queue(self):
        if self._update_processing or not self.net_online or not self.update_queue:
            return
        self._update_processing = True
        item = self.update_queue.pop(0)
        self.data["update_queue"] = self.update_queue
        save_data(self.data)

        def _do():
            try:
                op = item.get("op")
                if op == "check":
                    has, data = fetch_update_info()
                    if has and self.winfo_exists():
                        self.after(0, lambda d=data: self.on_update_available(d))
                elif op == "apply":
                    ok, msg = apply_update_now(item.get("info") or {})
                    if self.winfo_exists():
                        self.after(0, lambda m=msg: messagebox.showinfo("Atualizações", m))
                time.sleep(8)
            finally:
                if self.winfo_exists():
                    self.after(0, self._process_queue_finish)
        threading.Thread(target=_do, daemon=True).start()

    def _process_queue_finish(self):
        self._update_processing = False
        if self.update_queue and self.net_online:
            self.after(3000, self.process_update_queue)

    # ===== avisos de atualização =====
    def on_update_available(self, info: dict):
        if self._update_info and self._update_info.get("version") == info.get("version"):
            return
        self._update_info = info
        self._lbl_update.config(
            text=f"Atualização disponível: v{info.get('version')} — clique em 'Atualizar agora' para aplicar."
        )
        self._update_banner.grid(row=1, column=0, sticky="ew")

    def apply_update_from_banner(self):
        if not self._update_info:
            messagebox.showinfo("Atualizações", "Nada para aplicar.")
            return
        ok, msg = apply_update_now(self._update_info)
        try:
            messagebox.showinfo("Atualizações", msg)
        except Exception:
            pass

    def check_updates_now(self):
        has, data = fetch_update_info()
        if has:
            self.on_update_available(data)
            messagebox.showinfo("Atualizações", f"Nova versão disponível: v{data.get('version')}")
        else:
            messagebox.showinfo("Atualizações", str(data))


    def __init__(self):
        super().__init__()
        self.title(APP_NAME)

        # Esconde a janela principal enquanto a splash aparece
        self.withdraw()

        # Splash animada
        try:
            self._splash = SplashScreen(self)
            self._splash.update_idletasks()
            self._splash.set_status("Carregando configurações…")
            self._splash.update()
        except Exception as e:
            print(f"[splash] falhou: {e}")
            self._splash = None

        # Configuração responsiva da janela
        self.geometry("1400x800")  # Tamanho inicial maior e mais espaçoso
        self.minsize(1000, 600)    # Tamanho mínimo aumentado
        self.state('zoomed')       # Inicia maximizada no Windows

        # Configurar responsividade da janela principal
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)  # O painel principal (row 3) se expande
        # Rows: 0=header, 1=banner, 2=toolbar, 3=main_content, 4=status

        self.attributes("-alpha", 1.0)
        ensure_dirs()

        self.data = load_data()
        # Deixa explícito (1 instância por job, coalesce)
        # Scheduler dimensionado pra 80+ jobs simultaneos:
        # - ThreadPool de 64 workers (default sao 10 -> causava misfire com muitos jobs)
        # - misfire_grace_time de 600s (default 1s) -> tolera picos de CPU/IO
        # - coalesce True mantem, mas com grace time largo os jobs nao sao mais descartados
        self.scheduler = BackgroundScheduler(
            executors={"default": APSThreadPoolExecutor(max_workers=64)},
            job_defaults={
                "max_instances": 1,
                "coalesce": True,
                "misfire_grace_time": 600,  # 10 min de tolerancia
            },
        )
        # Listener para registrar jobs pulados/falhos no log
        try:
            self.scheduler.add_listener(
                self._on_scheduler_event,
                EVENT_JOB_MISSED | EVENT_JOB_ERROR | EVENT_JOB_MAX_INSTANCES,
            )
        except Exception as _e:
            print(f"[scheduler] listener falhou: {_e}")
        self.jobs = {}
        # Estado de rede / fila de updates
        self.net_online = is_online()
        self._update_processing = False
        self.update_queue = self.data.setdefault("update_queue", [])  # persiste no JSON
        
        # Dicionário para armazenar processos em execução
        self.running_processes = {}
        self.running_lock = threading.Lock()  # Para evitar condições de corrida


        # Estilos melhorados
        style = ttk.Style(self)
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"), padding=6)
        style.configure("Treeview", font=("Segoe UI", 10), rowheight=28)
        style.configure("TButton", padding=(8, 6), font=("Segoe UI", 10))
        style.configure("TLabel", font=("Segoe UI", 10))

        # Cabeçalho (logo + título + tema) - Row 0
        header = ttk.Frame(self, padding=(12, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)  # Espaço entre logo e botão tema

        # Banner de atualização (inicialmente oculto) - Row 1
        self._update_info = None
        self._update_banner = ttk.Frame(self, padding=(12, 8))
        self._update_banner.grid(row=1, column=0, sticky="ew")
        self._update_banner.grid_remove()  # Oculta inicialmente

        self._lbl_update = ttk.Label(self._update_banner, text="", font=("Segoe UI", 10, "bold"))
        self._lbl_update.pack(side="left")

        btns_up = ttk.Frame(self._update_banner)
        btns_up.pack(side="right")
        ttk.Button(btns_up, text="Atualizar agora", command=self.apply_update_from_banner)\
       .pack(side="left", padx=4)
        ttk.Button(btns_up, text="Mais tarde", command=lambda: self._update_banner.grid_remove())\
       .pack(side="left", padx=4)

        # v2025.10.11.15 — Welcome banner com tema dinâmico
        # Bg/fg vem do tema (light/dark); _refresh_welcome_banner_theme()
        # é chamado quando o tema muda.
        self._welcome_banner = tk.Frame(self, padx=18, pady=14)
        self._wb_left = tk.Frame(self._welcome_banner)
        self._wb_left.pack(side="left", fill="x", expand=True)
        self._wb_title = tk.Label(self._wb_left, text="👋  Bem-vindo ao Agendador-Bravo",
                                   font=("Segoe UI", 12, "bold"))
        self._wb_title.pack(anchor="w")
        self._wb_sub = tk.Label(self._wb_left,
                                 text="Comece criando seu primeiro job e configurando notificações.",
                                 font=("Segoe UI", 10))
        self._wb_sub.pack(anchor="w", pady=(3, 0))
        wb_right = tk.Frame(self._welcome_banner)
        wb_right.pack(side="right")
        self._wb_right = wb_right
        ttk.Button(wb_right, text="📚  Começar tutorial",
                   command=self.open_help_panel,
                   style="Accent.TButton").pack(side="left", padx=4)
        ttk.Button(wb_right, text="✕", width=3,
                   command=self._dismiss_welcome).pack(side="left", padx=(8, 0))
        # Inicialmente ocultado; mostrado em _maybe_show_welcome após startup


                # --- Ícone da janela / barra de tarefas + logo no cabeçalho ---
                # --- Ícone da janela / barra de tarefas + logo no cabeçalho (somente .ico) ---
                # --- Ícone da janela / barra de tarefas + logo no cabeçalho (somente .ico) ---
        self._header_img = None
        ico = find_logo_ico()
        if ico:
            try:
                # Ícone da janela / taskbar
                self.iconbitmap(default=str(ico))
            except Exception:
                pass
        # Logo e título (lado esquerdo)
        logo_frame = ttk.Frame(header)
        logo_frame.grid(row=0, column=0, sticky="w")
        
        # Mostra a mesma .ico no cabeçalho (renderizada via Pillow) — apenas visual
        try:
            if Image and ImageTk:
                _pil = Image.open(ico).resize((28, 28), Image.LANCZOS)
                _img = ImageTk.PhotoImage(_pil)
                ttk.Label(logo_frame, image=_img).pack(side="left")
                self._header_img = _img  # evita GC
        except Exception:
            pass
        
        # v2025.10.11.13 — título com tipografia Bravo (Montserrat)
        # A fonte é definida em _apply_theme; aqui usamos o style Title.TLabel
        self._title_label = ttk.Label(logo_frame, text="Agendador-Bravo",
                                      style="Title.TLabel",
                                      font=("Segoe UI", 16, "bold"))
        self._title_label.pack(side="left", padx=10)

        # Controles (lado direito)
        controls_frame = ttk.Frame(header)
        controls_frame.grid(row=0, column=2, sticky="e")
        
        self.var_dark = tk.BooleanVar(value=False)
        theme_button = ttk.Checkbutton(
            controls_frame, text="🌙 Modo escuro",
            variable=self.var_dark,
            command=lambda: self._toggle_theme()
        )
        theme_button.pack(side="right", padx=6)

        # --------- Toolbar responsiva ---------- Row 2
        toolbar_outer = ttk.Frame(self, padding=(10, 6))
        toolbar_outer.grid(row=2, column=0, sticky="ew")
        toolbar_outer.columnconfigure(0, weight=1)
        
        self._toolbar_canvas = tk.Canvas(toolbar_outer, height=50, highlightthickness=0)
        self._toolbar_canvas.grid(row=0, column=0, sticky="ew")
        self._toolbar_scroll = ttk.Scrollbar(toolbar_outer, orient="horizontal",
                                             command=self._toolbar_canvas.xview)
        self._toolbar_scroll.grid(row=1, column=0, sticky="ew")
        self._toolbar_canvas.configure(xscrollcommand=self._toolbar_scroll.set)
        self._toolbar_inner = ttk.Frame(self._toolbar_canvas)
        self._toolbar_canvas.create_window((0, 0), window=self._toolbar_inner, anchor="nw")

        def _sync_toolbar(_=None):
            self._toolbar_canvas.configure(scrollregion=self._toolbar_canvas.bbox("all"))
            self._toolbar_canvas.config(height=self._toolbar_inner.winfo_reqheight())
        self._toolbar_inner.bind("<Configure>", _sync_toolbar)
        self.bind("<Configure>", _sync_toolbar)
        self._toolbar_canvas.bind("<Shift-MouseWheel>",
                                  lambda e: (self._toolbar_canvas.xview_scroll((-1 if e.delta>0 else 1)*3, "units"),
                                             "break"))
        
        bar = self._toolbar_inner

        # v2025.10.11.11 — Toolbar MINIMALISTA
        # Antes: 15 botões enfileirados.
        # Agora: 6 essenciais + menu "⋯ Mais" pro resto.
        def _tip(widget, text):
            ToolTip(widget, text)
            return widget

        # ── Primários (texto + ícone) ──────────────────────────────────────
        b = ttk.Button(bar, text="➕  Nova", command=self.add_task, style="Accent.TButton")
        b.pack(side="left", padx=(0, 6))
        _tip(b, "Criar novo job (Ctrl+N)")

        self.btn_run = ttk.Button(bar, text="▶  Executar",
                                 command=self.run_now, style="Accent.TButton")
        self.btn_run.pack(side="left", padx=(0, 6))
        _tip(self.btn_run, "Roda o job selecionado agora (Ctrl+R).\nCtrl+Click pra rodar vários em paralelo.")

        self.btn_stop = ttk.Button(bar, text="⏹  Parar",
                                 command=self.stop_running_tasks, style="Danger.TButton")
        self.btn_stop.pack(side="left", padx=(0, 6))
        self.btn_stop.config(state="disabled")
        _tip(self.btn_stop, "Interromper job em execução (Esc)")

        # Toggle (estado dinâmico baseado em seleção) — fica visível
        self.btn_toggle = ttk.Button(bar, text="⏸  Pausar",
                                     command=self.toggle_task_status, style="Modern.TButton")
        self.btn_toggle.pack(side="left", padx=(12, 6))
        _tip(self.btn_toggle, "Pausa/retoma o job selecionado")

        # Spacer flexível
        ttk.Frame(bar).pack(side="left", padx=12)

        # ── Secundários (só ícones, à direita) ─────────────────────────────
        b = ttk.Button(bar, text="⚙", command=self.open_settings, width=4)
        b.pack(side="left", padx=2)
        _tip(b, "Configurações (Ctrl+,)\nE-mail, WhatsApp, webhooks, janelas, API REST")

        b = ttk.Button(bar, text="❓", command=self.open_help_panel, width=4)
        b.pack(side="left", padx=2)
        _tip(b, "Central de Ajuda (F1)\nTutoriais passo-a-passo de cada feature")

        # Menu "⋯" com tudo que não é uso diário
        b = ttk.Button(bar, text="⋯  Mais", command=self._show_more_menu, width=10)
        b.pack(side="left", padx=(8, 0))
        _tip(b, "Mais ações: Assistente, Editar, Remover, Logs, Backup, YAML")

        # ── Atalhos de teclado ────────────────────────────────────────────
        # v2025.10.11.16 — Ctrl+Shift+T = Executar tag
        self.bind("<Control-Shift-T>", lambda e: self.run_tag())
        self.bind("<Control-Shift-t>", lambda e: self.run_tag())
        self.bind("<Control-n>", lambda e: self.add_task())
        self.bind("<Control-N>", lambda e: self.add_task())
        self.bind("<Control-e>", lambda e: self.edit_task())
        self.bind("<Control-E>", lambda e: self.edit_task())
        self.bind("<Delete>", lambda e: self.remove_task() if self.tree.selection() else None)
        self.bind("<Control-r>", lambda e: self.run_now())
        self.bind("<Control-R>", lambda e: self.run_now())
        self.bind("<Escape>", lambda e: self.stop_running_tasks() if str(self.btn_stop.cget("state")) == "normal" else None)
        self.bind("<Control-comma>", lambda e: self.open_settings())
        self.bind("<F1>", lambda e: self.open_help_panel())
        self.bind("<F5>", lambda e: self.refresh_table())


        # --------- Layout principal ---------- Row 3 (principal, expansível)
        paned = ttk.Panedwindow(self, orient="horizontal")
        paned.grid(row=3, column=0, sticky="nsew", padx=10, pady=8)

        left = ttk.Frame(paned)
        left.rowconfigure(1, weight=1)   # row 1 = tree (row 0 = search bar)
        left.columnconfigure(0, weight=1)

        # v2025.10.11.17 — Search bar polida com bg do tema (light + dark)
        # Usa tk.Frame ao invés de ttk.Frame pra termos controle total sobre o bg.
        search_frame = tk.Frame(left)
        search_frame.grid(row=0, column=0, sticky="ew", columnspan=2, pady=(0, 8))
        search_frame.columnconfigure(1, weight=1)
        self._search_frame = search_frame  # pra atualizar tema depois

        # Ícone de busca
        self._search_icon_lbl = tk.Label(search_frame, text="🔎",
                                          font=("Segoe UI Emoji", 11))
        self._search_icon_lbl.grid(row=0, column=0, padx=(0, 6))

        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(search_frame, textvariable=self.search_var,
                                       font=("Segoe UI", 10))
        self.search_entry.grid(row=0, column=1, sticky="ew", ipady=4)
        ToolTip(self.search_entry,
                "Filtra por nome, tipo, tag ou arquivo. Tecle texto para filtrar em tempo real.")

        # Combobox de tag
        self.tag_filter_var = tk.StringVar(value="(todas as tags)")
        self.tag_filter = ttk.Combobox(search_frame, textvariable=self.tag_filter_var,
                                       state="readonly", width=22,
                                       font=("Segoe UI", 10))
        self.tag_filter.grid(row=0, column=2, padx=(8, 0), ipady=2)

        # v2025.10.11.17 — Botão Rodar tag usando tk.Button com cores
        # explícitas do tema. Isso garante que respeita light/dark sem
        # interferência do sv_ttk ou do estilo padrão.
        self.btn_run_tag = tk.Button(search_frame,
                                      text="▶  Rodar tag",
                                      command=self.run_tag,
                                      relief="flat", borderwidth=0,
                                      cursor="hand2",
                                      padx=14, pady=6,
                                      font=("Segoe UI", 10, "bold"))
        self.btn_run_tag.grid(row=0, column=3, padx=(8, 0))
        ToolTip(self.btn_run_tag,
                "Executa AGORA todos os jobs marcados com a tag selecionada (Ctrl+Shift+T).\n"
                "Útil para 'modo emergência': dispara N jobs em 1 clique em vez de N.")

        # Botão ✕ também como tk.Button pra harmonizar
        self.btn_clear_filters = tk.Button(search_frame, text="✕",
                                            relief="flat", borderwidth=0,
                                            cursor="hand2",
                                            padx=8, pady=4,
                                            font=("Segoe UI", 10),
                                            command=lambda: (
                                                self.search_var.set(""),
                                                self.tag_filter_var.set("(todas as tags)"),
                                                self.refresh_table()))
        self.btn_clear_filters.grid(row=0, column=4, padx=(6, 0))
        ToolTip(self.btn_clear_filters, "Limpar busca e filtros")
        # liga atualização em tempo real
        self.search_var.trace_add("write", lambda *_: self.refresh_table())
        self.tag_filter.bind("<<ComboboxSelected>>", lambda *_: self.refresh_table())

        # Definindo as colunas da tabela (Tags inserida antes de Arquivo)
        cols = ("Status", "Nome", "Horário", "Tipo", "Dias", "Tags", "Arquivo")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="extended")

        # Configurando os cabeçalhos
        for col_name in cols:
            self.tree.heading(col_name, text=col_name)

        self._style_table()

        # Configuração responsiva das colunas
        col_configs = {
            "Status": {"width": 100, "minwidth": 90, "stretch": False},
            "Nome": {"width": 200, "minwidth": 160, "stretch": True},
            "Horário": {"width": 150, "minwidth": 110, "stretch": False},
            "Tipo": {"width": 100, "minwidth": 90, "stretch": False},
            "Dias": {"width": 170, "minwidth": 140, "stretch": False},
            "Tags": {"width": 160, "minwidth": 90, "stretch": False},
            "Arquivo": {"width": 320, "minwidth": 240, "stretch": True}
        }

        for c in cols:
            self.tree.heading(c, text=c)
            config = col_configs[c]
            self.tree.column(c,
                           width=config["width"],
                           minwidth=config["minwidth"],
                           stretch=config["stretch"])
        vbar = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        hbar = ttk.Scrollbar(left, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        vbar.grid(row=1, column=1, sticky="ns")
        hbar.grid(row=2, column=0, sticky="ew")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Configure>", self._on_tree_resize)
        # v2025.10.11.11 — context menu (right-click)
        self.tree.bind("<Button-3>", self._show_tree_context_menu)
        # v2025.10.11.11 — duplo-clique edita o job
        self.tree.bind("<Double-1>", lambda e: self.edit_task())

        right = ttk.Frame(paned)
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        hist = ttk.LabelFrame(right, text="Histórico (últimas execuções)", padding=6)
        hist.grid(row=0, column=0, sticky="nsew")
        hist.rowconfigure(0, weight=1); hist.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(hist, bg="#ffffff", height=400)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", lambda e: self.draw_chart())

        paned.add(left, weight=3)
        paned.add(right, weight=2)

        # --------- Status bar ---------- Row 4
        status = ttk.Frame(self, relief="groove", padding=(10, 5))
        status.grid(row=4, column=0, sticky="ew")
        status.columnconfigure(0, weight=1)  # Status label se expande
        
        self.status_label = ttk.Label(status, text="Pronto.")
        self.status_label.grid(row=0, column=0, sticky="w")
        
        # Indicadores de status (lado direito)
        indicators_frame = ttk.Frame(status)
        indicators_frame.grid(row=0, column=1, sticky="e")
        
        self.pbar = ttk.Progressbar(indicators_frame, mode="indeterminate", length=160)
        self.pbar.pack(side="right")
        self.lbl_wa = ttk.Label(indicators_frame, text="WhatsApp ●")
        self.lbl_wa.pack(side="right", padx=(0,12))
        self.lbl_mail = ttk.Label(indicators_frame, text="E-mail ●")
        self.lbl_mail.pack(side="right", padx=(0,12))
        self.lbl_net = ttk.Label(indicators_frame, text="Internet ●")
        self.lbl_net.pack(side="right", padx=(0,12))

        # Dados / agendamento
        self._ensure_task_enabled_field()  # Garante que tarefas existentes tenham o campo enabled
        if self._splash:
            try: self._splash.set_status("Aplicando tema…")
            except Exception: pass
        self._apply_theme(False)
        if self._splash:
            try: self._splash.set_status("Preparando tabela…")
            except Exception: pass
        self.refresh_table()
        self.update_status_indicators()
        self.update_net_indicator()
        self.update_toggle_button()  # Inicializa o botão toggle
        self._pulse_status()
        self._monitor_stop_button()  # Inicia monitoramento do botão Interromper
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # Mostra a janela principal após a splash sair
        if self._splash:
            # Sequência de mensagens animadas na splash (~2.9s total)
            def _step(msg, delay, next_step=None):
                def _do():
                    try: self._splash.set_status(msg)
                    except Exception: pass
                    if next_step:
                        self.after(delay, next_step)
                return _do
            after_ready   = _step("Pronto.", 700, self._show_main_after_splash)
            after_jobs    = _step("Agendando tarefas…", 800, after_ready)
            after_load    = _step("Verificando integrações…", 700, after_jobs)
            # Começa a sequência
            self.after(700, after_load)
        else:
            self._show_main_after_splash()

    def _show_main_after_splash(self):
        """Fecha a splash com fade-out e apresenta a janela principal."""
        def _reveal():
            try:
                self.deiconify()
                self.lift()
                self.focus_force()
            except Exception:
                pass
            # Trabalho pesado adiado para DEPOIS da UI aparecer
            self.after(50, self._deferred_startup)

        if self._splash:
            try:
                self._splash.set_done()
                self._splash.finish(on_done=_reveal)
                return
            except Exception:
                pass
        _reveal()

    def _deferred_startup(self):
        """Trabalho pesado executado após a janela aparecer, para abertura mais rápida."""
        # v2025.10.11.8 — UI apareceu = startup OK, zera contador de crash
        try:
            startup_mark_success()
        except Exception:
            pass

        # v2025.10.11.8 — Migração silenciosa de segredos para DPAPI (primeira vez)
        try:
            if migrate_secrets_to_dpapi(self.data):
                save_data(self.data)
                print("[secrets] credenciais migradas para DPAPI")
        except Exception as e:
            print(f"[secrets] migração falhou: {e}")

        # v2025.10.11.8 — Inicia REST API local (se habilitada)
        try:
            self._rest_server = start_rest_api(self)
        except Exception as e:
            print(f"[rest] falha ao iniciar: {e}")
            self._rest_server = None

        # Agenda tarefas (cria scheduler + jobs)
        try:
            self.reschedule_all()
        except Exception as e:
            print(f"[startup] reschedule_all falhou: {e}")

        # Limpeza de processos órfãos em background (psutil é lento)
        def _cleanup_bg():
            try:
                cleanup_orphaned_python_processes()
            except Exception:
                pass
        threading.Thread(target=_cleanup_bg, daemon=True).start()

        # Monitor de rede e checagem de atualizações
        try:
            start_net_monitor(self)
            start_auto_update_thread(self)
        except Exception as e:
            print(f"[startup] monitores falharam: {e}")

        # Prompt de autostart (somente na 1ª vez, e só em modo compilado)
        self.after(400, self._maybe_prompt_autostart)

        # v2025.10.11.9 — Welcome banner se setup incompleto
        self.after(600, self._maybe_show_welcome)
        self.after(700, self._update_empty_state)

    def _maybe_prompt_autostart(self):
        """Se ainda não perguntamos e o app não está no autostart, pergunta ao usuário."""
        try:
            if not _is_frozen():
                return
            settings = self.data.setdefault("settings", {})
            if settings.get("autostart_prompted"):
                return
            if is_autostart_enabled():
                # Já está configurado; apenas marca como perguntado
                settings["autostart_prompted"] = True
                save_data(self.data)
                return

            ans = messagebox.askyesno(
                "Iniciar com o Windows",
                "O Agendador-Bravo ainda não está configurado para iniciar automaticamente com o Windows.\n\n"
                "Deseja que ele seja aberto junto com o Windows?",
                parent=self,
            )
            if ans:
                ok, msg = set_autostart(True)
                if ok:
                    self.set_status_line("Autostart habilitado.")
                else:
                    messagebox.showwarning("Autostart", f"Não foi possível habilitar:\n{msg}", parent=self)
            settings["autostart_prompted"] = True
            save_data(self.data)
        except Exception as e:
            print(f"[autostart] prompt falhou: {e}")

    def _ensure_task_enabled_field(self):
        """Garante que todas as tarefas existentes tenham o campo 'enabled'"""
        for task in self.data.get("tasks", []):
            if "enabled" not in task:
                task["enabled"] = True  # Por padrão, tarefas existentes ficam ativas

    # ===== Tema / animações modernas =====
    def _apply_theme(self, dark: bool):
        """v2025.10.11.15 — Identidade visual MDW Bravo TI unificada.

        Usa _bravo_theme(dark) como fonte única de verdade. Cores ficam
        consistentes entre App, SettingsDialog, HelpPanel e TaskDialog.
        """
        T = _bravo_theme(dark)
        # Aliases para o código legado de baixo
        colors = {
            'bg':          T['bg_app'],
            'surface':     T['bg_surface'],
            'overlay':     T['border'],
            'text':        T['text'],
            'subtext':     T['subtext'],
            'accent':      T['accent'],
            'accent_dark': T['accent_dark'],
            'success':     T['success'],
            'warning':     T['warning'],
            'error':       T['error'],
            'teal':        T['info'],
            'purple':      T['accent_light'],
        }

        # Tipografia oficial Bravo: Montserrat (display) + Roboto (sans).
        # Fallback inteligente baseado no que está instalado.
        try:
            available = set(tkfont.families())
        except Exception:
            available = set()
        if "Montserrat" in available:
            font_title = "Montserrat"
        elif "Segoe UI Variable" in available:
            font_title = "Segoe UI Variable"
        else:
            font_title = "Segoe UI"
        if "Roboto" in available:
            font_body = "Roboto"
        elif "Segoe UI Variable" in available:
            font_body = "Segoe UI Variable"
        else:
            font_body = "Segoe UI"
        self._font_title = font_title
        self._font_body = font_body

        # Tema base
        try:
            if sv_ttk:
                sv_ttk.use_dark_theme() if dark else sv_ttk.use_light_theme()
        except Exception:
            pass

        style = ttk.Style(self)

        # Paleta derivada
        btn_bg = colors['surface']
        btn_fg = colors['text']
        btn_hv = colors['overlay']
        acc_bg = colors['accent']
        acc_fg = '#ffffff'
        acc_hv = colors['accent_dark']
        acc_ac = self._lighten_color(colors['accent_dark'], -0.10)
        danger_bg = colors['error']
        danger_hv = self._lighten_color(colors['error'], -0.15)

        # Botões secundários
        style.configure("Modern.TButton",
                        padding=(12, 8),
                        font=(font_body, 10),
                        borderwidth=1,
                        relief="flat",
                        focuscolor='none',
                        background=btn_bg,
                        foreground=btn_fg,
                        bordercolor=colors['overlay'])
        style.map("Modern.TButton",
                  background=[('active', colors['overlay']), ('pressed', colors['overlay'])],
                  foreground=[('disabled', colors['subtext'])])

        # Botão de destaque (Bravo blue) — "+ Nova", "▶ Executar"
        style.configure("Accent.TButton",
                        padding=(16, 10),
                        font=(font_body, 10, "bold"),
                        borderwidth=0,
                        relief="flat",
                        focuscolor='none',
                        background=acc_bg,
                        foreground=acc_fg)
        style.map("Accent.TButton",
                  background=[('active', acc_hv), ('pressed', acc_ac)],
                  foreground=[('disabled', '#cbd5e1')])

        # Botão Toggle (Pausar)
        style.configure("Toggle.TButton",
                        padding=(12, 8),
                        font=(font_body, 10),
                        borderwidth=1,
                        relief="flat",
                        focuscolor='none',
                        background=btn_bg,
                        foreground=btn_fg)
        style.map("Toggle.TButton",
                  background=[('active', colors['overlay']), ('pressed', colors['overlay'])])

        # Botão de perigo (Parar)
        style.configure("Danger.TButton",
                        padding=(12, 8),
                        font=(font_body, 10, "bold"),
                        borderwidth=0,
                        relief="flat",
                        focuscolor='none',
                        background=danger_bg,
                        foreground='#ffffff')
        style.map("Danger.TButton",
                  background=[('active', danger_hv), ('pressed', danger_hv)],
                  foreground=[('disabled', '#e2e8f0')])

        # Tipografia
        style.configure("Title.TLabel",
                        font=(font_title, 14, "bold"),
                        padding=(0, 4),
                        foreground=colors['text'])
        style.configure("Subtitle.TLabel",
                        font=(font_body, 10),
                        padding=(0, 2),
                        foreground=colors['subtext'])

        # Fundo da janela principal com cor Bravo
        self.configure(bg=colors['bg'])

        # Atualiza o título principal com a fonte Montserrat (se disponível)
        try:
            if hasattr(self, '_title_label'):
                self._title_label.configure(
                    font=(font_title, 16, "bold"),
                    foreground=colors['accent'])  # azul Bravo
        except Exception:
            pass

        # v2025.10.11.15 — refresca welcome banner conforme tema
        try:
            if hasattr(self, '_welcome_banner'):
                self._refresh_welcome_banner_theme()
        except Exception:
            pass

        # v2025.10.11.17 — refresca search bar (botões customizados tk.Button)
        try:
            self._refresh_search_bar_theme()
        except Exception:
            pass

        # Aplica fonte sans-serif Bravo em widgets padrão
        try:
            style.configure("TLabel", font=(font_body, 10), foreground=colors['text'])
            style.configure("TCheckbutton", font=(font_body, 10), foreground=colors['text'])
            style.configure("TEntry", font=(font_body, 10))
            style.configure("TCombobox", font=(font_body, 10))
        except Exception:
            pass
        
        # Aplica estilos aos botões específicos
        try:
            self.btn_run.configure(style="Accent.TButton")
            self.btn_toggle.configure(style="Toggle.TButton")
            self.btn_stop.configure(style="Danger.TButton")
        except Exception:
            pass
        
        # Armazena cores para uso em outros componentes
        self._theme_colors = colors
        
        # Atualiza componentes visuais
        self._style_table()
        self._update_status_colors()
        
        # Só repinta o gráfico se o canvas já existir
        if hasattr(self, "canvas"):
            self.draw_chart()
    
    def _toggle_theme(self):
        """Alterna tema com animação suave"""
        dark_mode = self.var_dark.get()
        
        # Atualiza texto do botão
        try:
            theme_text = "☀️ Modo claro" if dark_mode else "🌙 Modo escuro"
            # Encontra o checkbutton e atualiza o texto
            for widget in self.winfo_children():
                if isinstance(widget, ttk.Frame):
                    for child in widget.winfo_children():
                        if isinstance(child, ttk.Frame):
                            for grandchild in child.winfo_children():
                                if isinstance(grandchild, ttk.Checkbutton):
                                    grandchild.configure(text=theme_text)
                                    break
        except Exception:
            pass
        
        # Aplica o tema com pequena animação
        self._animate_theme_change(dark_mode)
    
    def _animate_theme_change(self, dark_mode):
        """Animação suave na mudança de tema"""
        # Fade out rápido
        self.attributes("-alpha", 0.7)
        self.after(50, lambda: self._complete_theme_change(dark_mode))
    
    def _complete_theme_change(self, dark_mode):
        """Completa a mudança de tema"""
        self._apply_theme(dark_mode)
        # Fade in de volta
        self.attributes("-alpha", 1.0)
    
    def _update_status_colors(self):
        """Atualiza cores dos indicadores de status"""
        if not hasattr(self, '_theme_colors'):
            return
            
        colors = self._theme_colors
        try:
            # Atualiza cor de fundo da barra de status
            status_frame = self.lbl_net.master.master  # Frame da barra de status
            status_frame.configure(style="Status.TFrame")
        except Exception:
            pass

    def _fade_in(self, target=1.0, step=0.05):
        """Animação suave de fade-in com easing"""
        try:
            current_alpha = float(self.attributes("-alpha") or 0.0)
        except Exception:
            return
        
        # Easing suave (ease-out)
        progress = current_alpha / target
        eased_step = step * (2 - progress)  # Acelera no início, desacelera no final
        
        new_alpha = min(target, current_alpha + eased_step)
        self.attributes("-alpha", new_alpha)
        
        if new_alpha < target:
            self.after(16, self._fade_in, target, step)  # ~60fps
        else:
            # Animação completa, adiciona um pequeno bounce
            self._bounce_effect()
    
    def _bounce_effect(self):
        """Pequeno efeito de bounce no final do fade-in"""
        try:
            # Pequeno bounce para 1.02 e volta para 1.0
            self.attributes("-alpha", 1.02)
            self.after(50, lambda: self.attributes("-alpha", 1.0))
        except Exception:
            pass

    def _channels_ok(self):
        s = self.data.get("settings", {})
        em = s.get("email", {})
        em_ok = bool(
            em.get("enabled") and em.get("smtp_host") and em.get("smtp_port")
            and (em.get("from_email") or em.get("username")) and em.get("to_emails")
        )
        wa = s.get("whatsapp", {})
        wa_ok = bool(
            wa.get("enabled")
            and os.path.exists(wa.get("node_path","") or "")
            and os.path.exists(wa.get("webjs_script","") or "")
            and wa.get("to_targets")
        )
        return em_ok, wa_ok

    def _pulse_status(self):
        """Animação suave dos indicadores de status"""
        em_ok, wa_ok = self._channels_ok()
        
        # Usa cores do tema se disponível
        if hasattr(self, '_theme_colors'):
            colors = self._theme_colors
            success_bright = colors['success']
            success_dim = self._lighten_color(colors['success'], -0.3)
            error_color = colors['error']
            net_color = colors['teal']
        else:
            # Cores padrão melhoradas
            success_bright = "#4ade80"
            success_dim = "#22c55e"
            error_color = "#ef4444"
            net_color = "#06b6d4"
        
        # Animação de pulsação mais suave
        t = getattr(self, "_pulse_toggle", False)
        self._pulse_toggle = not t
        
        # Aplica cores com transição suave
        mail_color = (success_bright if t else success_dim) if em_ok else error_color
        wa_color = (success_bright if t else success_dim) if wa_ok else error_color
        net_color_pulse = (net_color if t else self._lighten_color(net_color, -0.2)) if self.net_online else error_color
        
        self.lbl_mail.config(foreground=mail_color)
        self.lbl_wa.config(foreground=wa_color)
        self.lbl_net.config(foreground=net_color_pulse)
        
        # Frequência mais suave (800ms ao invés de 650ms)
        self.after(800, self._pulse_status)
    
    def _monitor_stop_button(self):
        """Monitora periodicamente o estado do botão Interromper para garantir consistência."""
        try:
            self._update_stop_button_state()
        except Exception as e:
            print(f"Erro no monitoramento do botão Interromper: {e}")
        finally:
            # Verifica a cada 2 segundos
            self.after(2000, self._monitor_stop_button)

    # ===== utilidades UI =====
    def _on_tree_resize(self, event=None):
        w = max(300, self.tree.winfo_width())
        ratios = {
            "Status": 0.08,
            "Nome": 0.18,
            "Horário": 0.14,
            "Tipo": 0.10,
            "Dias": 0.16,
            "Arquivo": 0.34
        }
        for col, r in ratios.items():
            self.tree.column(col, width=max(60, int(w * r)), stretch=True)

    def show_tips(self):
        tips = (
            "• Para Node.js: Arquivo/Comando = C:\\Program Files\\nodejs\\node.exe, "
            "Argumentos = bot.js, Pasta de trabalho = onde está o bot.js.\n"
            "• Para Python: Arquivo/Comando = python (ou caminho do python.exe), Argumentos = seu_script.py.\n"
            "• Para Pentaho (.ktr/.kjb): selecione o arquivo .ktr/.kjb; configure o PDI Home em Configurações.\n"
            f"• Logs: {LOG_DIR}\n• Cache WhatsApp: {WA_DIR}\n"
        )
        messagebox.showinfo("Dicas", tips)

    def save(self, silent=False):
        save_data(self.data)
        self.update_status_indicators()
        if not silent:
            messagebox.showinfo("Salvo", "Configurações salvas.")

    def _register_process(self, process, task_name):
        """Registra um processo em execução e atualiza a interface."""
        def update_ui():
            try:
                with self.running_lock:
                    # Garante que temos o PID do processo
                    if not hasattr(process, 'pid') and hasattr(process, 'popen'):
                        process.pid = process.popen.pid
                    self.running_processes[task_name] = process
                    # Habilita o botão de parada se houver processos em execução
                    if self.running_processes and self.btn_stop['state'] == 'disabled':
                        self.btn_stop.config(state="normal")
                print(f"Processo registrado: {task_name} (PID: {process.pid})")
            except Exception as e:
                print(f"Erro ao registrar processo: {e}")
        
        # Garante que a atualização da UI seja feita na thread principal
        if self.winfo_exists():
            self.after(0, update_ui)

    def _log_interruption(self, task_name):
        """Registra a interrupção de uma tarefa no arquivo de log."""
        try:
            ensure_dirs()
            log_file = LOG_DIR / f"{task_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_INTERROMPIDO.log"
            with open(log_file, "w", encoding="utf-8", errors="ignore") as f:
                f.write(f"# {task_name} @ {now_str()}\n")
                f.write("### TAREFA INTERROMPIDA PELO USUÁRIO ###\n")
                f.write(f"Data/Hora: {now_str()}\n")
                f.write("Status: Interrompida manualmente\n")
            print(f"Log de interrupção criado: {log_file}")
        except Exception as e:
            print(f"Erro ao criar log de interrupção: {e}")

    def _update_ui_after_stop(self):
        """Atualiza a interface após parar as tarefas."""
        self.btn_stop.config(state="disabled")
        self.set_status_line("Tarefas interrompidas pelo usuário.")
        self.refresh_table()

    def _stop_single_task(self, task_name):
        """Para uma única tarefa em execução."""
        try:
            # Obtém o processo SEM manter o lock por muito tempo
            process = None
            with self.running_lock:
                process = self.running_processes.get(task_name)
                if not process:
                    return False
            
            print(f"Tentando interromper tarefa: {task_name}")
            
            # Para Windows
            if os.name == 'nt':
                # Verifica se o processo já terminou
                if process.poll() is not None:
                    print(f"Processo {task_name} (PID: {process.pid}) já foi finalizado")
                    with self.running_lock:
                        if task_name in self.running_processes:
                            del self.running_processes[task_name]
                    return True
                
                try:
                    # Tenta encerrar o processo e seus filhos
                    result = subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], 
                                        timeout=5, 
                                        check=False,  # Não levantar exceção em caso de erro
                                        capture_output=True,
                                        text=True,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
                    
                    # Verifica se o processo foi encerrado com sucesso ou se já estava finalizado
                    error_output = result.stderr or ''
                    if result.returncode == 0 or 'não está em execução' in error_output or 'not running' in error_output:
                        print(f"Processo {task_name} (PID: {process.pid}) encerrado com sucesso")
                        with self.running_lock:
                            if task_name in self.running_processes:
                                del self.running_processes[task_name]
                        # Registra a interrupção no histórico com código especial -999
                        append_history(self.data, task_name, rc=-999, dur=0.0)
                        # Registra no log
                        self._log_interruption(task_name)
                        return True
                    else:
                        # Se o processo já foi finalizado
                        if 'não encontrado' in error_output or 'not found' in error_output or 'no tasks' in error_output.lower():
                            print(f"Processo {task_name} (PID: {process.pid}) já foi finalizado")
                            with self.running_lock:
                                if task_name in self.running_processes:
                                    del self.running_processes[task_name]
                            # Registra a interrupção no histórico
                            append_history(self.data, task_name, rc=-999, dur=0.0)
                            self._log_interruption(task_name)
                            return True
                        # Não exibe mensagens de erro no console
                        return False
                except subprocess.TimeoutExpired:
                    # Não exibe mensagem de timeout
                    pass
                
                # Se ainda estiver rodando, tenta métodos alternativos silenciosamente
                if process.poll() is None:
                    try:
                        process.terminate()
                        process.wait(timeout=2)
                        with self.running_lock:
                            if task_name in self.running_processes:
                                del self.running_processes[task_name]
                        # Registra a interrupção
                        append_history(self.data, task_name, rc=-999, dur=0.0)
                        self._log_interruption(task_name)
                        return True
                    except (subprocess.TimeoutExpired, Exception):
                        try:
                            process.kill()
                            process.wait(timeout=1)
                            with self.running_lock:
                                if task_name in self.running_processes:
                                    del self.running_processes[task_name]
                            # Registra a interrupção
                            append_history(self.data, task_name, rc=-999, dur=0.0)
                            self._log_interruption(task_name)
                            return True
                        except Exception:
                            pass
            
            # Para sistemas Unix/Linux
            else:
                # Código para Unix/Linux...
                pass
            
            # Se chegou aqui, não conseguiu encerrar
            return False
                
        except Exception as e:
            print(f"Erro ao interromper tarefa {task_name}: {e}")
            return False
    
    def stop_running_tasks(self):
        """Interrompe as tarefas em execução que estão selecionadas."""
        print("Método stop_running_tasks chamado")
        
        # Verifica seleção
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("Aviso", "Nenhuma tarefa selecionada.")
            return False
            
        task_names = [self.tree.item(i, 'values')[1] for i in selected]
        running_tasks = []
        
        # Filtra apenas as tarefas selecionadas que estão em execução
        # IMPORTANTE: Faz isso FORA do messagebox para evitar deadlock
        with self.running_lock:
            running_tasks = [name for name in task_names if name in self.running_processes]
        
        if not running_tasks:
            print("Nenhuma das tarefas selecionadas está em execução")
            messagebox.showinfo("Informação", 
                "Nenhuma das tarefas selecionadas está em execução.")
            return False
        
        # Pede confirmação ao usuário (FORA do lock para evitar deadlock)
        task_list = "\n- " + "\n- ".join(running_tasks)
        if not messagebox.askyesno("Confirmar", 
                                 f"Deseja realmente interromper as seguintes tarefas?\n{task_list}"):
            print("Usuário cancelou a interrupção")
            return False
        
        # Mostra mensagem de status
        self.set_status_line(f"Interrompendo {len(running_tasks)} tarefa(s)...")
        print(f"Iniciando interrupção de {len(running_tasks)} tarefa(s)...")
        
        # Executa o stop em uma thread separada para não travar a interface
        def stop_tasks():
            stopped_count = 0
            for task_name in running_tasks:
                if self._stop_single_task(task_name):
                    stopped_count += 1
            
            # Atualiza a interface após finalizar (na thread principal)
            self.after(0, lambda: self._after_stop_tasks(running_tasks))
            # Força a atualização do botão de parada e da interface
            self.after(100, self._update_stop_button_state)
            self.after(100, self.update)
            print(f"{stopped_count} tarefa(s) interrompida(s) com sucesso")
        
        # Inicia a thread de parada em segundo plano
        threading.Thread(target=stop_tasks, daemon=True).start()
        return True
    
    def _after_stop_tasks(self, stopped_processes):
        """Atualiza a interface após parar as tarefas."""
        if not stopped_processes:
            self.after(100, lambda: messagebox.showinfo("Informação", "Nenhuma tarefa foi interrompida."))
            return
            
        success_count = len(stopped_processes)  # Assume que todas foram interrompidas
        interrupted_tasks = stopped_processes.copy()
        
        # Atualiza o estado do botão de parada (já não há processos rodando)
        with self.running_lock:
            if not self.running_processes:
                self.btn_stop.config(state="disabled")
        
        # Marca as tarefas como interrompidas visualmente
        for task_name in interrupted_tasks:
            self._set_task_interrupted(task_name)
        
        # Atualiza a interface
        status_message = f"{success_count} tarefa(s) interrompida(s) com sucesso."
        self.set_status_line(status_message)
        # NÃO chama refresh_table aqui para não sobrescrever o status "Interrompido"
        
        # Mostra mensagem de sucesso
        if success_count > 0:
            self.after(100, lambda: messagebox.showinfo("Sucesso", 
                f"{success_count} tarefa(s) foram interrompida(s) com sucesso!"))
        
        # Atualiza o estado da UI
        self._set_ui_busy(False)
        self.draw_chart()
    
    def on_close(self):
        # Verifica se há tarefas em execução
        with self.running_lock:
            has_running_tasks = bool(self.running_processes)
        
        # Se houver tarefas em execução, pergunta ao usuário o que fazer
        if has_running_tasks:
            response = messagebox.askyesnocancel(
                "Tarefas em Execução",
                "Existem tarefas em execução.\n\n"
                "• 'Sim' para interromper e sair\n"
                "• 'Não' para sair sem interromper\n"
                "• 'Cancelar' para continuar executando"
            )
            
            if response is None:  # Cancelar
                return
                
            if response:  # Sim - interromper e sair
                # Cria uma janela de progresso
                progress_win = tk.Toplevel(self)
                progress_win.title("Aguarde...")
                progress_win.geometry("300x100")
                progress_win.resizable(False, False)
                
                # Centraliza a janela
                window_width = 300
                window_height = 100
                screen_width = progress_win.winfo_screenwidth()
                screen_height = progress_win.winfo_screenheight()
                x = (screen_width // 2) - (window_width // 2)
                y = (screen_height // 2) - (window_height // 2)
                progress_win.geometry(f"{window_width}x{window_height}+{x}+{y}")
                
                # Adiciona um label e uma barra de progresso
                tk.Label(progress_win, text="Interrompendo tarefas, aguarde...").pack(pady=10)
                progress = ttk.Progressbar(progress_win, mode="indeterminate")
                progress.pack(pady=10, padx=20, fill="x")
                progress.start()
                
                # Força a atualização da interface
                progress_win.update_idletasks()
                
                # Função para encerrar as tarefas e fechar o programa
                def stop_and_quit():
                    # Para todas as tarefas diretamente (sem confirmação)
                    with self.running_lock:
                        tasks_to_stop = list(self.running_processes.keys())
                    
                    # Interrompe cada tarefa
                    for task_name in tasks_to_stop:
                        self._stop_single_task(task_name)
                    
                    # Aguarda um pouco para as tarefas serem interrompidas
                    time.sleep(1)
                    
                    # Fecha o agendador
                    try:
                        self.scheduler.shutdown(wait=False)
                    except Exception:
                        pass
                    
                    # Fecha a janela de progresso e a aplicação (na thread principal)
                    self.after(0, lambda: progress_win.destroy())
                    self.after(100, self.destroy)
                
                # Executa em uma thread separada para não travar a interface
                threading.Thread(target=stop_and_quit, daemon=True).start()
                
                # Mantém a janela de progresso aberta
                self.wait_window(progress_win)
                return
            
            # Se o usuário escolheu "Não", apenas continua para fechar o programa
            # sem interromper as tarefas em execução
        
        # Se não houver tarefas em execução ou se o usuário escolheu "Não"
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass
        self.destroy()

    # ===== agendamento =====
    def reschedule_all(self):
        """
        Recria todos os jobs do APScheduler a partir das tarefas salvas.
        Suporta:
          - schedule_type == "cron"          -> horários fixos (lista `times`)
          - schedule_type == "interval"      -> a cada N minutos/horas (com filtro de dias)
          - schedule_type == "start_repeat"  -> início HH:MM + repetir a cada N por X vezes
        """
        # limpa jobs antigos
        for job in list(self.scheduler.get_jobs()):
            try:
                self.scheduler.remove_job(job.id)
            except Exception:
                pass
        self.jobs.clear()

        # recria jobs
        for t in self.data.get("tasks", []):
            # Pula tarefas desativadas
            if not t.get("enabled", True):
                continue
                
            stype = (t.get("schedule_type") or "cron").lower()

            # dias válidos
            days = t.get("days", [True] * 7)
            dows = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
            use_days = [dows[i] for i, v in enumerate(days) if v]
            if not use_days:
                continue

            # ------------------ INTERVALO ------------------
            if stype == "interval":
                try:
                    val = int(t.get("every_value") or 0)
                except Exception:
                    val = 0
                if val <= 0:
                    continue
                unit = (t.get("every_unit") or "minutes").lower()

                if unit == "minutes":
                    trig = CronTrigger(day_of_week=",".join(use_days), minute=f"*/{val}")
                else:  # "hours"
                    trig = CronTrigger(day_of_week=",".join(use_days), hour=f"*/{val}", minute=0)

                job = self.scheduler.add_job(
                    self._job_wrapper, trigger=trig, id=t["name"], name=t["name"], args=[t]
                )
                self.jobs[t["name"]] = [job.id]
                continue

            # -------------- INÍCIO + REPETIÇÃO --------------
            if stype == "start_repeat":
                start = (t.get("sr_start") or t.get("time") or "06:00")
                try:
                    ev = int(t.get("sr_every_value", t.get("every_value", 5)) or 5)
                except Exception:
                    ev = 5
                unit = (t.get("sr_every_unit") or t.get("every_unit") or "minutes").lower()
                try:
                    cnt = int(t.get("sr_count", 0) or 0)
                except Exception:
                    cnt = 0

                times = expand_start_repeat(start, ev, unit, cnt)
                if not times:
                    continue

                job_ids = []
                for idx, hhmm in enumerate(times):
                    try:
                        hh, mm = map(int, hhmm.split(":"))
                    except Exception:
                        continue
                    trig = CronTrigger(day_of_week=",".join(use_days), hour=hh, minute=mm)
                    jid = f"{t['name']}::sr::{idx}"
                    self.scheduler.add_job(self._job_wrapper, trigger=trig, id=jid, name=t["name"], args=[t])
                    job_ids.append(jid)
                self.jobs[t["name"]] = job_ids
                continue

            # ----------------- HORÁRIO(S) FIXO(S) -----------------
            times = t.get("times") or [t.get("time", "06:00")]
            try:
                times = parse_times(",".join(times) if isinstance(times, list) else str(times))
            except Exception:
                times = [t.get("time", "06:00")]
            job_ids = []
            for idx, hhmm in enumerate(times):
                try:
                    hh, mm = map(int, hhmm.split(":"))
                except Exception:
                    continue
                trig = CronTrigger(day_of_week=",".join(use_days), hour=hh, minute=mm)
                jid = f"{t['name']}::{idx}"
                self.scheduler.add_job(self._job_wrapper, trigger=trig, id=jid, name=t["name"], args=[t])
                job_ids.append(jid)
            self.jobs[t["name"]] = job_ids

        # Agenda limpeza automática de logs (se habilitada)
        self._schedule_log_cleanup()

        # v2025.10.11.8 — Agenda digest diário (se habilitado)
        try:
            self._schedule_digest_email()
        except Exception as _e:
            print(f"[digest] falha ao agendar: {_e}")

        # garante scheduler rodando
        if not self.scheduler.running:
            self.scheduler.start()

    def _schedule_digest_email(self):
        """v2025.10.11.8 — Agenda envio diário de digest por e-mail."""
        cfg = self.data.get("settings", {}).get("digest_email", {}) or {}
        if not cfg.get("enabled"):
            return
        try:
            hh, mm = map(int, (cfg.get("time", "08:00") or "08:00").split(":"))
        except Exception:
            hh, mm = 8, 0
        days = cfg.get("days") or [True]*5 + [False, False]
        dows = ["mon","tue","wed","thu","fri","sat","sun"]
        use_days = [dows[i] for i, v in enumerate(days[:7]) if v]
        if not use_days:
            return
        trig = CronTrigger(day_of_week=",".join(use_days), hour=hh, minute=mm)
        self.scheduler.add_job(
            self._send_digest_email,
            trigger=trig,
            id="__digest_email__",
            name="Digest diário",
            replace_existing=True,
        )

    def _send_digest_email(self):
        """v2025.10.11.8 — Constrói e envia o digest das últimas 24h."""
        cfg = self.data.get("settings", {}).get("digest_email", {}) or {}
        if not cfg.get("enabled"):
            return
        try:
            now = datetime.now()
            since = now - timedelta(hours=24)
            ok_count = 0
            fail_items = []
            success_items = []
            duration_items = []
            for task_name, hist in (self.data.get("history", {}) or {}).items():
                for e in hist:
                    try:
                        dt = datetime.strptime(e.get("ts", ""), "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        continue
                    if dt < since:
                        continue
                    rc = int(e.get("rc", -1))
                    dur = float(e.get("dur", 0))
                    if rc == 0:
                        ok_count += 1
                        success_items.append((task_name, dt, dur))
                        duration_items.append((task_name, dur))
                    elif rc == -999:
                        # interrompido — ignora
                        pass
                    else:
                        fail_items.append((task_name, dt, rc))

            total = ok_count + len(fail_items)
            taxa = (ok_count / total * 100.0) if total else 0.0

            lines = [
                f"Agendador-Bravo — Digest diário",
                f"Período: {since.strftime('%d/%m %H:%M')} → {now.strftime('%d/%m %H:%M')}",
                "",
                f"📊 Resumo das últimas 24h",
                f"  • Execuções:   {total}",
                f"  • Sucessos:    {ok_count}",
                f"  • Falhas:      {len(fail_items)}",
                f"  • Taxa sucesso: {taxa:.1f}%",
                "",
            ]
            if cfg.get("include_failures", True) and fail_items:
                lines.append(f"❌ Falhas ({len(fail_items)})")
                for n, dt, rc in fail_items[-20:]:
                    lines.append(f"  • {dt.strftime('%H:%M')} — {n} (RC={rc})")
                lines.append("")
            if cfg.get("include_success", True) and success_items:
                lines.append(f"✅ Sucessos ({len(success_items)})")
                for n, dt, dur in success_items[-20:]:
                    lines.append(f"  • {dt.strftime('%H:%M')} — {n} ({dur:.1f}s)")
                lines.append("")
            if cfg.get("include_top_slow", True) and duration_items:
                top_slow = sorted(duration_items, key=lambda x: x[1], reverse=True)[:5]
                lines.append("🐢 Top 5 mais lentas")
                for n, dur in top_slow:
                    lines.append(f"  • {n} — {dur:.1f}s")

            body = "\n".join(lines)
            subject = f"[Agendador-Bravo] Digest diário — {ok_count} OK / {len(fail_items)} falhas"

            settings = self.data["settings"]
            try:
                send_email(settings, subject, body)
            except Exception as e:
                print(f"[digest] erro e-mail: {e}")
            try:
                send_webhook(settings, subject, body, ok=(len(fail_items) == 0))
            except Exception as e:
                print(f"[digest] erro webhook: {e}")
        except Exception as e:
            print(f"[digest] falha geral: {e}")

    # =====================================================================
    # v2025.10.11.8 — Backup / Restore / YAML
    # =====================================================================
    def action_backup(self):
        """Gera backup ZIP em pasta escolhida pelo usuário."""
        try:
            default_name = f"AgendadorBravo_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
            dest = filedialog.asksaveasfilename(
                parent=self,
                title="Salvar backup",
                defaultextension=".zip",
                initialfile=default_name,
                filetypes=[("Arquivo ZIP", "*.zip")],
            )
            if not dest:
                return
            out = backup_to_zip(Path(dest))
            messagebox.showinfo("Backup", f"Backup criado em:\n{out}")
        except Exception as e:
            messagebox.showerror("Backup", f"Falha ao criar backup:\n{e}")

    def action_restore(self):
        """Restaura backup ZIP."""
        try:
            src = filedialog.askopenfilename(
                parent=self,
                title="Selecionar backup",
                filetypes=[("Arquivo ZIP", "*.zip")],
            )
            if not src:
                return
            if not messagebox.askyesno(
                "Restaurar backup",
                "Isso irá sobrescrever o config.json atual.\n"
                "Recomendado: fazer um Backup antes.\n\n"
                "Deseja continuar?",
                parent=self):
                return
            ok, msg = restore_from_zip(Path(src))
            if ok:
                messagebox.showinfo("Restaurar", msg)
                # recarrega data e UI
                self.data = load_data()
                self.refresh_table()
                self.draw_chart()
                try:
                    self.reschedule_all()
                except Exception:
                    pass
            else:
                messagebox.showerror("Restaurar", msg)
        except Exception as e:
            messagebox.showerror("Restaurar", f"Falha:\n{e}")

    def action_export_yaml(self):
        try:
            default_name = f"jobs_{datetime.now().strftime('%Y%m%d')}.yaml"
            dest = filedialog.asksaveasfilename(
                parent=self,
                title="Exportar jobs em YAML",
                defaultextension=".yaml",
                initialfile=default_name,
                filetypes=[("YAML", "*.yaml *.yml"), ("JSON", "*.json")],
            )
            if not dest:
                return
            ok, msg = export_tasks_yaml(Path(dest), self.data.get("tasks", []))
            if ok:
                messagebox.showinfo("Export YAML", f"{msg}\n{dest}")
            else:
                messagebox.showerror("Export YAML", msg)
        except Exception as e:
            messagebox.showerror("Export YAML", f"Falha:\n{e}")

    # v2025.10.11.17 — Refresca cores da search bar conforme tema ativo
    def _refresh_search_bar_theme(self):
        try:
            dark = bool(self.var_dark.get())
        except Exception:
            dark = False
        T = _bravo_theme(dark)
        try:
            # Frame da search bar acompanha o bg do app
            self._search_frame.configure(bg=T['bg_app'])
            self._search_icon_lbl.configure(bg=T['bg_app'], fg=T['subtext'])
            # Botão Rodar tag em azul Bravo
            self.btn_run_tag.configure(
                bg=T['accent'], fg="#ffffff",
                activebackground=T['accent_dark'],
                activeforeground="#ffffff",
                disabledforeground=T['text_muted'],
                highlightbackground=T['accent'],
                highlightthickness=0,
            )
            # Hover effect (simulado via bind)
            def _on_enter(_e, btn=self.btn_run_tag, T=T):
                btn.configure(bg=T['accent_dark'])
            def _on_leave(_e, btn=self.btn_run_tag, T=T):
                btn.configure(bg=T['accent'])
            self.btn_run_tag.bind("<Enter>", _on_enter)
            self.btn_run_tag.bind("<Leave>", _on_leave)
            # Botão limpar (mais sutil)
            self.btn_clear_filters.configure(
                bg=T['bg_app'], fg=T['subtext'],
                activebackground=T['bg_hover'],
                activeforeground=T['text'],
                highlightthickness=0,
            )
            def _ce_enter(_e, btn=self.btn_clear_filters, T=T):
                btn.configure(bg=T['bg_hover'], fg=T['text'])
            def _ce_leave(_e, btn=self.btn_clear_filters, T=T):
                btn.configure(bg=T['bg_app'], fg=T['subtext'])
            self.btn_clear_filters.bind("<Enter>", _ce_enter)
            self.btn_clear_filters.bind("<Leave>", _ce_leave)
        except Exception as e:
            print(f"[refresh_search_bar] {e}")

    # v2025.10.11.15 — Refresca welcome banner conforme o tema ativo
    def _refresh_welcome_banner_theme(self):
        try:
            dark = bool(self.var_dark.get())
        except Exception:
            dark = False
        T = _bravo_theme(dark)
        bg = T['accent_bg']
        fg = T['accent_dark'] if not dark else T['accent_light']
        try:
            self._welcome_banner.configure(bg=bg, highlightthickness=1,
                                            highlightbackground=T['accent_light'])
            self._wb_left.configure(bg=bg)
            self._wb_right.configure(bg=bg)
            self._wb_title.configure(bg=bg, fg=fg)
            self._wb_sub.configure(bg=bg, fg=T['text'])
        except Exception:
            pass

    # v2025.10.11.9 — Welcome banner + empty state ------------------------
    def _dismiss_welcome(self):
        try:
            self._welcome_banner.grid_remove()
            self.data.setdefault("settings", {})["welcome_dismissed"] = True
            save_data(self.data)
        except Exception:
            pass

    def _maybe_show_welcome(self):
        """Mostra o welcome banner se setup ainda está incompleto e não foi dispensado."""
        try:
            settings = self.data.get("settings", {})
            if settings.get("welcome_dismissed"):
                return
            has_tasks = bool(self.data.get("tasks"))
            has_email = bool(settings.get("email", {}).get("enabled"))
            has_wa = bool(settings.get("whatsapp", {}).get("enabled"))
            has_wh = bool(settings.get("webhooks", {}).get("enabled"))
            # Mostra se não tem nenhum job OU se não tem notificação configurada
            should_show = (not has_tasks) or not (has_email or has_wa or has_wh)
            if should_show:
                self._welcome_banner.grid(row=1, column=0, sticky="ew")
                self._welcome_banner.lift()
        except Exception:
            pass

    def _update_empty_state(self):
        """v2025.10.11.15 — Empty state com tema dinâmico."""
        try:
            has_tasks = bool(self.data.get("tasks"))
            if has_tasks:
                if getattr(self, "_empty_state_lbl", None):
                    try:
                        self._empty_state_lbl.place_forget()
                    except Exception:
                        pass
                return
            # recria sempre pra pegar tema atual (light vs dark)
            if getattr(self, "_empty_state_lbl", None):
                try:
                    self._empty_state_lbl.destroy()
                except Exception:
                    pass
                self._empty_state_lbl = None

            try:
                dark = bool(self.var_dark.get())
            except Exception:
                dark = False
            T = _bravo_theme(dark)

            parent = self.tree.master
            lbl = tk.Frame(parent, bg=T['bg_surface'])
            tk.Label(lbl, text="📋",
                     bg=T['bg_surface'], fg=T['text_muted'],
                     font=("Segoe UI Emoji", 48)).pack(pady=(40, 8))
            tk.Label(lbl, text="Nenhum job ainda",
                     bg=T['bg_surface'], fg=T['text_strong'],
                     font=("Segoe UI", 14, "bold")).pack()
            tk.Label(lbl, text="Crie seu primeiro job para começar.",
                     bg=T['bg_surface'], fg=T['subtext'],
                     font=("Segoe UI", 10)).pack(pady=(4, 16))
            btns = tk.Frame(lbl, bg=T['bg_surface'])
            btns.pack()
            ttk.Button(btns, text="🧙 Abrir Assistente",
                       command=self.open_assistant,
                       style="Accent.TButton").pack(side="left", padx=4)
            ttk.Button(btns, text="➕ Criar do zero",
                       command=self.add_task).pack(side="left", padx=4)
            ttk.Button(btns, text="📚 Ver tutorial",
                       command=self.open_help_panel).pack(side="left", padx=4)
            self._empty_state_lbl = lbl
            lbl.place(relx=0.5, rely=0.5, anchor="center")
        except Exception as e:
            print(f"[empty_state] {e}")

    # v2025.10.11.16 — Executar todos jobs de uma tag --------------------
    def run_tag(self):
        """Abre modal pra disparar todos jobs marcados com a tag selecionada."""
        try:
            tag = (self.tag_filter_var.get() or "").strip()
        except Exception:
            tag = ""
        if not tag or tag == "(todas as tags)":
            messagebox.showwarning(
                "Executar tag",
                "Selecione uma tag específica no filtro antes.\n\n"
                "Como adicionar tags: edite um job → seção Avançado → campo Tags.\n"
                "Depois escolha a tag no combobox no topo da tabela e clique\n"
                "'▶ Rodar tag'.",
                parent=self)
            return
        # Coleta jobs com a tag (apenas ativos)
        jobs = [t for t in self.data.get("tasks", [])
                if tag in (t.get("tags") or []) and t.get("enabled", True)]
        if not jobs:
            messagebox.showinfo(
                "Executar tag",
                f"Nenhum job ATIVO com a tag '{tag}'.\n\n"
                "Verifique se os jobs com essa tag não estão pausados.",
                parent=self)
            return
        self._show_run_tag_dialog(tag, jobs)

    def _show_run_tag_dialog(self, tag, jobs):
        """v2025.10.11.16 — Modal de confirmação + opções para Executar tag."""
        try:
            dark = bool(self.var_dark.get())
        except Exception:
            dark = False
        T = _bravo_theme(dark)
        font_title, font_body = _resolve_fonts()

        dlg = tk.Toplevel(self)
        dlg.title(f"Executar tag — {tag}")
        dlg.geometry("560x560")
        dlg.minsize(480, 480)
        dlg.transient(self)
        dlg.grab_set()
        dlg.configure(bg=T['bg_app'])
        try:
            ico = find_logo_ico()
            if ico:
                dlg.iconbitmap(default=str(ico))
        except Exception:
            pass

        # Header
        header = tk.Frame(dlg, bg=T['bg_surface'])
        header.pack(fill="x")
        h_inner = tk.Frame(header, bg=T['bg_surface'])
        h_inner.pack(fill="x", padx=20, pady=(18, 12))
        tk.Label(h_inner, text=f"🚀  Executar tag",
                 bg=T['bg_surface'], fg=T['accent'],
                 font=(font_title, 15, "bold")).pack(anchor="w")
        tk.Label(h_inner,
                 text=f"{len(jobs)} job(s) ativo(s) com a tag '{tag}' serão disparados em paralelo.",
                 bg=T['bg_surface'], fg=T['subtext'],
                 font=(font_body, 9)).pack(anchor="w", pady=(4, 0))
        tk.Frame(dlg, bg=T['border'], height=1).pack(fill="x")

        # Body
        body = tk.Frame(dlg, bg=T['bg_app'])
        body.pack(fill="both", expand=True, padx=20, pady=14)

        # Lista de jobs
        list_lbl = tk.Label(body, text="Jobs que serão executados:",
                            bg=T['bg_app'], fg=T['text'],
                            font=(font_body, 10, "bold"))
        list_lbl.pack(anchor="w", pady=(0, 6))

        list_container = tk.Frame(body, bg=T['border'], bd=0)
        list_container.pack(fill="both", expand=True)
        listbox = tk.Listbox(list_container,
                             font=(font_body, 10),
                             bg=T['bg_surface'], fg=T['text'],
                             selectbackground=T['accent'],
                             selectforeground='#ffffff',
                             activestyle='none',
                             borderwidth=0, highlightthickness=0,
                             height=8)
        for j in jobs:
            listbox.insert("end", f"   ✓   {j['name']}")
        listbox.pack(fill="both", expand=True, padx=1, pady=1)

        # Opções
        opt_lbl = tk.Label(body, text="Opções:",
                          bg=T['bg_app'], fg=T['text'],
                          font=(font_body, 10, "bold"))
        opt_lbl.pack(anchor="w", pady=(14, 6))

        opt_frame = tk.Frame(body, bg=T['bg_surface'],
                             highlightthickness=1, highlightbackground=T['border'])
        opt_frame.pack(fill="x")
        opt_inner = tk.Frame(opt_frame, bg=T['bg_surface'])
        opt_inner.pack(fill="x", padx=12, pady=10)

        var_deps = tk.BooleanVar(value=True)
        var_ignore_mw = tk.BooleanVar(value=False)
        var_ignore_running = tk.BooleanVar(value=True)
        tk.Checkbutton(opt_inner,
                       text="Respeitar dependências entre jobs (depends_on)",
                       variable=var_deps,
                       bg=T['bg_surface'], fg=T['text'],
                       activebackground=T['bg_surface'],
                       selectcolor=T['bg_surface'],
                       font=(font_body, 9)).pack(anchor="w", pady=2)
        tk.Checkbutton(opt_inner,
                       text="Ignorar janelas de manutenção (modo emergência)",
                       variable=var_ignore_mw,
                       bg=T['bg_surface'], fg=T['text'],
                       activebackground=T['bg_surface'],
                       selectcolor=T['bg_surface'],
                       font=(font_body, 9)).pack(anchor="w", pady=2)
        tk.Checkbutton(opt_inner,
                       text="Pular jobs já em execução (recomendado)",
                       variable=var_ignore_running,
                       bg=T['bg_surface'], fg=T['text'],
                       activebackground=T['bg_surface'],
                       selectcolor=T['bg_surface'],
                       font=(font_body, 9)).pack(anchor="w", pady=2)

        # Footer com botões
        tk.Frame(dlg, bg=T['border'], height=1).pack(fill="x")
        footer = tk.Frame(dlg, bg=T['bg_surface'])
        footer.pack(fill="x")
        f_inner = tk.Frame(footer, bg=T['bg_surface'])
        f_inner.pack(fill="x", padx=20, pady=14)
        ttk.Button(f_inner, text="Cancelar",
                   command=dlg.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(f_inner, text=f"▶ Executar {len(jobs)} jobs",
                   style="Accent.TButton",
                   command=lambda: self._do_run_tag(
                       tag, jobs,
                       var_deps.get(), var_ignore_mw.get(),
                       var_ignore_running.get(), dlg
                   )).pack(side="right")

    def _do_run_tag(self, tag, jobs, respect_deps, ignore_mw, skip_running, dialog):
        """Dispara todos os jobs em paralelo, aplicando os overrides escolhidos."""
        try:
            dialog.destroy()
        except Exception:
            pass

        # Salva última tag (futuro: 'Modo Manhã' usa essa)
        self.data["last_run_tag"] = tag
        try:
            save_data(self.data)
        except Exception:
            pass

        launched = 0
        skipped = 0
        for task in jobs:
            # Skip se já está rodando (opção)
            if skip_running:
                with self.running_lock:
                    if task["name"] in self.running_processes:
                        skipped += 1
                        continue
            # Cópia com overrides
            t_run = dict(task)
            if not respect_deps:
                t_run["depends_on"] = []
            if ignore_mw:
                t_run["respect_maintenance"] = False
            # Dispara em thread (igual ao run_now)
            threading.Thread(
                target=lambda t=t_run: self._job_wrapper(t),
                daemon=True
            ).start()
            launched += 1

        msg = f"🏃 Rodando {launched} job(s) da tag '{tag}'"
        if skipped:
            msg += f"  ·  {skipped} já estavam rodando (pulados)"
        self.set_status_line(msg)

    # v2025.10.11.11 — Menu "Mais" + context menu --------------------------
    def _show_more_menu(self):
        """Menu dropdown com tudo que tirei da toolbar pra ficar minimalista."""
        try:
            m = tk.Menu(self, tearoff=0)
            m.add_command(label="🚀  Executar todos da tag...",
                          accelerator="Ctrl+Shift+T",
                          command=self.run_tag)
            m.add_separator()
            m.add_command(label="🧙  Assistente de criação...",
                          command=self.open_assistant)
            m.add_command(label="✏️  Editar job selecionado",
                          accelerator="Ctrl+E",
                          command=self.edit_task)
            m.add_command(label="🗑️  Remover job selecionado",
                          accelerator="Del",
                          command=self.remove_task)
            m.add_separator()
            m.add_command(label="📂  Abrir pasta de logs",
                          command=lambda: os.startfile(LOG_DIR))
            m.add_command(label="📄  Abrir último log do job",
                          command=self.open_last_log)
            m.add_separator()
            m.add_command(label="💾  Fazer backup (.zip)",
                          command=self.action_backup)
            m.add_command(label="↩  Restaurar backup...",
                          command=self.action_restore)
            m.add_separator()
            m.add_command(label="📤  Exportar jobs (YAML)",
                          command=self.action_export_yaml)
            m.add_command(label="📥  Importar jobs (YAML)",
                          command=self.action_import_yaml)
            m.add_separator()
            m.add_command(label="🧪  Simular erro (teste de notificação)",
                          command=self.simulate_error)
            m.add_command(label="🔄  Verificar atualizações",
                          command=self.check_updates_now)
            # Posiciona o menu logo abaixo do widget que disparou
            # Usa coordenadas do mouse
            x = self.winfo_pointerx()
            y = self.winfo_pointery()
            m.tk_popup(x, y)
        except Exception as e:
            print(f"[more menu] {e}")

    def _show_tree_context_menu(self, event):
        """v2025.10.11.11 — Menu de contexto ao clicar direito num job."""
        try:
            # seleciona o item sob o cursor
            iid = self.tree.identify_row(event.y)
            if iid:
                if iid not in self.tree.selection():
                    self.tree.selection_set(iid)
            sel = self.tree.selection()
            if not sel:
                return
            task = next((t for t in self.data.get("tasks", []) if t.get("name") == sel[0]), None)
            if not task:
                return
            m = tk.Menu(self, tearoff=0)
            m.add_command(label="▶  Executar agora",
                          accelerator="Ctrl+R",
                          command=self.run_now)
            enabled = task.get("enabled", True)
            label = "▶️  Ativar" if not enabled else "⏸️  Pausar"
            m.add_command(label=label, command=self.toggle_task_status)
            m.add_separator()
            m.add_command(label="✏️  Editar...",
                          accelerator="Ctrl+E",
                          command=self.edit_task)
            m.add_command(label="🗑️  Remover...",
                          accelerator="Del",
                          command=self.remove_task)
            m.add_separator()
            m.add_command(label="📄  Abrir último log",
                          command=self.open_last_log)
            m.tk_popup(event.x_root, event.y_root)
        except Exception as e:
            print(f"[context menu] {e}")

    # v2025.10.11.9 — Painel de Ajuda --------------------------------------
    def open_help_panel(self):
        """Abre (ou foca) o painel de Ajuda."""
        try:
            if getattr(self, "_help_window", None) and self._help_window.winfo_exists():
                self._help_window.lift()
                self._help_window.focus_force()
                return
        except Exception:
            pass
        self._help_window = HelpPanel(self)

    def action_import_yaml(self):
        try:
            src = filedialog.askopenfilename(
                parent=self,
                title="Importar jobs (YAML/JSON)",
                filetypes=[("YAML/JSON", "*.yaml *.yml *.json"), ("Todos", "*.*")],
            )
            if not src:
                return
            ok, tasks, msg = import_tasks_yaml(Path(src))
            if not ok:
                messagebox.showerror("Import YAML", msg)
                return
            # diálogo de merge: substituir tudo, ou só adicionar ausentes
            choice = messagebox.askyesnocancel(
                "Importar jobs",
                f"{msg}\n\n"
                "Sim = MESCLAR (adiciona/substitui pelo nome)\n"
                "Não = SUBSTITUIR todos os jobs atuais\n"
                "Cancelar = não fazer nada",
                parent=self)
            if choice is None:
                return
            if choice is True:
                existing = {t["name"]: i for i, t in enumerate(self.data.get("tasks", []))}
                for nt in tasks:
                    if not isinstance(nt, dict) or "name" not in nt:
                        continue
                    if nt["name"] in existing:
                        self.data["tasks"][existing[nt["name"]]] = nt
                    else:
                        self.data["tasks"].append(nt)
            else:
                self.data["tasks"] = [t for t in tasks if isinstance(t, dict) and "name" in t]
            # garante defaults do schema
            self.data = _migrate_data_schema(self.data)
            save_data(self.data)
            self.refresh_table()
            try:
                self.reschedule_all()
            except Exception:
                pass
            messagebox.showinfo("Import YAML", f"Importação concluída: {len(self.data['tasks'])} jobs ativos.")
        except Exception as e:
            messagebox.showerror("Import YAML", f"Falha:\n{e}")

    def _schedule_log_cleanup(self):
        """Agenda a limpeza automática de logs baseada nas configurações."""
        cleanup_cfg = self.data.get("settings", {}).get("log_cleanup", {})
        
        if not cleanup_cfg.get("enabled", True):
            return
        
        try:
            # Extrai configurações
            schedule_day = int(cleanup_cfg.get("schedule_day", 6))  # 0=segunda, 6=domingo
            schedule_time = cleanup_cfg.get("schedule_time", "02:00")
            hh, mm = map(int, schedule_time.split(":"))
            
            # Mapeia número do dia para string do APScheduler
            day_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
            day_name = day_names[schedule_day % 7]
            
            # Cria trigger para executar semanalmente
            trig = CronTrigger(day_of_week=day_name, hour=hh, minute=mm)
            
            # Agenda job de limpeza
            self.scheduler.add_job(
                self._cleanup_logs_job,
                trigger=trig,
                id="__log_cleanup__",
                name="Limpeza automática de logs"
            )
            
        except Exception:
            # Se houver erro no agendamento, não faz nada
            pass

    def _cleanup_logs_job(self):
        """Job que executa a limpeza automática de logs."""
        try:
            cleanup_logs(self.data.get("settings", {}))
        except Exception:
            # Se houver erro na limpeza, não faz nada
            pass

    def _on_scheduler_event(self, event):
        """Registra jobs pulados/com erro num log dedicado para diagnostico."""
        try:
            sched_log = LOG_DIR / "_scheduler.log"
            kind = "UNKNOWN"
            if event.code == EVENT_JOB_MISSED:
                kind = "MISSED"        # job nao rodou dentro do misfire_grace_time
            elif event.code == EVENT_JOB_ERROR:
                kind = "ERROR"         # excecao durante a execucao
            elif event.code == EVENT_JOB_MAX_INSTANCES:
                kind = "MAX_INSTANCES" # job anterior ainda rodando, nova execucao descartada
            line = f"[{now_str()}] {kind} job_id={event.job_id} scheduled_run_time={getattr(event, 'scheduled_run_time', '-')}\n"
            with open(sched_log, "a", encoding="utf-8", errors="ignore") as f:
                f.write(line)
            print(f"[SCHEDULER] {kind}: {event.job_id}")
        except Exception as e:
            print(f"[SCHEDULER] Falha ao logar evento: {e}")

    def _job_wrapper(self, task, attempt: int = 1):
        """Executa um job aplicando: janelas de manutenção, dependências e retry.

        attempt: número da tentativa atual (1 = primeira). Usado por _schedule_retry.
        """
        def progress(_):
            pass

        task_name = task["name"]
        settings = self.data.get("settings", {})

        # ---- 1) Maintenance windows ---------------------------------------
        if task.get("respect_maintenance", True):
            inside, win_name = in_maintenance_window(settings)
            if inside:
                try:
                    sched_log = LOG_DIR / "_scheduler.log"
                    with open(sched_log, "a", encoding="utf-8", errors="ignore") as f:
                        f.write(f"[{now_str()}] SKIP_MAINTENANCE job={task_name} window={win_name}\n")
                except Exception:
                    pass
                if self.winfo_exists():
                    self.after(0, lambda: self.set_status_line(
                        f"'{task_name}' pulado: janela de manutenção '{win_name}'"))
                return

        # ---- 2) Dependências (depends_on) ---------------------------------
        depends_on = task.get("depends_on", []) or []
        if depends_on:
            history = self.data.get("history", {})
            missing = []
            for dep_name in depends_on:
                dep_hist = history.get(dep_name, [])
                if not dep_hist:
                    missing.append(f"{dep_name}(sem histórico)")
                    continue
                last = dep_hist[-1]
                if int(last.get("rc", -1)) != 0:
                    missing.append(f"{dep_name}(última falhou)")
            if missing:
                try:
                    sched_log = LOG_DIR / "_scheduler.log"
                    with open(sched_log, "a", encoding="utf-8", errors="ignore") as f:
                        f.write(f"[{now_str()}] SKIP_DEPENDS job={task_name} faltando={missing}\n")
                except Exception:
                    pass
                if self.winfo_exists():
                    self.after(0, lambda m=missing: self.set_status_line(
                        f"'{task_name}' pulado: dependências não satisfeitas ({', '.join(m)})"))
                return

        # ---- callbacks de UI ---------------------------------------------
        def on_task_start():
            if self.winfo_exists():
                self._on_job_start(task)  # Já chama _set_task_running internamente

        def on_task_end(rc, dur, log_path):
            def update_ui():
                if not self.winfo_exists():
                    return
                was_interrupted = False
                with self.running_lock:
                    if task_name not in self.running_processes:
                        was_interrupted = True
                    else:
                        del self.running_processes[task_name]
                    if not self.running_processes:
                        self.btn_stop.config(state="disabled")
                if not was_interrupted:
                    self._set_task_running(task_name, False)
                self.draw_chart()
                self._on_job_end(task, rc, dur, log_path)
            if self.winfo_exists():
                self.after(0, update_ui)

        # Inicia a tarefa
        try:
            if self.winfo_exists():
                self.after(0, on_task_start)
        except Exception as e:
            print(f"Erro ao iniciar tarefa {task_name}: {e}")

        # Executa a tarefa
        try:
            rc, dur, log_path = run_task(task, settings,
                                      progress_cb=progress,
                                      process_callback=self._register_process)
            append_history(self.data, task_name, rc, dur)
            self._maybe_notify(task, rc, log_path)
        except Exception as e:
            print(f"Erro ao executar tarefa {task_name}: {e}")
            rc, dur, log_path = 1, 0, None

        # Atualiza a interface após o término
        on_task_end(rc, dur, log_path)

        # ---- 3) Retry com backoff -----------------------------------------
        if rc != 0:
            retry_cfg = task.get("retry", {}) or {}
            if retry_cfg.get("enabled"):
                max_attempts = int(retry_cfg.get("max_attempts", 3) or 3)
                backoff = retry_cfg.get("backoff_seconds", [60, 300, 900]) or [60, 300, 900]
                if attempt < max_attempts and attempt - 1 < len(backoff):
                    delay = int(backoff[attempt - 1])
                    self._schedule_retry(task, attempt + 1, delay)

    def _schedule_retry(self, task, next_attempt: int, delay_seconds: int):
        """Agenda uma re-execução do job daqui a delay_seconds, contando como tentativa next_attempt."""
        try:
            from apscheduler.triggers.date import DateTrigger
            run_at = datetime.now() + timedelta(seconds=delay_seconds)
            jid = f"{task['name']}::retry::{next_attempt}::{int(time.time())}"
            self.scheduler.add_job(
                lambda t=task, n=next_attempt: self._job_wrapper(t, attempt=n),
                trigger=DateTrigger(run_date=run_at),
                id=jid, name=f"{task['name']} (retry {next_attempt})",
                misfire_grace_time=600, replace_existing=True,
            )
            try:
                sched_log = LOG_DIR / "_scheduler.log"
                with open(sched_log, "a", encoding="utf-8", errors="ignore") as f:
                    f.write(f"[{now_str()}] RETRY_SCHEDULED job={task['name']} attempt={next_attempt} in={delay_seconds}s at={run_at.strftime('%H:%M:%S')}\n")
            except Exception:
                pass
            if self.winfo_exists():
                self.after(0, lambda: self.set_status_line(
                    f"'{task['name']}' falhou. Retry {next_attempt} em {delay_seconds}s."))
        except Exception as e:
            print(f"[retry] falha ao agendar: {e}")

    def _maybe_notify(self, task, rc, log_path):
        # Verifica se a tarefa foi interrompida manualmente
        was_stopped_manually = False
        with self.running_lock:
            if task['name'] not in self.running_processes:
                was_stopped_manually = True
        if was_stopped_manually:
            return

        ok = (rc == 0)
        settings = self.data["settings"]

        # E-mail e WhatsApp: só em falha (comportamento existente)
        if (not ok) and task.get("notify_fail", True):
            subject = f"[{task['name']}] FALHA (RC={rc})"
            body = f"Tarefa: {task['name']}\nData: {now_str()}\nRC: {rc}\nLog: {log_path}"
            try:
                send_email(settings, subject, body)
            except Exception as e:
                print("Erro e-mail:", e)
            try:
                send_whatsapp(settings, subject, body)
            except Exception as e:
                print("Erro WhatsApp:", e)

        # Webhooks (Discord/Slack/Teams): sucesso e/ou falha conforme settings
        try:
            tag = "OK" if ok else f"FALHA (RC={rc})"
            subj = f"[{task['name']}] {tag}"
            body = f"Tarefa: {task['name']}\nData: {now_str()}\nRC: {rc}\nLog: {log_path}"
            send_webhook(settings, subj, body, ok)
        except Exception as e:
            print("Erro webhook:", e)

    def _update_task_status_color(self, name):
        """Atualiza a cor do status de uma tarefa baseado no seu estado atual"""
        try:
            # Verifica se a tarefa está rodando
            with self.running_lock:
                is_running = name in self.running_processes
            
            # Busca a tarefa
            task = next((t for t in self.data.get("tasks", []) if t.get("name") == name), None)
            if not task:
                return
            
            enabled = task.get("enabled", True)
            
            # Define texto de status
            if is_running:
                status_text = "Rodando"
            elif enabled:
                status_text = "Ativo"
            else:
                status_text = "Parado"
            
            # Atualiza apenas a coluna Status
            vals = list(self.tree.item(name, "values"))
            if vals:
                vals[0] = status_text
                self.tree.item(name, values=tuple(vals))
        except Exception:
            pass
    
    def _set_task_running(self, name, running: bool):
        try:
            vals = list(self.tree.item(name, "values"))
            if not vals:
                return
            enabled = True
            try:
                enabled = next((t for t in self.data.get("tasks", []) if t.get("name") == name), {}).get("enabled", True)
            except Exception:
                pass
            
            # Define texto de status
            if running:
                vals[0] = "Rodando"
            elif enabled:
                vals[0] = "Ativo"
            else:
                vals[0] = "Parado"
            
            # Atualiza os valores
            self.tree.item(name, values=tuple(vals))
        except Exception:
            pass

    def _set_task_interrupted(self, name):
        """Marca uma tarefa como interrompida"""
        try:
            print(f"Marcando tarefa '{name}' como interrompida")
            vals = list(self.tree.item(name, "values"))
            if not vals:
                print(f"Erro: valores vazios para tarefa '{name}'")
                return
            
            print(f"Status anterior: {vals[0]}")
            vals[0] = "Interrompido"
            print(f"Novo status: {vals[0]}")
            
            # Atualiza os valores
            self.tree.item(name, values=tuple(vals))
            print(f"Status atualizado na interface para '{name}'")
            
            # Após 3 segundos, volta para o estado normal (ativo/inativo)
            def reset_status():
                try:
                    if self.winfo_exists():
                        print(f"Resetando status de '{name}' após 3 segundos")
                        self._set_task_running(name, False)
                except Exception as e:
                    print(f"Erro ao resetar status: {e}")
            self.after(3000, reset_status)
        except Exception as e:
            print(f"Erro em _set_task_interrupted: {e}")
    
    def _on_job_start(self, task):
        try:
            task_name = task['name']
            self.set_status_line(f"Iniciando '{task_name}'…")
            self._show_toast(f"Iniciando: {task_name}", task.get('path', ''))
            # Atualiza o status visual para "Rodando"
            self._set_task_running(task_name, True)
        except Exception:
            pass

    def _on_job_end(self, task, rc, dur, log_path):
        try:
            task_name = task['name']
            was_stopped_manually = False
            
            # Verifica se a tarefa foi interrompida manualmente
            with self.running_lock:
                if task_name not in self.running_processes:
                    was_stopped_manually = True
            
            # Se foi interrompida manualmente, marca como interrompida
            if was_stopped_manually and rc != 0:
                status = "Interrompido"
                self._set_task_interrupted(task_name)
                self.set_status_line(f"Tarefa '{task_name}' interrompida pelo usuário")
                self._show_toast(f"Tarefa interrompida", f"{task_name} foi interrompida com sucesso", ok=True)
            else:
                status = "OK" if rc == 0 else f"Falha (RC={rc})"
                self.set_status_line(f"Concluído '{task_name}' — {status} em {dur:.1f}s")
                self._show_toast(f"Concluído: {task_name}", f"{status} • {dur:.1f}s", ok=(rc == 0))
        except Exception:
            pass

    def _show_toast(self, title: str, subtitle: str = "", duration: int = 4000, ok: bool = True):
        try:
            win = tk.Toplevel(self)
            win.overrideredirect(True)
            try:
                win.attributes("-topmost", True)
            except Exception:
                pass
            colors = getattr(self, "_theme_colors", None)
            bg = (colors.get('surface') if colors else "#333333") if isinstance(colors, dict) else "#333333"
            fg = (colors.get('text') if colors else "#ffffff") if isinstance(colors, dict) else "#ffffff"
            border = (colors.get('success') if ok else colors.get('error')) if isinstance(colors, dict) else ("#22c55e" if ok else "#ef4444")
            frame = tk.Frame(win, bg=bg, highlightthickness=2, highlightbackground=border)
            frame.pack(fill="both", expand=True)
            lbl1 = tk.Label(frame, text=title, bg=bg, fg=fg, font=("Segoe UI", 10, "bold"))
            lbl1.pack(anchor="w", padx=10, pady=(8, 0))
            if subtitle:
                lbl2 = tk.Label(frame, text=subtitle, bg=bg, fg=fg, font=("Segoe UI", 9))
                lbl2.pack(anchor="w", padx=10, pady=(2, 10))
            else:
                tk.Label(frame, text="", bg=bg).pack(pady=(0, 6))
            self.update_idletasks()
            win.update_idletasks()
            w = max(260, frame.winfo_reqwidth() + 4)
            h = max(60, frame.winfo_reqheight() + 4)
            try:
                rx, ry = self.winfo_rootx(), self.winfo_rooty()
                rw, rh = self.winfo_width(), self.winfo_height()
                x = rx + max(0, rw - w - 24)
                y = ry + max(0, rh - h - 60)
            except Exception:
                x = y = 40
            win.geometry(f"{w}x{h}+{x}+{y}")
            win.after(int(duration), win.destroy)
        except Exception:
            pass

    # ===== helpers busy/status =====
    def _set_ui_busy(self, busy=True, msg=None):
        try:
            self.btn_run.config(state=("disabled" if busy else "normal"))
            # Atualiza o estado do botão Interromper - SEMPRE verifica se há tarefas rodando
            with self.running_lock:
                has_running = bool(self.running_processes)
                # Botão habilitado se busy OU se há tarefas rodando
                should_enable = busy or has_running
                self.btn_stop.config(state="normal" if should_enable else "disabled")
        except Exception as e:
            print(f"Erro ao atualizar estado da UI: {e}")
            
        if busy:
            # Adapta cor ao tema
            dark_mode = getattr(self, 'var_dark', None)
            if dark_mode and dark_mode.get():
                text_color = "#e5e7eb"  # Cinza claro para modo escuro
            else:
                text_color = "#1f2937"  # Cinza escuro para modo claro
            self.status_label.config(text=msg or "Executando...", foreground=text_color)
            self.pbar.start(10)
        else:
            self.pbar.stop()
            # Adapta cor ao tema
            dark_mode = getattr(self, 'var_dark', None)
            if dark_mode and dark_mode.get():
                text_color = "#e5e7eb"  # Cinza claro para modo escuro
            else:
                text_color = "#1f2937"  # Cinza escuro para modo claro
            self.status_label.config(text="Pronto.", foreground=text_color)
        self.update_idletasks()

    def set_status_line(self, text, color=None):
        """Atualiza a barra de status de forma segura (thread-safe).
        
        Args:
            text: Texto a ser exibido
            color: Cor do texto (None para usar cor baseada no tema)
        """
        def update():
            try:
                if self.winfo_exists():
                    # Usa cor baseada no tema se não especificada
                    if color is None:
                        # Verifica se está no modo escuro
                        dark_mode = getattr(self, 'var_dark', None)
                        if dark_mode and dark_mode.get():
                            text_color = "#e5e7eb"  # Cinza claro para modo escuro
                        else:
                            text_color = "#1f2937"  # Cinza escuro para modo claro
                    else:
                        text_color = color
                    
                    self.status_label.config(text=text[:160], foreground=text_color)
                    self.update_idletasks()
            except Exception as e:
                print(f"Erro ao atualizar barra de status: {e}")
        
        # Se estamos na thread principal, atualiza diretamente
        # Caso contrário, agenda para a thread principal
        try:
            if threading.current_thread() is threading.main_thread():
                update()
            else:
                self.after(0, update)
        except Exception:
            # Fallback: sempre tenta agendar
            try:
                self.after(0, update)
            except Exception as e:
                print(f"Erro ao agendar atualização de status: {e}")

    def _hora_dias_text(self, t):
     st = (t.get("schedule_type") or "cron").lower()
     if st == "interval":
        val = t.get("every_value") or 0
        unit = (t.get("every_unit") or "minutes").lower()
        hora = f"cada {val} " + ("min" if unit == "minutes" else "h")
        dias = "—"
     elif st == "start_repeat":
        start = (t.get("sr_start") or t.get("time") or "06:00")
        ev = int(t.get("sr_every_value", 5) or 5)
        unit = (t.get("sr_every_unit") or "minutes").lower()
        cnt = int(t.get("sr_count", 5) or 5)
        times = expand_start_repeat(start, ev, unit, cnt)
        hora = ", ".join(times)
        dias = format_days_bool(t.get("days", [True]*7))
     else:  # cron
        times = t.get("times") or [t.get("time", "06:00")]
        try:
            times = parse_times(",".join(times) if isinstance(times, list) else str(times))
        except Exception:
            times = [t.get("time", "06:00")]
        hora = ", ".join(times)
        dias = format_days_bool(t.get("days", [True]*7))
     return hora, dias


    # ===== tabela & gráfico =====
    def refresh_table(self, full_refresh=True):
        """Atualiza a tabela. full_refresh=False apenas atualiza status."""
        # Salva a seleção atual
        current_selection = self.tree.selection()

        # Se não for refresh completo, apenas atualiza status
        if not full_refresh:
            for task in self.data.get("tasks", []):
                self._update_task_status_color(task["name"])
            return

        # v2025.10.11.8 — atualiza universo de tags no combobox de filtro
        try:
            self._refresh_tag_filter_options()
        except Exception:
            pass

        # filtros ativos
        search_term = ""
        try:
            search_term = (self.search_var.get() or "").strip().lower()
        except Exception:
            pass
        tag_filter = ""
        try:
            tf = (self.tag_filter_var.get() or "").strip()
            if tf and tf != "(todas as tags)":
                tag_filter = tf
        except Exception:
            pass

        # Limpa a tabela
        for i in self.tree.get_children():
            self.tree.delete(i)

        # Preenche com as tarefas (aplicando filtros)
        for i, task in enumerate(self.data.get("tasks", [])):
            task_name = task["name"]
            task_tags = task.get("tags", []) or []

            # filtro por tag
            if tag_filter and tag_filter not in task_tags:
                continue
            # filtro por termo livre (nome, tipo, tags, arquivo)
            if search_term:
                hay = " ".join([
                    task_name.lower(),
                    (task.get("path", "") or "").lower(),
                    (task.get("schedule_type", "") or "").lower(),
                    " ".join(task_tags).lower(),
                ])
                if search_term not in hay:
                    continue

            is_selected = task_name in current_selection

            # Define as tags visuais do item
            tags = ["evenrow" if i % 2 == 0 else "oddrow"]
            if not task.get("enabled", True):
                tags.append("disabled")
            if is_selected:
                tags.append("selected")

            # Texto de status - verifica se está rodando
            with self.running_lock:
                is_running = task_name in self.running_processes

            if is_running:
                status_text = "Rodando"
            elif task.get("enabled", True):
                status_text = "Ativo"
            else:
                status_text = "Parado"

            # Obtém o tipo de agendamento
            schedule_type = task.get("schedule_type", "cron")

            # Formata os horários de acordo com o tipo de agendamento
            if schedule_type == "cron":
                times = ", ".join(task.get("times", [task.get("time", "06:00")]))
            elif schedule_type == "interval":
                every_val = task.get("every_value", 30)
                every_unit = task.get("every_unit", "minutes")
                times = f"A cada {every_val} {every_unit}"
            elif schedule_type == "start_repeat":
                start = task.get("sr_start", "06:00")
                every_val = task.get("sr_every_value", 30)
                every_unit = task.get("sr_every_unit", "minutes")
                times = f"{start} + {every_val}{every_unit[0]}"
            else:
                times = "Desconhecido"

            tags_text = " · ".join(task_tags) if task_tags else "—"

            # Adiciona a tarefa à tabela
            self.tree.insert("", "end", iid=task_name,
                           values=(
                               status_text,
                               task_name,
                               times,
                               schedule_type.capitalize(),
                               self._format_days(task.get("days", [True]*7)),
                               tags_text,
                               task.get("path", "")
                           ),
                           tags=tags)

        # Aplica as cores de status após inserir todos os itens
        for task in self.data.get("tasks", []):
            self._update_task_status_color(task["name"])

        # v2025.10.11.9 — empty state quando 0 jobs
        try:
            self._update_empty_state()
        except Exception:
            pass

        # Ajusta o tamanho das colunas
        self._resize_columns()
        
        # Restaura a seleção
        if current_selection:
            try:
                for item in current_selection:
                    self.tree.selection_add(item)
            except:
                pass

    def _format_days(self, days):
        """Formata os dias da semana para exibição na tabela."""
        if not isinstance(days, (list, tuple)) or len(days) != 7:
            return ""
        
        dias_semana = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
        ativos = []
        
        for i, ativo in enumerate(days[:7]):  # Garante que pegamos no máximo 7 dias
            if ativo:
                ativos.append(dias_semana[i])
                
        return ", ".join(ativos) if ativos else "Nunca"
        
    def _refresh_tag_filter_options(self):
        """Atualiza opções do combobox de tags com o universo atual."""
        try:
            universe = set()
            for t in self.data.get("tasks", []) or []:
                for tg in (t.get("tags") or []):
                    if tg:
                        universe.add(tg)
            # também inclui tags definidas em data["tags"]
            for tg in self.data.get("tags", []) or []:
                if tg:
                    universe.add(tg)
            opts = ["(todas as tags)"] + sorted(universe)
            cur = self.tag_filter_var.get()
            self.tag_filter["values"] = opts
            if cur not in opts:
                self.tag_filter_var.set("(todas as tags)")
        except Exception:
            pass

    def _resize_columns(self):
        """Ajusta automaticamente o tamanho das colunas da tabela."""
        # Configuração de largura fixa para a coluna Status
        self.tree.column("#0", width=0, stretch=tk.NO)
        self.tree.column("Status", width=90, minwidth=90, stretch=tk.NO, anchor="center")

        # Configuração de largura para as outras colunas
        col_widths = {
            "Nome": 180,
            "Horário": 130,
            "Tipo": 100,
            "Dias": 150,
            "Tags": 160,
            "Arquivo": 300
        }

        # Ajusta o tamanho das colunas baseado no conteúdo
        for col, width in col_widths.items():
            try:
                self.tree.column(col, width=width, minwidth=width, stretch=tk.YES, anchor="w")
            except Exception:
                pass
        
        # Ajusta o cabeçalho para manter o alinhamento
        for col in self.tree["columns"]:
            self.tree.heading(col, text=col, anchor="w")
        
        # Ajusta a coluna Status para centralizar o conteúdo
        self.tree.heading("Status", text="Status", anchor="center")

    def _on_tree_select(self, event):
        # Atualiza o botão de ativar/desativar
        self.update_toggle_button()
        
        # Atualiza o gráfico com base na primeira seleção
        sel = self.tree.selection()
        if sel:
            self.draw_chart()
            
        # Força a atualização do estilo dos itens selecionados
        self._update_selection_styles()
    
    def _update_selection_styles(self):
        """Atualiza os estilos dos itens selecionados"""
        # Primeiro, remove a tag 'selected' de todos os itens
        for item in self.tree.get_children():
            tags = list(self.tree.item(item, 'tags'))
            if 'selected' in tags:
                tags.remove('selected')
                self.tree.item(item, tags=tags)
        
        # Adiciona a tag 'selected' aos itens selecionados
        for item in self.tree.selection():
            tags = list(self.tree.item(item, 'tags'))
            if 'selected' not in tags:
                tags.append('selected')
                self.tree.item(item, tags=tags)

    def draw_chart(self, force=False):
        """Desenha gráfico moderno com animações suaves e debounce."""
        # Debounce: evita múltiplos redesenhos em sequência
        if not force and hasattr(self, '_chart_redraw_scheduled'):
            return
        
        def _do_draw():
            try:
                if hasattr(self, '_chart_redraw_scheduled'):
                    delattr(self, '_chart_redraw_scheduled')
                self._draw_chart_impl()
            except Exception:
                pass
        
        self._chart_redraw_scheduled = True
        self.after(100, _do_draw)  # Aguarda 100ms antes de redesenhar
    
    def _draw_chart_impl(self):
        """Implementação real do desenho do gráfico."""
        self.canvas.delete("all")
        dark = bool(self.var_dark.get())
        
        # Usa cores do tema se disponível
        if hasattr(self, '_theme_colors'):
            colors = self._theme_colors
            bg_color = colors['bg']
            grid_color = colors['overlay']
            text_color = colors['text']
            subtext_color = colors['subtext']
            success_color = colors['success']
            error_color = colors['error']
            accent_color = colors['accent']
        else:
            # Cores padrão melhoradas
            bg_color = "#1a1a1a" if dark else "#ffffff"
            grid_color = "#404040" if dark else "#e0e0e0"
            text_color = "#ffffff" if dark else "#000000"
            subtext_color = "#b0b0b0" if dark else "#666666"
            success_color = "#4ade80" if dark else "#22c55e"
            error_color = "#f87171" if dark else "#ef4444"
            accent_color = "#60a5fa" if dark else "#3b82f6"
        
        self.canvas.configure(bg=bg_color)

        sel = self.tree.selection()
        if not sel:
            # v2025.10.11.8 — Dashboard global dos últimos 7 dias
            self._draw_global_dashboard(
                bg_color, grid_color, text_color, subtext_color,
                success_color, error_color, accent_color
            )
            return

        name = sel[0]
        hist = self.data.get("history", {}).get(name, [])
        if not hist:
            # Mensagem estilizada quando não há histórico
            w = int(self.canvas.winfo_width() or 400)
            h = int(self.canvas.winfo_height() or 300)
            
            self.canvas.create_text(
                w//2, h//2 - 20, anchor="center",
                fill=subtext_color,
                font=("Segoe UI", 12),
                text="📈 Sem dados ainda"
            )
            self.canvas.create_text(
                w//2, h//2 + 10, anchor="center",
                fill=subtext_color,
                font=("Segoe UI", 10),
                text="Execute a tarefa para ver o histórico aqui"
            )
            return

        items = hist[-30:]
        w = int(self.canvas.winfo_width() or 400)
        h = int(self.canvas.winfo_height() or 300)
        pad = 28
        H1 = int(h * 0.62)
        chart_w = w - 2 * pad

        # Gráfico de duração modernizado
        N = len(items)
        chart_h = H1 - pad
        max_dur = max(1.0, max(i["dur"] for i in items))
        bar_w = max(3, int(chart_w / max(N, 1) * 0.8))  # Barras um pouco mais largas
        
        # Título do gráfico com estilo moderno
        self.canvas.create_text(
            pad, 12, anchor="nw",
            text=f"⏱️ Tempo de Execução — últimas {N} execuções",
            fill=text_color,
            font=("Segoe UI", 10, "bold")
        )
        
        # Grid moderno com linhas mais sutis
        self.canvas.create_line(pad, H1 - 10, w - pad, H1 - 10, fill=grid_color, width=2)
        for k in (0.25, 0.5, 0.75):
            y = (H1 - 10) - k * chart_h
            self.canvas.create_line(pad, y, w - pad, y, fill=grid_color, width=1, dash=(2, 4))

        # Barras com bordas arredondadas (simuladas)
        for idx, it in enumerate(items):
            x_center = pad + (idx + 0.5) * (chart_w / N)
            bh = max(4, (it["dur"] / max_dur) * (chart_h - 6))  # Altura mínima
            y0 = (H1 - 10) - bh
            
            # Cor baseada no resultado
            # -999 = interrompido (amarelo), 0 = sucesso (verde), outros = erro (vermelho)
            if it["rc"] == -999:
                color = "#fbbf24" if dark else "#f59e0b"  # Amarelo para interrompido
            elif it["rc"] == 0:
                color = success_color
            else:
                color = error_color
            
            # Barra principal
            self.canvas.create_rectangle(
                x_center - bar_w / 2, y0,
                x_center + bar_w / 2, H1 - 10,
                fill=color, outline="", width=0
            )
            
            # Efeito de brilho no topo (simulando material design)
            if bh > 8:
                highlight_h = min(4, bh * 0.3)
                highlight_color = self._lighten_color(color, 0.3)
                self.canvas.create_rectangle(
                    x_center - bar_w / 2, y0,
                    x_center + bar_w / 2, y0 + highlight_h,
                    fill=highlight_color, outline=""
                )

        # Gráfico de barras horizontal moderno (Sucesso vs Interrompido vs Falha)
        ok = sum(1 for i in items if i["rc"] == 0)
        interrupted = sum(1 for i in items if i["rc"] == -999)
        fail = N - ok - interrupted
        total = max(1, N)
        y_top = H1 + 15
        
        self.canvas.create_text(
            pad, y_top, anchor="nw",
            text=f"📊 Resultados — {ok} OK | {interrupted} Interrompidas | {fail} Falhas",
            fill=text_color,
            font=("Segoe UI", 10, "bold")
        )
        
        y_bar = y_top + 25
        bar_h = max(20, h - y_bar - 40)  # Deixa 40px para a legenda (antes era 20)
        full_w = w - 2 * pad
        ok_w = int(full_w * (ok / total))
        interrupted_w = int(full_w * (interrupted / total))
        fail_w = full_w - ok_w - interrupted_w
        
        # Fundo da barra
        self.canvas.create_rectangle(pad, y_bar, pad + full_w, y_bar + bar_h, 
                                   fill=grid_color, outline="")
        
        # Barra de sucesso
        if ok_w > 0:
            self.canvas.create_rectangle(pad, y_bar, pad + ok_w, y_bar + bar_h, 
                                       fill=success_color, outline="")
        
        # Barra de interrompidas
        if interrupted_w > 0:
            interrupted_color = "#fbbf24" if dark else "#f59e0b"
            self.canvas.create_rectangle(pad + ok_w, y_bar, pad + ok_w + interrupted_w, y_bar + bar_h, 
                                       fill=interrupted_color, outline="")
        
        # Barra de falha
        if fail_w > 0:
            self.canvas.create_rectangle(pad + ok_w + interrupted_w, y_bar, 
                                       pad + ok_w + interrupted_w + fail_w, y_bar + bar_h, 
                                       fill=error_color, outline="")
        
        # Texto sobre as barras
        if ok > 0 and ok_w > 30:
            self.canvas.create_text(pad + ok_w//2, y_bar + bar_h//2, 
                                  text=f"{ok} OK", anchor="center", 
                                  fill="white", font=("Segoe UI", 9, "bold"))
        if interrupted > 0 and interrupted_w > 30:
            self.canvas.create_text(pad + ok_w + interrupted_w//2, y_bar + bar_h//2, 
                                  text=f"{interrupted} Int.", anchor="center", 
                                  fill="white", font=("Segoe UI", 9, "bold"))
        if fail > 0 and fail_w > 30:
            self.canvas.create_text(pad + ok_w + interrupted_w + fail_w//2, y_bar + bar_h//2, 
                                  text=f"{fail} Falhas", anchor="center", 
                                  fill="white", font=("Segoe UI", 9, "bold"))
        
        # Legenda compacta abaixo do gráfico de barras
        legend_y = y_bar + bar_h + 10
        interrupted_color = "#fbbf24" if dark else "#f59e0b"
        legend_x_start = pad
        
        # Sucesso
        self.canvas.create_oval(legend_x_start, legend_y, legend_x_start + 10, legend_y + 10, 
                               fill=success_color, outline="")
        self.canvas.create_text(legend_x_start + 14, legend_y + 5, text="✓ Sucesso", 
                               anchor="w", fill=text_color, font=("Segoe UI", 8))
        
        # Interrompido
        legend_x_start += 80
        self.canvas.create_oval(legend_x_start, legend_y, legend_x_start + 10, legend_y + 10, 
                               fill=interrupted_color, outline="")
        self.canvas.create_text(legend_x_start + 14, legend_y + 5, text="⏸ Interrompido", 
                               anchor="w", fill=text_color, font=("Segoe UI", 8))
        
        # Erro
        legend_x_start += 100
        self.canvas.create_oval(legend_x_start, legend_y, legend_x_start + 10, legend_y + 10, 
                               fill=error_color, outline="")
        self.canvas.create_text(legend_x_start + 14, legend_y + 5, text="✗ Falha", 
                               anchor="w", fill=text_color, font=("Segoe UI", 8))
    
    def _draw_global_dashboard(self, bg_color, grid_color, text_color, subtext_color,
                               success_color, error_color, accent_color):
        """v2025.10.11.8 — Dashboard com agregados dos últimos 7 dias."""
        w = int(self.canvas.winfo_width() or 600)
        h = int(self.canvas.winfo_height() or 400)
        pad = 28

        # Agrega histórico dos últimos 7 dias para todos os jobs
        today = date.today()
        days = [today - timedelta(days=i) for i in range(6, -1, -1)]  # 7 dias, do mais antigo ao mais recente
        ok_per_day = [0]*7
        fail_per_day = [0]*7
        interrupted_per_day = [0]*7
        all_durations = []
        per_task_avg_dur = {}
        per_task_runs = {}
        total_ok = 0
        total_fail = 0
        total_interrupted = 0
        total_today = 0

        history = self.data.get("history", {}) or {}
        for task_name, hist in history.items():
            for e in hist:
                try:
                    ts_str = e.get("ts", "")
                    dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    d = dt.date()
                except Exception:
                    continue
                rc = int(e.get("rc", -1))
                dur = float(e.get("dur", 0))
                if d == today:
                    total_today += 1
                # bucket por dia
                for idx, dd in enumerate(days):
                    if d == dd:
                        if rc == 0:
                            ok_per_day[idx] += 1
                            total_ok += 1
                        elif rc == -999:
                            interrupted_per_day[idx] += 1
                            total_interrupted += 1
                        else:
                            fail_per_day[idx] += 1
                            total_fail += 1
                        # média e contagem para top slow
                        if rc == 0 and dur > 0:
                            per_task_avg_dur[task_name] = (per_task_avg_dur.get(task_name, 0) * per_task_runs.get(task_name, 0) + dur) / (per_task_runs.get(task_name, 0) + 1)
                            per_task_runs[task_name] = per_task_runs.get(task_name, 0) + 1
                            all_durations.append(dur)
                        break

        # Title
        self.canvas.create_text(
            pad, 12, anchor="nw",
            text="📊 Dashboard — últimos 7 dias",
            fill=text_color, font=("Segoe UI", 11, "bold"),
        )

        # Stat tiles (4 colunas)
        tiles_y = 36
        tiles_h = 56
        gap = 8
        tile_w = max(80, (w - 2*pad - 3*gap) // 4)
        total_exec = total_ok + total_fail + total_interrupted
        success_rate = (total_ok / total_exec * 100.0) if total_exec else 0.0
        tiles = [
            ("Execuções (7d)", str(total_exec), accent_color),
            ("Hoje", str(total_today), accent_color),
            ("Taxa sucesso", f"{success_rate:.0f}%", success_color if success_rate >= 80 else error_color),
            ("Falhas (7d)", str(total_fail), error_color if total_fail else success_color),
        ]
        for i, (label, value, color) in enumerate(tiles):
            x0 = pad + i * (tile_w + gap)
            x1 = x0 + tile_w
            self.canvas.create_rectangle(x0, tiles_y, x1, tiles_y + tiles_h,
                                         fill=grid_color, outline="")
            self.canvas.create_text(x0 + 10, tiles_y + 10, anchor="nw",
                                    text=label, fill=subtext_color,
                                    font=("Segoe UI", 9))
            self.canvas.create_text(x0 + 10, tiles_y + 28, anchor="nw",
                                    text=value, fill=color,
                                    font=("Segoe UI", 16, "bold"))

        # Barras empilhadas por dia
        chart_top = tiles_y + tiles_h + 24
        chart_bottom = h - 60
        chart_h = max(60, chart_bottom - chart_top)
        chart_w = w - 2 * pad
        self.canvas.create_text(pad, chart_top - 18, anchor="nw",
                                text="Execuções por dia (verde=OK · amarelo=interrompido · vermelho=falha)",
                                fill=subtext_color, font=("Segoe UI", 9))

        # eixo
        self.canvas.create_line(pad, chart_bottom, w - pad, chart_bottom,
                                fill=grid_color, width=2)

        max_y = max(1, max(ok_per_day[i] + fail_per_day[i] + interrupted_per_day[i] for i in range(7)))
        # gridlines a cada 25%
        for k in (0.25, 0.5, 0.75, 1.0):
            y = chart_bottom - k * chart_h
            self.canvas.create_line(pad, y, w - pad, y,
                                    fill=grid_color, width=1, dash=(2, 4))
            self.canvas.create_text(pad - 4, y, anchor="e",
                                    text=str(int(max_y * k)),
                                    fill=subtext_color, font=("Segoe UI", 8))

        col_w = chart_w / 7
        bar_w = max(8, col_w * 0.6)
        interrupted_color = "#fbbf24"
        for idx in range(7):
            x_center = pad + (idx + 0.5) * col_w
            ok_n = ok_per_day[idx]
            int_n = interrupted_per_day[idx]
            fail_n = fail_per_day[idx]
            total_n = ok_n + int_n + fail_n
            if total_n == 0:
                # marker vazio
                self.canvas.create_oval(x_center - 2, chart_bottom - 2,
                                        x_center + 2, chart_bottom + 2,
                                        fill=grid_color, outline="")
            else:
                # empilhado: ok no fundo, depois interrupted, falha no topo
                cum = 0.0
                for n, c in [(ok_n, success_color),
                             (int_n, interrupted_color),
                             (fail_n, error_color)]:
                    if n == 0:
                        continue
                    seg_h = (n / max_y) * chart_h
                    y_top = chart_bottom - cum - seg_h
                    y_bot = chart_bottom - cum
                    self.canvas.create_rectangle(
                        x_center - bar_w/2, y_top,
                        x_center + bar_w/2, y_bot,
                        fill=c, outline="")
                    cum += seg_h
                # número total acima da barra
                self.canvas.create_text(x_center, chart_bottom - cum - 8,
                                        text=str(total_n), anchor="s",
                                        fill=text_color, font=("Segoe UI", 8, "bold"))
            # rótulo do dia
            dia = days[idx]
            label = dia.strftime("%a %d/%m").capitalize()
            if dia == today:
                label = "HOJE"
            self.canvas.create_text(x_center, chart_bottom + 6,
                                    text=label, anchor="n",
                                    fill=subtext_color, font=("Segoe UI", 8))

        # Top 3 jobs lentos
        if per_task_avg_dur:
            slow = sorted(per_task_avg_dur.items(), key=lambda kv: kv[1], reverse=True)[:3]
            y_slow = h - 30
            text = "⏱ Top 3 lentos (7d): " + " · ".join(
                f"{n} ({d:.1f}s)" for n, d in slow
            )
            self.canvas.create_text(pad, y_slow, anchor="nw",
                                    text=text, fill=subtext_color,
                                    font=("Segoe UI", 9))
        else:
            self.canvas.create_text(pad, h - 30, anchor="nw",
                                    text="Selecione uma tarefa para ver detalhes individuais.",
                                    fill=subtext_color, font=("Segoe UI", 9, "italic"))

    def _lighten_color(self, color, factor):
        """Clareia ou escurece uma cor hexadecimal por um fator (-1.0 a 1.0)"""
        try:
            # Remove o # se presente
            color = color.lstrip('#')
            # Converte para RGB
            r, g, b = tuple(int(color[i:i+2], 16) for i in (0, 2, 4))
            
            if factor >= 0:
                # Clareia (mistura com branco)
                r = min(255, int(r + (255 - r) * factor))
                g = min(255, int(g + (255 - g) * factor))
                b = min(255, int(b + (255 - b) * factor))
            else:
                # Escurece (mistura com preto)
                factor = abs(factor)
                r = max(0, int(r * (1 - factor)))
                g = max(0, int(g * (1 - factor)))
                b = max(0, int(b * (1 - factor)))
            
            # Converte de volta para hex
            return f"#{r:02x}{g:02x}{b:02x}"
        except:
            return color  # Retorna a cor original se houver erro

    # ===== ações =====
    def _existing_job_names(self):
        return [t["name"] for t in self.data.get("tasks", [])]

    def _all_known_tags(self):
        tags = set()
        for t in self.data.get("tasks", []):
            for tg in t.get("tags", []) or []:
                tags.add(tg)
        return sorted(tags)

    def add_task(self):
        dlg = TaskDialog(self,
                         existing_jobs=self._existing_job_names(),
                         all_tags=self._all_known_tags())
        self.wait_window(dlg)
        if dlg.result:
            if any(t["name"] == dlg.result["name"] for t in self.data["tasks"]):
                messagebox.showerror("Erro", "Já existe uma tarefa com esse nome."); return
            # Garante que novas tarefas sejam criadas ativadas por padrão
            dlg.result.setdefault("enabled", True)
            self.data["tasks"].append(dlg.result)
            self.save(silent=True); self.refresh_table(); self.reschedule_all()

    def open_assistant(self):
        dlg = AssistantDialog(self); self.wait_window(dlg)
        if not dlg.result: return
        td = TaskDialog(self, dlg.result,
                        existing_jobs=self._existing_job_names(),
                        all_tags=self._all_known_tags())
        self.wait_window(td)
        if td.result:
            if any(t["name"] == td.result["name"] for t in self.data["tasks"]):
                messagebox.showerror("Erro", "Já existe uma tarefa com esse nome."); return
            # Garante que novas tarefas sejam criadas ativadas por padrão
            td.result.setdefault("enabled", True)
            self.data["tasks"].append(td.result)
            self.save(silent=True); self.refresh_table(); self.reschedule_all()

    def edit_task(self):
        sel = self.tree.selection()
        if not sel: return
        name = sel[0]
        task = next((t for t in self.data["tasks"] if t["name"]==name), None)
        if not task: return
        dlg = TaskDialog(self, task,
                         existing_jobs=self._existing_job_names(),
                         all_tags=self._all_known_tags())
        self.wait_window(dlg)
        if dlg.result:
            idx = self.data["tasks"].index(task)
            self.data["tasks"][idx] = dlg.result
            self.save(silent=True); self.refresh_table(); self.reschedule_all()

    def remove_task(self):
     sel = self.tree.selection()
     if not sel:
        return
     name = sel[0]
     if not messagebox.askyesno("Confirmar", f"Remover a tarefa '{name}'?"):
        return

     # remove do JSON
     self.data["tasks"] = [t for t in self.data["tasks"] if t["name"] != name]

     # remove TODOS os jobs agendados dessa tarefa (um por horário)
     ids = self.jobs.get(name, [])
     for jid in ids:
        try:
            self.scheduler.remove_job(jid)
        except Exception:
            pass
     self.jobs.pop(name, None)

     self.save(silent=True)
     self.refresh_table()

    def toggle_task_status(self):
        """Ativa ou desativa a tarefa selecionada"""
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("Aviso", "Selecione uma tarefa para ativar/desativar.")
            return
        
        name = sel[0]
        task = next((t for t in self.data["tasks"] if t["name"] == name), None)
        if not task:
            return
        
        # Alterna o status
        current_status = task.get("enabled", True)
        task["enabled"] = not current_status
        
        # Atualiza interface
        action = "desativada" if current_status else "ativada"
        self.set_status_line(f"Tarefa '{name}' {action}.")
        
        # Reagenda todas as tarefas (para aplicar mudança)
        self.save(silent=True)
        self.refresh_table()
        self.reschedule_all()
        self.update_toggle_button()

    def update_toggle_button(self):
        """Atualiza o texto do botão de ativar/desativar baseado na seleção"""
        sel = self.tree.selection()
        if not sel:
            self.btn_toggle.config(text="⏸️ Desativar", state="disabled")
            return
        
        name = sel[0]
        task = next((t for t in self.data["tasks"] if t["name"] == name), None)
        if not task:
            self.btn_toggle.config(text="⏸️ Desativar", state="disabled")
            return
        
        is_enabled = task.get("enabled", True)
        if is_enabled:
            self.btn_toggle.config(text="⏸️ Desativar", state="normal")
        else:
            self.btn_toggle.config(text="▶️ Ativar", state="normal")

    def open_last_log(self):
        sel = self.tree.selection()
        if not sel: return
        name = sel[0]
        files = sorted(LOG_DIR.glob(f"{name}_*.log"))
        if not files:
            messagebox.showinfo("Logs", "Sem logs desta tarefa ainda."); return
        os.startfile(files[-1])

    def run_now(self, task_name=None):
        """Executa uma tarefa imediatamente."""
        try:
            if task_name is None:
                # Se nenhum nome for fornecido, verifica a seleção na árvore
                selected = self.tree.selection()
                if not selected:
                    messagebox.showwarning("Aviso", "Nenhuma tarefa selecionada.")
                    return
                
                # Obtém os nomes das tarefas selecionadas
                task_names = [self.tree.item(i, 'values')[1] for i in selected]
                
                # Executa as tarefas selecionadas
                if len(task_names) > 1:
                    print(f"Executando tarefas selecionadas: {', '.join(task_names)}")
                    self.run_multiple_tasks(task_names)
                else:
                    # Executa uma única tarefa diretamente
                    print(f"Executando tarefa: {task_names[0]}")
                    self._run_single_task(task_names[0])
            else:
                # Executa uma única tarefa pelo nome
                print(f"Executando tarefa: {task_name}")
                self._run_single_task(task_name)
                
        except Exception as e:
            print(f"Erro ao executar tarefa: {e}")
            self.after(0, lambda: messagebox.showerror("Erro", f"Falha ao executar a tarefa: {e}"))
    
    def _update_stop_button_state(self):
        """Atualiza o estado do botão Interromper com base nas tarefas em execução."""
        try:
            with self.running_lock:
                # O botão deve estar habilitado se houver tarefas em execução
                # independente de quais tarefas estão selecionadas
                has_running = bool(self.running_processes)
                
                # Atualiza o estado do botão - SEMPRE habilitado se há tarefas rodando
                new_state = "normal" if has_running else "disabled"
                current_state = str(self.btn_stop['state'])
                
                # Só atualiza se o estado mudou
                if current_state != new_state:
                    self.btn_stop.config(state=new_state)
                    print(f"Botão Interromper: {current_state} -> {new_state} (tarefas rodando: {len(self.running_processes)})")
                
        except Exception as e:
            print(f"Erro ao atualizar estado do botão de parada: {e}")
            # Em caso de erro, tenta habilitar se houver processos
            try:
                with self.running_lock:
                    if self.running_processes:
                        self.btn_stop.config(state="normal")
            except Exception:
                pass

    def run_multiple_tasks(self, task_names):
        """Executa múltiplas tarefas em paralelo."""
        try:
            if not task_names:
                return

            # Filtra apenas tarefas que existem
            valid_tasks = [name for name in task_names 
                          if name in {t["name"] for t in self.data["tasks"]}]
            
            if not valid_tasks:
                self.after(0, lambda: messagebox.showinfo("Aviso", "Nenhuma tarefa válida para executar."))
                return
            
            # Atualiza a interface para mostrar que as tarefas estão sendo executadas
            self._set_ui_busy(True, f"Executando {len(valid_tasks)} tarefa(s)...")
            
            # Habilita o botão de parada
            self.after(0, lambda: self.btn_stop.config(state="normal"))
            
            # Cria uma thread para cada tarefa
            threads = []
            for task_name in valid_tasks:
                thread = threading.Thread(
                    target=self._run_single_task,
                    args=(task_name,),
                    daemon=True
                )
                threads.append(thread)
                thread.start()
            
            # Inicia uma thread para monitorar o término das tarefas
            monitor_thread = threading.Thread(
                target=self._monitor_tasks_completion,
                args=(threads, valid_tasks),
                daemon=True
            )
            monitor_thread.start()
            
        except Exception as e:
            print(f"Erro ao executar tarefas: {e}")
            self.after(0, lambda: messagebox.showerror("Erro", f"Falha ao executar as tarefas: {e}"))
            self._set_ui_busy(False, "Erro ao executar tarefas")

    def _run_single_task(self, task_name):
        task = next((t for t in self.data["tasks"] if t["name"] == task_name), None)
        if not task:
            return
        
        self._set_ui_busy(True, f"Executando '{task['name']}'...")
        
        # Atualiza o status visual para "Rodando"
        self.after(0, lambda: self._set_task_running(task_name, True))
        
        def worker(task=task):
            def progress(line):
                self.after(0, lambda: self.set_status_line(f"[{task['name']}] {line}"))
            
            rc, dur, log_path = run_task(task, self.data["settings"], 
                                     progress_cb=progress,
                                     process_callback=self._register_process)
            append_history(self.data, task["name"], rc, dur)
            self._maybe_notify(task, rc, log_path)
            
            def finish():
                # Atualiza o status visual para "Ativo"
                self._set_task_running(task_name, False)
                self._set_ui_busy(False)
                self.draw_chart()
                msg = "SUCESSO" if rc == 0 else f"FALHA (RC={rc})"
                messagebox.showinfo(
                    "Execução", 
                    f"{task['name']}: {msg}\n\nDuração: {dur:.1f}s\nLog:\n{log_path}"
                )
            
            self.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def _update_task_status(self, task_name, success):
        """Atualiza o status de uma tarefa na interface.
        
        Args:
            task_name: Nome da tarefa
            success: Booleano indicando se a tarefa foi bem-sucedida
        """
        # Remove a tarefa dos processos em execução
        with self.running_lock:
            if task_name in self.running_processes:
                del self.running_processes[task_name]
        
        # Atualiza o status na árvore de tarefas
        for item in self.tree.get_children():
            if self.tree.item(item, 'values')[1] == task_name:  # Índice 1 é a coluna 'Nome'
                # Atualiza o status (índice 0 é a coluna 'Status')
                values = list(self.tree.item(item, 'values'))
                values[0] = '✅' if success else '❌'
                self.tree.item(item, values=values)
                break
        
        # Atualiza o estado do botão de parada
        self.after(0, self._update_stop_button_state)
                
    def _update_ui_after_completion(self, success_count, failed_count):
        """Atualiza a interface após a conclusão de todas as tarefas.
        
        Args:
            success_count: Número de tarefas concluídas com sucesso
            failed_count: Número de tarefas que falharam
        """
        self._set_ui_busy(False)
        self.refresh_table()
        
        # Atualiza o botão de parada
        self.after(0, self._update_stop_button_state)
        
        # Prepara a mensagem de status
        total = success_count + failed_count
        if total > 0:
            if success_count > 0 and failed_count > 0:
                msg = f"{success_count} tarefa(s) concluída(s) com sucesso.\n{failed_count} tarefa(s) falharam."
                messagebox.showinfo("Execução concluída", msg)
            elif success_count > 0:
                msg = f"Todas as {success_count} tarefa(s) foram concluídas com sucesso!"
                messagebox.showinfo("Sucesso", msg)
            else:
                msg = f"Todas as {failed_count} tarefa(s) falharam."
                messagebox.showerror("Falha", msg)
            
            # Atualiza a barra de status
            status_msg = f"✅ {success_count} concluídas | ❌ {failed_count} falhas | Total: {total}"
            self.set_status_line(status_msg)
        else:
            # Caso não haja tarefas para processar
            self.set_status_line("Nenhuma tarefa para executar.")
            
        # Garante que o botão de parada esteja desativado
        with self.running_lock:
            if not self.running_processes:
                self.after(0, lambda: self.btn_stop.config(state="disabled"))

    def _monitor_tasks_completion(self, threads, task_names):
        total = len(task_names)
        completed = 0
        success_count = 0
        failed_count = 0
        completion_shown = False
        
        # Função para atualizar o estado da tarefa de forma thread-safe
        def task_completed(name, success):
            nonlocal completed, success_count, failed_count, completion_shown
            with threading.Lock():
                completed += 1
                if success:
                    success_count += 1
                else:
                    failed_count += 1
                
                # Atualiza a UI na thread principal
                self.after(0, lambda: self._update_task_status(name, success))
                
                # Atualiza a barra de progresso e status
                progress = int((completed / total) * 100)
                status = f"Concluído: {completed}/{total} | Sucesso: {success_count} | Falhas: {failed_count}"
                self.after(0, lambda: self.set_status_line(status))
                
                # Atualiza a barra de progresso se existir
                if hasattr(self, 'progress') and self.progress.winfo_exists():
                    self.after(0, lambda: self.progress.config(value=progress))
                
                # Se todas as tarefas foram concluídas
                if completed >= total and not completion_shown:
                    completion_shown = True
                    self.after(0, self._update_ui_after_completion, success_count, failed_count)
        
        # Inicia cada tarefa em uma thread separada
        for task_name in task_names:
            task = next((t for t in self.data["tasks"] if t["name"] == task_name), None)
            if task:
                def worker(task=task):
                    try:
                        rc, dur, log_path = run_task(task, self.data["settings"],
                                             process_callback=self._register_process)
                        append_history(self.data, task["name"], rc, dur)
                        self._maybe_notify(task, rc, log_path)
                        task_completed(task["name"], rc == 0)
                    except Exception as e:
                        print(f"Erro ao executar tarefa {task['name']}: {e}")
                        task_completed(task["name"], False)
                
                threading.Thread(target=worker, daemon=True).start()
            else:
                print(f"Tarefa não encontrada: {task_name}")
                task_completed(task_name, False)
            
            # Garante que o botão Interromper esteja ativado
            self.after(100, self._update_stop_button_state)

    def open_settings(self):
        dlg = SettingsDialog(
    self,
    self.data["settings"],
    on_check_updates=self.check_updates_now,
    current_version=APP_VERSION,
); self.wait_window(dlg)
        if dlg.result:
            self.data["settings"] = dlg.result
            self.save(silent=True); self.reschedule_all()
            self.update_status_indicators()

    def simulate_error(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("Simular erro", "Selecione uma tarefa na lista.")
            return
        task_name = sel[0]
        task = next((t for t in self.data["tasks"] if t["name"] == task_name), None)
        if not task:
            messagebox.showerror("Simular erro", "Tarefa não encontrada.")
            return
        ensure_dirs()
        fake_log = LOG_DIR / f"{task_name}_SIMULADO.log"
        try:
            fake_log.write_text(f"# Simulação de erro @ {now_str()}\n", encoding="utf-8")
        except Exception:
            pass
        self._set_ui_busy(True, "Simulando falha e enviando notificações...")

        def worker():
            errs = []
            try:
                append_history(self.data, task_name, rc=1, dur=0.0)
            except Exception as e:
                errs.append(f"Histórico: {e}")
            subject = f"[{task_name}] FALHA (RC=1) – Simulada"
            body = f"Tarefa: {task_name}\nData: {now_str()}\nRC: 1 (Simulação)\nLog: {fake_log}"
            try: send_email(self.data["settings"], subject, body)
            except Exception as e: errs.append(f"E-mail: {e}")
            try: send_whatsapp(self.data["settings"], subject, body)
            except Exception as e: errs.append(f"WhatsApp: {e}")

            def finish():
                self._set_ui_busy(False); self.draw_chart()
                if errs:
                    messagebox.showwarning("Simular erro",
                                           "Falha simulada. Algumas notificações falharam:\n- " + "\n- ".join(errs))
                else:
                    messagebox.showinfo("Simular erro","Falha simulada. Notificações enviadas (se configuradas).")
            self.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    # ===== status =====
    def _status_color(self, ok: bool):
        return "#3cb371" if ok else "#dc143c"

    def update_status_indicators(self):
        s = self.data.get("settings", {})
        em = s.get("email", {})
        em_ok = bool(
            em.get("enabled")
            and em.get("smtp_host")
            and em.get("smtp_port")
            and (em.get("from_email") or em.get("username"))
            and em.get("to_emails")
        )
        self.lbl_mail.config(foreground=self._status_color(em_ok))
        wa = s.get("whatsapp", {})
        wa_ok = bool(
            wa.get("enabled")
            and os.path.exists(wa.get("node_path","") or "")
            and os.path.exists(wa.get("webjs_script","") or "")
            and wa.get("to_targets")
        )
        self.lbl_wa.config(foreground=self._status_color(wa_ok))

if __name__ == "__main__":
    # ── Modo pre-extract ─────────────────────────────────────────────────────
    # Acionado pelo updater VBS logo após copiar o novo exe.
    # O bootloader PyInstaller já extraiu TODOS os DLLs antes de chegar aqui.
    # Dormimos para o AV terminar de escanear os DLLs extraídos e adicioná-los
    # ao seu cache por hash.  O updater então mata este processo com /F (sem
    # cleanup do bootloader → _MEI persiste) e em seguida abre o exe de verdade.
    # Como o AV já confia nos hashes, a segunda extração passa sem erro.
    if "--pre-extract" in sys.argv:
        import time as _t
        _t.sleep(8)   # 8 s ≥ tempo médio de scan do Windows Defender
        sys.exit(0)
    # ─────────────────────────────────────────────────────────────────────────

    # v2025.10.11.8 — Rollback automático em crash 3x consecutivo
    # Incrementamos o contador AGORA; se exceder o threshold, fazemos rollback
    # antes de tentar instanciar o App() de novo.
    try:
        _count, _should_rollback = startup_increment_and_check()
        if _should_rollback and _is_frozen():
            try:
                ok, msg = try_rollback_exe()
                # log do que aconteceu
                try:
                    crash_log = APP_DIR / "logs" / f"rollback_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
                    crash_log.parent.mkdir(parents=True, exist_ok=True)
                    crash_log.write_text(
                        f"[{now_str()}] crash_counter={_count} -> rollback={ok}\n{msg}\n",
                        encoding="utf-8")
                except Exception:
                    pass
                # zera para evitar loop
                _write_crash_counter(0)
                if ok:
                    sys.exit(0)
            except Exception as _e:
                print(f"[rollback] falhou: {_e}")
    except Exception:
        pass

    app = App()
    try:
        app.mainloop()
    except KeyboardInterrupt:
        # Encerra de forma amigável se o usuário der Ctrl+C
        try:
            app.scheduler.shutdown(wait=False)
        except Exception:
            pass
        try:
            app.destroy()
        except Exception:
            pass
