# Agendador-Bravo

Agendador gráfico para Windows (Tkinter) que executa **scripts e processos** em horários fixos ou em **intervalos**, com **histórico visual**, **notificações por e-mail** e **WhatsApp (QR via WebJS)**, além de **autoatualização** opcional.

> Testado com **Python 3.13** no Windows 10/11.

---

## ✨ Recursos

* ✅ **Agendamento por horário(s)** (cron-like) **ou por intervalo** (minutos/horas)
* 🗓️ Marcação de **dias da semana** por tarefa
* 📄 **Assistente** para sugerir comando/args a partir de um arquivo
* 📨 **Notificações** de falha por **e-mail** e/ou **WhatsApp (QR)**
* 📊 **Histórico** com duração e status (OK/Falha) + gráfico embutido
* 🧰 Suporte a `.exe`, `.bat/.cmd`, `.ps1`, `.py`, `.ktr`/`.kjb` (Pentaho)
* 🔄 **Auto-update** via manifesto remoto (opcional)
* 🌓 Tema claro/escuro (sv-ttk, opcional)
* 🧪 Botão de **Simular erro** (para testar notificações)

---

## 📦 Requisitos

* **Windows** + **Python 3.10+** (usado 3.13)

* Tkinter (vem com o Python oficial)

* Pacotes Python:

  * `apscheduler`
  * `sv-ttk` *(opcional, temas)*
  * `Pillow` *(opcional, exibir logo PNG no header)*
  * `psutil` *(opcional, checagem de PID)*
  * `twilio` *(opcional, se quiser modo WhatsApp via Twilio em vez de QR)*

* **Node.js** (apenas se usar WhatsApp QR)

* Script `wa/wa_send.js` (incluído na pasta `wa/` do projeto)

---

## 🚀 Instalação e execução (dev)

```powershell
# 1) Clone
git clone https://github.com/seu-usuario/Agendador-Bravo.git
cd Agendador-Bravo

# 2) Ambiente virtual
python -m venv .venv
. .\.venv\Scripts\Activate.ps1

# 3) Dependências
python -m pip install --upgrade pip
pip install apscheduler sv-ttk pillow psutil twilio

# 4) Rode
python agendador_pro.py
```

> Dica: se não for usar tema/WhatsApp/Twilio, pode omitir esses pacotes.

---

## 🧰 Empacotar (EXE com PyInstaller)

```powershell
pip install pyinstaller

pyinstaller ^
  --noconfirm ^
  --windowed ^
  --name "Agendador-Bravo" ^
  --icon "Logo.ico" ^
  --add-data "Logo.ico;." ^
  --add-data "logo.png;." ^
  --add-data "wa;wa" ^
  agendador_pro.py
```

O executável sairá em `dist/Agendador-Bravo/Agendador-Bravo.exe`.

> O app cria/usa pastas graváveis em:
> `C:\ProgramData\AgendadorBravo\` (config, logs, cache do WA, pids, etc.)

---

## 🛠️ Uso rápido

1. **Abrir** o app.
2. Clique em **“Nova tarefa”** ou use o **Assistente**.
3. Preencha:

   * **Arquivo/Comando** (ex.: `C:\Program Files\nodejs\node.exe` ou `python.exe` ou script .bat/.ps1/.exe)
   * **Argumentos** (ex.: `bot.js` ou `seu_script.py`)
   * **Pasta de trabalho** (onde o arquivo reside)
4. Escolha **Horários…** (um ou vários) *ou* mude para **Intervalo** (ex.: a cada 30 minutos).
5. Marque **dias da semana** e se quer **Notificar ao falhar**.
6. **Salvar** a tarefa.

### Configurações

* **PDI Home**: caminho do Pentaho (`data-integration`) para `.ktr/.kjb`.
* **E-mail (SMTP)**: host, porta, usuário, senha, de/para (pode testar).
* **WhatsApp (QR)**: caminho do `node.exe`, `wa_send.js`, seu número (informativo) e destinos (ex.: `group:Nome do Grupo` ou `+55xxxxxxxxxx`).
  Use **Testar WhatsApp** para abrir a janela do Node e capturar o QR.

### Dicas rápidas

* **Pentaho**: basta selecionar o `.ktr`/`.kjb`; o app chamará `Pan.bat`/`Kitchen.bat` do PDI Home.
* **Spawn** (“Executar em segundo plano”): não espera o término; grava PID para evitar instâncias duplicadas.
* **Logs**: botão **Abrir pasta de logs**; **Ver último log** abre direto.
* **Histórico**: selecione a tarefa para ver o gráfico.

---

## 🔔 Notificações

* **E-mail**: usa TLS (STARTTLS). Ative “Senha de app” quando usar Gmail.
* **WhatsApp (QR)**: roda `node wa/wa_send.js` em uma pasta de cache própria.
  Primeira execução pede o **QR Code** no WhatsApp do **número emissor**.

Se uma notificação falhar, o erro aparece no console/log.

---

## 🔄 Auto-Update (opcional)

Defina as variáveis de ambiente para apontar o manifesto:

* `AGENDADOR_UPDATE_MANIFEST` – URL do `manifest.json`
* `AGENDADOR_UPDATE_EVERY_MIN` – intervalo de checagem (min), padrão **240**

**Exemplo de `manifest.json`:**

```json
{
  "version": "2025.09.20.0",
  "exe_url": "https://seu-servidor/Agendador-Bravo.exe",
  "sha256": "abcdef0123... (64 hex)"
}
```

* Se estiver rodando **empacotado** (PyInstaller), o app baixa e troca o `.exe` com um script `.cmd` temporário.
* Em **modo dev** (rodando `.py`), ele apenas avisa que há nova versão.

---

## 🗂️ Estrutura de dados

* `C:\ProgramData\AgendadorBravo\config.json`

  * `settings.pdi_home`
  * `settings.email` (enabled, smtp\_host, smtp\_port, username, password, from\_email, to\_emails)
  * `settings.whatsapp` (enabled, mode, node\_path, webjs\_script, my\_number, to\_targets)
  * `tasks`: lista de tarefas

    * `name, path, args, working_dir, schedule_type (cron|interval), times[], every_value, every_unit, days[7], timeout, notify_fail, spawn`
  * `history`: últimos resultados por tarefa

* Pastas:

  * `/logs` – arquivos de log rotacionados por execução
  * `/wa` – cache do WhatsApp WebJS
  * `/pids` – arquivos PID para modo spawn

---

## 🧩 Exemplos comuns

**Node.js (bot.js)**

* Arquivo/Comando: `C:\Program Files\nodejs\node.exe`
* Argumentos: `bot.js`
* Pasta: pasta onde está o `bot.js`

**Python (script.py)**

* Arquivo/Comando: `python` (ou caminho do `python.exe` do venv)
* Argumentos: `script.py`
* Pasta: onde está o `script.py`

**Pentaho (.ktr)**

* Arquivo/Comando: selecione o próprio `.ktr`
* PDI Home: aponte para `C:\Pentaho\data-integration`

---

## 🐞 Solução de problemas

* **Erro ao abrir “Configurações”**: certifique-se de que o arquivo não tem duplicidades de métodos (`__init__`) e que a classe `SettingsDialog` não foi “colada” com partes do `AssistantDialog`.
* **Logs sem acentuação**: o app força `UTF-8` nos processos Python filhos via `PYTHONIOENCODING`/`PYTHONUTF8`.
* **WhatsApp QR não envia**: confira `node.exe`, `wa_send.js` e os **destinos** (use `group:Nome do Grupo` ou número internacional `+55...`). Faça o **Teste WhatsApp** para relogar o QR.
* **Tarefa em spawn duplicada**: o app usa arquivo **PID** + `tasklist`/`psutil`. Se travou, apague o `.pid` na pasta `/pids`.

---

## 🤝 Contribuindo

1. Faça um fork, crie uma branch: `feat/minha-ideia`
2. Commit com mensagens claras
3. PR com descrição do que mudou

---

## 📜 Licença

Defina a licença do projeto (ex.: MIT).
Exemplo: © 2025 Seu Nome — liberado sob **MIT**.

---

## 🖼️ Screenshots (opcional)

* Tela principal com lista de tarefas
* Diálogo “Tarefa” com editor de horários
* “Configurações” com SMTP/WhatsApp
* Gráfico de histórico
