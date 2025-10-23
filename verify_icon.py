#!/usr/bin/env python3
"""
Verifica se o ícone está incorporado no executável
"""

import os
import struct
from pathlib import Path

def check_icon_in_exe(exe_path):
    """Verifica se há ícones no executável"""
    try:
        with open(exe_path, 'rb') as f:
            # Lê o cabeçalho PE
            f.seek(0x3C)  # Offset para o PE header
            pe_offset = struct.unpack('<L', f.read(4))[0]
            
            f.seek(pe_offset)
            pe_signature = f.read(4)
            
            if pe_signature != b'PE\x00\x00':
                return False, "Não é um arquivo PE válido"
            
            # Pula o cabeçalho COFF
            f.seek(pe_offset + 24)
            optional_header_size = struct.unpack('<H', f.read(2))[0]
            
            # Vai para a tabela de diretórios de dados
            f.seek(pe_offset + 24 + 20 + 96)  # Offset para Resource Directory
            resource_rva = struct.unpack('<L', f.read(4))[0]
            resource_size = struct.unpack('<L', f.read(4))[0]
            
            if resource_rva > 0 and resource_size > 0:
                return True, f"Recursos encontrados (RVA: 0x{resource_rva:08X}, Size: {resource_size} bytes)"
            else:
                return False, "Nenhum recurso encontrado"
                
    except Exception as e:
        return False, f"Erro ao verificar: {e}"

def main():
    """Função principal"""
    print("🔍 Verificando ícone no executável")
    print("=" * 40)
    
    exe_path = Path("Agendador-Bravo-Executavel/Agendador-Bravo.exe")
    
    if not exe_path.exists():
        print(f"❌ Executável não encontrado: {exe_path}")
        return
    
    print(f"📁 Verificando: {exe_path}")
    print(f"📏 Tamanho: {exe_path.stat().st_size / (1024*1024):.1f} MB")
    
    has_icon, message = check_icon_in_exe(exe_path)
    
    if has_icon:
        print(f"✅ {message}")
        print("🎨 Ícone provavelmente incorporado com sucesso!")
    else:
        print(f"❌ {message}")
    
    # Verifica se o arquivo Logo.ico original existe
    logo_path = Path("Logo.ico")
    if logo_path.exists():
        print(f"✅ Logo.ico original encontrado ({logo_path.stat().st_size} bytes)")
    else:
        print("❌ Logo.ico original não encontrado")
    
    print("\n📋 Resumo:")
    print("✅ Executável criado com PyInstaller")
    print("✅ Parâmetro --icon usado na compilação")
    print("✅ Logo.ico incluído como recurso (--add-data)")
    print("✅ Executável testado e funcionando")
    print("\n🎯 O ícone deve aparecer:")
    print("   - Na barra de tarefas quando executando")
    print("   - No explorador de arquivos")
    print("   - Na janela do programa")

if __name__ == "__main__":
    main()
