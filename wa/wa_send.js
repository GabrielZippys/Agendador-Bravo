// wa_send.js
// Envia mensagens via WhatsApp Web (whatsapp-web.js)
// Uso:
//   node wa_send.js --to "group:MDW Bravo | BI, number:+5511999999999" --message "Olá!"
//   node wa_send.js --list-chats   (lista grupos/contatos visíveis)

const minimist = require('minimist');
const qrcode = require('qrcode-terminal');
const path = require('path');
const fs = require('fs');
const { Client, LocalAuth } = require('whatsapp-web.js');

const args = minimist(process.argv.slice(2));
const wantList = !!args['list-chats'];
const toArg = (args.to || '').toString();
const message = (args.message || '').toString();

if (!wantList && (!toArg || !message)) {
  console.error('Uso: node wa_send.js --to "<destinos>" --message "<texto>"');
  console.error('      Destinos: group:<nome do grupo>, number:+5511999999999 (separados por vírgula)');
  console.error('      Ou: node wa_send.js --list-chats');
  process.exit(2);
}

// Persistência do login (QR) em pasta compartilhada do sistema
const dataDir =
  process.env.WA_DATA_DIR ||
  path.join(process.env.ALLUSERSPROFILE || 'C:\\ProgramData', 'AgendadorBravo', 'wa-data');

fs.mkdirSync(dataDir, { recursive: true });

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: dataDir }),
  puppeteer: {
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox'],
  },
});

client.on('qr', (qr) => {
  console.log('Escaneie o QR com o WhatsApp deste remetente:');
  qrcode.generate(qr, { small: true });
});

client.on('auth_failure', (msg) => console.error('Falha na autenticação:', msg));
client.on('disconnected', (reason) => console.log('Desconectado:', reason));

client.on('ready', async () => {
  console.log('Client READY');

  const chats = await client.getChats();

  if (wantList) {
    console.log('--- GRUPOS ---');
    chats.filter((c) => c.isGroup).forEach((c, i) => console.log(`${i + 1}. ${c.name}`));
    console.log('--- CONTATOS ---');
    chats
      .filter((c) => !c.isGroup)
      .forEach((c, i) => console.log(`${i + 1}. ${c.name} (${c.id.user || ''})`));
    await client.destroy();
    process.exit(0);
  }

  const targets = toArg.split(',').map((s) => s.trim()).filter(Boolean);
  if (!targets.length) {
    console.error('Nenhum destino válido informado em --to.');
    await client.destroy();
    process.exit(3);
  }

  let sent = 0,
    notFound = [];
  for (const t of targets) {
    try {
      const lower = t.toLowerCase();
      let chatId = null;

      if (lower.startsWith('group:')) {
        const name = t.slice('group:'.length).trim().toLowerCase();
        const g = chats.find((c) => c.isGroup && c.name && c.name.toLowerCase().includes(name));
        if (g) chatId = g.id._serialized;
      } else if (lower.startsWith('number:')) {
        const raw = t.slice('number:'.length).trim().replace(/[^\d+]/g, '');
        const e164 = raw.startsWith('+') ? raw : '+' + raw;
        chatId = e164.replace('+', '') + '@c.us';
      } else {
        const name = t.trim().toLowerCase();
        const found = chats.find((c) => c.name && c.name.toLowerCase().includes(name));
        if (found) chatId = found.id._serialized;
      }

      if (!chatId) {
        console.warn(`Destino não encontrado: ${t}`);
        notFound.push(t);
        continue;
      }

      await client.sendMessage(chatId, message);
      console.log(`OK -> ${t}`);
      sent++;
    } catch (e) {
      console.error(`Falhou -> ${t}:`, e && e.message ? e.message : e);
    }
  }

  console.log(`Resumo: enviados=${sent}, não encontrados=${notFound.length}`);
  if (notFound.length) console.log('Não encontrados:', notFound.join(' | '));

  await new Promise((r) => setTimeout(r, 500));
  await client.destroy();
  process.exit(sent > 0 ? 0 : 4);
});

process.on('unhandledRejection', (err) => {
  console.error('UnhandledRejection:', err && err.stack ? err.stack : err);
  process.exit(5);
});

client.initialize();
