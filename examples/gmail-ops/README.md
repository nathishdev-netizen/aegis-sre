# gmail-ops: a read-only agent team for the Gmail MS and agent

Open Claude Code in this folder and ask about any email. A lead agent splits
the question across four specialists, runs them in parallel where it can,
cross-checks what they find and answers with evidence.

```
you ──► lead (CLAUDE.md)
          ├─ logs-investigator  ─► Aegis: one email's lines across both services, verdicts, incidents
          ├─ system-state       ─► Aegis HTTP tools: MS queues, mailboxes, workers, thread; agent handovers
          ├─ code-reader        ─► ./bin/code: both repos at the commit DEPLOYED on the server
          └─ email-inspector    ─► headless browser: render a reply, read a CRM/console page
```

Nothing in the Gmail agent or Gmail MS is changed. Every tool reads:
GET-only HTTP calls enforced in code, git reads that never check out, a
browser with typing, clicking and script execution denied.

## One-time setup

### On the Aegis server

```bash
# 1. Aegis with run keys + HTTP tools (branch feat/http-tools or later)
cd /opt/aegis && sudo git fetch origin feat/http-tools:feat/http-tools && sudo git checkout feat/http-tools

# 2. The tool list (edit base_url if the MS or agent listen elsewhere)
sudo mkdir -p /etc/aegis
sudo cp /opt/aegis/examples/http-tools.gmail.json /etc/aegis/http-tools.json

# 3. Secrets for those tools, readable only by the SSH user
sudo install -m 600 -o ubuntu -g ubuntu /dev/null /etc/aegis/mcp.env
sudo tee /etc/aegis/mcp.env >/dev/null <<'EOF'
GMAIL_MS_ADMIN_KEY=<ADMIN_API_KEY from the MS .env>
GMAIL_MS_AGENT_KEY=<AGENT_MS_KEY from the MS .env>
EOF

# 4. The launcher Claude Code runs over SSH
sudo install -m 755 /opt/aegis/examples/gmail-ops/server/aegis-gmail-mcp /usr/local/bin/aegis-gmail-mcp
```

Check it answers (Ctrl-D to end):

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | /usr/local/bin/aegis-gmail-mcp | head -c 300
```

### On your laptop

```bash
export AEGIS_SSH_TARGET=ubuntu@16.112.129.113
export AEGIS_SSH_KEY=~/.ssh/<your-key>.pem
export GMAIL_AGENT_REPO=~/Desktop/Nathish/tt/email/gmail/GMAIL_AGENT
export GMAIL_MS_REPO=~/Desktop/Nathish/tt/email/gmail/GMAIL_MS
cd <aegis checkout>/examples/gmail-ops && claude
```

`/mcp` should show `aegis` and `playwright` connected; `/agents` lists the four
specialists.

For the CRM pages, log in once with the read-only CRM user in the browser
profile (`~/.gmail-ops/browser-profile`); run the Playwright server without
`--headless` that one time.

## Ask it

- Why didn't thread `1a1eab0503cb817b` get a reply?
- Did we receive an email from ravi@… this morning?
- Why was this handed to a human?
- The reply on thread X looks broken - what's wrong with it?
- Is the system healthy right now?
- What changed in the agent since yesterday, and did anything get worse?

Headless, for scripts and later for Teams:

```bash
claude -p "Why didn't thread 1a1eab0503cb817b get a reply?" --output-format json
```

## Storage

Code is read from your existing clones with `git show <sha>:<path>`; nothing
is cloned per question. `./bin/code fetch` downloads only new commits.
