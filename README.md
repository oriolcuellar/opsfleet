# Retail Data Analysis Assistant

A CLI chat agent that lets retail executives ask questions about sales, products and customers
in plain English, discuss the results and create reports with action items. Built with
**LangGraph**, **Gemini** and **BigQuery** (`bigquery-public-data.thelook_ecommerce`).

- **Design document (architecture, diagrams, and implementation details):** [docs/architecture.md](docs/architecture.md)

## Features

| Feature | Implementation |
|---|---|
| Natural-language analysis | Gemini writes BigQuery SQL through a `run_sql` tool, can run several queries per question, and explains the result. |
| Reports | On request it writes a Markdown report (summary, findings, insights, action items) and saves it to the Saved Reports library. |
| Hybrid intelligence | A `search_golden_bucket` tool returns similar past trios (question, analyst notes, SQL) before the model writes SQL. |
| Safety & PII | Every query is parsed and rewritten so it can only read the user's products and never PII columns (`sql_safety.py`); results and answers are masked; obvious prompt injection is blocked. |
| High-stakes oversight | `delete_reports` pauses the graph with `interrupt()`, shows the exact reports and only deletes after the user types `delete`. |
| Resilience | SQL errors and empty results go back to the model to fix (max 5 queries per question); Gemini and BigQuery calls retry with backoff; a fallback Gemini model takes over if the primary fails; if no query could reach the data the answer is replaced so no figure is invented; the CLI never crashes. |
| Observability | One trace id per message; LLM calls, queries, retries, fallbacks, blocks and confirmations are logged to `.data/traces.jsonl`; per-answer footer and `/stats`. |
| Persona | Tone is in `config/persona.md`, re-read on every call (edit it while chatting). |

## Project layout

```
src/retail_analyst/
  agent.py            LangGraph graph (model ⇄ tools) and the tools
  sql_safety.py       SQL validation and rewrite onto the user's scope
  pii.py              PII masking of rows and answers
  bigquery_runner.py  BigQuery with dry run, byte cap and retries
  llm.py              Gemini with retries and fallback model
  reports.py          Saved Reports library (SQLite)
  golden_bucket.py    Trio loading and search
  observability.py    Per-turn traces and metrics
  prompts.py          System prompt
  cli.py              Command-line interface
config/               users and their scope, persona
golden_bucket/        example trios
tests/                unit tests (no network needed)
```

## Setup

Requirements: Python 3.11+, a [Gemini API key](https://aistudio.google.com/apikey) and a
Google Cloud project with BigQuery enabled (the public dataset fits in the free 1 TB/month).

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .

cp .env.example .env             # set GOOGLE_API_KEY and GOOGLE_CLOUD_PROJECT
gcloud auth application-default login

pytest                           # unit tests
retail-analyst users             # sample users and their data access
retail-analyst chat --user ceo
```

### Environment configuration

The application loads `.env` from the project root. [.env.example](.env.example)
documents the required Gemini API key and Google Cloud project, plus the supported
optional model names, BigQuery byte cap, SQL attempt limit (`MAX_SQL_ATTEMPTS`) and
storage directory (`DATA_DIR`). Active optional values match the code defaults.
Keep real credentials in `.env`, which is excluded from Git.

Additional settings from the local configuration are included as commented-out
references in the example, but are not read by this prototype. In particular,
`MAX_SQL_CALLS_PER_TURN` and `RETAIL_AGENT_DATA_DIR` are not aliases for
`MAX_SQL_ATTEMPTS` and `DATA_DIR`. Embeddings, Vertex AI, explicit BigQuery location,
separate repair limits, price estimates and Langfuse cannot be enabled through
those inactive settings.

Sample users: `ceo` (all products), `menswear` (department Men), `levis` (brand Levi's).

Chat commands: `/reports`, `/stats`, `/help`, `/exit`.

## Example run

Real session with the `levis` user (output shortened):

```text
you: What was my monthly revenue over the last 6 months?
  Over the last 6 completed months, your monthly revenue for Levi's was:
  * 2026-04: $1,698.24   * 2026-05: $1,774.91   * 2026-06: $2,312.62
  * 2026-07: $2,303.41   * 2026-08: $2,144.64   * 2026-09: $2,863.81
trace 5f1c… · 21.4s · 3 LLM calls · 4,885 tokens · 1 queries ok

you: List my top 5 customers with their emails
  I cannot reveal personal data about customers, such as their email addresses.
  I can list your top 5 customers by user ID and total revenue instead.

you: Show me Nike's revenue last year
  I can only assist with analysis for Levi's data, so I cannot provide figures for Nike.

you: Create a short report on my Q1 performance with action items for Q2
  # Q1 Levi's Performance Report
  ## Summary
  In Q1 2026, Levi's generated $3,816.70 in revenue across 84 orders ...
  ## Action items
  1. Boost Q2 basket sizes ... Owner: Brand Manager | Priority: High
  Saved as report r-e08eb8.

you: Delete all the reports we made in this conversation
  These 1 report(s) will be deleted permanently
  | r-e08eb8 | Q1 Levi's Performance Report | 2026-10-16T... |
  Type 'delete' to confirm (anything else cancels): delete
  The report created in this conversation (r-e08eb8) has been deleted.
```

## Framework choice

**LangGraph** provides explicit control flow for bounded self-correction, mandatory PII
scrubbing and confirm-before-delete. Its `interrupt()` and checkpointer make these safeguards
structural rather than dependent on the model. It also works with any LangChain chat model,
so switching from AI Studio to Gemini Enterprise Agent Platform (formerly Vertex AI) is a
configuration change. Agent Runtime on Agent Platform can deploy LangGraph agents through
its managed template.

**Gemini Flash-Lite** as the default model for cost and latency (it is called several times per
question), with an older Flash-Lite version as fallback. Both are set in `.env`.
