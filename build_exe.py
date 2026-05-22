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
APP_VERSION = "2025.10.11.19"
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
    """v2025.10.11.14 — Cria a aplicação em modo ONEDIR (pasta com .exe + DLLs).

    Por que mudamos de --onefile pra --onedir:
    - O bug 'Failed to load python313.dll' acontecia porque --onefile extraía
      todos os DLLs num _MEI temporário a cada execução. O antivírus (Defender)
      bloqueava ou removia DLLs durante a extração → LoadLibrary falhava.
    - Com --onedir, todos os arquivos ficam no disco desde o install. O AV
      escaneia UMA vez (no install), e o app abre direto sem extração.
    - Boot time fica mais rápido (sem extrair 25 MB de DLLs a cada run).
    """
    print("[BUILD] Criando aplicacao em modo ONEDIR (pasta com .exe + DLLs)...")

    current_dir = Path(__file__).parent
    main_script = current_dir / "agendador.py"
    icon_file   = current_dir / "Logo.ico"

    if not main_script.exists():
        print(f"[ERRO] Arquivo principal não encontrado: {main_script}")
        return False
    if not icon_file.exists():
        print(f"[ERRO] Ícone não encontrado: {icon_file}")
        return False

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onedir",                    # << pasta com tudo (não mais .exe único)
        "--windowed",
        "--noupx",                     # << evita UPX (AV trata UPX como suspeito)
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
        "--hidden-import=yaml",
        "--hidden-import=win32crypt",
        "--hidden-import=pywintypes",
        "--hidden-import=http.server",
        "--hidden-import=socketserver",
        "--hidden-import=urllib.parse",
        str(main_script),
    ]

    print(f"[RUN]  Executando PyInstaller...")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=current_dir)

    if result.returncode != 0:
        print("[ERRO] Erro durante a criação do executável:")
        print(result.stdout[-3000:])
        print(result.stderr[-3000:])
        return False

    # Em modo onedir, o resultado é uma PASTA dist/AgendadorBravo/ contendo
    # AgendadorBravo.exe + _internal/ com todos os DLLs e libs.
    onedir_path = current_dir / "dist" / "AgendadorBravo"
    exe_inside = onedir_path / "AgendadorBravo.exe"
    if not onedir_path.exists() or not exe_inside.exists():
        print("[ERRO] Pasta onedir não criada conforme esperado")
        return False

    # Conta tamanho total da pasta
    total_size = sum(p.stat().st_size for p in onedir_path.rglob("*") if p.is_file())
    file_count = sum(1 for p in onedir_path.rglob("*") if p.is_file())
    print(f"[OK] Build onedir: {onedir_path}")
    print(f"     {file_count} arquivos, {total_size / 1024 / 1024:.1f} MB total")
    return True


def create_distribution_zip():
    """v2025.10.11.14 — Gera ZIP da pasta onedir pra auto-update."""
    import zipfile
    current_dir = Path(__file__).parent
    onedir = current_dir / "dist" / "AgendadorBravo"
    if not onedir.exists():
        print("[ERRO] dist/AgendadorBravo nao existe; rode o build primeiro")
        return False
    zip_path = current_dir / "dist" / f"AgendadorBravo-{APP_VERSION}.zip"
    print(f"[ZIP] Empacotando {onedir} -> {zip_path.name}...")
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in onedir.rglob("*"):
            if p.is_file():
                # arcname mantém estrutura AgendadorBravo/...
                zf.write(p, arcname=str(p.relative_to(onedir.parent)))
    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"[OK] ZIP criado: {zip_path}  ({size_mb:.1f} MB)")
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

    onedir_path = Path(__file__).parent / "dist" / "AgendadorBravo"
    skip_build = args.installer_only and (onedir_path / "AgendadorBravo.exe").exists()

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

        # v2025.10.11.14 — Gera ZIP do onedir pra auto-update
        create_distribution_zip()
    else:
        print(f"[OK] Reutilizando onedir existente: {onedir_path}")

    if args.installer or args.installer_only:
        print()
        ok_inst = build_installer()
        if not ok_inst:
            print("\n[AVISO] Instalador falhou.")
            print("   Instale o Inno Setup 6 e rode:  python build_exe.py --installer-only")
            sys.exit(2)

    print(f"\n[CONCLUIDO] Build concluido!")
    print(f"   App (onedir) : dist/AgendadorBravo/")
    print(f"   ZIP update   : dist/AgendadorBravo-{APP_VERSION}.zip")
    if args.installer or args.installer_only:
        print(f"   Instalador   : dist/AgendadorBravo-Setup-{APP_VERSION}.exe")


if __name__ == "__main__":
    main()
