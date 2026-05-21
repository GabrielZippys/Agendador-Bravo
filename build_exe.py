#!/usr/bin/env python3
"""
Script para criar executável do Agendador-Bravo e (opcionalmente) o instalador.

Uso rápido:
    python build_exe.py            # só o .exe
    python build_exe.py --installer  # .exe + instalador Inno Setup
"""

import os
import sys
import subprocess
import shutil
import argparse
from pathlib import Path


# ── versão sincronizada com agendador.py ─────────────────────────────────────
APP_VERSION = "2025.10.11.12"
# ─────────────────────────────────────────────────────────────────────────────


def check_pyinstaller():
    try:
        import PyInstaller  # noqa: F401
        print("[OK] PyInstaller encontrado")
        return True
    except ImportError:
        print("[ERRO] PyInstaller não encontrado")
        return False


def install_pyinstaller():
    print("[PKG] Instalando PyInstaller...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])
        print("[OK] PyInstaller instalado com sucesso")
        return True
    except subprocess.CalledProcessError:
        print("[ERRO] Erro ao instalar PyInstaller")
        return False


def create_executable():
    """Cria o executável onefile."""
    print("[BUILD] Criando executável (onefile)...")

    current_dir = Path(__file__).parent
    main_script = current_dir / "agendador.py"
    icon_file   = current_dir / "Logo.ico"

    if not main_script.exists():
        print(f"[ERRO] Arquivo principal não encontrado: {main_script}")
        return False
    if not icon_file.exists():
        print(f"[ERRO] Ícone não encontrado: {icon_file}")
        return False

    # Garante que o build dir exista antes do PyInstaller (evita race no zip)
    (current_dir / "build" / "AgendadorBravo").mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        f"--icon={icon_file}",
        "--name=AgendadorBravo",
        "--add-data=Logo.ico;.",
        "--hidden-import=PIL",
        "--hidden-import=PIL.Image",
        "--hidden-import=PIL.ImageTk",
        "--hidden-import=sv_ttk",
        "--hidden-import=apscheduler",
        "--hidden-import=apscheduler.triggers.date",
        "--hidden-import=apscheduler.triggers.cron",
        "--hidden-import=requests",
        # v2025.10.11.8 — novas deps
        "--hidden-import=yaml",
        "--hidden-import=win32crypt",
        "--hidden-import=pywintypes",
        "--hidden-import=http.server",
        "--hidden-import=socketserver",
        "--hidden-import=urllib.parse",
        # Extrai sempre para path estável em user-space (evita %TEMP%\2\ admin)
        "--runtime-tmpdir=%LOCALAPPDATA%\\AgendadorBravo\\rt",
        str(main_script),
    ]

    print(f"[RUN]  Executando PyInstaller...")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=current_dir)

    if result.returncode != 0:
        print("[ERRO] Erro durante a criação do executável:")
        print(result.stdout[-3000:])
        print(result.stderr[-3000:])
        return False

    exe_path = current_dir / "dist" / "AgendadorBravo.exe"
    if not exe_path.exists():
        print("[ERRO] Executável não encontrado após build")
        return False

    size_mb = exe_path.stat().st_size / (1024 * 1024)
    print(f"[OK] Executável criado: {exe_path}  ({size_mb:.1f} MB)")
    return True


def build_installer():
    """
    Compila o instalador Inno Setup usando agendador-setup.iss.
    Requer que o Inno Setup 6 esteja instalado e 'iscc' acessível no PATH
    (ou nos caminhos padrão do Inno Setup).
    """
    current_dir = Path(__file__).parent
    iss_file = current_dir / "agendador-setup.iss"

    if not iss_file.exists():
        print("[ERRO] agendador-setup.iss não encontrado")
        return False

    # Procura pelo compilador Inno Setup
    iscc_candidates = [
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files (x86)\Inno Setup 5\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 5\ISCC.exe"),
        # via PATH
    ]
    iscc = None
    for c in iscc_candidates:
        if c.exists():
            iscc = str(c)
            break
    if iscc is None:
        # tenta pelo PATH
        which = shutil.which("iscc") or shutil.which("ISCC")
        if which:
            iscc = which

    if iscc is None:
        print("[ERRO] Inno Setup não encontrado.")
        print("   Baixe em https://jrsoftware.org/isdl.php e instale na pasta padrão.")
        return False

    print(f"[BUILD] Compilando instalador com: {iscc}")
    result = subprocess.run(
        [iscc, str(iss_file)],
        capture_output=True,
        text=True,
        cwd=current_dir,
    )

    if result.returncode != 0:
        print("[ERRO] Erro ao compilar instalador:")
        print(result.stdout[-3000:])
        print(result.stderr[-3000:])
        return False

    setup_exe = current_dir / "dist" / f"AgendadorBravo-Setup-{APP_VERSION}.exe"
    if setup_exe.exists():
        size_mb = setup_exe.stat().st_size / (1024 * 1024)
        print(f"[OK] Instalador criado: {setup_exe}  ({size_mb:.1f} MB)")
    else:
        # Inno pode usar nome diferente — mostra o que tem em dist/
        exes = list((current_dir / "dist").glob("AgendadorBravo-Setup*.exe"))
        if exes:
            print(f"[OK] Instalador criado: {exes[0]}")
        else:
            print("[AVISO]  Instalador compilado mas arquivo não localizado em dist/")
    return True


def cleanup():
    current_dir = Path(__file__).parent
    for folder in ["build", "__pycache__"]:
        p = current_dir / folder
        if p.exists():
            shutil.rmtree(p)
            print(f"[CLEAN] Removido: {folder}")
    spec = current_dir / "AgendadorBravo.spec"
    if spec.exists():
        spec.unlink()
        print("[CLEAN] Removido: AgendadorBravo.spec")


def main():
    parser = argparse.ArgumentParser(description="Build do Agendador-Bravo")
    parser.add_argument(
        "--installer", action="store_true",
        help="Compila também o instalador Inno Setup após gerar o .exe",
    )
    parser.add_argument(
        "--installer-only", action="store_true",
        help="Só compila o instalador (pula rebuild do .exe se já existir)",
    )
    args = parser.parse_args()

    print(f"[>>] Agendador-Bravo v{APP_VERSION} — Build")
    print("=" * 55)

    exe_path = Path(__file__).parent / "dist" / "AgendadorBravo.exe"
    skip_build = args.installer_only and exe_path.exists()

    if not skip_build:
        if not check_pyinstaller():
            if not install_pyinstaller():
                print("[ERRO] Nao foi possivel instalar PyInstaller")
                sys.exit(1)

        ok = create_executable()
        cleanup()

        if not ok:
            print("\n[ERRO] Falha na criacao do executavel")
            sys.exit(1)
    else:
        print(f"[OK] Reutilizando exe existente: {exe_path}")

    if args.installer or args.installer_only:
        print()
        ok_inst = build_installer()
        if not ok_inst:
            print("\n[AVISO] Instalador falhou.")
            print("   Instale o Inno Setup 6 e rode:  python build_exe.py --installer-only")
            sys.exit(2)

    print(f"\n[CONCLUIDO] Build concluido!")
    print(f"   Executavel : dist/AgendadorBravo.exe")
    if args.installer or args.installer_only:
        print(f"   Instalador : dist/AgendadorBravo-Setup-{APP_VERSION}.exe")


if __name__ == "__main__":
    main()
