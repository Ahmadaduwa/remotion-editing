"""
SQLite database layer for job persistence and audit logging.
Uses aiosqlite for async compatibility with FastAPI.
"""
import aiosqlite
import json
import os
from datetime import datetime, timezone
from typing import Optional

DB_PATH = os.environ.get("DB_PATH", "/app/data/jobs.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    task_id          TEXT PRIMARY KEY,
    status           TEXT NOT NULL DEFAULT 'queued',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    input_path       TEXT NOT NULL,
    output_path      TEXT,
    params_json      TEXT NOT NULL,
    error_message    TEXT,
    progress_percent INTEGER DEFAULT 0,
    backend          TEXT
);

CREATE TABLE IF NOT EXISTS deletion_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    deleted_at          TEXT NOT NULL,
    old_output_path     TEXT NOT NULL,
    old_task_id         TEXT NOT NULL,
    replaced_by_task_id TEXT NOT NULL,
    reason              TEXT
);

CREATE TABLE IF NOT EXISTS projects (
    project_id           TEXT PRIMARY KEY,
    video_name           TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'uploaded',
    raw_transcript_json  TEXT,
    corrected_text       TEXT,
    aligned_transcript_json TEXT,
    overlays_json        TEXT,
    render_plan_json     TEXT,
    subtitle_style_json  TEXT,
    bgm_settings_json    TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_output_path ON jobs(output_path);
CREATE INDEX IF NOT EXISTS idx_jobs_input_path ON jobs(input_path);
CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);
"""

_db: Optional[aiosqlite.Connection] = None


def _now() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


async def get_db() -> aiosqlite.Connection:
    """Retrieve the global persistent database connection, initializing if needed."""
    global _db
    if _db is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
    return _db


async def close_db() -> None:
    """Release global database connection resources."""
    global _db
    if _db is not None:
        await _db.close()
        _db = None


async def init_db() -> None:
    """Create database tables if they don't exist and run migrations."""
    db = await get_db()
    await db.executescript(SCHEMA_SQL)
    # Migrate existing db to add 'backend' if missing
    try:
        await db.execute("SELECT backend FROM jobs LIMIT 1")
    except aiosqlite.OperationalError:
        await db.execute("ALTER TABLE jobs ADD COLUMN backend TEXT")
        await db.commit()
        
    # Migrate projects to add subtitle_style_json if missing
    try:
        await db.execute("SELECT subtitle_style_json FROM projects LIMIT 1")
    except aiosqlite.OperationalError:
        await db.execute("ALTER TABLE projects ADD COLUMN subtitle_style_json TEXT")
        await db.commit()

    # Migrate projects to add bgm_settings_json if missing
    try:
        await db.execute("SELECT bgm_settings_json FROM projects LIMIT 1")
    except aiosqlite.OperationalError:
        await db.execute("ALTER TABLE projects ADD COLUMN bgm_settings_json TEXT")
        await db.commit()

    # Migrate projects to add render_plan_json if missing
    try:
        await db.execute("SELECT render_plan_json FROM projects LIMIT 1")
    except aiosqlite.OperationalError:
        await db.execute("ALTER TABLE projects ADD COLUMN render_plan_json TEXT")
        await db.commit()


async def create_job(
    task_id: str,
    input_path: str,
    output_path: str,
    params: dict,
) -> dict:
    """Insert a new job record with status='queued'. Returns the job dict."""
    now = _now()
    params_json = json.dumps(params, ensure_ascii=False)
    db = await get_db()
    await db.execute(
        """INSERT INTO jobs
           (task_id, status, created_at, updated_at, input_path, output_path, params_json, progress_percent, backend)
           VALUES (?, 'queued', ?, ?, ?, ?, ?, 0, NULL)""",
        (task_id, now, now, input_path, output_path, params_json),
    )
    await db.commit()
    return {
        "task_id": task_id,
        "status": "queued",
        "created_at": now,
        "updated_at": now,
        "input_path": input_path,
        "output_path": output_path,
        "params": params,
        "error_message": None,
        "progress_percent": 0,
        "backend": None,
    }


async def get_job(task_id: str) -> Optional[dict]:
    """Fetch a single job by task_id. Returns None if not found."""
    db = await get_db()
    cursor = await db.execute("SELECT * FROM jobs WHERE task_id = ?", (task_id,))
    row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


async def update_job(
    task_id: str,
    status: Optional[str] = None,
    progress_percent: Optional[int] = None,
    output_path: Optional[str] = None,
    error_message: Optional[str] = None,
    backend: Optional[str] = None,
) -> None:
    """Update selected fields on a job. Only non-None fields are updated."""
    sets = []
    vals = []
    if status is not None:
        sets.append("status = ?")
        vals.append(status)
    if progress_percent is not None:
        sets.append("progress_percent = ?")
        vals.append(progress_percent)
    if output_path is not None:
        sets.append("output_path = ?")
        vals.append(output_path)
    if error_message is not None:
        sets.append("error_message = ?")
        vals.append(error_message)
    if backend is not None:
        sets.append("backend = ?")
        vals.append(backend)
    if not sets:
        return
    sets.append("updated_at = ?")
    vals.append(_now())
    vals.append(task_id)
    sql = f"UPDATE jobs SET {', '.join(sets)} WHERE task_id = ?"
    db = await get_db()
    await db.execute(sql, vals)
    await db.commit()


async def list_jobs_by_status(status: str) -> list[dict]:
    """Return all jobs with the given status, ordered by created_at."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM jobs WHERE status = ? ORDER BY created_at",
        (status,),
    )
    rows = await cursor.fetchall()
    return [_row_to_dict(r) for r in rows]


async def count_jobs_by_status() -> dict[str, int]:
    """Return a dict of {status: count} for all statuses."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status"
    )
    rows = await cursor.fetchall()
    return {row[0]: row[1] for row in rows}


async def find_completed_job_by_output(output_path: str) -> Optional[dict]:
    """Find the most recent completed job that wrote to the given output_path."""
    db = await get_db()
    cursor = await db.execute(
        """SELECT * FROM jobs
           WHERE status = 'completed' AND output_path = ?
           ORDER BY updated_at DESC LIMIT 1""",
        (output_path,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


async def log_deletion(
    old_output_path: str,
    old_task_id: str,
    replaced_by_task_id: str,
    reason: str = "same-output deduplication",
) -> None:
    """Record a file deletion in the audit log."""
    db = await get_db()
    await db.execute(
        """INSERT INTO deletion_log
           (deleted_at, old_output_path, old_task_id, replaced_by_task_id, reason)
           VALUES (?, ?, ?, ?, ?)""",
        (_now(), old_output_path, old_task_id, replaced_by_task_id, reason),
    )
    await db.commit()


async def requeue_stuck_jobs() -> int:
    """Reset any jobs stuck in 'processing' (from a crash) back to 'queued'.
    Returns the number of requeued jobs."""
    now = _now()
    db = await get_db()
    cursor = await db.execute(
        "UPDATE jobs SET status = 'queued', updated_at = ?, progress_percent = 0 WHERE status = 'processing'",
        (now,),
    )
    await db.commit()
    return cursor.rowcount


def _row_to_dict(row) -> dict:
    """Convert an aiosqlite Row to a plain dict, parsing params_json."""
    d = dict(row)
    if "params_json" in d and d["params_json"]:
        try:
            d["params"] = json.loads(d["params_json"])
        except (json.JSONDecodeError, TypeError):
            d["params"] = {}
    else:
        d["params"] = {}
    return d


async def create_project(project_id: str, video_name: str) -> dict:
    now = _now()
    db = await get_db()
    await db.execute(
        """INSERT INTO projects
           (project_id, video_name, status, raw_transcript_json, corrected_text, aligned_transcript_json, overlays_json, render_plan_json, subtitle_style_json, bgm_settings_json, created_at, updated_at)
           VALUES (?, ?, 'uploaded', NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)""",
        (project_id, video_name, now, now),
    )
    await db.commit()
    return {
        "project_id": project_id,
        "video_name": video_name,
        "status": "uploaded",
        "raw_transcript": None,
        "corrected_text": None,
        "aligned_transcript": None,
        "overlays": None,
        "render_plan": None,
        "subtitle_style": None,
        "bgm_settings": None,
        "created_at": now,
        "updated_at": now,
    }


async def get_project(project_id: str) -> Optional[dict]:
    db = await get_db()
    cursor = await db.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,))
    row = await cursor.fetchone()
    if row is None:
        return None
    return _project_row_to_dict(row)


async def update_project(
    project_id: str,
    status: Optional[str] = None,
    raw_transcript: Optional[dict] = None,
    corrected_text: Optional[str] = None,
    aligned_transcript: Optional[dict] = None,
    overlays: Optional[list] = None,
    render_plan: Optional[dict] = None,
    subtitle_style: Optional[dict] = None,
    bgm_settings: Optional[dict] = None,
) -> None:
    sets = []
    vals = []
    if status is not None:
        sets.append("status = ?")
        vals.append(status)
    if raw_transcript is not None:
        sets.append("raw_transcript_json = ?")
        vals.append(json.dumps(raw_transcript, ensure_ascii=False))
    if corrected_text is not None:
        sets.append("corrected_text = ?")
        vals.append(corrected_text)
    if aligned_transcript is not None:
        sets.append("aligned_transcript_json = ?")
        vals.append(json.dumps(aligned_transcript, ensure_ascii=False))
    if overlays is not None:
        sets.append("overlays_json = ?")
        vals.append(json.dumps(overlays, ensure_ascii=False))
    if render_plan is not None:
        sets.append("render_plan_json = ?")
        vals.append(json.dumps(render_plan, ensure_ascii=False))
    if subtitle_style is not None:
        sets.append("subtitle_style_json = ?")
        vals.append(json.dumps(subtitle_style, ensure_ascii=False))
    if bgm_settings is not None:
        sets.append("bgm_settings_json = ?")
        vals.append(json.dumps(bgm_settings, ensure_ascii=False))
    
    if not sets:
        return
    sets.append("updated_at = ?")
    vals.append(_now())
    vals.append(project_id)
    sql = f"UPDATE projects SET {', '.join(sets)} WHERE project_id = ?"
    db = await get_db()
    await db.execute(sql, vals)
    await db.commit()


async def list_projects() -> list[dict]:
    db = await get_db()
    cursor = await db.execute("SELECT * FROM projects ORDER BY created_at DESC")
    rows = await cursor.fetchall()
    return [_project_row_to_dict(r) for r in rows]


async def delete_project(project_id: str) -> bool:
    db = await get_db()
    cursor = await db.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
    await db.commit()
    return cursor.rowcount > 0


def _project_row_to_dict(row) -> dict:
    d = dict(row)
    for field in ["raw_transcript_json", "aligned_transcript_json", "overlays_json", "render_plan_json", "subtitle_style_json", "bgm_settings_json"]:
        key = field.replace("_json", "")
        if field in d and d[field] is not None:
            try:
                d[key] = json.loads(d[field])
            except (json.JSONDecodeError, TypeError):
                d[key] = None
        else:
            d[key] = None
        d.pop(field, None)
    return d
