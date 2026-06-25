import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.services.epub_parser import ParsedBook


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "epub_translator.sqlite3"


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with get_connection() as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                author TEXT,
                source_filename TEXT NOT NULL,
                uploaded_path TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS chapters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL,
                order_index INTEGER NOT NULL,
                title TEXT NOT NULL,
                href TEXT NOT NULL,
                original_html TEXT NOT NULL,
                selected INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'Pending',
                error_message TEXT,
                FOREIGN KEY (book_id) REFERENCES books (id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS paragraphs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chapter_id INTEGER NOT NULL,
                order_index INTEGER NOT NULL,
                original_html TEXT NOT NULL,
                original_text TEXT NOT NULL,
                translated_html TEXT,
                status TEXT NOT NULL DEFAULT 'Pending',
                error_message TEXT,
                FOREIGN KEY (chapter_id) REFERENCES chapters (id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL,
                source_language TEXT NOT NULL,
                target_language TEXT NOT NULL,
                model TEXT NOT NULL,
                text_direction TEXT NOT NULL,
                layout TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Configured',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                completed_at TEXT,
                active_seconds INTEGER NOT NULL DEFAULT 0,
                active_started_at TEXT,
                pause_requested INTEGER NOT NULL DEFAULT 0,
                run_mode TEXT NOT NULL DEFAULT 'full',
                FOREIGN KEY (book_id) REFERENCES books (id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS exports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                filename TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (job_id) REFERENCES jobs (id) ON DELETE CASCADE
            );
            """
        )
        _ensure_chapter_selection_column(connection)
        _ensure_job_timing_columns(connection)
        _recover_interrupted_jobs(connection)


def _ensure_chapter_selection_column(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(chapters)")
    }
    if "selected" not in columns:
        connection.execute(
            "ALTER TABLE chapters ADD COLUMN selected INTEGER NOT NULL DEFAULT 1"
        )


def _ensure_job_timing_columns(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(jobs)")
    }

    if "started_at" not in columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN started_at TEXT")
    if "completed_at" not in columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN completed_at TEXT")
    if "active_seconds" not in columns:
        connection.execute(
            "ALTER TABLE jobs ADD COLUMN active_seconds INTEGER NOT NULL DEFAULT 0"
        )
    if "active_started_at" not in columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN active_started_at TEXT")
    if "pause_requested" not in columns:
        connection.execute(
            "ALTER TABLE jobs ADD COLUMN pause_requested INTEGER NOT NULL DEFAULT 0"
        )
    if "run_mode" not in columns:
        connection.execute(
            "ALTER TABLE jobs ADD COLUMN run_mode TEXT NOT NULL DEFAULT 'full'"
        )

    connection.execute(
        """
        UPDATE jobs
        SET started_at = updated_at
        WHERE status = 'Translating' AND started_at IS NULL
        """
    )
    connection.execute(
        """
        UPDATE jobs
        SET completed_at = updated_at
        WHERE status IN ('Completed', 'CompletedWithErrors', 'Failed')
          AND completed_at IS NULL
        """
    )
    connection.execute(
        """
        UPDATE jobs
        SET active_started_at = COALESCE(started_at, updated_at)
        WHERE status IN ('Translating', 'Pausing')
          AND active_started_at IS NULL
        """
    )


def _recover_interrupted_jobs(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        UPDATE jobs
        SET
            status = 'Paused',
            updated_at = CURRENT_TIMESTAMP,
            active_seconds = active_seconds + CASE
                WHEN active_started_at IS NULL THEN 0
                ELSE MAX(
                    0,
                    CAST(
                        (julianday(CURRENT_TIMESTAMP) - julianday(active_started_at))
                        * 86400 AS INTEGER
                    )
                )
            END,
            active_started_at = NULL,
            pause_requested = 0
        WHERE status IN ('Translating', 'Pausing')
        """
    )


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DB_PATH, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")

    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_books_for_title_refresh() -> list[dict[str, Any]]:
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT id, uploaded_path FROM books ORDER BY id"
        ).fetchall()
    return [dict(row) for row in rows]


def get_history_items() -> list[dict[str, Any]]:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT
                b.id AS book_id,
                b.title AS book_title,
                b.author AS book_author,
                b.source_filename,
                b.created_at AS book_created_at,
                j.id AS job_id,
                j.target_language,
                j.status,
                j.created_at AS job_created_at,
                j.completed_at,
                j.updated_at,
                j.active_seconds,
                (
                    SELECT e.filename
                    FROM exports e
                    WHERE e.job_id = j.id AND e.mode = 'bilingual'
                    ORDER BY e.id DESC
                    LIMIT 1
                ) AS bilingual_filename,
                (
                    SELECT e.filename
                    FROM exports e
                    WHERE e.job_id = j.id AND e.mode = 'translated-only'
                    ORDER BY e.id DESC
                    LIMIT 1
                ) AS translated_filename
            FROM jobs j
            JOIN books b ON b.id = j.book_id
            WHERE j.status IN ('Completed', 'CompletedWithErrors')
            ORDER BY COALESCE(j.completed_at, j.updated_at, j.created_at) DESC, j.id DESC
            """
        ).fetchall()

    return [dict(row) for row in rows]


def save_export_record(job_id: int, mode: str, filename: str) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO exports (job_id, mode, filename)
            VALUES (?, ?, ?)
            """,
            (job_id, mode, filename),
        )


def get_latest_export_filename(job_id: int, mode: str) -> str | None:
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT filename
            FROM exports
            WHERE job_id = ? AND mode = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (job_id, mode),
        ).fetchone()

    return str(row["filename"]) if row else None


def get_book_cleanup_paths(book_id: int) -> dict[str, Any] | None:
    with get_connection() as connection:
        book = connection.execute(
            """
            SELECT id, uploaded_path
            FROM books
            WHERE id = ?
            """,
            (book_id,),
        ).fetchone()
        if book is None:
            return None

        export_rows = connection.execute(
            """
            SELECT e.filename
            FROM exports e
            JOIN jobs j ON j.id = e.job_id
            WHERE j.book_id = ?
            """,
            (book_id,),
        ).fetchall()

    return {
        "uploaded_path": book["uploaded_path"],
        "export_filenames": [row["filename"] for row in export_rows],
    }


def delete_book(book_id: int) -> bool:
    with get_connection() as connection:
        cursor = connection.execute(
            "DELETE FROM books WHERE id = ?",
            (book_id,),
        )
        return cursor.rowcount == 1


def update_chapter_titles(book_id: int, titles_by_href: dict[str, str]) -> None:
    with get_connection() as connection:
        for href, title in titles_by_href.items():
            connection.execute(
                """
                UPDATE chapters
                SET title = ?
                WHERE book_id = ? AND href = ?
                """,
                (title, book_id, href),
            )


def save_parsed_book(
    parsed_book: ParsedBook,
    source_filename: str,
    uploaded_path: Path,
) -> int:
    with get_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO books (title, author, source_filename, uploaded_path)
            VALUES (?, ?, ?, ?)
            """,
            (
                parsed_book.title,
                parsed_book.author,
                source_filename,
                str(uploaded_path),
            ),
        )
        book_id = int(cursor.lastrowid)

        for chapter in parsed_book.chapters:
            chapter_cursor = connection.execute(
                """
                INSERT INTO chapters (
                    book_id,
                    order_index,
                    title,
                    href,
                    original_html,
                    status
                )
                VALUES (?, ?, ?, ?, ?, 'Pending')
                """,
                (
                    book_id,
                    chapter.order_index,
                    chapter.title,
                    chapter.href,
                    chapter.original_html,
                ),
            )
            chapter_id = int(chapter_cursor.lastrowid)

            connection.executemany(
                """
                INSERT INTO paragraphs (
                    chapter_id,
                    order_index,
                    original_html,
                    original_text,
                    status
                )
                VALUES (?, ?, ?, ?, 'Pending')
                """,
                [
                    (
                        chapter_id,
                        paragraph.order_index,
                        paragraph.original_html,
                        paragraph.original_text,
                    )
                    for paragraph in chapter.paragraphs
                ],
            )

    return book_id


def get_book_overview(book_id: int) -> dict[str, Any] | None:
    with get_connection() as connection:
        book = connection.execute(
            """
            SELECT id, title, author, source_filename, uploaded_path, created_at
            FROM books
            WHERE id = ?
            """,
            (book_id,),
        ).fetchone()

        if book is None:
            return None

        chapter_rows = connection.execute(
            """
            SELECT
                c.id,
                c.order_index,
                c.title,
                c.href,
                c.selected,
                c.status,
                c.error_message,
                COUNT(p.id) AS paragraph_count
            FROM chapters c
            LEFT JOIN paragraphs p ON p.chapter_id = c.id
            WHERE c.book_id = ?
            GROUP BY c.id
            ORDER BY c.order_index
            """,
            (book_id,),
        ).fetchall()

        paragraph_rows = connection.execute(
            """
            SELECT p.original_text
            FROM paragraphs p
            JOIN chapters c ON c.id = p.chapter_id
            WHERE c.book_id = ?
            """,
            (book_id,),
        ).fetchall()

    chapters = [dict(row) for row in chapter_rows]
    estimated_word_count = sum(
        len(row["original_text"].split()) for row in paragraph_rows
    )

    return {
        "book": dict(book),
        "chapters": chapters,
        "total_chapters": len(chapters),
        "estimated_word_count": estimated_word_count,
    }


def get_job_settings(book_id: int) -> dict[str, Any] | None:
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT
                id,
                book_id,
                source_language,
                target_language,
                model,
                text_direction,
                layout,
                status,
                created_at,
                updated_at
            FROM jobs
            WHERE book_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (book_id,),
        ).fetchone()

    return dict(row) if row else None


def get_job(job_id: int) -> dict[str, Any] | None:
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT
                j.id,
                j.book_id,
                j.source_language,
                j.target_language,
                j.model,
                j.text_direction,
                j.layout,
                j.status,
                j.created_at,
                j.updated_at,
                j.started_at,
                j.completed_at,
                j.active_seconds,
                j.active_started_at,
                j.pause_requested,
                j.run_mode,
                b.title AS book_title,
                b.author AS book_author,
                b.source_filename,
                b.uploaded_path
            FROM jobs j
            JOIN books b ON b.id = j.book_id
            WHERE j.id = ?
            """,
            (job_id,),
        ).fetchone()

    return dict(row) if row else None


def get_export_data(job_id: int) -> dict[str, Any] | None:
    job = get_job(job_id)
    if job is None:
        return None

    with get_connection() as connection:
        chapter_rows = connection.execute(
            """
            SELECT
                id,
                order_index,
                title,
                href,
                original_html,
                status,
                error_message
            FROM chapters
            WHERE book_id = ?
            ORDER BY order_index
            """,
            (job["book_id"],),
        ).fetchall()

        chapters: list[dict[str, Any]] = []
        for chapter_row in chapter_rows:
            chapter = dict(chapter_row)
            paragraph_rows = connection.execute(
                """
                SELECT
                    id,
                    order_index,
                    original_html,
                    original_text,
                    translated_html,
                    status,
                    error_message
                FROM paragraphs
                WHERE chapter_id = ?
                ORDER BY order_index
                """,
                (chapter["id"],),
            ).fetchall()
            chapter["paragraphs"] = [dict(row) for row in paragraph_rows]
            chapters.append(chapter)

    return {"job": job, "chapters": chapters}


def get_preview_paragraphs(
    job_id: int,
    preview_scope: str,
) -> list[dict[str, Any]]:
    job = get_job(job_id)
    if job is None:
        return []

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT
                p.id,
                p.chapter_id,
                p.order_index,
                p.original_html,
                p.original_text,
                p.translated_html,
                p.status,
                c.order_index AS chapter_order,
                c.title AS chapter_title
            FROM paragraphs p
            JOIN chapters c ON c.id = p.chapter_id
            WHERE c.book_id = ? AND c.selected = 1
            ORDER BY c.order_index, p.order_index
            """,
            (job["book_id"],),
        ).fetchall()

    paragraphs = [dict(row) for row in rows]
    if not paragraphs:
        return []

    if preview_scope == "first-chapter":
        first_chapter_id = paragraphs[0]["chapter_id"]
        return [
            paragraph
            for paragraph in paragraphs
            if paragraph["chapter_id"] == first_chapter_id
        ]

    word_limit = 2500 if preview_scope == "first-10-pages" else 3000
    selected: list[dict[str, Any]] = []
    word_count = 0

    for paragraph in paragraphs:
        if selected and word_count >= word_limit:
            break
        selected.append(paragraph)
        word_count += len(paragraph["original_text"].split())

    return selected


def save_paragraph_translation(paragraph_id: int, translated_html: str) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE paragraphs
            SET
                translated_html = ?,
                status = 'Completed',
                error_message = NULL
            WHERE id = ?
            """,
            (translated_html, paragraph_id),
        )


def claim_job_for_translation(job_id: int) -> bool:
    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE jobs
            SET
                status = 'Translating',
                updated_at = CURRENT_TIMESTAMP,
                started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                completed_at = NULL,
                active_started_at = CURRENT_TIMESTAMP,
                pause_requested = 0,
                run_mode = 'full'
            WHERE id = ? AND status = 'Configured'
            """,
            (job_id,),
        )
        return cursor.rowcount == 1


def claim_job_for_retry(job_id: int) -> bool:
    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE jobs
            SET
                status = 'Translating',
                updated_at = CURRENT_TIMESTAMP,
                started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                completed_at = NULL,
                active_started_at = CURRENT_TIMESTAMP,
                pause_requested = 0,
                run_mode = 'retry'
            WHERE id = ? AND status IN ('CompletedWithErrors', 'Failed')
            """,
            (job_id,),
        )
        return cursor.rowcount == 1


def request_job_pause(job_id: int) -> bool:
    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE jobs
            SET
                status = 'Pausing',
                pause_requested = 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'Translating'
            """,
            (job_id,),
        )
        return cursor.rowcount == 1


def is_job_pause_requested(job_id: int) -> bool:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT pause_requested FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    return bool(row and row["pause_requested"])


def mark_job_paused(job_id: int) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE jobs
            SET
                status = 'Paused',
                updated_at = CURRENT_TIMESTAMP,
                active_seconds = active_seconds + CASE
                    WHEN active_started_at IS NULL THEN 0
                    ELSE MAX(
                        0,
                        CAST(
                            (julianday(CURRENT_TIMESTAMP) - julianday(active_started_at))
                            * 86400 AS INTEGER
                        )
                    )
                END,
                active_started_at = NULL,
                pause_requested = 0
            WHERE id = ?
            """,
            (job_id,),
        )


def claim_job_for_resume(job_id: int) -> str | None:
    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE jobs
            SET
                status = 'Translating',
                updated_at = CURRENT_TIMESTAMP,
                completed_at = NULL,
                active_started_at = CURRENT_TIMESTAMP,
                pause_requested = 0
            WHERE id = ? AND status = 'Paused'
            """,
            (job_id,),
        )
        if cursor.rowcount != 1:
            return None
        row = connection.execute(
            "SELECT run_mode FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        return str(row["run_mode"])


def set_job_status(job_id: int, status: str) -> None:
    terminal_statuses = {"Completed", "CompletedWithErrors", "Failed"}
    completed_at = "CURRENT_TIMESTAMP" if status in terminal_statuses else "NULL"

    with get_connection() as connection:
        connection.execute(
            f"""
            UPDATE jobs
            SET
                status = ?,
                updated_at = CURRENT_TIMESTAMP,
                completed_at = {completed_at},
                active_seconds = active_seconds + CASE
                    WHEN active_started_at IS NULL THEN 0
                    ELSE MAX(
                        0,
                        CAST(
                            (julianday(CURRENT_TIMESTAMP) - julianday(active_started_at))
                            * 86400 AS INTEGER
                        )
                    )
                END,
                active_started_at = NULL,
                pause_requested = 0
            WHERE id = ?
            """,
            (status, job_id),
        )


def get_job_chapters(job_id: int) -> list[dict[str, Any]]:
    job = get_job(job_id)
    if job is None:
        return []

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT
                c.id,
                c.order_index,
                c.title,
                c.selected,
                c.status,
                c.error_message,
                COUNT(p.id) AS paragraph_count,
                SUM(CASE WHEN p.status = 'Completed' THEN 1 ELSE 0 END)
                    AS completed_paragraphs,
                SUM(CASE WHEN p.status = 'Failed' THEN 1 ELSE 0 END)
                    AS failed_paragraphs
            FROM chapters c
            LEFT JOIN paragraphs p ON p.chapter_id = c.id
            WHERE c.book_id = ?
            GROUP BY c.id
            ORDER BY c.order_index
            """,
            (job["book_id"],),
        ).fetchall()

    return [dict(row) for row in rows]


def save_chapter_selection(
    book_id: int,
    selected_chapter_ids: list[int],
) -> None:
    selected_ids = {int(chapter_id) for chapter_id in selected_chapter_ids}

    with get_connection() as connection:
        chapter_rows = connection.execute(
            "SELECT id, status FROM chapters WHERE book_id = ?",
            (book_id,),
        ).fetchall()

        for chapter in chapter_rows:
            chapter_id = int(chapter["id"])
            selected = chapter_id in selected_ids
            current_status = chapter["status"]

            if selected and current_status == "Skipped":
                next_status = "Pending"
            elif not selected and current_status in {"Pending", "Skipped"}:
                next_status = "Skipped"
            else:
                next_status = current_status

            connection.execute(
                """
                UPDATE chapters
                SET selected = ?, status = ?
                WHERE id = ? AND book_id = ?
                """,
                (int(selected), next_status, chapter_id, book_id),
            )


def get_pending_chapter_paragraphs(chapter_id: int) -> list[dict[str, Any]]:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT
                id,
                chapter_id,
                order_index,
                original_html,
                original_text,
                translated_html,
                status
            FROM paragraphs
            WHERE chapter_id = ? AND status != 'Completed'
            ORDER BY order_index
            """,
            (chapter_id,),
        ).fetchall()

    return [dict(row) for row in rows]


def set_chapter_status(
    chapter_id: int,
    status: str,
    error_message: str | None = None,
) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE chapters
            SET status = ?, error_message = ?
            WHERE id = ?
            """,
            (status, error_message, chapter_id),
        )


def set_paragraphs_status(paragraph_ids: list[int], status: str) -> None:
    if not paragraph_ids:
        return

    placeholders = ",".join("?" for _ in paragraph_ids)
    with get_connection() as connection:
        connection.execute(
            f"""
            UPDATE paragraphs
            SET status = ?
            WHERE id IN ({placeholders})
            """,
            (status, *paragraph_ids),
        )


def set_paragraphs_failed(
    paragraph_ids: list[int],
    error_message: str,
) -> None:
    if not paragraph_ids:
        return

    placeholders = ",".join("?" for _ in paragraph_ids)
    with get_connection() as connection:
        connection.execute(
            f"""
            UPDATE paragraphs
            SET status = 'Failed', error_message = ?
            WHERE id IN ({placeholders})
            """,
            (error_message, *paragraph_ids),
        )


def get_job_progress(job_id: int) -> dict[str, Any] | None:
    job = get_job(job_id)
    if job is None:
        return None

    chapters = get_job_chapters(job_id)
    selected_chapters = [
        chapter for chapter in chapters if chapter["selected"]
    ]
    completed_paragraphs = sum(
        chapter["completed_paragraphs"] or 0 for chapter in selected_chapters
    )
    failed_paragraphs = sum(
        chapter["failed_paragraphs"] or 0 for chapter in selected_chapters
    )
    total_paragraphs = sum(
        chapter["paragraph_count"] for chapter in selected_chapters
    )
    progress_percent = (
        (completed_paragraphs * 100) // total_paragraphs
        if total_paragraphs
        else 0
    )
    elapsed_seconds = _elapsed_seconds(job)

    return {
        "job_id": job["id"],
        "book_id": job["book_id"],
        "book_title": job["book_title"],
        "status": job["status"],
        "total_chapters": len(selected_chapters),
        "completed_chapters": sum(
            chapter["status"] == "Completed" for chapter in selected_chapters
        ),
        "partial_chapters": sum(
            chapter["status"] == "Partial" for chapter in selected_chapters
        ),
        "failed_chapters": sum(
            chapter["status"] == "Failed" for chapter in selected_chapters
        ),
        "skipped_chapters": sum(
            not chapter["selected"] for chapter in chapters
        ),
        "total_paragraphs": total_paragraphs,
        "completed_paragraphs": completed_paragraphs,
        "failed_paragraphs": failed_paragraphs,
        "progress_percent": progress_percent,
        "elapsed_seconds": elapsed_seconds,
        "chapters": chapters,
    }


def _elapsed_seconds(job: dict[str, Any]) -> int:
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT ? + CASE
                WHEN ? IS NULL THEN 0
                ELSE MAX(
                    0,
                    CAST(
                        (julianday(CURRENT_TIMESTAMP) - julianday(?))
                        * 86400 AS INTEGER
                    )
                )
            END AS elapsed_seconds
            """,
            (
                int(job.get("active_seconds") or 0),
                job.get("active_started_at"),
                job.get("active_started_at"),
            ),
        ).fetchone()

    return int(row["elapsed_seconds"] or 0)


def save_job_settings(
    book_id: int,
    source_language: str,
    target_language: str,
    model: str,
    text_direction: str,
    layout: str,
) -> int:
    existing = get_job_settings(book_id)

    with get_connection() as connection:
        if existing:
            connection.execute(
                """
                UPDATE jobs
                SET
                    source_language = ?,
                    target_language = ?,
                    model = ?,
                    text_direction = ?,
                    layout = ?,
                    status = 'Configured',
                    updated_at = CURRENT_TIMESTAMP,
                    started_at = NULL,
                    completed_at = NULL,
                    active_seconds = 0,
                    active_started_at = NULL,
                    pause_requested = 0,
                    run_mode = 'full'
                WHERE id = ?
                """,
                (
                    source_language,
                    target_language,
                    model,
                    text_direction,
                    layout,
                    existing["id"],
                ),
            )
            return int(existing["id"])

        cursor = connection.execute(
            """
            INSERT INTO jobs (
                book_id,
                source_language,
                target_language,
                model,
                text_direction,
                layout,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, 'Configured')
            """,
            (
                book_id,
                source_language,
                target_language,
                model,
                text_direction,
                layout,
            ),
        )
        return int(cursor.lastrowid)
