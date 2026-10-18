#!/usr/bin/env python3
"""Report LLM token throughput for T3 Code / OpenCode sessions.

Uses only the Python standard library. Opens SQLite databases read-only so it
can run while T3 Code and OpenCode are active.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_STATE_DB = Path("~/.t3/userdata/statev2.sqlite")
DEFAULT_OPENCODE_DB = Path("~/.local/share/opencode/opencode.db")


def connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"Database not found: {path}")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    return db


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def to_ms(value: datetime | None) -> int | None:
    return int(value.timestamp() * 1000) if value else None


def as_utc(ms: int | None) -> str:
    if ms is None:
        return "unknown"
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def load_messages(db: sqlite3.Connection, session_id: str) -> list[dict]:
    rows = db.execute(
        """
        SELECT seq, data
        FROM session_message
        WHERE session_id = ? AND type = 'assistant'
        ORDER BY seq
        """,
        (session_id,),
    ).fetchall()

    messages: list[dict] = []
    for row in rows:
        data = json.loads(row["data"])
        tokens = data.get("tokens") or {}
        timing = data.get("time") or {}
        created = timing.get("created")
        ended = timing.get("streamed") or timing.get("completed")
        if not created or not ended:
            continue
        output = int(tokens.get("output") or 0)
        reasoning = int(tokens.get("reasoning") or 0)
        generated = output + reasoning
        duration_ms = int(ended) - int(created)
        messages.append(
            {
                "seq": row["seq"],
                "created_ms": int(created),
                "streamed_ms": int(ended),
                "duration_ms": duration_ms,
                "output": output,
                "reasoning": reasoning,
                "generated": generated,
                "input": int(tokens.get("input") or 0),
                "cache_read": int((tokens.get("cache") or {}).get("read") or 0),
                "model": data.get("model") or {},
            }
        )
    return messages


def summarize(messages: list[dict], start_ms: int, end_ms: int) -> dict:
    usable = [m for m in messages if m["duration_ms"] > 0]
    skipped = len(messages) - len(usable)

    generated = sum(m["generated"] for m in usable)
    output = sum(m["output"] for m in usable)
    reasoning = sum(m["reasoning"] for m in usable)
    input_tokens = sum(m["input"] for m in usable)
    cache_read = sum(m["cache_read"] for m in usable)
    stream_seconds = sum(m["duration_ms"] for m in usable) / 1000
    wall_seconds = (end_ms - start_ms) / 1000 if end_ms > start_ms else None

    per_message = [m["generated"] / (m["duration_ms"] / 1000) for m in usable]
    percentiles = {
        "p50": percentile(per_message, 0.50),
        "p75": percentile(per_message, 0.75),
        "p90": percentile(per_message, 0.90),
        "max": max(per_message) if per_message else None,
    }

    return {
        "messages": len(usable),
        "skipped_messages": skipped,
        "started_ms": start_ms,
        "ended_ms": end_ms,
        "started_at": as_utc(start_ms),
        "ended_at": as_utc(end_ms),
        "wall_seconds": wall_seconds,
        "stream_seconds": stream_seconds,
        "generated_tokens": generated,
        "output_tokens": output,
        "reasoning_tokens": reasoning,
        "input_tokens": input_tokens,
        "cached_input_tokens": cache_read,
        "generation_tok_per_sec": generated / stream_seconds if stream_seconds else None,
        "end_to_end_tok_per_sec": generated / wall_seconds if wall_seconds else None,
        "text_only_tok_per_sec": output / stream_seconds if stream_seconds else None,
        "per_message_tok_per_sec": percentiles,
        "message_model": usable[0]["model"] if usable else {},
    }


def load_thread(db: sqlite3.Connection, thread_id: str) -> dict:
    thread = db.execute(
        "SELECT title, payload_json FROM orchestration_v2_projection_threads WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    if not thread:
        db_path = db.execute("PRAGMA database_list").fetchone()[2]
        raise SystemExit(
            f"Thread not found in {db_path}: {thread_id}\n"
            "Pass an OpenCode session ID (ses_...) instead if you only have a session."
        )

    title = thread["title"]
    thread_payload = json.loads(thread["payload_json"])
    model_selection = thread_payload.get("modelSelection") or {}
    default_model = model_selection.get("model")

    runs = {
        row["run_id"]: row
        for row in db.execute(
            """
            SELECT run_id, ordinal, status, requested_at, completed_at, payload_json
            FROM orchestration_v2_projection_runs
            WHERE thread_id = ?
            ORDER BY ordinal
            """,
            (thread_id,),
        )
    }

    attempts = db.execute(
        """
        SELECT attempt_id, run_id, status, payload_json
        FROM orchestration_v2_projection_run_attempts
        WHERE thread_id = ?
        ORDER BY run_id, attempt_ordinal
        """,
        (thread_id,),
    ).fetchall()

    parsed = []
    for row in attempts:
        payload = json.loads(row["payload_json"])
        run = runs.get(row["run_id"])
        run_payload = json.loads(run["payload_json"]) if run else {}
        start = parse_time(payload.get("startedAt") or (run["requested_at"] if run else None))
        end = parse_time(payload.get("completedAt") or (run["completed_at"] if run else None))
        parsed.append(
            {
                "attempt_id": row["attempt_id"],
                "run_id": row["run_id"],
                "run_ordinal": run["ordinal"] if run else None,
                "status": row["status"],
                "native_session": payload.get("nativeThreadId"),
                "start": start,
                "end": end,
                "model": (run_payload.get("modelSelection") or {}).get("model"),
            }
        )

    # A running attempt has no end. Stop its window at the next attempt that
    # uses the same native session, including attempts from other threads: a
    # shared OpenCode session can be reused after the T3 run is abandoned.
    all_attempt_starts: dict[str, list[int]] = {}
    for row in db.execute("SELECT payload_json FROM orchestration_v2_projection_run_attempts"):
        payload = json.loads(row["payload_json"])
        session_id = payload.get("nativeThreadId")
        started = parse_time(payload.get("startedAt"))
        if session_id and started:
            all_attempt_starts.setdefault(session_id, []).append(to_ms(started))

    for item in parsed:
        if item["end"] is not None:
            continue
        candidates = [
            start
            for start in all_attempt_starts.get(item["native_session"] or "", [])
            if start is not None and start > to_ms(item["start"])
        ]
        if candidates:
            item["end"] = min(candidates)

    return {
        "thread_id": thread_id,
        "title": title,
        "default_model": default_model,
        "attempts": parsed,
    }


def split_by_gaps(messages: list[dict], gap_seconds: int) -> list[list[dict]]:
    if not messages:
        return []
    segments: list[list[dict]] = [[messages[0]]]
    for message in messages[1:]:
        previous = segments[-1][-1]
        gap_ms = message["created_ms"] - previous["streamed_ms"]
        if gap_ms > gap_seconds * 1000:
            segments.append([message])
        else:
            segments[-1].append(message)
    return segments


def fmt(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def print_report(report: dict) -> None:
    print(f"Model: {report['model'] or 'unknown'}")
    print()

    for run in report["runs"]:
        status = run["status"]
        label = f"Run {run['run_ordinal']}" if run["run_ordinal"] is not None else f"Segment {run['segment']}"
        print(
            f"{label} ({status})  {run['started_at']} -> {run['ended_at']} UTC  "
            f"({fmt(run['wall_seconds'])}s)"
        )
        print(f"  Messages: {run['messages']:,} assistant messages")
        print(
            f"  Generated: {run['generated_tokens']:,} tokens "
            f"({run['output_tokens']:,} output + {run['reasoning_tokens']:,} reasoning)"
        )
        print(
            f"  Input: {run['input_tokens'] + run['cached_input_tokens']:,} tokens "
            f"({run['cached_input_tokens']:,} cached)"
        )
        print(
            f"  Generation: {fmt(run['generation_tok_per_sec'])} tok/s "
            f"over {fmt(run['stream_seconds'])}s of streaming"
        )
        print(
            f"  End-to-end: {fmt(run['end_to_end_tok_per_sec'])} tok/s "
            f"over {fmt(run['wall_seconds'])}s wall time"
        )
        print(f"  Text-only: {fmt(run['text_only_tok_per_sec'])} tok/s")
        p = run["per_message_tok_per_sec"]
        print(
            "  Per-message: "
            f"p50 {fmt(p['p50'])} | p75 {fmt(p['p75'])} | "
            f"p90 {fmt(p['p90'])} | max {fmt(p['max'])} tok/s"
        )
        if run["skipped_messages"]:
            print(f"  Skipped {run['skipped_messages']} message(s) without a positive duration")
        print()

    print("Generation throughput is the model's active decode speed; end-to-end includes tool execution and idle time.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("identifier", help="T3 thread UUID or OpenCode session ID (ses_...)")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    parser.add_argument(
        "--state-db",
        type=Path,
        default=Path(os.environ.get("T3_STATE_DB", DEFAULT_STATE_DB)),
    )
    parser.add_argument(
        "--opencode-db",
        type=Path,
        default=Path(os.environ.get("OPENCODE_DB", DEFAULT_OPENCODE_DB)),
    )
    parser.add_argument(
        "--gap-seconds",
        type=int,
        default=300,
        help="When only a session ID is given, split turns at gaps longer than this (default: 300)",
    )
    args = parser.parse_args()

    ident = args.identifier
    opencode = connect(args.opencode_db.expanduser())

    if ident.startswith("ses_"):
        messages = load_messages(opencode, ident)
        if not messages:
            raise SystemExit(f"No assistant messages found for session {ident}")
        model = messages[0]["model"]
        model_name = f"{model.get('providerID', '')}/{model.get('id', '')}".strip("/")
        if model.get("variant"):
            model_name += f" (variant {model['variant']})"
        runs = []
        for index, segment in enumerate(split_by_gaps(messages, args.gap_seconds), start=1):
            runs.append(
                {
                    "segment": index,
                    "run_ordinal": None,
                    "status": "session segment",
                    **summarize(segment, segment[0]["created_ms"], segment[-1]["streamed_ms"]),
                }
            )
        report = {"session_id": ident, "model": model_name, "runs": runs}
    else:
        state = connect(args.state_db.expanduser())
        thread = load_thread(state, ident)
        if not thread["attempts"]:
            raise SystemExit(f"No run attempts found for thread {ident}")

        runs = []
        for attempt in thread["attempts"]:
            session_id = attempt["native_session"]
            if not session_id:
                continue
            messages = load_messages(opencode, session_id)
            start_ms = to_ms(attempt["start"])
            end_ms = to_ms(attempt["end"])
            if start_ms is None:
                continue
            if end_ms is None:
                in_window = [m for m in messages if m["created_ms"] >= start_ms]
                if not in_window:
                    continue
                end_ms = in_window[-1]["streamed_ms"]
            in_window = [
                m for m in messages if start_ms <= m["created_ms"] <= end_ms
            ]
            if not in_window:
                continue
            runs.append(
                {
                    "run_ordinal": attempt["run_ordinal"],
                    "attempt_id": attempt["attempt_id"],
                    "status": attempt["status"],
                    "native_session": session_id,
                    "model": attempt["model"] or thread["default_model"],
                    **summarize(in_window, start_ms, end_ms),
                }
            )

        model_names = {run["model"] for run in runs if run["model"]}
        report = {
            "thread_id": thread["thread_id"],
            "title": thread["title"],
            "model": ", ".join(sorted(model_names)) or thread["default_model"],
            "runs": runs,
        }

    if not report["runs"]:
        raise SystemExit("No assistant messages found in the selected run windows.")

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        if "title" in report:
            print(f"Thread: {report['thread_id']} - {report['title']}")
        else:
            print(f"Session: {report['session_id']}")
        print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
