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
NET_CHECK_EVERY_SEC = int(os.getenv("AGENDADOR_NET_EVERY_SEC", "10"))
NET_FLAP_STABLE = int(os.getenv("AGENDADOR_NET_STABLE", "2"))
UPDATE_QUEUE_MAX = 50

def is_online(timeout=3) -> bool:
    try:
        urllib.request.urlopen("https://www.gstatic.com/generate_204", timeout=timeout)
        return True
    except Exception:
        return False

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


APP_VERSION = "2025.09.25.1.1"   # << aumente em cada build
UPDATE_MANIFEST_URL = os.getenv(
    "AGENDADOR_UPDATE_MANIFEST",
    "https://raw.githubusercontent.com/GabrielZippys/Agendador-Bravo/main/update/manifest.json"
)
UPDATE_CHECK_EVERY_MIN = int(os.getenv("AGENDADOR_UPDATE_EVERY_MIN", "240"))  # 4h

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


def _write_update_cmd(pid: int, src_new: Path, dst_exe: Path) -> Path:
    cmd = f"""@echo off
setlocal enabledelayedexpansion
set "SRC={src_new}"
set "DST={dst_exe}"
set "PID={pid}"
set "MAXWAIT=120"
set /a COUNT=0

:wait
>nul 2>&1 timeout /t 1
>nul 2>&1 tasklist /FI "PID eq %PID%" | find "%PID%"
if %ERRORLEVEL%==0 (
  set /a COUNT+=1
  if !COUNT! lss %MAXWAIT% goto wait
  >nul 2>&1 taskkill /PID %PID% /T /F
)

>nul 2>&1 copy /y "%SRC%" "%DST%"
start "" /b "%DST%"
>nul 2>&1 del "%SRC%"
>nul 2>&1 del "%~f0"
"""
    p = Path(tempfile.gettempdir()) / f"agendador_update_{pid}.cmd"
    p.write_text(cmd, encoding="utf-8")
    return p


def _apply_update_and_restart(new_exe: Path):
    flags = 0x08000000 | 0x00000008 | 0x00000200  # CREATE_NO_WINDOW | DETACHED | NEW_PROCESS_GROUP

    updater = _write_update_cmd(os.getpid(), new_exe, _exe_path())
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE

    subprocess.Popen(["cmd", "/c", str(updater)], creationflags=flags, startupinfo=si)
    os._exit(0)


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
        if info.get("sha256"):
            got = _sha256(tmp_new).lower()
            if got != info["sha256"]:
                tmp_new.unlink(missing_ok=True)
                return (False, f"SHA256 divergente (esperado {info['sha256']}, obtido {got}).")
        # agenda troca e reinicia
        _apply_update_and_restart(tmp_new)
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

def load_data():
    """Carrega config.json; cria defaults se não existir/corrompido."""
    ensure_dirs()
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
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

def save_data(data):
    ensure_dirs()
    DATA_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

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
    cleanup_cfg = settings.get("log_cleanup", {})
    if not cleanup_cfg.get("enabled", True):
        return
    
    keep_days = int(cleanup_cfg.get("keep_days", 7))
    if keep_days <= 0:
        return
    
    ensure_dirs()
    if not LOG_DIR.exists():
        return
    
    # Calcula data limite (logs mais antigos que isso serão removidos)
    from datetime import timedelta
    cutoff_date = datetime.now() - timedelta(days=keep_days)
    
    removed_count = 0
    total_size = 0
    
    try:
        # Lista todos os arquivos .log na pasta de logs
        for log_file in LOG_DIR.glob("*.log"):
            try:
                # Verifica a data de modificação do arquivo
                file_mtime = datetime.fromtimestamp(log_file.stat().st_mtime)
                
                if file_mtime < cutoff_date:
                    file_size = log_file.stat().st_size
                    log_file.unlink()
                    removed_count += 1
                    total_size += file_size
            except Exception:
                # Se houver erro com um arquivo específico, continua com os outros
                continue
    except Exception:
        # Se houver erro geral, não faz nada
        pass
    
    # Log da limpeza (se removeu algum arquivo)
    if removed_count > 0:
        cleanup_log = LOG_DIR / f"cleanup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        try:
            size_mb = total_size / (1024 * 1024)
            with open(cleanup_log, "w", encoding="utf-8") as f:
                f.write(f"# Limpeza automática de logs @ {now_str()}\n")
                f.write(f"Arquivos removidos: {removed_count}\n")
                f.write(f"Espaço liberado: {size_mb:.2f} MB\n")
                f.write(f"Critério: logs mais antigos que {keep_days} dias\n")
        except Exception:
            pass

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
        py = sys.executable
        return [py, path] + arg_list
    if ext == ".ktr":
        return [str(Path(pdi_home)/"Pan.bat"), f"/file:{path}"] + arg_list
    if ext == ".kjb":
        return [str(Path(pdi_home)/"Kitchen.bat"), f"/file:{path}"] + arg_list
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

def run_task(task, settings, progress_cb=None):
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


    # força UTF-8 no filho Python
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")

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

            while True:
                line = proc.stdout.readline()
                if not line and proc.poll() is not None:
                    break
                if line:
                    f.write(line)
                    if progress_cb:
                        progress_cb(line.strip()[:140])
            if timeout and (time.time() - start) > timeout:
                try: proc.kill()
                except Exception: pass
                rc = -9
                f.write("\n### TIMEOUT atingido.\n")
            else:
                rc = proc.returncode
        except Exception as e:
            rc = -1
            f.write("\n### ERRO ao iniciar/executar:\n" + "".join(traceback.format_exception(e)))

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


class TaskDialog(tk.Toplevel):
    """
    Dialogo de tarefa com botão 'Horários...' que abre um editor de horários.
    - No modo 'Horário fixo' (cron): é obrigatório ter >= 1 horário.
    - No modo 'Intervalo': o botão de horários é desabilitado e
      usam-se os campos 'A cada N minutes/hours'.
    """
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
#  Aplicação principal (GUI)
# ======================================================================================

class App(tk.Tk):


    def _style_table(self):
        """Aplica estilo moderno à tabela com cores do tema atual"""
        dark = bool(self.var_dark.get())
        
        # Usa cores do tema se disponível, senão usa cores padrão
        if hasattr(self, '_theme_colors'):
            colors = self._theme_colors
            even = colors['surface']
            odd = colors['bg']
            sel_bg = colors['accent'] + '40'  # Accent com transparência
            text_color = colors['text']
            heading_bg = colors['overlay']
        else:
            # Cores padrão melhoradas
            if dark:
                even = "#2a2a2a"
                odd = "#1e1e1e"
                sel_bg = "#404040"
                text_color = "#ffffff"
                heading_bg = "#3a3a3a"
            else:
                even = "#f8f9fa"
                odd = "#ffffff"
                sel_bg = "#e3f2fd"
                text_color = "#212529"
                heading_bg = "#e9ecef"

        style = ttk.Style(self)
        
        # Cabeçalhos modernos
        style.configure("Treeview.Heading", 
                       padding=(8, 6),
                       font=("Segoe UI", 9, "bold"),
                       relief="flat")
        
        # Configuração da tabela
        style.configure("Treeview",
                       rowheight=28,  # Linhas mais altas
                       font=("Segoe UI", 9))

        # Cores alternadas modernas
        try:
            self.tree.tag_configure("evenrow", 
                                  background=even,
                                  foreground=text_color)
            self.tree.tag_configure("oddrow", 
                                  background=odd,
                                  foreground=text_color)
        except Exception:
            pass

        # Seleção com destaque mais visível
        try:
            # Cores mais contrastantes para melhor visibilidade
            selected_bg = "#0078d7"  # Azul mais forte
            selected_fg = "#ffffff"   # Texto branco
            
            style.map("Treeview",
                     background=[("selected", selected_bg)],
                     foreground=[("selected", selected_fg)],
                     fieldbackground=[("selected", selected_bg)])
            
            # Ajusta a cor do foco para combinar
            style.map("Treeview", 
                     background=[("focus", selected_bg)],
                     foreground=[("focus", selected_fg)])
            
            # Remove a borda de foco para um visual mais limpo
            style.layout("Treeview.Item", 
                        [('Treeitem.padding', 
                          {'sticky': 'nswe', 
                           'children': [('Treeitem.indicator', {'side': 'left', 'sticky': ''}),
                                       ('Treeitem.image', {'side': 'left', 'sticky': ''}),
                                       ('Treeitem.text', {'side': 'left', 'sticky': ''})],
                          })])
        except Exception as e:
            print(f"Erro ao configurar estilo da tabela: {e}")


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
        
        # Configuração responsiva da janela
        self.geometry("1200x700")  # Tamanho inicial maior
        self.minsize(800, 500)     # Tamanho mínimo
        self.state('zoomed')       # Inicia maximizada no Windows
        
        # Configurar responsividade da janela principal
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)  # O painel principal (row 3) se expande
        # Rows: 0=header, 1=banner, 2=toolbar, 3=main_content, 4=status
        
        self.attributes("-alpha", 0.0)  # fade-in
        ensure_dirs()
        self.data = load_data()
        # Deixa explícito (1 instância por job, coalesce)
        self.scheduler = BackgroundScheduler(job_defaults={"max_instances": 1, "coalesce": True})
        self.jobs = {}
        # Estado de rede / fila de updates
        self.net_online = is_online()
        self._update_processing = False
        self.update_queue = self.data.setdefault("update_queue", [])  # persiste no JSON


        # Estilos
        style = ttk.Style(self)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
        style.configure("TButton", padding=4)
        style.configure("TLabel", font=("Segoe UI", 9))

        # Cabeçalho (logo + título + tema) - Row 0
        header = ttk.Frame(self, padding=(8, 6))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)  # Espaço entre logo e botão tema

        # Banner de atualização (inicialmente oculto) - Row 1
        self._update_info = None
        self._update_banner = ttk.Frame(self, padding=(8, 6))
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
        toolbar_outer = ttk.Frame(self)
        toolbar_outer.grid(row=2, column=0, sticky="ew", padx=6)
        toolbar_outer.columnconfigure(0, weight=1)
        
        self._toolbar_canvas = tk.Canvas(toolbar_outer, height=40, highlightthickness=0)
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
        
        # Botões principais com estilos modernos
        ttk.Button(bar, text="➕ Nova", command=self.add_task, style="Modern.TButton").pack(side="left", padx=2)
        ttk.Button(bar, text="🧙 Assistente", command=self.open_assistant, style="Modern.TButton").pack(side="left", padx=2)
        ttk.Button(bar, text="✏️ Editar", command=self.edit_task, style="Modern.TButton").pack(side="left", padx=2)
        ttk.Button(bar, text="🗑️ Remover", command=self.remove_task, style="Modern.TButton").pack(side="left", padx=2)
        
        # Separador visual
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        
        # Botão de ativar/desativar
        self.btn_toggle = ttk.Button(bar, text="⏸️ Desativar", command=self.toggle_task_status, style="Toggle.TButton")
        self.btn_toggle.pack(side="left", padx=2)
        
        # Botão de execução (destaque)
        self.btn_run = ttk.Button(bar, text="▶ Executar agora", command=self.run_now, style="Accent.TButton")
        self.btn_run.pack(side="left", padx=(8, 2))
        
        # Separador visual
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        
        # Botões secundários
        ttk.Button(bar, text="🧪 Simular erro", command=self.simulate_error, style="Modern.TButton").pack(side="left", padx=2)
        ttk.Button(bar, text="⚙️ Configurações", command=self.open_settings, style="Modern.TButton").pack(side="left", padx=2)
        ttk.Button(bar, text="📂 Logs", command=lambda: os.startfile(LOG_DIR), style="Modern.TButton").pack(side="left", padx=2)
        ttk.Button(bar, text="📄 Último log", command=self.open_last_log, style="Modern.TButton").pack(side="left", padx=2)
        ttk.Button(bar, text="💡 Dicas", command=self.show_tips, style="Modern.TButton").pack(side="left", padx=2)
        ttk.Button(bar, text="💾 Salvar", command=self.save, style="Modern.TButton").pack(side="left", padx=2)


        # --------- Layout principal ---------- Row 3 (principal, expansível)
        paned = ttk.Panedwindow(self, orient="horizontal")
        paned.grid(row=3, column=0, sticky="nsew", padx=6, pady=6)

        left = ttk.Frame(paned)
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)

        cols = ("Status","Nome","Hora","Dias","Arquivo","NotificarFalha","Timeout")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse")
        self._style_table()
        
        # Configuração responsiva das colunas
        col_configs = {
            "Status": {"width": 80, "minwidth": 60, "stretch": False},
            "Nome": {"width": 200, "minwidth": 150, "stretch": True},
            "Hora": {"width": 80, "minwidth": 60, "stretch": False},
            "Dias": {"width": 100, "minwidth": 80, "stretch": False},
            "Arquivo": {"width": 300, "minwidth": 200, "stretch": True},
            "NotificarFalha": {"width": 100, "minwidth": 80, "stretch": False},
            "Timeout": {"width": 80, "minwidth": 60, "stretch": False}
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
        status = ttk.Frame(self, relief="groove", padding=(6,3))
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
        self.refresh_table()
        self.reschedule_all()
        self.update_status_indicators()
        self.update_net_indicator()
        self.update_toggle_button()  # Inicializa o botão toggle
        self._apply_theme(False)
        self._fade_in()
        self._pulse_status()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        
        start_net_monitor(self)
        start_auto_update_thread(self)

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
        
        # Botões modernos com gradiente sutil
        style.configure("Modern.TButton", 
                       padding=(12, 8),
                       font=("Segoe UI", 9),
                       borderwidth=0,
                       focuscolor='none')
        
        # Botão de destaque (Executar)
        style.configure("Accent.TButton",
                       padding=(12, 8),
                       font=("Segoe UI", 9, "bold"),
                       borderwidth=0,
                       focuscolor='none')
        
        # Botão de toggle (Ativar/Desativar)
        style.configure("Toggle.TButton",
                       padding=(10, 6),
                       font=("Segoe UI", 9),
                       borderwidth=0,
                       focuscolor='none')
        
        # Labels com tipografia moderna
        style.configure("Title.TLabel",
                       font=("Segoe UI", 14, "bold"),
                       padding=(0, 4))
        
        style.configure("Subtitle.TLabel",
                       font=("Segoe UI", 10),
                       padding=(0, 2))
        
        # Configura cores do canvas
        self.configure(bg=colors['bg'])
        
        # Aplica estilos aos botões específicos
        try:
            self.btn_run.configure(style="Accent.TButton")
            self.btn_toggle.configure(style="Toggle.TButton")
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

    # ===== utilidades UI =====
    def _on_tree_resize(self, event=None):
        w = max(300, self.tree.winfo_width())
        ratios = {"Status":0.08, "Nome":0.18, "Hora":0.08, "Dias":0.20, "Arquivo":0.32, "NotificarFalha":0.08, "Timeout":0.06}
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

    def on_close(self):
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
                continue

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
        try:
            if self.winfo_exists():
                self.after(0, lambda t=task: (self._set_task_running(t["name"], True), self._on_job_start(t)))
        except Exception:
            pass
        rc, dur, log_path = run_task(task, self.data["settings"], progress_cb=progress)
        append_history(self.data, task["name"], rc, dur)
        self._maybe_notify(task, rc, log_path)
        try:
            if self.winfo_exists():
                self.after(0, lambda t=task, r=rc, d=dur, lp=log_path: (self._set_task_running(t["name"], False), self.draw_chart(), self._on_job_end(t, r, d, lp)))
        except Exception:
            pass

    def _maybe_notify(self, task, rc, log_path):
        if rc != 0 and task.get("notify_fail", True):
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
            vals[0] = "⏳ Rodando" if running else ("✅ Ativo" if enabled else "⏸️ Inativo")
            self.tree.item(name, values=tuple(vals))
        except Exception:
            pass

    def _on_job_start(self, task):
        try:
            self.set_status_line(f"Iniciando '{task['name']}'…")
            self._show_toast(f"Iniciando: {task['name']}", task.get('path', ''))
        except Exception:
            pass

    def _on_job_end(self, task, rc, dur, log_path):
        try:
            status = "OK" if rc == 0 else f"Falha (RC={rc})"
            self.set_status_line(f"Concluído '{task['name']}' — {status} em {dur:.1f}s")
            self._show_toast(f"Concluído: {task['name']}", f"{status} • {dur:.1f}s", ok=(rc == 0))
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
        except Exception:
            pass
        if busy:
            self.status_label.config(text=msg or "Executando...")
            self.pbar.start(10)
        else:
            self.pbar.stop()
            self.status_label.config(text="Pronto.")
        self.update_idletasks()

    def set_status_line(self, text):
        self.status_label.config(text=text[:160])
        self.update_idletasks()

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
    def refresh_table(self):
        for i in self.tree.get_children():
            self.tree.delete(i)

        for idx, t in enumerate(self.data.get("tasks", [])):
            hora, dias = self._hora_dias_text(t)
            tag = ("evenrow" if idx % 2 == 0 else "oddrow")
            
            # Status da tarefa (ativo/inativo)
            status = "✅ Ativo" if t.get("enabled", True) else "⏸️ Inativo"
            
            self.tree.insert(
                "", "end", iid=t["name"], tags=(tag,), values=(
                    status, t["name"], hora, dias, t["path"],
                    "Sim" if t.get("notify_fail", True) else "Não",
                    t.get("timeout", "0")
                )
            )
        self.draw_chart()

    def _on_tree_select(self, event=None):
        """Chamado quando uma linha da tabela é selecionada"""
        self.draw_chart()
        self.update_toggle_button()

    def draw_chart(self):
        """Desenha gráfico moderno com animações suaves"""
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
            color = success_color if it["rc"] == 0 else error_color
            
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

        # Legenda moderna
        legend_y = 30
        # Sucesso
        self.canvas.create_oval(w - pad - 120, legend_y, w - pad - 108, legend_y + 12, 
                               fill=success_color, outline="")
        self.canvas.create_text(w - pad - 102, legend_y + 6, text="✓ Sucesso", 
                               anchor="w", fill=text_color, font=("Segoe UI", 9))
        # Erro
        self.canvas.create_oval(w - pad - 60, legend_y, w - pad - 48, legend_y + 12, 
                               fill=error_color, outline="")
        self.canvas.create_text(w - pad - 42, legend_y + 6, text="✗ Falha", 
                               anchor="w", fill=text_color, font=("Segoe UI", 9))

        # Gráfico de barras horizontal moderno (Sucesso vs Falha)
        ok = sum(1 for i in items if i["rc"] == 0)
        fail = N - ok
        total = max(1, N)
        y_top = H1 + 15
        
        self.canvas.create_text(
            pad, y_top, anchor="nw",
            text=f"📊 Taxa de Sucesso — {ok}/{N} ({(ok/total)*100:.1f}%)",
            fill=text_color,
            font=("Segoe UI", 10, "bold")
        )
        
        y_bar = y_top + 25
        bar_h = max(20, h - y_bar - 20)
        full_w = w - 2 * pad
        ok_w = int(full_w * (ok / total))
        fail_w = full_w - ok_w
        
        # Fundo da barra
        self.canvas.create_rectangle(pad, y_bar, pad + full_w, y_bar + bar_h, 
                                   fill=grid_color, outline="")
        
        # Barra de sucesso
        if ok_w > 0:
            self.canvas.create_rectangle(pad, y_bar, pad + ok_w, y_bar + bar_h, 
                                       fill=success_color, outline="")
        
        # Barra de falha
        if fail_w > 0:
            self.canvas.create_rectangle(pad + ok_w, y_bar, pad + ok_w + fail_w, y_bar + bar_h, 
                                       fill=error_color, outline="")
        
        # Texto sobre as barras
        if ok > 0:
            self.canvas.create_text(pad + ok_w//2, y_bar + bar_h//2, 
                                  text=f"{ok} OK", anchor="center", 
                                  fill="white", font=("Segoe UI", 9, "bold"))
        if fail > 0:
            self.canvas.create_text(pad + ok_w + fail_w//2, y_bar + bar_h//2, 
                                  text=f"{fail} Falhas", anchor="center", 
                                  fill="white", font=("Segoe UI", 9, "bold"))
    
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

    def run_now(self):
        sel = self.tree.selection()
        if not sel: return
        name = sel[0]
        task = next((t for t in self.data["tasks"] if t["name"]==name), None)
        if not task: return

        self._set_ui_busy(True, f"Executando '{task['name']}'...")

        def worker():
            def progress(line):
                self.after(0, lambda: self.set_status_line(f"[{task['name']}] {line}"))
            rc, dur, log_path = run_task(task, self.data["settings"], progress_cb=progress)
            append_history(self.data, task["name"], rc, dur)
            self._maybe_notify(task, rc, log_path)
            def finish():
                self._set_ui_busy(False)
                self.draw_chart()
                msg = "SUCESSO" if rc == 0 else f"FALHA (RC={rc})"
                messagebox.showinfo("Execução", f"{task['name']}: {msg}\n\nDuração: {dur:.1f}s\nLog:\n{log_path}")
            self.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

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
