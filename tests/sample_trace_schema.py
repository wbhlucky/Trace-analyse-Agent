from __future__ import annotations


def sample_trace_schema() -> str:
    """Shared TraceStreamer-like fixture schema.

    Keeps every hot table consumed by the index layer and explicitly contains
    the production callstack columns (name / dur / depth / parent_id /
    child_callid / ts) so end-to-end and cache tests exercise the same contract
    as real TraceStreamer exports.
    """
    return """
    CREATE TABLE process (
        id INTEGER PRIMARY KEY,
        ipid INTEGER
    );
    CREATE TABLE thread (
        itid INTEGER PRIMARY KEY,
        ipid INTEGER
    );
    CREATE TABLE callstack (
        id INTEGER PRIMARY KEY,
        callid INTEGER,
        ts INTEGER,
        dur INTEGER,
        name TEXT,
        depth INTEGER,
        parent_id INTEGER,
        child_callid INTEGER
    );
    CREATE TABLE sched_slice (
        itid INTEGER,
        ts INTEGER
    );
    CREATE TABLE frame_slice (
        id INTEGER PRIMARY KEY,
        ipid INTEGER
    );
    CREATE TABLE frame_maps (
        src_row INTEGER,
        dst_row INTEGER
    );
    CREATE TABLE diskio (id INTEGER);
    CREATE TABLE instant (ref INTEGER, ts INTEGER);
    """
