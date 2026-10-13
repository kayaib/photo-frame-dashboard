const Bring = require('bring-shopping');
const { writeFileSync } = require('fs');
const { resolve } = require('path');

const OUT = resolve(__dirname, '..', 'bring.json');

const email    = process.env.BRING_EMAIL;
const password = process.env.BRING_PASSWORD;
const listName = process.env.BRING_LIST_NAME || null;

if (!email || !password) {
  console.error('BRING_EMAIL and BRING_PASSWORD must be set');
  process.exit(1);
}

async function main() {
  const bring = new Bring({ mail: email, password });

  await bring.login();
  console.log(`Logged in as ${bring.name}`);

  const { lists } = await bring.loadLists();
  if (!lists?.length) {
    console.error('No lists found');
    process.exit(1);
  }

  const list = listName
    ? (lists.find(l => l.name === listName) ?? lists[0])
    : lists[0];

  console.log(`Using list: ${list.name} (${list.listUuid})`);

  const { purchase } = await bring.getItems(list.listUuid);

  const items = (purchase || []).map(item => ({
    name: item.name,
    specification: item.specification || '',
  }));

  const output = {
    updatedAt: new Date().toISOString(),
    listName: list.name,
    items,
  };

  writeFileSync(OUT, JSON.stringify(output, null, 2));
  console.log(`Wrote ${items.length} items to bring.json`);
}

main().catch(err => { console.error(err); process.exit(1); });
