"""The settings sections OpsPilot knows about. Add a mailbox provider by adding fields with a `when`
condition and an option to `provider`, then registering a connection tester (adapters)."""

from __future__ import annotations

from .spec import FieldSpec, Option, SectionSpec

IMAP = (("provider", "imap"),)
GMAIL = (("provider", "gmail"),)
GRAPH = (("provider", "graph"),)

MAILBOX = SectionSpec(
    id="mailbox",
    title="Mailbox",
    description="Where support emails arrive. OpsPilot reads new mail and sends approved replies from it.",
    fields=(
        FieldSpec(
            "provider",
            "Provider",
            "select",
            required=True,
            default="imap",
            options=(
                Option("imap", "IMAP / SMTP (any provider, e.g. Gmail with an app password)"),
                Option("gmail", "Google Workspace (Gmail API)"),
                Option("graph", "Microsoft 365 (Graph API)"),
            ),
        ),
        FieldSpec("folder", "Folder to read", default="INBOX", required=True),
        FieldSpec(
            "poll_interval_seconds",
            "Check for new mail every (seconds)",
            "number",
            required=True,
            default=120,
            minimum=30,
            maximum=3600,
        ),
        # IMAP / SMTP
        FieldSpec("imap_host", "IMAP server", "host", required=True, when=IMAP, placeholder="imap.gmail.com"),
        FieldSpec(
            "imap_port",
            "IMAP port",
            "number",
            required=True,
            default=993,
            minimum=1,
            maximum=65535,
            when=IMAP,
        ),
        FieldSpec(
            "imap_ssl",
            "Use TLS (SSL) for IMAP",
            "bool",
            default=True,
            when=IMAP,
            help="Turn off only for a local test server.",
        ),
        FieldSpec(
            "username", "Mailbox user name", required=True, when=IMAP, placeholder="support@company.com"
        ),
        FieldSpec(
            "password",
            "Password or app password",
            "secret",
            required=True,
            when=IMAP,
            help="For Gmail and Outlook.com use an app password, not your normal password.",
        ),
        FieldSpec("smtp_host", "SMTP server (for replies)", "host", when=IMAP, placeholder="smtp.gmail.com"),
        FieldSpec("smtp_port", "SMTP port", "number", default=587, minimum=1, maximum=65535, when=IMAP),
        # Gmail API
        FieldSpec("gmail_user", "Mailbox address", "email", required=True, when=GMAIL),
        FieldSpec("gmail_client_id", "OAuth client ID", required=True, when=GMAIL),
        FieldSpec("gmail_client_secret", "OAuth client secret", "secret", required=True, when=GMAIL),
        FieldSpec(
            "gmail_refresh_token",
            "OAuth refresh token",
            "secret",
            required=True,
            when=GMAIL,
            help="Create one with scope gmail.modify in the Google OAuth Playground or gcloud.",
        ),
        # Microsoft Graph
        FieldSpec("graph_tenant_id", "Directory (tenant) ID", required=True, when=GRAPH),
        FieldSpec("graph_client_id", "Application (client) ID", required=True, when=GRAPH),
        FieldSpec(
            "graph_client_secret",
            "Client secret",
            "secret",
            required=True,
            when=GRAPH,
            help="App registration needs Mail.ReadWrite and Mail.Send application permissions.",
        ),
        FieldSpec("graph_mailbox", "Mailbox address", "email", required=True, when=GRAPH),
    ),
)

JIRA = SectionSpec(
    id="jira",
    title="Jira",
    description="Where approved incidents are created.",
    fields=(
        FieldSpec(
            "base_url", "Jira site", "url", required=True, placeholder="https://your-team.atlassian.net"
        ),
        FieldSpec(
            "email",
            "Account email",
            "email",
            required=True,
            help="The Atlassian account OpsPilot acts as. Use a dedicated service account.",
        ),
        FieldSpec(
            "api_token",
            "API token",
            "secret",
            required=True,
            help="Create at id.atlassian.com → Security → API tokens.",
        ),
        FieldSpec("project_key", "Project key", required=True, placeholder="KAN"),
        FieldSpec(
            "issue_type",
            "Issue type for incidents",
            required=True,
            default="Incident",
            help="Must exist in the project, for example Incident, Bug or Task.",
        ),
    ),
)

LLM = SectionSpec(
    id="llm",
    title="Local AI model",
    description="The model that reads and triages emails. It runs on your own machine through Ollama.",
    fields=(
        FieldSpec(
            "ollama_url",
            "Ollama address",
            "url",
            required=True,
            default="http://host.docker.internal:11434",
            help="From containers, your PC is host.docker.internal.",
        ),
        FieldSpec("model", "Chat model", required=True, default="deepseek-r1:8b"),
        FieldSpec(
            "embedding_model",
            "Embedding model",
            required=True,
            default="nomic-embed-text",
            help="Used for duplicate detection and runbook search.",
        ),
        FieldSpec(
            "thinking",
            "Let the model reason before answering",
            "bool",
            default=True,
            help="More accurate triage, but about 70 s per email on a typical PC.",
        ),
    ),
)

SECTIONS: dict[str, SectionSpec] = {s.id: s for s in (MAILBOX, JIRA, LLM)}

# Which services may read a section's resolved configuration, decrypted secrets included (internal API).
READERS: dict[str, frozenset[str]] = {
    "mailbox": frozenset({"mail-service"}),
    "jira": frozenset({"jira-connector"}),
    "llm": frozenset({"agent-orchestrator", "ops-service"}),
}
