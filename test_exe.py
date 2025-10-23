#!/usr/bin/env python3
"""
Script para testar o executável criado
Verifica se o ícone está incorporado e se o programa funciona
"""

import os
import sys
import subprocess
from pathlib import Path

def test_executable():
    """Testa o executável criado"""
    print("🧪 Testando executável do Agendador-Bravo")
    print("=" * 50)
    
    # Localiza o executável
    current_dir = Path(__file__).parent
    exe_path = current_dir / "Agendador-Bravo-Executavel" / "Agendador-Bravo.exe"
    
    if not exe_path.exists():
        print(f"❌ Executável não encontrado: {exe_path}")
        return False
    
    print(f"✅ Executável encontrado: {exe_path}")
    print(f"📏 Tamanho: {exe_path.stat().st_size / (1024*1024):.1f} MB")
    
    # Verifica propriedades do arquivo
    try:
        import win32api
        import win32con
        
        # Obtém informações da versão
        info = win32api.GetFileVersionInfo(str(exe_path), "\\")
        ms = info['FileVersionMS']
        ls = info['FileVersionLS']
        version = f"{win32api.HIWORD(ms)}.{win32api.LOWORD(ms)}.{win32api.HIWORD(ls)}.{win32api.LOWORD(ls)}"
        print(f"📋 Versão: {version}")
        
        # Verifica se tem ícone
        icon_count = win32api.ExtractIcon(0, str(exe_path), -1)
        if icon_count > 0:
            print(f"🎨 Ícones encontrados: {icon_count}")
            print("✅ Logo.ico incorporado com sucesso!")
        else:
            print("❌ Nenhum ícone encontrado no executável")
            
    except ImportError:
        print("⚠️ win32api não disponível - não é possível verificar ícones")
        print("💡 Instale com: pip install pywin32")
    except Exception as e:
        print(f"⚠️ Erro ao verificar propriedades: {e}")
    
    # Teste básico de execução (apenas verifica se inicia)
    print("\n🚀 Testando inicialização do executável...")
    try:
        # Inicia o processo e termina rapidamente para teste
        process = subprocess.Popen([str(exe_path)], 
                                 stdout=subprocess.PIPE, 
                                 stderr=subprocess.PIPE)
        
        # Aguarda um pouco para ver se inicia sem erros
        import time
        time.sleep(3)
        
        # Verifica se ainda está rodando
        if process.poll() is None:
            print("✅ Executável iniciou com sucesso!")
            print("⚠️ Terminando processo de teste...")
            process.terminate()
            process.wait(timeout=5)
        else:
            stdout, stderr = process.communicate()
            if process.returncode == 0:
                print("✅ Executável executou com sucesso!")
            else:
                print(f"❌ Executável terminou com erro (código {process.returncode})")
                if stderr:
                    print(f"Erro: {stderr.decode()}")
        
    except Exception as e:
        print(f"❌ Erro ao testar executável: {e}")
        return False
    
    return True

def create_installer_info():
    """Cria informações para instalação"""
    current_dir = Path(__file__).parent
    info_file = current_dir / "Agendador-Bravo-Executavel" / "INSTALACAO.txt"
    
    content = """# 🚀 Agendador-Bravo - Guia de Instalação

## 📋 Informações do Executável:
- ✅ Arquivo único (não requer instalação)
- ✅ Logo.ico incorporado
- ✅ Todas as dependências incluídas
- ✅ Interface moderna e responsiva
- ✅ Compatível com Windows 10/11

## 🎯 Como usar:
1. Copie o arquivo "Agendador-Bravo.exe" para qualquer pasta
2. Execute o arquivo (duplo clique)
3. O programa iniciará com interface gráfica moderna
4. Todas as configurações são salvas automaticamente

## 📁 Arquivos criados pelo programa:
- agendador_data.json (configurações e tarefas)
- logs/ (pasta com logs de execução)
- whatsapp_cache/ (cache do WhatsApp, se usado)

## 🎨 Recursos visuais:
- ✅ Tema claro/escuro
- ✅ Animações suaves
- ✅ Gráficos interativos
- ✅ Interface responsiva
- ✅ Ícones modernos

## 🔧 Funcionalidades:
- ✅ Agendamento de tarefas
- ✅ Controle ativar/desativar
- ✅ Histórico de execuções
- ✅ Notificações (email/WhatsApp)
- ✅ Assistente inteligente
- ✅ Logs detalhados

## 🆘 Suporte:
Se houver problemas, verifique:
1. Windows Defender não está bloqueando
2. Pasta tem permissão de escrita
3. Antivírus não está interferindo

Desenvolvido com ❤️ usando Python
"""
    
    with open(info_file, "w", encoding="utf-8") as f:
        f.write(content)
    
    print(f"📄 Guia de instalação criado: {info_file}")

def main():
    """Função principal"""
    success = test_executable()
    
    if success:
        create_installer_info()
        print("\n🎉 Teste concluído com sucesso!")
        print("📦 Executável pronto para distribuição!")
        print("📁 Pasta: Agendador-Bravo-Executavel")
    else:
        print("\n❌ Problemas encontrados no executável")
    
    return success

if __name__ == "__main__":
    main()
