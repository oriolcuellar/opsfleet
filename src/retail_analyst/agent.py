"""The chat agent: a LangGraph loop between Gemini and five tools.

    question ─► injection check ─► model ⇄ tools ─► PII mask ─► answer
                                            │
                     delete_reports ─► interrupt() ─► user confirms in the CLI

The model decides which tools to call. Safety does not depend on it: every query goes
through ``make_safe``, and a deletion only runs after the user says yes.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from langchain_core.messages import (AIMessage, BaseMessage, HumanMessage, SystemMessage,
                                     trim_messages)
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import Command, interrupt

from retail_analyst.bigquery_runner import BigQueryRunner, QueryError
from retail_analyst.config import ROOT, Settings
from retail_analyst.golden_bucket import GoldenBucket
from retail_analyst.llm import GeminiClient, LLMUnavailable
from retail_analyst.observability import Tracer, Turn
from retail_analyst.pii import clean_rows, mask_text
from retail_analyst.prompts import INJECTION_REFUSAL, NO_DATA_ANSWER, SYSTEM_PROMPT
from retail_analyst.reports import ReportStore
from retail_analyst.sql_safety import UnsafeQuery, make_safe
from retail_analyst.users import User

ROWS_TO_MODEL = 50
HISTORY_MESSAGES = 40
MAX_GRAPH_STEPS = 25

# Cheap first filter for obvious prompt injection; the system prompt is the second.
_INJECTION = re.compile(
    r"ignore (all |any |the |your )?(previous |prior )?(instructions|rules)"
    r"|(reveal|show|print|repeat).{0,30}(system prompt|your instructions)"
    r"|\byou are now\b|developer mode|jailbreak",
    re.IGNORECASE,
)

ConfirmFn = Callable[[dict[str, Any]], bool]


@dataclass
class Answer:
    text: str
    outcome: str  # answered | refused | error
    turn: Turn


class ChatSession:
    def __init__(self, settings: Settings, user: User) -> None:
        self.settings = settings
        self.user = user
        self.conversation_id = f"c-{uuid.uuid4().hex[:8]}"
        self._thread_id = self.conversation_id
        self.tracer = Tracer(settings.data_dir / "traces.jsonl", user.id, self.conversation_id)
        self.reports = ReportStore(settings.data_dir / "reports.db")
        self.golden_bucket = GoldenBucket(ROOT / "golden_bucket")
        self._bigquery: BigQueryRunner | None = None
        self._sql_attempts = 0
        self._data_failed = False
        tools = self._tools()
        self._llm = GeminiClient(settings, tools, self.tracer)
        self._graph = self._build_graph(tools)

    # ------------------------------------------------------------------------------------
    def ask(self, question: str, confirm: ConfirmFn) -> Answer:
        self.tracer.start_turn(question)
        self._sql_attempts = 0
        self._data_failed = False
        if _INJECTION.search(question):
            self.tracer.event("input_blocked", reason="prompt_injection")
            return self._finish(INJECTION_REFUSAL, "refused")

        config: Any = {"configurable": {"thread_id": self._thread_id},
                       "recursion_limit": MAX_GRAPH_STEPS}
        payload: Any = {"messages": [HumanMessage(question)]}
        try:
            while True:
                self._graph.invoke(payload, config)
                state = self._graph.get_state(config)
                if not state.interrupts:
                    break
                payload = Command(resume=confirm(state.interrupts[0].value))
            last = state.values["messages"][-1]
            turn = self.tracer.current
            if self._data_failed and not (turn.count("sql", ok=True)
                                          or turn.count("sql", error="no rows")):
                # No query reached the data, so any figure in the answer is invented.
                # Replace it, in the history too, so later turns do not build on it.
                self.tracer.event("answer_replaced", reason="no_data")
                self._graph.update_state(config, {"messages": [AIMessage(NO_DATA_ANSWER,
                                                                         id=last.id)]},
                                         as_node="model")
                return self._finish(NO_DATA_ANSWER, "error")
            return self._finish(_text(last), "answered")
        except LLMUnavailable:
            message = "The AI service is not available right now. Please try again in a minute."
        except GraphRecursionError:
            message = ("I could not finish this analysis within my step limit. "
                       "Try a more specific question.")
        except Exception as error:  # noqa: BLE001 - the chat must never crash
            self.tracer.event("error", error_type=type(error).__name__, error=str(error)[:500])
            message = f"Sorry, something went wrong on my side ({type(error).__name__})."
        # A failed turn can leave the graph state half way through a tool call, so the next
        # question starts on a clean thread (saved reports keep the same conversation id).
        self._thread_id = f"{self.conversation_id}-{uuid.uuid4().hex[:4]}"
        return self._finish(message, "error")

    def _finish(self, text: str, outcome: str) -> Answer:
        text, masked = mask_text(text)
        if masked:
            self.tracer.event("pii_masked_in_answer", count=masked)
        turn = self.tracer.end_turn(outcome, text)
        return Answer(text=text, outcome=outcome, turn=turn)

    # ------------------------------------------------------------------------------------
    def _build_graph(self, tools: list[BaseTool]) -> Any:
        def call_model(state: MessagesState) -> dict[str, Any]:
            history = trim_messages(state["messages"], strategy="last", token_counter=len,
                                    max_tokens=HISTORY_MESSAGES, start_on="human")
            reply = self._llm.invoke([SystemMessage(self._system_prompt()), *history])
            return {"messages": [reply]}

        graph = StateGraph(MessagesState)
        graph.add_node("model", call_model)
        graph.add_node("tools", ToolNode(tools))
        graph.add_edge(START, "model")
        graph.add_conditional_edges("model", tools_condition)
        graph.add_edge("tools", "model")
        return graph.compile(checkpointer=InMemorySaver())

    def _system_prompt(self) -> str:
        # Read on every call, so editing persona.md changes the tone without a restart.
        persona = (ROOT / "config" / "persona.md").read_text(encoding="utf-8")
        return SYSTEM_PROMPT.format(today=date.today().isoformat(), name=self.user.name,
                                    role=self.user.role, scope=self.user.scope(),
                                    persona=persona)

    def _tools(self) -> list[BaseTool]:
        @tool
        def search_golden_bucket(question: str) -> str:
            """Find how human analysts answered similar questions before (question, SQL and
            conclusion). Call it before writing SQL for a new analysis question."""
            trios = self.golden_bucket.search(question)
            self.tracer.event("golden_bucket", matches=[t.question for t in trios])
            if not trios:
                return "No similar past analyses found. Use the definitions in your instructions."
            return "\n\n---\n\n".join(t.as_example() for t in trios)

        @tool
        def run_sql(sql: str) -> str:
            """Run one read-only BigQuery SELECT on orders, order_items, products and users
            (bare table names). Returns the result rows."""
            return self._run_sql(sql)

        @tool
        def save_report(title: str, content_markdown: str) -> str:
            """Save a finished report in the user's Saved Reports library."""
            body, masked = mask_text(content_markdown)
            report = self.reports.save(self.user.id, self.conversation_id,
                                       mask_text(title)[0], body)
            self.tracer.event("report_saved", report_id=report.id, pii_masked=masked)
            return f"Saved as report {report.id}."

        @tool
        def list_reports() -> str:
            """List the user's saved reports."""
            reports = self.reports.list(self.user.id)
            if not reports:
                return "The user has no saved reports."
            return "\n".join(f"{r.id} | {r.title} | {r.created_at}" for r in reports)

        @tool
        def delete_reports(mentions: str | None = None, this_conversation: bool = False,
                           all_reports: bool = False) -> str:
            """Delete the user's saved reports: those mentioning a text (e.g. a client or
            brand), those created in this conversation, or all of them. The user is always
            asked to confirm before anything is deleted."""
            if not (mentions or this_conversation or all_reports):
                return "Ask the user which reports they want to delete."
            matches = self.reports.find(
                self.user.id, mentions=mentions,
                conversation_id=self.conversation_id if this_conversation else None)
            if not matches:
                return "No saved reports match, so nothing was deleted."
            # Pauses the graph; the CLI shows the list and resumes with True or False.
            confirmed = interrupt({"reports": [{"id": r.id, "title": r.title,
                                                "created_at": r.created_at} for r in matches]})
            self.tracer.event("delete_confirmation", confirmed=confirmed is True,
                              count=len(matches))
            if confirmed is not True:
                return "The user cancelled. Nothing was deleted."
            deleted = self.reports.delete(self.user.id, [r.id for r in matches])
            self.tracer.event("reports_deleted", ids=[r.id for r in matches])
            return f"Deleted {deleted} report(s): " + ", ".join(r.id for r in matches)

        return [search_golden_bucket, run_sql, save_report, list_reports, delete_reports]

    # ------------------------------------------------------------------------------------
    @property
    def bigquery(self) -> BigQueryRunner:
        if self._bigquery is None:
            try:
                self._bigquery = BigQueryRunner(self.settings.gcp_project,
                                                self.settings.max_bytes_billed, self.tracer)
            except Exception as error:  # noqa: BLE001 - e.g. missing credentials
                raise QueryError(f"BigQuery is not configured: {error}", fixable=False) from error
        return self._bigquery

    def _run_sql(self, sql: str) -> str:
        self._sql_attempts += 1
        if self._sql_attempts > self.settings.max_sql_attempts:
            self.tracer.event("sql_limit_reached")
            self._data_failed = True
            return ("STOP: the query limit for this question has been reached. Answer with "
                    "what you already have and say what could not be checked.")
        try:
            safe_sql = make_safe(sql, self.user)
        except UnsafeQuery as error:
            self.tracer.event("sql_blocked", sql=sql, reason=str(error))
            return f"REJECTED: {error} Rewrite the query."
        try:
            result = self.bigquery.run(safe_sql)
        except QueryError as error:
            self.tracer.event("sql", ok=False, sql=sql, executed_sql=safe_sql, error=str(error))
            if error.fixable:
                return f"ERROR: {error} Fix the SQL and try again."
            self._sql_attempts = self.settings.max_sql_attempts  # retrying would only add cost
            self._data_failed = True
            return f"ERROR: {error} Changing the query will not help; tell the user."
        if result.total_rows == 0:
            self.tracer.event("sql", ok=False, sql=sql, executed_sql=safe_sql, error="no rows")
            return ("The query returned no rows. Check the filters (status values are "
                    "capitalised, date ranges, exact names) and try again, or tell the user "
                    "there is no data for this.")
        self.tracer.event("sql", ok=True, sql=sql, executed_sql=safe_sql,
                          rows=result.total_rows, bytes=result.bytes_processed)
        return _as_table(clean_rows(result.rows[:ROWS_TO_MODEL]), result.total_rows)


def _as_table(rows: list[dict[str, Any]], total: int) -> str:
    columns = list(rows[0])
    lines = [" | ".join(columns)]
    lines += [" | ".join("" if row[c] is None else str(row[c]) for c in columns) for row in rows]
    if total > len(rows):
        lines.append(f"(showing {len(rows)} of {total} rows)")
    return "\n".join(lines)


def _text(message: BaseMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return "".join(part.get("text", "") if isinstance(part, dict) else str(part)
                   for part in message.content)
