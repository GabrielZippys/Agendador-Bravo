# agendador_pro.py

import os, sys, json, subprocess, traceback, time, smtplib, ssl, threading, shlex
from email.mime.text import MIMEText
from email.utils import formatdate
from datetime import datetime
from pathlib import Path
import re
from tkinter import ttk, filedialog, messagebox, simpledialog


# --- AUTOUPDATE (com aviso na UI) -------------------------------------------
import urllib.request, hashlib, tempfile

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


APP_VERSION = "2025.10.11.2"   # << aumente em cada build
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


def _write_update_scripts(pid: int, src_new: Path, dst_exe: Path, sha256_hex: str = "") -> Path:
    """
    Cria um .cmd + wrapper .vbs para:
      1) matar AgendadorBravo.exe por nome (mata bootloader PyInstaller e child)
      2) limpar pastas _MEI antigas em %TEMP%
      3) copiar o exe novo por cima
      4) validar SHA256 pós-copy; se divergir, restaura backup
      5) iniciar o novo exe
      6) auto-deletar temporários

    Retorna o caminho do .vbs (rodar com wscript = 100% invisível, sem janela preta).
    """
    exe_name = dst_exe.name
    backup = dst_exe.with_suffix(".exe.bak")
    log = Path(tempfile.gettempdir()) / f"agendador_update_{pid}.log"

    # Gera .cmd — usa encoding do Windows (cp850/mbcs) pra evitar bugs com acentos
    sha_line = ""
    if sha256_hex:
        # certutil -hashfile saída contém cabeçalho/rodapé; filtramos com findstr
        sha_line = f'''
rem --- validar SHA256 pos-copy ---
for /f "skip=1 tokens=*" %%H in ('certutil -hashfile "%DST%" SHA256 ^| findstr /v "hash CertUtil"') do (
  set "GOT=%%H"
  goto :gotsha
)
:gotsha
set "GOT=%GOT: =%"
if /i not "%GOT%"=="{sha256_hex.lower()}" (
  echo [%date% %time%] SHA mismatch: esperado {sha256_hex.lower()} obtido %GOT% >> "%LOG%"
  if exist "%BAK%" copy /y "%BAK%" "%DST%" >nul 2>&1
  goto :launch
)
'''

    cmd = f"""@echo off
setlocal enabledelayedexpansion
set "SRC={src_new}"
set "DST={dst_exe}"
set "BAK={backup}"
set "EXE_NAME={exe_name}"
set "LOG={log}"
echo [%date% %time%] updater start pid={pid} >> "%LOG%"

rem --- mata todas as instancias do app (bootloader + child) ---
taskkill /IM "%EXE_NAME%" /F >nul 2>&1
ping -n 2 127.0.0.1 >nul

rem --- limpa pastas _MEI antigas no TEMP ---
for /d %%D in ("%TEMP%\\_MEI*") do rmdir /s /q "%%D" >nul 2>&1

rem --- backup do exe atual ---
if exist "%DST%" copy /y "%DST%" "%BAK%" >nul 2>&1

rem --- tenta copiar o novo exe (com retry se arquivo estiver travado) ---
set /a TRY=0
:copyloop
copy /y "%SRC%" "%DST%" >nul 2>&1
if not errorlevel 1 goto copied
set /a TRY+=1
if %TRY% lss 10 (
  ping -n 2 127.0.0.1 >nul
  goto copyloop
)
echo [%date% %time%] copy failed apos %TRY% tentativas >> "%LOG%"
if exist "%BAK%" copy /y "%BAK%" "%DST%" >nul 2>&1
goto :launch

:copied
echo [%date% %time%] copy ok >> "%LOG%"
{sha_line}

:launch
rem --- limpa _MEI novamente (caso o kill tenha deixado sobras) ---
for /d %%D in ("%TEMP%\\_MEI*") do rmdir /s /q "%%D" >nul 2>&1

rem --- inicia o novo exe ---
start "" "%DST%"
echo [%date% %time%] launched >> "%LOG%"

rem --- cleanup ---
del "%SRC%" >nul 2>&1
del "%BAK%" >nul 2>&1
(goto) 2>nul & del "%~f0"
"""
    cmd_path = Path(tempfile.gettempdir()) / f"agendador_update_{pid}.cmd"
    # Usa mbcs pra o cmd interpretar corretamente em pt-BR
    try:
        cmd_path.write_text(cmd, encoding="mbcs")
    except Exception:
        cmd_path.write_text(cmd, encoding="utf-8")

    # Wrapper VBS pra rodar INVISIVEL (sem janela preta)
    vbs = (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.Run "cmd /c """"{cmd_path}""""", 0, False\r\n'
    )
    vbs_path = Path(tempfile.gettempdir()) / f"agendador_update_{pid}.vbs"
    try:
        vbs_path.write_text(vbs, encoding="mbcs")
    except Exception:
        vbs_path.write_text(vbs, encoding="utf-8")
    return vbs_path


def _apply_update_and_restart(new_exe: Path, sha256_hex: str = ""):
    """Dispara o updater invisível e encerra o processo atual."""
    vbs = _write_update_scripts(os.getpid(), new_exe, _exe_path(), sha256_hex)

    # Executa via wscript.exe (roda VBS sem janela)
    try:
        CREATE_NO_WINDOW = 0x08000000
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        subprocess.Popen(
            ["wscript.exe", str(vbs)],
            creationflags=CREATE_NO_WINDOW,
            startupinfo=si,
            close_fds=True,
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
        info = {
        "version": remote_v,
        "exe_url": mf.get("exe_url") or mf.get("url") or "",
        "sha256": (mf.get("sha256") or "").lower(),
        }
        if not info["exe_url"]:
         return (False, "Manifesto sem 'exe_url'.")

        return (True, info)
    except Exception as e:
        return (False, f"Falha ao checar: {e}")


def apply_update_now(info: dict) -> tuple[bool, str]:
    """
    Baixa e troca o EXE. Em modo dev (não frozen), só informa.
    """
    if not _is_frozen():
        return (False, f"Nova versão {info.get('version')} disponível (modo dev: não aplica).")
    try:
        tmp_new = Path(tempfile.gettempdir()) / f"{APP_BASENAME}.new.exe"
        _download(info["exe_url"], tmp_new)
        sha_expected = (info.get("sha256") or "").lower()
        if sha_expected:
            got = _sha256(tmp_new).lower()
            if got != sha_expected:
                tmp_new.unlink(missing_ok=True)
                return (False, f"SHA256 divergente (esperado {sha_expected}, obtido {got}).")
        # agenda troca e reinicia
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
            }
        },
        "tasks": [],
        "history": {}
    }
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
    with smtplib.SMTP(cfg["smtp_host"], int(cfg.get("smtp_port", 587)), timeout=30) as server:
        server.ehlo()
        server.starttls(context=context)
        server.login(cfg["username"], cfg["password"])
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
        client = Client(cfg.get("account_sid",""), cfg.get("auth_token",""))
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
    workdir = task.get("working_dir") or str(Path(task["path"]).parent)
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
    def __init__(self, master, task=None):
        super().__init__(master)
        self.title("Tarefa")
        self.resizable(False, False)
        self.result = None

        # ---------- Vars ----------
        self.var_name = tk.StringVar(value=(task or {}).get("name", ""))
        self.var_path = tk.StringVar(value=(task or {}).get("path", ""))
        self.var_args = tk.StringVar(value=(task or {}).get("args", ""))
        self.var_work = tk.StringVar(value=(task or {}).get("working_dir", ""))
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
        frm = ttk.Frame(self, padding=10)
        frm.grid(sticky="nsew")
        frm.columnconfigure(1, weight=1)

        row = 0
        ttk.Label(frm, text="Nome:").grid(row=row, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.var_name, width=42)\
            .grid(row=row, column=1, columnspan=3, sticky="we")
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



class SettingsDialog(tk.Toplevel):
    def __init__(self, master, settings, on_check_updates=None, current_version=APP_VERSION):
        self._on_check_updates = on_check_updates
        self._current_version = current_version

        super().__init__(master)
        self.title("Configurações")
        self.resizable(True, True)
        self.geometry("500x400")  # Tamanho inicial mais compacto
        self.minsize(450, 350)    # Tamanho mínimo
        self.result = None

        # ----- PDI (Pentaho) -----
        self.var_pdi = tk.StringVar(value=settings.get("pdi_home", r"C:\Pentaho\data-integration"))

        # ----- E-mail (SMTP) -----
        email = settings.get("email", {})
        self.var_mail_on = tk.BooleanVar(value=email.get("enabled", False))
        self.var_host    = tk.StringVar(value=email.get("smtp_host", "smtp.gmail.com"))
        self.var_port    = tk.StringVar(value=str(email.get("smtp_port", 587)))
        self.var_user    = tk.StringVar(value=email.get("username", ""))
        self.var_pass    = tk.StringVar(value=email.get("password", ""))
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

        # ---------- LAYOUT COM ABAS ----------
        main_frame = ttk.Frame(self, padding=10)
        main_frame.grid(sticky="nsew")
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(0, weight=1)

        # Notebook para abas
        notebook = ttk.Notebook(main_frame)
        notebook.grid(row=0, column=0, sticky="nsew", pady=(0, 10))

        # === ABA 1: GERAL ===
        tab_geral = ttk.Frame(notebook, padding=10)
        notebook.add(tab_geral, text="Geral")
        
        row = 0
        ttk.Label(tab_geral, text="PDI Home (.ktr/.kjb):").grid(row=row, column=0, sticky="w")
        ttk.Entry(tab_geral, textvariable=self.var_pdi, width=40).grid(row=row, column=1, sticky="we", padx=(5, 5))
        ttk.Button(tab_geral, text="...", command=self.pick_pdi, width=3).grid(row=row, column=2); row += 1

        # === ABA 2: E-MAIL ===
        tab_email = ttk.Frame(notebook, padding=10)
        notebook.add(tab_email, text="E-mail")
        
        row = 0
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

        ttk.Button(tab_email, text="Testar e-mail", command=self.test_email)\
            .grid(row=row, column=1, sticky="w", pady=(8, 0))

        # === ABA 3: WHATSAPP ===
        tab_wa = ttk.Frame(notebook, padding=10)
        notebook.add(tab_wa, text="WhatsApp")
        
        row = 0
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

        # === ABA 4: LIMPEZA DE LOGS ===
        tab_logs = ttk.Frame(notebook, padding=10)
        notebook.add(tab_logs, text="Logs")
        
        row = 0
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

        ttk.Button(tab_logs, text="Limpar logs agora", command=self.cleanup_logs_now)\
            .grid(row=row, column=0, sticky="w", pady=(8, 0))

        # === ABA 5: SISTEMA ===
        tab_sistema = ttk.Frame(notebook, padding=10)
        notebook.add(tab_sistema, text="Sistema")

        row = 0
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

        # Configurar expansão das colunas nas abas
        for tab in [tab_geral, tab_email, tab_wa, tab_logs, tab_sistema]:
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

    def cleanup_logs_now(self):
        """Executa limpeza de logs imediatamente."""
        try:
            # Cria configuração temporária baseada nos valores atuais da tela
            temp_settings = {
                "log_cleanup": {
                    "enabled": self.var_cleanup_enabled.get(),
                    "keep_days": int(self.var_cleanup_days.get() or 7)
                }
            }
            
            # Conta logs antes da limpeza
            log_count_before = len(list(LOG_DIR.glob("*.log"))) if LOG_DIR.exists() else 0
            
            # Executa limpeza
            cleanup_logs(temp_settings)
            
            # Conta logs após a limpeza
            log_count_after = len(list(LOG_DIR.glob("*.log"))) if LOG_DIR.exists() else 0
            removed = log_count_before - log_count_after
            
            if removed > 0:
                messagebox.showinfo("Limpeza de logs", f"Limpeza concluída!\n{removed} arquivo(s) de log removido(s).")
            else:
                messagebox.showinfo("Limpeza de logs", "Nenhum arquivo de log antigo encontrado para remoção.")
                
        except Exception as e:
            messagebox.showerror("Erro", f"Erro ao executar limpeza de logs:\n{e}")

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
        return {
            "pdi_home": self.var_pdi.get().strip(),
            "email": {
                "enabled": self.var_mail_on.get(),
                "smtp_host": self.var_host.get().strip(),
                "smtp_port": port,
                "username": self.var_user.get().strip(),
                "password": self.var_pass.get(),
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
        for col in ["Nome", "Horário", "Tipo", "Dias", "Arquivo"]:
            self.tree.column(col, anchor="w")
            self.tree.heading(col, text=col, anchor="w")

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
        self.scheduler = BackgroundScheduler(job_defaults={"max_instances": 1, "coalesce": True})
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
        
        title_label = ttk.Label(logo_frame, text="🚀 Agendador-Bravo", font=("Segoe UI", 14, "bold"))
        title_label.pack(side="left", padx=8)

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
        
        # Botões principais com estilos modernos e espaçamento melhorado
        ttk.Button(bar, text="➕ Nova", command=self.add_task, style="Modern.TButton").pack(side="left", padx=3)
        ttk.Button(bar, text="🧙 Assistente", command=self.open_assistant, style="Modern.TButton").pack(side="left", padx=3)
        ttk.Button(bar, text="✏️ Editar", command=self.edit_task, style="Modern.TButton").pack(side="left", padx=3)
        ttk.Button(bar, text="🗑️ Remover", command=self.remove_task, style="Modern.TButton").pack(side="left", padx=3)
        
        # Separador visual
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10)
        
        # Botão de ativar/desativar
        self.btn_toggle = ttk.Button(bar, text="⏸️ Desativar", command=self.toggle_task_status, style="Toggle.TButton")
        self.btn_toggle.pack(side="left", padx=3)
        
        # Botão de execução (destaque)
        self.btn_run = ttk.Button(bar, text="▶ Executar agora", 
                                 command=self.run_now, 
                                 style="Accent.TButton")
        self.btn_run.pack(side="left", padx=(10, 3))
        
        # Dica de ferramenta para o botão de execução
        ToolTip(self.btn_run, "Executa a tarefa selecionada. Selecione múltiplas tarefas para executá-las simultaneamente.")
        
        # Botão de interrupção
        self.btn_stop = ttk.Button(bar, text="⏹️ Interromper", 
                                 command=self.stop_running_tasks,
                                 style="Danger.TButton")
        self.btn_stop.pack(side="left", padx=3)
        self.btn_stop.config(state="disabled")  # Inicialmente desabilitado
        
        # Separador visual
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10)
        
        # Botões secundários
        ttk.Button(bar, text="🧪 Simular erro", command=self.simulate_error, style="Modern.TButton").pack(side="left", padx=3)
        ttk.Button(bar, text="⚙️ Configurações", command=self.open_settings, style="Modern.TButton").pack(side="left", padx=3)
        ttk.Button(bar, text="📂 Logs", command=lambda: os.startfile(LOG_DIR), style="Modern.TButton").pack(side="left", padx=3)
        ttk.Button(bar, text="📄 Último log", command=self.open_last_log, style="Modern.TButton").pack(side="left", padx=3)


        # --------- Layout principal ---------- Row 3 (principal, expansível)
        paned = ttk.Panedwindow(self, orient="horizontal")
        paned.grid(row=3, column=0, sticky="nsew", padx=10, pady=8)

        left = ttk.Frame(paned)
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)

        # Definindo as colunas da tabela
        cols = ("Status", "Nome", "Horário", "Tipo", "Dias", "Arquivo")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="extended")
        
        # Configurando os cabeçalhos
        self.tree.heading("Status", text="Status")
        self.tree.heading("Nome", text="Nome")
        self.tree.heading("Horário", text="Horário")
        self.tree.heading("Tipo", text="Tipo")
        self.tree.heading("Dias", text="Dias")
        self.tree.heading("Arquivo", text="Arquivo")
        
        self._style_table()
        
        # Configuração responsiva das colunas
        col_configs = {
            "Status": {"width": 100, "minwidth": 90, "stretch": False},
            "Nome": {"width": 220, "minwidth": 180, "stretch": True},
            "Horário": {"width": 160, "minwidth": 120, "stretch": False},
            "Tipo": {"width": 110, "minwidth": 90, "stretch": False},
            "Dias": {"width": 180, "minwidth": 150, "stretch": False},
            "Arquivo": {"width": 350, "minwidth": 250, "stretch": True}
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
        self.tree.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Configure>", self._on_tree_resize)

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
            try: self._splash.set_status("Pronto.")
            except Exception: pass
            self.after(900, self._show_main_after_splash)
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
        """Aplica tema moderno com cores e animações atualizadas"""
        # Define paleta de cores moderna
        if dark:
            colors = {
                'bg': '#1e1e2e',           # Fundo principal (Catppuccin Mocha)
                'surface': '#313244',       # Superfícies (cards, frames)
                'overlay': '#6c7086',       # Overlays e bordas
                'text': '#cdd6f4',         # Texto principal
                'subtext': '#a6adc8',      # Texto secundário
                'accent': '#89b4fa',       # Azul accent
                'success': '#a6e3a1',      # Verde sucesso
                'warning': '#f9e2af',      # Amarelo aviso
                'error': '#f38ba8',        # Rosa erro
                'purple': '#cba6f7',       # Roxo
                'teal': '#94e2d5',         # Teal
            }
        else:
            colors = {
                'bg': '#eff1f5',           # Fundo principal (Catppuccin Latte)
                'surface': '#e6e9ef',       # Superfícies
                'overlay': '#9ca0b0',       # Overlays
                'text': '#4c4f69',         # Texto principal
                'subtext': '#6c6f85',      # Texto secundário
                'accent': '#1e66f5',       # Azul accent
                'success': '#40a02b',      # Verde sucesso
                'warning': '#df8e1d',      # Amarelo aviso
                'error': '#d20f39',        # Vermelho erro
                'purple': '#8839ef',       # Roxo
                'teal': '#179299',         # Teal
            }
        
        # Configura o tema base
        try:
            if sv_ttk:
                sv_ttk.use_dark_theme() if dark else sv_ttk.use_light_theme()
        except Exception:
            pass

        # Aplica estilos personalizados
        style = ttk.Style(self)

        # Paleta derivada
        btn_bg   = colors['surface']
        btn_fg   = colors['text']
        btn_hv   = colors['overlay']
        acc_bg   = colors['accent']
        acc_fg   = '#ffffff' if dark else '#ffffff'
        acc_hv   = self._lighten_color(colors['accent'], -0.12)
        acc_ac   = self._lighten_color(colors['accent'], -0.22)
        danger_bg = colors['error']
        danger_hv = self._lighten_color(colors['error'], -0.15)

        # Botões modernos
        style.configure("Modern.TButton",
                        padding=(12, 8),
                        font=("Segoe UI", 9),
                        borderwidth=0,
                        relief="flat",
                        focuscolor='none',
                        background=btn_bg,
                        foreground=btn_fg)
        style.map("Modern.TButton",
                  background=[('active', btn_hv), ('pressed', btn_hv)],
                  foreground=[('disabled', colors['subtext'])])

        # Botão de destaque (Executar)
        style.configure("Accent.TButton",
                        padding=(14, 9),
                        font=("Segoe UI", 10, "bold"),
                        borderwidth=0,
                        relief="flat",
                        focuscolor='none',
                        background=acc_bg,
                        foreground=acc_fg)
        style.map("Accent.TButton",
                  background=[('active', acc_hv), ('pressed', acc_ac)],
                  foreground=[('disabled', '#cbd5e1')])

        # Botão de toggle
        style.configure("Toggle.TButton",
                        padding=(10, 6),
                        font=("Segoe UI", 9),
                        borderwidth=0,
                        relief="flat",
                        focuscolor='none',
                        background=btn_bg,
                        foreground=btn_fg)
        style.map("Toggle.TButton",
                  background=[('active', btn_hv), ('pressed', btn_hv)])

        # Botão de interrupção (Danger)
        style.configure("Danger.TButton",
                        padding=(10, 6),
                        font=("Segoe UI", 9, "bold"),
                        borderwidth=0,
                        relief="flat",
                        focuscolor='none',
                        background=danger_bg,
                        foreground='#ffffff')
        style.map("Danger.TButton",
                  background=[('active', danger_hv), ('pressed', danger_hv)],
                  foreground=[('disabled', '#e2e8f0')])

        # Labels com tipografia moderna
        style.configure("Title.TLabel",
                        font=("Segoe UI", 14, "bold"),
                        padding=(0, 4),
                        foreground=colors['text'])
        style.configure("Subtitle.TLabel",
                        font=("Segoe UI", 10),
                        padding=(0, 2),
                        foreground=colors['subtext'])
        
        # Configura cores do canvas
        self.configure(bg=colors['bg'])
        
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

        # garante scheduler rodando
        if not self.scheduler.running:
            self.scheduler.start()

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

    def _job_wrapper(self, task):
        def progress(_):
            pass
            
        task_name = task["name"]
        
        def on_task_start():
            if self.winfo_exists():
                self._on_job_start(task)  # Já chama _set_task_running internamente
        
        def on_task_end(rc, dur, log_path):
            def update_ui():
                if not self.winfo_exists():
                    return
                
                # Verifica se foi interrompido manualmente
                was_interrupted = False
                with self.running_lock:
                    if task_name not in self.running_processes:
                        was_interrupted = True
                    else:
                        # Remove o processo da lista de processos em execução
                        del self.running_processes[task_name]
                    
                    # Atualiza o estado do botão de parada
                    if not self.running_processes:
                        self.btn_stop.config(state="disabled")
                
                # Atualiza a interface
                # Só atualiza o status se NÃO foi interrompido (pois _set_task_interrupted já foi chamado)
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
            rc, dur, log_path = run_task(task, self.data["settings"], 
                                      progress_cb=progress,
                                      process_callback=self._register_process)
            append_history(self.data, task_name, rc, dur)
            self._maybe_notify(task, rc, log_path)
        except Exception as e:
            print(f"Erro ao executar tarefa {task_name}: {e}")
            rc, dur, log_path = 1, 0, None
        
        # Atualiza a interface após o término
        on_task_end(rc, dur, log_path)

    def _maybe_notify(self, task, rc, log_path):
        # Verifica se a tarefa foi interrompida manualmente
        was_stopped_manually = False
        with self.running_lock:
            if task['name'] not in self.running_processes:
                was_stopped_manually = True
                
        # Não envia notificação se foi interrompida manualmente
        if rc != 0 and task.get("notify_fail", True) and not was_stopped_manually:
            subject = f"[{task['name']}] FALHA (RC={rc})"
            body = f"Tarefa: {task['name']}\nData: {now_str()}\nRC: {rc}\nLog: {log_path}"
            try:
                send_email(self.data["settings"], subject, body)
            except Exception as e:
                print("Erro e-mail:", e)
            try:
                send_whatsapp(self.data["settings"], subject, body)
            except Exception as e:
                print("Erro WhatsApp:", e)

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
        
        # Limpa a tabela
        for i in self.tree.get_children():
            self.tree.delete(i)
        
        # Preenche com as tarefas
        for i, task in enumerate(self.data.get("tasks", [])):
            task_name = task["name"]
            is_selected = task_name in current_selection
            
            # Define as tags do item
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
            
            # Adiciona a tarefa à tabela
            item_id = self.tree.insert("", "end", iid=task_name,
                           values=(
                               status_text,  # Texto de status
                               task_name,
                               times,
                               schedule_type.capitalize(),
                               self._format_days(task.get("days", [True]*7)),
                               task.get("path", "")
                           ),
                           tags=tags)
        
        # Aplica as cores de status após inserir todos os itens
        for task in self.data.get("tasks", []):
            self._update_task_status_color(task["name"])
        
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
            "Arquivo": 300
        }
        
        # Ajusta o tamanho das colunas baseado no conteúdo
        for col, width in col_widths.items():
            self.tree.column(col, width=width, minwidth=width, stretch=tk.YES, anchor="w")
        
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
            # Mensagem estilizada quando nenhuma tarefa está selecionada
            w = int(self.canvas.winfo_width() or 400)
            h = int(self.canvas.winfo_height() or 300)
            
            self.canvas.create_text(
                w//2, h//2 - 20, anchor="center",
                fill=subtext_color,
                font=("Segoe UI", 12),
                text="📊 Histórico de Execuções"
            )
            self.canvas.create_text(
                w//2, h//2 + 10, anchor="center",
                fill=subtext_color,
                font=("Segoe UI", 10),
                text="Selecione uma tarefa para visualizar o histórico"
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
    def add_task(self):
        dlg = TaskDialog(self); self.wait_window(dlg)
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
        td = TaskDialog(self, dlg.result); self.wait_window(td)
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
        dlg = TaskDialog(self, task); self.wait_window(dlg)
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
