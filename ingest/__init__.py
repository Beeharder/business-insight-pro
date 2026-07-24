"""Reading from the outside world.

Everything that crosses the network lives here. The rest of the codebase reads
only from the DuckDB store, through ``store.AsOf`` — so a source outage can
break ingestion, but it can never quietly change what a backtest sees.
"""

from .base import FetchStats, HttpClient, IngestError, SourceUnavailable, record_watermark

__all__ = [
    "FetchStats",
    "HttpClient",
    "IngestError",
    "SourceUnavailable",
    "record_watermark",
]
