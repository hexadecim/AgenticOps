# OpsPilot

Agentic email-to-incident console: agents read a mailbox, pseudonymise and triage emails with a local
`deepseek-r1:8b`, and propose Jira incidents, production fixes and customer replies. A named person
approves each one, and every step is recorded in an append-only audit log.

- **Start here:** [CLAUDE.md](CLAUDE.md) (rules, architecture memory, commands)
- Status: [docs/implementation-plan.md](docs/implementation-plan.md)
- Architecture: [docs/architecture.md](docs/architecture.md) · decisions: [docs/adr](docs/adr)

```bash
cp .env.example .env      # fill FERNET_KEY, JWT_SECRET
make install
make test
make infra-up             # needs Docker Desktop running; Ollama runs on the host
```
