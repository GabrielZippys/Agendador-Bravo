#!/usr/bin/env python3
"""
Script para criar executável do Agendador-Bravo
Inclui o logo.ico e todas as dependências necessárias
"""

import os
import sys
import subprocess
import shutil
from pathlib import Path

def check_pyinstaller():
    """Verifica se PyInstaller está instalado"""
    try:
        import PyInstaller
        print("✅ PyInstaller encontrado")
        return True
    except ImportError:
        print("❌ PyInstaller não encontrado")
        return False

def install_pyinstaller():
    """Instala PyInstaller"""
    print("📦 Instalando PyInstaller...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])
        print("✅ PyInstaller instalado com sucesso")
        return True
    except subprocess.CalledProcessError:
        print("❌ Erro ao instalar PyInstaller")
        return False

def create_executable():
    """Cria o executável"""
    print("🔨 Criando executável...")
    
    # Diretório atual
    current_dir = Path(__file__).parent
    
    # Arquivos necessários
    main_script = current_dir / "agendador.py"
    icon_file = current_dir / "Logo.ico"
    
    # Verifica se os arquivos existem
    if not main_script.exists():
        print(f"❌ Arquivo principal não encontrado: {main_script}")
        return False
    
    if not icon_file.exists():
        print(f"❌ Ícone não encontrado: {icon_file}")
        return False
    
    # Comando PyInstaller
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",                    # Arquivo único
        "--windowed",                   # Sem console
        f"--icon={icon_file}",          # Ícone do executável
        "--name=Agendador-Bravo",       # Nome do executável
        "--add-data=Logo.ico;.",        # Inclui o ícone nos recursos
        "--hidden-import=PIL",          # Importações ocultas
        "--hidden-import=PIL.Image",
        "--hidden-import=PIL.ImageTk",
        "--hidden-import=sv_ttk",
        "--hidden-import=apscheduler",
        "--hidden-import=requests",
        "--clean",                      # Limpa cache anterior
        str(main_script)
    ]
    
    try:
        print("⚙️ Executando PyInstaller...")
        print(f"Comando: {' '.join(cmd)}")
        
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=current_dir)
        
        if result.returncode == 0:
            print("✅ Executável criado com sucesso!")
            
            # Localiza o executável
            exe_path = current_dir / "dist" / "Agendador-Bravo.exe"
            if exe_path.exists():
                print(f"📁 Executável disponível em: {exe_path}")
                print(f"📏 Tamanho: {exe_path.stat().st_size / (1024*1024):.1f} MB")
                
                # Cria pasta de distribuição limpa
                dist_folder = current_dir / "Agendador-Bravo-Executavel"
                if dist_folder.exists():
                    shutil.rmtree(dist_folder)
                dist_folder.mkdir()
                
                # Copia executável
                shutil.copy2(exe_path, dist_folder / "Agendador-Bravo.exe")
                
                # Cria README
                readme_content = """# Agendador-Bravo - Executável

## Como usar:
1. Execute o arquivo Agendador-Bravo.exe
2. O programa iniciará automaticamente
3. Todas as configurações são salvas em agendador_data.json

## Recursos incluídos:
- ✅ Interface moderna e responsiva
- ✅ Sistema de temas (claro/escuro)
- ✅ Controle de ativação/desativação de tarefas
- ✅ Gráficos e histórico de execuções
- ✅ Notificações por email e WhatsApp
- ✅ Logs detalhados
- ✅ Assistente inteligente

## Requisitos:
- Windows 10/11
- Nenhuma instalação adicional necessária

Desenvolvido com ❤️ usando Python e Tkinter
"""
                
                with open(dist_folder / "README.txt", "w", encoding="utf-8") as f:
                    f.write(readme_content)
                
                print(f"📦 Pacote completo criado em: {dist_folder}")
                return True
            else:
                print("❌ Executável não encontrado após build")
                return False
        else:
            print("❌ Erro durante a criação do executável:")
            print(result.stdout)
            print(result.stderr)
            return False
            
    except Exception as e:
        print(f"❌ Erro: {e}")
        return False

def cleanup():
    """Limpa arquivos temporários"""
    current_dir = Path(__file__).parent
    
    # Remove pastas temporárias do PyInstaller
    for folder in ["build", "__pycache__"]:
        folder_path = current_dir / folder
        if folder_path.exists():
            shutil.rmtree(folder_path)
            print(f"🧹 Removido: {folder}")
    
    # Remove arquivo .spec
    spec_file = current_dir / "Agendador-Bravo.spec"
    if spec_file.exists():
        spec_file.unlink()
        print(f"🧹 Removido: {spec_file}")

def main():
    """Função principal"""
    print("🚀 Agendador-Bravo - Criador de Executável")
    print("=" * 50)
    
    # Verifica PyInstaller
    if not check_pyinstaller():
        if not install_pyinstaller():
            print("❌ Não foi possível instalar PyInstaller")
            return False
    
    # Cria executável
    success = create_executable()
    
    # Limpa arquivos temporários
    cleanup()
    
    if success:
        print("\n🎉 Processo concluído com sucesso!")
        print("📁 Verifique a pasta 'Agendador-Bravo-Executavel' para o arquivo final")
    else:
        print("\n❌ Falha na criação do executável")
    
    return success

if __name__ == "__main__":
    main()
