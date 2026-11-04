# secbrief

Skill-only native Hermes plugin for operating an existing local
[secbrief](https://github.com/tenhkspark/secbrief) checkout.

- Registers exactly one read-only plugin skill: `secbrief:secbrief-operations`.
- Provides no tools, hooks, commands, scheduling, online scans, or remediation.
- Does not copy, vendor, or import the secbrief engine.

The skill resolves the checkout through `SECBRIEF_HOME`, then
`skills.config.secbrief.home`, then explicit discovery in a user-approved
location. It refuses to clone or install secbrief automatically. The path is
profile-local configuration, not part of this repository.

The skill covers the actual local CLI flow: offline inventory, opt-in online
matching, one-shot `cycle`, report rendering, and `doctor` status. It states the
coverage limits and keeps `changed`, `nochange`, and `failed` distinct. Hermes
owns scheduling and delivery; the plugin never creates cron jobs.

Load it with:

```text
skill_view("secbrief:secbrief-operations")
```

Verify:

```bash
python3 -m unittest discover -s secbrief -p 'test_*.py' -v
hermes plugins validate ./secbrief
hermes plugins doctor ./secbrief --ci
```
