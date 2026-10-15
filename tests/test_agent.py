"""Agent loop with a scripted model and a fake BigQuery (no network)."""

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from retail_analyst.agent import ChatSession
from retail_analyst.bigquery_runner import QueryError, QueryResult
from retail_analyst.config import Settings
from retail_analyst.prompts import NO_DATA_ANSWER
from retail_analyst.users import User


class ScriptedModel:
    """Asks for one query per scripted SQL, then gives the final answer."""

    def __init__(self, sqls: list[str], answer: str) -> None:
        self._replies = [AIMessage("", tool_calls=[{"name": "run_sql", "args": {"sql": sql},
                                                    "id": f"call-{i}"}])
                         for i, sql in enumerate(sqls)] + [AIMessage(answer)]

    def invoke(self, messages):  # noqa: ANN001
        return self._replies.pop(0)


class FakeBigQuery:
    def __init__(self, outcomes: list) -> None:  # noqa: ANN001
        self._outcomes = outcomes

    def run(self, sql: str) -> QueryResult:
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def session(tmp_path: Path) -> ChatSession:
    settings = Settings(google_api_key="test", gcp_project="test", model="m",
                        fallback_model="f", max_bytes_billed=10**9, max_sql_attempts=5,
                        data_dir=tmp_path)
    return ChatSession(settings, User(id="ceo", name="Test", role="CEO", all_products=True))


def test_invented_numbers_are_replaced_when_no_query_succeeded(session: ChatSession) -> None:
    session._llm = ScriptedModel(["SELECT COUNT(*) FROM orders"], "We had 28,941 orders.")
    session._bigquery = FakeBigQuery([QueryError("BigQuery is unavailable", fixable=False)])

    answer = session.ask("How many orders last month?", lambda request: False)

    assert answer.text == NO_DATA_ANSWER
    assert answer.outcome == "error"
    history = session._graph.get_state({"configurable": {"thread_id": session._thread_id}})
    assert history.values["messages"][-1].content == NO_DATA_ANSWER


def test_answer_is_kept_after_self_correction(session: ChatSession) -> None:
    session._llm = ScriptedModel(["SELECT zip FROM users", "SELECT COUNT(*) FROM users"],
                                 "There are 100 customers.")
    session._bigquery = FakeBigQuery([
        QueryError("Unrecognized name: zip", fixable=True),
        QueryResult(rows=[{"n": 100}], total_rows=1, bytes_processed=0),
    ])

    answer = session.ask("How many customers?", lambda request: False)

    assert answer.text == "There are 100 customers."
    assert answer.turn.summary()["sql_failed"] == 1
    assert answer.turn.summary()["sql_ok"] == 1
