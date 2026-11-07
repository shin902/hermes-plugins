// Read-only historical Graph lookup. Private results go only to the parent process.
const fs = require('node:fs');
const path = require('node:path');
const {PublicClientApplication} = require('@azure/msal-node');
(async () => {
  const home = process.env.HERMES_HOME;
  const cfg = JSON.parse(fs.readFileSync(path.join(__dirname, 'settings.json')));
  const cache = path.join(home, 'credentials', 'graph-cache.json');
  const app = new PublicClientApplication({auth:{clientId:cfg.clientId, authority:`https://login.microsoftonline.com/${cfg.tenantId}`}});
  app.getTokenCache().deserialize(fs.readFileSync(cache, 'utf8'));
  const accounts = await app.getTokenCache().getAllAccounts();
  if (accounts.length !== 1) throw Error('account');
  const token = await app.acquireTokenSilent({account:accounts[0], scopes:cfg.scopes});
  let input = ''; for await (const chunk of process.stdin) input += chunk;
  const ids = JSON.parse(input);
  if (!Array.isArray(ids) || ids.some(id => typeof id !== 'string')) throw Error('input');
  const checkpoint = path.join(home, 'cron/mail-monitor/historical-lookup.json');
  const results = fs.existsSync(checkpoint) ? JSON.parse(fs.readFileSync(checkpoint, 'utf8')) : {};
  const remaining = ids.filter(id => !results[id]); let cursor = 0;
  function save() { fs.writeFileSync(checkpoint + '.tmp', JSON.stringify(results), {mode:0o600}); fs.renameSync(checkpoint + '.tmp', checkpoint); }
  await Promise.all(Array.from({length:4}, async () => {
    while (cursor < remaining.length) {
      const id = remaining[cursor++];
      for (let attempt = 0; attempt < 4; attempt++) {
        const r = await fetch('https://graph.microsoft.com/v1.0/me/messages/' + encodeURIComponent(id) + '?$select=id,subject,from,receivedDateTime,body,webLink,internetMessageHeaders', {
          headers:{Authorization:'Bearer ' + token.accessToken, Prefer:'outlook.body-content-type="text", IdType="ImmutableId"'},
          redirect:'error', signal:AbortSignal.timeout(30000)
        });
        if ((r.status === 429 || r.status >= 500) && attempt < 3) {
          await new Promise(resolve => setTimeout(resolve, Math.min(60000, Math.max(1000, Number(r.headers.get('retry-after') || 2) * 1000))));
          continue;
        }
        if (r.status === 404) { results[id] = {missing:true}; save(); break; }
        if (!r.ok) throw Error('Graph HTTP ' + r.status);
        const m = await r.json();
        if ((m.body?.contentType || '').toLowerCase() !== 'text') throw Error('body format');
        results[id] = {id,subject:m.subject || '',from:m.from?.emailAddress || {},received:m.receivedDateTime || '',body:m.body?.content || '',webLink:m.webLink || '',headers:m.internetMessageHeaders || []};
        save();
        break;
      }
    }
  }));
  process.stdout.write(JSON.stringify(Object.fromEntries(ids.map(id => [id, results[id]]))));
})().catch(e => {process.stderr.write(JSON.stringify({error:'Historical Graph lookup failed',kind:e.name,status:/^Graph HTTP \d+$/.test(e.message)?e.message:undefined})+'\n');process.exitCode = 1;});
