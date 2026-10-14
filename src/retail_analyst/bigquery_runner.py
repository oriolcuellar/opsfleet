"""BigQuery access with a cost check and retries for transient failures."""

from __future__ import annotations

import datetime as dt
import decimal
from dataclasses import dataclass
from typing import Any

from google.api_core import exceptions as gexc
from google.cloud import bigquery
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from retail_analyst.observability import Tracer

MAX_ROWS = 200
_TRANSIENT = (gexc.TooManyRequests, gexc.InternalServerError, gexc.ServiceUnavailable,
              gexc.GatewayTimeout, gexc.DeadlineExceeded, TimeoutError, ConnectionError)
_SQL_ERROR_REASONS = {"invalidQuery", "invalid", "resourcesExceeded", "responseTooLarge"}


class QueryError(Exception):
    """A query failed. ``fixable`` tells the agent whether rewriting the SQL can help."""

    def __init__(self, message: str, fixable: bool) -> None:
        super().__init__(message)
        self.fixable = fixable


@dataclass
class QueryResult:
    rows: list[dict[str, Any]]
    total_rows: int
    bytes_processed: int


class BigQueryRunner:
    def __init__(self, project: str, max_bytes_billed: int, tracer: Tracer) -> None:
        self._client = bigquery.Client(project=project or None)
        self._max_bytes = max_bytes_billed
        self._tracer = tracer

    def run(self, sql: str) -> QueryResult:
        # A dry run is free: it validates the SQL and tells us how much data it would scan.
        dry = self._call(lambda: self._client.query(
            sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)))
        if (dry.total_bytes_processed or 0) > self._max_bytes:
            raise QueryError(f"The query would scan {dry.total_bytes_processed / 1e9:.1f} GB, "
                             f"over the {self._max_bytes / 1e9:.1f} GB limit. Aggregate more "
                             "or filter by date.", fixable=True)

        config = bigquery.QueryJobConfig(maximum_bytes_billed=self._max_bytes)

        def execute() -> tuple[Any, Any]:
            job = self._client.query(sql, job_config=config)
            return job, job.result(timeout=60, max_results=MAX_ROWS)

        job, result = self._call(execute)
        rows = [{key: _to_json(value) for key, value in row.items()} for row in result]
        return QueryResult(rows=rows, total_rows=result.total_rows or 0,
                           bytes_processed=job.total_bytes_processed or 0)

    def _call(self, fn: Any) -> Any:
        try:
            for attempt in Retrying(stop=stop_after_attempt(3),
                                    wait=wait_exponential(multiplier=1, max=8),
                                    retry=retry_if_exception_type(_TRANSIENT),
                                    before_sleep=self._log_retry, reraise=True):
                with attempt:
                    return fn()
        except gexc.BadRequest as error:
            # Only problems in the SQL itself can be fixed by rewriting it; a bad project or
            # configuration is also a 400 but would just burn the attempts.
            reason = ((getattr(error, "errors", None) or [{}])[0]).get("reason", "invalidQuery")
            raise QueryError(_short(error), fixable=reason in _SQL_ERROR_REASONS) from error
        except _TRANSIENT as error:
            raise QueryError(f"BigQuery is unavailable right now: {_short(error)}",
                             fixable=False) from error
        except gexc.GoogleAPIError as error:
            raise QueryError(f"BigQuery access error: {_short(error)}", fixable=False) from error

    def _log_retry(self, state: Any) -> None:
        error = state.outcome.exception() if state.outcome else None
        self._tracer.event("retry", service="bigquery", attempt=state.attempt_number,
                           error=str(error)[:200])


def _short(error: Exception) -> str:
    # Prefer BigQuery's own message; str(error) starts with the request URL and project id.
    details = getattr(error, "errors", None) or [{}]
    message = details[0].get("message") or getattr(error, "message", None) or str(error)
    return message.split("\n")[0][:400]


def _to_json(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return value.isoformat()
    return value
