# agendador_pro.py

import os, sys, json, subprocess, traceback, time, smtplib, ssl, threading, shlex
from email.mime.text import MIMEText
from email.utils import formatdate
from datetime import datetime
from pathlib import Path
import re
from tkinter import ttk, filedialog, messagebox, simpledialog
icon=['Logo.ico'],

# --- AUTOUPDATE (com aviso na UI) -------------------------------------------
import urllib.request, hashlib, tempfile

APP_VERSION = "2025.09.15.0"   # << aumente em cada build
UPDATE_MANIFEST_URL = os.getenv("AGENDADOR_UPDATE_MANIFEST", "https://SEU-LINK/manifest.json")
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

def _write_update_cmd(pid: int, src_new: Path, dst_exe: Path) -> Path:
    cmd = f"""@echo off
setlocal
set SRC="{src_new}"
set DST="{dst_exe}"
set PID={pid}
:wait
timeout /t 1 >nul
tasklist /FI "PID eq %PID%" | find "%PID%" >nul && goto wait
copy /y %SRC% %DST% >nul
start "" %DST%
del %SRC% >nul 2>&1
del "%~f0" >nul 2>&1
"""
    p = Path(tempfile.gettempdir()) / f"agendador_update_{pid}.cmd"
    p.write_text(cmd, encoding="utf-8")
    return p

def _apply_update_and_restart(new_exe: Path):
    flags = 0x08000000 | 0x00000008 | 0x00000200  # CREATE_NO_WINDOW | DETACHED | NEW_PROCESS_GROUP
    updater = _write_update_cmd(os.getpid(), new_exe, _exe_path())
    subprocess.Popen(["cmd", "/c", str(updater)], creationflags=flags)
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
            "exe_url": mf.get("exe_url", ""),
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
    """Checa no início e depois periodicamente; avisa a UI se houver nova versão."""
    def worker():
        time.sleep(20)  # deixa a UI abrir
        for _ in range(2):  # checa agora e logo depois agenda
            has, data = fetch_update_info()
            if has and app_ref and app_ref.winfo_exists():
                # passa o dict para a UI criar o aviso
                app_ref.after(0, lambda d=data: app_ref.on_update_available(d))
            break
        # ciclo
        while True:
            time.sleep(max(60, UPDATE_CHECK_EVERY_MIN * 60))
            has, data = fetch_update_info()
            if has and app_ref and app_ref.winfo_exists():
                app_ref.after(0, lambda d=data: app_ref.on_update_available(d))
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
from apscheduler.triggers.interval import IntervalTrigger

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
        return ["powershell", "-ExecutionPolicy", "Bypass", "-File", path] + arg_list
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

            popen_kwargs = dict(cwd=workdir, stdout=log_fh, stderr=subprocess.STDOUT, env=env)
            if os.name == "nt":
                CREATE_NEW_PROCESS_GROUP = 0x00000200
                DETACHED_PROCESS = 0x00000008
                CREATE_NO_WINDOW = 0x08000000
                popen_kwargs["creationflags"] = (CREATE_NEW_PROCESS_GROUP |
                                                 DETACHED_PROCESS | CREATE_NO_WINDOW)
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
                cmd, cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="ignore", bufsize=1, env=env
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

        self.int_row = ttk.Frame(sched)
        self.int_row.grid(row=1, column=0, columnspan=4, pady=(6, 0), sticky="w")
        ttk.Label(self.int_row, text="A cada").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.int_row, textvariable=self.var_every_val, width=6)\
            .grid(row=0, column=1, padx=(4, 6))
        ttk.Combobox(self.int_row, textvariable=self.var_every_unit,
                     values=("minutes", "hours"), width=10, state="readonly")\
            .grid(row=0, column=2)

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
        # desabilita botão e "cinza" o label quando for intervalo
        state = ("disabled" if is_interval else "normal")
        try:
            self.btn_times.configure(state=state)
            self.lbl_times.configure(foreground=("#888" if is_interval else ""))
            self.lbl_time_title.configure(foreground=("#888" if is_interval else ""))
        except Exception:
            pass
        # mostra/oculta controles do intervalo
        if is_interval:
            self.int_row.grid()
        else:
            self.int_row.grid_remove()

    # ---------- Ações ----------
    def on_save(self):
        # validações
        if not self.var_name.get().strip():
            messagebox.showerror("Erro", "Informe o nome da tarefa.")
            return
        if not self.var_path.get().strip():
            messagebox.showerror("Erro", "Escolha o arquivo/comando.")
            return

        mode = (self.var_schedule.get() or "cron").lower()
        if mode == "cron":
            if not self.times:
                messagebox.showerror("Erro", "Adicione pelo menos um horário.")
                return
            times_list = list(self.times)
        else:
            try:
                ev = int(self.var_every_val.get())
                assert ev > 0
            except Exception:
                messagebox.showerror("Erro", "Informe um intervalo válido (>0).")
                return
            times_list = []

        self.result = {
            "name": self.var_name.get().strip(),
            "path": self.var_path.get().strip(),
            "args": self.var_args.get().strip(),
            "working_dir": self.var_work.get().strip(),
            "time": (times_list[0] if times_list else "06:00"),
            "times": times_list,
            "days": [v.get() for v in self.days_vars],
            "timeout": self.var_timeout.get().strip(),
            "notify_fail": self.var_notify_fail.get(),
            "schedule_type": self.var_schedule.get(),
            "every_value": int(self.var_every_val.get() or 0),
            "every_unit": self.var_every_unit.get(),
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





class SettingsDialog(tk.Toplevel):
    def __init__(self, master, settings, on_check_updates=None, current_version=APP_VERSION):
        self._on_check_updates = on_check_updates
        self._current_version = current_version

        super().__init__(master)
        self.title("Configurações")
        self.resizable(False, False)
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

        # ---------- LAYOUT ----------
        frm = ttk.Frame(self, padding=10)
        frm.grid(sticky="nsew")
        row = 0

        ttk.Label(frm, text="PDI Home (.ktr/.kjb):").grid(row=row, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.var_pdi, width=46).grid(row=row, column=1, sticky="we")
        ttk.Button(frm, text="Procurar...", command=self.pick_pdi).grid(row=row, column=2); row += 1

        ttk.Separator(frm).grid(row=row, column=0, columnspan=3, pady=8, sticky="we"); row += 1

        ttk.Checkbutton(frm, text="Ativar e-mail (SMTP)", variable=self.var_mail_on)\
            .grid(row=row, column=0, sticky="w"); row += 1

        for label, var in [
            ("SMTP host", self.var_host),
            ("SMTP porta", self.var_port),
            ("Usuário", self.var_user),
            ("Senha de app", self.var_pass),
            ("De (from)", self.var_from),
            ("Para (vírgula)", self.var_to),
        ]:
            ttk.Label(frm, text=label + ":").grid(row=row, column=0, sticky="w")
            ttk.Entry(frm, textvariable=var, width=46, show="*" if "Senha" in label else "")\
                .grid(row=row, column=1, columnspan=2, sticky="we"); row += 1

        ttk.Button(frm, text="Testar e-mail", command=self.test_email)\
            .grid(row=row, column=1, sticky="w"); row += 1

        ttk.Separator(frm).grid(row=row, column=0, columnspan=3, pady=8, sticky="we"); row += 1

        ttk.Checkbutton(frm, text="Ativar WhatsApp (QR – WhatsApp Web)", variable=self.var_wa_on)\
            .grid(row=row, column=0, sticky="w"); row += 1

        ttk.Label(frm, text="Node.exe:").grid(row=row, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.var_node_path, width=46).grid(row=row, column=1, sticky="we")
        ttk.Button(frm, text="Procurar...", command=lambda: self._pick(self.var_node_path, True))\
            .grid(row=row, column=2); row += 1

        ttk.Label(frm, text="Script wa_send.js:").grid(row=row, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.var_script, width=46).grid(row=row, column=1, sticky="we")
        ttk.Button(frm, text="Procurar...", command=lambda: self._pick(self.var_script, True))\
            .grid(row=row, column=2); row += 1

        ttk.Label(frm, text="Meu número:").grid(row=row, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.var_my_number, width=46)\
            .grid(row=row, column=1, columnspan=2, sticky="we"); row += 1

        ttk.Label(frm, text="Destinos (vírgula):").grid(row=row, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.var_to_targets, width=46)\
            .grid(row=row, column=1, columnspan=2, sticky="we"); row += 1

        ttk.Button(frm, text="Testar WhatsApp", command=self.test_whatsapp_qr)\
            .grid(row=row, column=1, sticky="w"); row += 1

        ttk.Separator(frm).grid(row=row, column=0, columnspan=3, pady=8, sticky="we"); row += 1

        up = ttk.LabelFrame(frm, text="Atualizações", padding=(6,6))
        up.grid(row=row, column=0, columnspan=3, sticky="we"); row += 1
        ttk.Label(up, text=f"Versão instalada: v{self._current_version}").grid(row=0, column=0, sticky="w")
        ttk.Button(up, text="Verificar se há atualização", command=self._check_updates)\
            .grid(row=0, column=1, padx=8, sticky="e")

        btns = ttk.Frame(frm); btns.grid(row=row, column=0, columnspan=3, pady=(10,0))
        ttk.Button(btns, text="Salvar", command=self.on_save).grid(row=0, column=0, padx=6)
        ttk.Button(btns, text="Cancelar", command=self.destroy).grid(row=0, column=1, padx=6)

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
            creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
            subprocess.Popen([node, script, "--to", tos, "--message", msg],
                             creationflags=creationflags, cwd=str(WA_DIR))
            messagebox.showinfo(
                "WhatsApp",
                "Janela aberta.\nSe for a primeira vez, leia o QR Code com o WhatsApp do número emissor."
            )
        except Exception as e:
            messagebox.showerror("Falha", f"Não foi possível abrir o teste do WhatsApp:\n{e}")

    # ===== Coleta / salvar =====
    def get_result_preview(self):
        try:
            port = int(self.var_port.get())
        except Exception:
            port = 587
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
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1060x600")
        self.attributes("-alpha", 0.0)  # fade-in
        ensure_dirs()
        self.data = load_data()
        self.scheduler = BackgroundScheduler(job_defaults={"max_instances": 1, "coalesce": True})
        self.jobs = {}

        # Estilos
        style = ttk.Style(self)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
        style.configure("TButton", padding=4)
        style.configure("TLabel", font=("Segoe UI", 9))

        # Cabeçalho (logo + título + tema)
        header = ttk.Frame(self, padding=(8, 6))
        header.pack(fill="x")

        # --- Banner de atualização (inicialmente oculto) ---
        self._update_info = None
        self._update_banner = ttk.Frame(self, padding=(8, 6))
        self._update_banner.pack(fill="x")
        self._update_banner.pack_forget()

        self._lbl_update = ttk.Label(self._update_banner, text="", font=("Segoe UI", 10, "bold"))
        self._lbl_update.pack(side="left")

        btns_up = ttk.Frame(self._update_banner)
        btns_up.pack(side="right")
        ttk.Button(btns_up, text="Atualizar agora", command=self.apply_update_from_banner).pack(side="left", padx=4)
        ttk.Button(btns_up, text="Mais tarde", command=lambda: self._update_banner.pack_forget()).pack(side="left", padx=4)

        # ... (o resto do seu __init__ permanece igual)

        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # inicia checagem automática de atualização
        start_auto_update_thread(self)

    # ===== avisos de atualização =====
    def on_update_available(self, info: dict):
        if self._update_info and self._update_info.get("version") == info.get("version"):
            return
        self._update_info = info
        self._lbl_update.config(
            text=f"Atualização disponível: v{info.get('version')} — clique em 'Atualizar agora' para aplicar."
        )
        self._update_banner.pack(fill="x")

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
        self.geometry("1060x600")
        self.attributes("-alpha", 0.0)  # fade-in
        ensure_dirs()
        self.data = load_data()
        # Deixa explícito (1 instância por job, coalesce)
        self.scheduler = BackgroundScheduler(job_defaults={"max_instances": 1, "coalesce": True})
        self.jobs = {}

        # Estilos
        style = ttk.Style(self)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
        style.configure("TButton", padding=4)
        style.configure("TLabel", font=("Segoe UI", 9))

        # Cabeçalho (logo + título + tema)
        header = ttk.Frame(self, padding=(8, 6))
        header.pack(fill="x")

        # Banner de atualização (inicialmente oculto)
        self._update_info = None
        self._update_banner = ttk.Frame(self, padding=(8, 6))
        self._update_banner.pack(fill="x")
        self._update_banner.pack_forget()

        self._lbl_update = ttk.Label(self._update_banner, text="", font=("Segoe UI", 10, "bold"))
        self._lbl_update.pack(side="left")

        btns_up = ttk.Frame(self._update_banner)
        btns_up.pack(side="right")
        ttk.Button(btns_up, text="Atualizar agora", command=self.apply_update_from_banner)\
       .pack(side="left", padx=4)
        ttk.Button(btns_up, text="Mais tarde", command=lambda: self._update_banner.pack_forget())\
       .pack(side="left", padx=4)


                # --- Ícone da janela / barra de tarefas + logo no cabeçalho ---
        self._logo_img = None

        # 1) Tenta aplicar um .ico (melhor para Windows e barra de tarefas)
        try:
            for ico_path in (
                APP_DIR / "Logo.ico",
                APP_DIR / "Logo" / "Logo.ico",
                resource_path("Logo.ico"),
                resource_path("Logo", "Logo.ico"),
            ):
                if ico_path.exists():
                    self.iconbitmap(default=str(ico_path))
                    break
        except Exception:
            pass

        # 2) Mostra um PNG no cabeçalho e registra com iconphoto (fallback)
        try:
            for png_path in (
                APP_DIR / "logo.png",
                APP_DIR / "Logo" / "logo.png",
                resource_path("logo.png"),
                resource_path("Logo", "logo.png"),
            ):
                if png_path.exists() and Image and ImageTk:
                    _img = ImageTk.PhotoImage(Image.open(png_path).resize((32, 32), Image.LANCZOS))
                    ttk.Label(header, image=_img).pack(side="left")
                    self._logo_img = _img  # evita GC
                    try:
                        self.iconphoto(True, _img)
                    except Exception:
                        pass
                    break
        except Exception:
            pass

        ttk.Label(header, text="Agendador-Bravo", font=("Segoe UI", 12, "bold")).pack(side="left", padx=8)


        try:
            lf = APP_DIR / "logo.png"
            if not lf.exists():
                lf = resource_path("logo.png")
            if lf.exists() and Image and ImageTk:
                _ico = ImageTk.PhotoImage(Image.open(lf).resize((32, 32), Image.LANCZOS))
                self.iconphoto(True, _ico)
                self._icon_img = _ico
        except Exception:
            pass

        self.var_dark = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            header, text="Modo escuro",
            variable=self.var_dark,
            command=lambda: self._apply_theme(self.var_dark.get())
        ).pack(side="right", padx=6)
        self._apply_theme(False)

        # --------- Toolbar responsiva ----------
        toolbar_outer = ttk.Frame(self); toolbar_outer.pack(fill="x")
        self._toolbar_canvas = tk.Canvas(toolbar_outer, height=40, highlightthickness=0)
        self._toolbar_canvas.pack(fill="x", expand=True, side="top")
        self._toolbar_scroll = ttk.Scrollbar(toolbar_outer, orient="horizontal",
                                             command=self._toolbar_canvas.xview)
        self._toolbar_scroll.pack(fill="x", side="bottom")
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
        ttk.Button(bar, text="Nova tarefa", command=self.add_task).pack(side="left")
        ttk.Button(bar, text="Assistente", command=self.open_assistant).pack(side="left", padx=5)
        ttk.Button(bar, text="Editar", command=self.edit_task).pack(side="left")
        ttk.Button(bar, text="Remover", command=self.remove_task).pack(side="left")
        self.btn_run = ttk.Button(bar, text="Executar agora", command=self.run_now)
        self.btn_run.pack(side="left", padx=(10, 0))
        ttk.Button(bar, text="Simular erro", command=self.simulate_error).pack(side="left", padx=5)
        ttk.Button(bar, text="Configurações", command=self.open_settings).pack(side="left", padx=5)
        ttk.Button(bar, text="Abrir pasta de logs", command=lambda: os.startfile(LOG_DIR)).pack(side="left", padx=5)
        ttk.Button(bar, text="Ver último log", command=self.open_last_log).pack(side="left", padx=5)
        ttk.Button(bar, text="Dicas", command=self.show_tips).pack(side="left", padx=5)
        ttk.Button(bar, text="Salvar", command=self.save).pack(side="left", padx=5)

        # --------- Layout principal ----------
        paned = ttk.Panedwindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=6, pady=6)

        left = ttk.Frame(paned)
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)

        cols = ("Nome","Hora","Dias","Arquivo","NotificarFalha","Timeout")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse")
        for c in cols:
            self.tree.heading(c, text=c)
            self.tree.column(c, stretch=True)
        vbar = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        hbar = ttk.Scrollbar(left, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.draw_chart())
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

        # --------- Status bar ----------
        status = ttk.Frame(self, relief="groove", padding=(6,3)); status.pack(fill="x")
        self.status_label = ttk.Label(status, text="Pronto."); self.status_label.pack(side="left")
        self.lbl_mail = ttk.Label(status, text="E-mail ●"); self.lbl_mail.pack(side="right", padx=(0,12))
        self.lbl_wa   = ttk.Label(status, text="WhatsApp ●"); self.lbl_wa.pack(side="right", padx=(0,12))
        self.pbar = ttk.Progressbar(status, mode="indeterminate", length=160); self.pbar.pack(side="right")

        # Dados / agendamento
        self.refresh_table()
        self.reschedule_all()
        self.update_status_indicators()
        self._apply_theme(False)
        self._fade_in()
        self._pulse_status()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ===== Tema / animações =====
    def _apply_theme(self, dark: bool):
        try:
            if sv_ttk:
                sv_ttk.use_dark_theme() if dark else sv_ttk.use_light_theme()
        except Exception:
            pass
        style = ttk.Style(self)
        style.configure("Accent.TButton", font=("Segoe UI", 9, "bold"))
        try:
            self.btn_run.configure(style="Accent.TButton")
        except Exception:
            pass

    def _fade_in(self, target=1.0, step=0.08):
        try:
            a = float(self.attributes("-alpha") or 0.0)
        except Exception:
            return
        a = min(target, a + step)
        self.attributes("-alpha", a)
        if a < target:
            self.after(30, self._fade_in, target, step)

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
        em_ok, wa_ok = self._channels_ok()
        g1, g2 = "#2ecc71", "#27ae60"
        r = "#dc143c"
        t = getattr(self, "_pulse_toggle", False)
        self._pulse_toggle = not t
        self.lbl_mail.config(foreground=(g1 if t else g2) if em_ok else r)
        self.lbl_wa.config(foreground=(g1 if t else g2) if wa_ok else r)
        self.after(650, self._pulse_status)

    # ===== utilidades UI =====
    def _on_tree_resize(self, event=None):
        w = max(300, self.tree.winfo_width())
        ratios = {"Nome":0.18, "Hora":0.08, "Dias":0.22, "Arquivo":0.38, "NotificarFalha":0.08, "Timeout":0.06}
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
        # limpa jobs antigos
        for job in list(self.scheduler.get_jobs()):
            self.scheduler.remove_job(job.id)
        self.jobs.clear()

        for t in self.data.get("tasks", []):
            stype = (t.get("schedule_type") or "cron").lower()

            if stype == "interval":
                # a cada N minutos/horas (respeitando dias marcados)
                try:
                    val = int(str(t.get("every_value") or 0))
                except Exception:
                    val = 0
                if val <= 0:
                    continue
                unit = (t.get("every_unit") or "minutes").lower()

                days = t.get("days", [True] * 7)
                dows = ["mon","tue","wed","thu","fri","sat","sun"]
                use_days = [dows[i] for i, v in enumerate(days) if v]
                if not use_days:
                    continue

                if unit == "minutes":
                    trig = CronTrigger(day_of_week=",".join(use_days), minute=f"*/{val}")
                else:  # hours
                    trig = CronTrigger(day_of_week=",".join(use_days), hour=f"*/{val}", minute=0)
            else:
                # horário(s) fixo(s) nos dias marcados
                days = t.get("days", [True] * 7)
                dows = ["mon","tue","wed","thu","fri","sat","sun"]
                use_days = [dows[i] for i, v in enumerate(days) if v]
                if not use_days:
                    continue

                # aceita lista "times" (novo) ou fallback para "time" único
                times = t.get("times") or [t.get("time", "06:00")]
                # normaliza por segurança
                try:
                    times = parse_times(",".join(times) if isinstance(times, list) else str(times))
                except Exception:
                    continue

                # cria vários triggers (um por horário)
                job_ids = []
                for idx, hhmm in enumerate(times):
                    hh, mm = map(int, hhmm.split(":"))
                    trig = CronTrigger(day_of_week=",".join(use_days), hour=hh, minute=mm)
                    jid = f"{t['name']}::{idx}"
                    self.scheduler.add_job(self._job_wrapper, trigger=trig, id=jid, name=t["name"], args=[t])
                    job_ids.append(jid)
                self.jobs[t["name"]] = job_ids
                continue  # já adicionamos todos os triggers; segue p/ próximo task

            # (para o modo intervalo, continua como antes)
            job = self.scheduler.add_job(self._job_wrapper, trigger=trig, id=t["name"], name=t["name"], args=[t])
            self.jobs[t["name"]] = [job.id]


        if not self.scheduler.running:
            self.scheduler.start()

    def _job_wrapper(self, task):
        def progress(_):
            pass
        rc, dur, log_path = run_task(task, self.data["settings"], progress_cb=progress)
        append_history(self.data, task["name"], rc, dur)
        self._maybe_notify(task, rc, log_path)
        # GUI pode já ter sido destruída (Ctrl+C / fechamento)
        try:
            if self.winfo_exists():
                self.after(0, self.draw_chart)
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
        """Texto das colunas Hora/Dias conforme o tipo de agendamento."""
        if (t.get("schedule_type") or "cron").lower() == "interval":
            val = t.get("every_value") or 0
            unit = (t.get("every_unit") or "minutes").lower()
            hora = f"cada {val} " + ("min" if unit == "minutes" else "h")
            dias = "—"
        else:
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

        for t in self.data.get("tasks", []):
            hora, dias = self._hora_dias_text(t)
            self.tree.insert(
                "", "end", iid=t["name"], values=(
                    t["name"],
                    hora,
                    dias,
                    t["path"],
                    "Sim" if t.get("notify_fail", True) else "Não",
                    t.get("timeout", "0")
                )
            )
        self.draw_chart()

    def draw_chart(self):
        self.canvas.delete("all")
        sel = self.tree.selection()
        if not sel:
            self.canvas.create_text(10, 10, anchor="nw", text="Selecione uma tarefa para ver o histórico.")
            return
        name = sel[0]
        hist = self.data.get("history", {}).get(name, [])
        if not hist:
            self.canvas.create_text(10, 10, anchor="nw", text="Ainda sem histórico para esta tarefa.")
            return

        items = hist[-30:]
        w = int(self.canvas.winfo_width() or 400)
        h = int(self.canvas.winfo_height() or 300)
        pad = 28
        H1 = int(h * 0.62)
        chart_w = w - 2 * pad

        # duração (barras)
        N = len(items)
        chart_h = H1 - pad
        max_dur = max(1.0, max(i["dur"] for i in items))
        bar_w = max(2, int(chart_w / max(N, 1) * 0.7))
        self.canvas.create_text(pad, 8, anchor="nw", text=f"Duração (s) — últimas {N}")
        self.canvas.create_line(pad, H1 - 10, w - pad, H1 - 10)
        for k in (0.25, 0.5, 0.75):
            y = (H1 - 10) - k * chart_h
            self.canvas.create_line(pad, y, w - pad, y, fill="#e0e0e0")
        for idx, it in enumerate(items):
            x_center = pad + (idx + 0.5) * (chart_w / N)
            bh = (it["dur"] / max_dur) * (chart_h - 6)
            y0 = (H1 - 10) - bh
            color = "#3cb371" if it["rc"] == 0 else "#dc143c"
            self.canvas.create_rectangle(x_center - bar_w / 2, y0,
                                         x_center + bar_w / 2, H1 - 10,
                                         fill=color, outline="")

        # legenda
        self.canvas.create_rectangle(w - pad - 120, 6, w - pad - 102, 24, fill="#3cb371", outline="")
        self.canvas.create_text(w - pad - 96, 15, text="Ok", anchor="w")
        self.canvas.create_rectangle(w - pad - 60, 6, w - pad - 42, 24, fill="#dc143c", outline="")
        self.canvas.create_text(w - pad - 36, 15, text="Falha", anchor="w")

        # acumulado OK x Falha
        ok = sum(1 for i in items if i["rc"] == 0)
        fail = N - ok
        total = max(1, N)
        y_top = H1 + 10
        self.canvas.create_text(pad, y_top, anchor="nw", text=f"Acertos x Falhas — últimas {N}")
        y_bar = y_top + 18
        bar_h = max(18, h - y_bar - 14)
        full_w = w - 2 * pad
        ok_w = int(full_w * (ok / total))
        fail_w = full_w - ok_w
        self.canvas.create_rectangle(pad, y_bar, pad + ok_w, y_bar + bar_h, fill="#3cb371", outline="")
        self.canvas.create_rectangle(pad + ok_w, y_bar, pad + ok_w + fail_w, y_bar + bar_h, fill="#dc143c", outline="")
        self.canvas.create_text(pad + 6, y_bar + bar_h / 2, text=f"{ok} OK", anchor="w", fill="white")
        self.canvas.create_text(pad + ok_w + fail_w - 6, y_bar + bar_h / 2, text=f"{fail} Falhas", anchor="e", fill="white")

    # ===== ações =====
    def add_task(self):
        dlg = TaskDialog(self); self.wait_window(dlg)
        if dlg.result:
            if any(t["name"] == dlg.result["name"] for t in self.data["tasks"]):
                messagebox.showerror("Erro", "Já existe uma tarefa com esse nome."); return
            self.data["tasks"].append(dlg.result)
            self.save(silent=True); self.refresh_table(); self.reschedule_all()

    def open_assistant(self):
        dlg = AssistantDialog(self); self.wait_window(dlg)
        if not dlg.result: return
        td = TaskDialog(self, dlg.result); self.wait_window(td)
        if td.result:
            if any(t["name"] == td.result["name"] for t in self.data["tasks"]):
                messagebox.showerror("Erro", "Já existe uma tarefa com esse nome."); return
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
