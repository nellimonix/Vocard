import re
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from secrets import randbelow


trace_id = ContextVar("vocard_trace_id", default=None)
_lookup_events = ContextVar("vocard_lookup_events", default=None)
_report_sink = None

_HTTP_STATUS = re.compile(
    r"\b(?:HTTP(?:/\d(?:\.\d)?)?|(?:response|status) code(?: from channel info)?(?: is)?)"
    r"\s*[:=]?\s*([45]\d\d)\b",
    re.IGNORECASE,
)
_JAVA_FRAME = re.compile(r"(?m)^\s*at ((?:[\w$]+\.)+[\w$<>]+\([\w.$:-]+(?: [\w.$:-]+)?\))")
_JAVA_EXCEPTION = re.compile(r"(?m)^\s*(?:Caused by:\s+)?((?:[\w$]+\.)+[\w$]+(?:Exception|Error))\b")
_SOURCE_TIMEOUTS = ("ConnectTimeoutException", "SocketTimeoutException", "UnknownHostException")


@dataclass(frozen=True)
class DiagnosticEvent:
    name: str
    event_id: str
    fields: dict


def new_trace_id():
    return str(100_000_000_000 + randbelow(900_000_000_000))


def set_report_sink(sink):
    global _report_sink
    _report_sink = sink


@contextmanager
def lookup_report():
    token = trace_id.set(trace_id.get() or new_trace_id())
    events = []
    capture = _lookup_events.set(events)
    try:
        yield
    finally:
        _lookup_events.reset(capture)
        trace_id.reset(token)
        if events and _report_sink:
            _report_sink(tuple(events))


def source_failure(data):
    fields = (data.get("message"), data.get("cause"), data.get("causeStackTrace"))
    details = "\n".join(field for field in fields if isinstance(field, str))
    status_match = _HTTP_STATUS.search(details)
    status = int(status_match[1]) if status_match else None
    severity = data.get("severity")
    if severity not in ("common", "suspicious", "fault"):
        severity = "unknown"
    if status in (403, 451):
        category = "source_restricted"
    elif status == 429:
        category = "source_rate_limit"
    elif any(kind in details for kind in _SOURCE_TIMEOUTS):
        category = "source_timeout"
    else:
        category = "source_fault" if severity == "fault" else "source_error"
    return {
        "category": category,
        "severity": severity,
        "status": status,
        "exception_types": tuple(dict.fromkeys(_JAVA_EXCEPTION.findall(details)))[:8],
        "frames": tuple(_JAVA_FRAME.findall(details))[:48],
    }


def python_trace(error):
    chain = []
    seen = set()
    while error is not None and id(error) not in seen and len(chain) < 8:
        seen.add(id(error))
        frames = traceback.extract_tb(error.__traceback__, limit=-24)
        chain.append((type(error).__name__, tuple(
            f"{Path(frame.filename).name}:{frame.lineno} {frame.name}" for frame in frames
        )))
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return tuple(chain)


def record(logger, event, *, level="warning", **fields):
    event_id = trace_id.get() or new_trace_id()
    summary = " ".join(f"{name}={value!r}" for name, value in fields.items() if value is not None)
    getattr(logger, level)("%s trace_id=%s %s", event, event_id, summary)
    if _report_sink:
        entry = DiagnosticEvent(event, event_id, fields)
        events = _lookup_events.get()
        if events is not None and event.startswith("lavalink_lookup_"):
            events.append(entry)
        else:
            _report_sink((entry,))
    return event_id
