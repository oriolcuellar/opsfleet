"""Command-line chat.

    retail-analyst chat --user ceo
    retail-analyst users
"""

from __future__ import annotations

import sys
from typing import Any

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from retail_analyst.agent import Answer, ChatSession
from retail_analyst.config import ROOT, Settings
from retail_analyst.users import load_users

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Chat with your retail data in plain English.")
for _stream in (sys.stdout, sys.stderr):  # old Windows consoles cannot print some characters
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")
console = Console()

HELP = """\
Ask any question about sales, products or customers, or ask for a report.

- `/reports` list your saved reports
- `/stats` metrics for this session
- `/help` this help
- `/exit` quit
"""


@app.command()
def chat(user: str = typer.Option(..., "--user", "-u", help="User id (see `users`)")) -> None:
    """Start a chat session."""
    settings = Settings.from_env()
    users = load_users(ROOT / "config" / "users.yaml")
    if user not in users:
        console.print(f"[red]Unknown user '{user}'. Available: {', '.join(users)}[/]")
        raise typer.Exit(1)
    if not settings.google_api_key or not settings.gcp_project:
        console.print("[red]Set GOOGLE_API_KEY and GOOGLE_CLOUD_PROJECT in .env first "
                      "(see .env.example).[/]")
        raise typer.Exit(1)

    session = ChatSession(settings, users[user])
    current = session.user
    console.print(Panel(f"[bold]{current.name}[/] ({current.role})\n"
                        f"Data access: [cyan]{current.scope()}[/]\n"
                        f"Model: {settings.model} (fallback: {settings.fallback_model})",
                        title="Retail Analyst", border_style="cyan"))
    console.print("[dim]Type /help for commands.[/]")

    while True:
        try:
            text = Prompt.ask("\n[bold]you[/]").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text.startswith("/"):
            if not _command(session, text):
                break
            continue
        with console.status("Thinking..."):
            answer = session.ask(text, _confirm_deletion)
        _show(answer)
    console.print("[dim]Bye.[/]")


def _confirm_deletion(request: dict[str, Any]) -> bool:
    reports = request["reports"]
    table = Table(title=f"[bold red]These {len(reports)} report(s) will be deleted permanently")
    for column in ("id", "title", "created"):
        table.add_column(column)
    for r in reports:
        table.add_row(r["id"], escape(r["title"]), r["created_at"])
    console.print(table)
    try:
        reply = Prompt.ask("[bold red]Type 'delete' to confirm[/] (anything else cancels)",
                           default="")
    except (EOFError, KeyboardInterrupt):
        return False
    return reply.strip().lower() == "delete"


def _show(answer: Answer) -> None:
    color = {"error": "red", "refused": "yellow"}.get(answer.outcome, "green")
    console.print(Panel(Markdown(answer.text or "(no answer)"), border_style=color))
    stats = answer.turn.summary()
    parts = [f"trace {answer.turn.trace_id}", f"{answer.turn.latency_s}s",
             f"{stats['llm_calls']} LLM calls",
             f"{stats['input_tokens'] + stats['output_tokens']:,} tokens",
             f"{stats['sql_ok']} queries ok"]
    if stats["sql_failed"]:
        parts.append(f"{stats['sql_failed']} failed")
    if stats["sql_blocked"]:
        parts.append(f"{stats['sql_blocked']} blocked")
    if stats["retries"]:
        parts.append(f"{stats['retries']} retries")
    if stats["fallbacks"]:
        parts.append("fallback model used")
    console.print("[dim]" + " · ".join(parts) + "[/]")


def _command(session: ChatSession, text: str) -> bool:
    command = text.split()[0].lower()
    if command in ("/exit", "/quit"):
        return False
    if command == "/help":
        console.print(Markdown(HELP))
    elif command == "/reports":
        reports = session.reports.list(session.user.id)
        if not reports:
            console.print("[dim]No saved reports.[/]")
        else:
            table = Table("id", "title", "created")
            for r in reports:
                table.add_row(r.id, escape(r.title), r.created_at)
            console.print(table)
    elif command == "/stats":
        table = Table("metric", "value", title="Session metrics")
        for key, value in session.tracer.session_stats().items():
            table.add_row(key, str(value))
        console.print(table)
    else:
        console.print(f"[red]Unknown command {command}. Type /help.[/]")
    return True


@app.command()
def users() -> None:
    """List the sample users and the data they can access."""
    table = Table("id", "name", "role", "data access")
    for u in load_users(ROOT / "config" / "users.yaml").values():
        table.add_row(u.id, u.name, u.role, u.scope())
    console.print(table)


if __name__ == "__main__":
    app()
