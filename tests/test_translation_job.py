import asyncio
import json
import sqlite3

import app.db as db
import app.main as main
import app.services.job_runner as job_runner
import app.services.translator as translator
from fastapi.testclient import TestClient
from tests.conftest import seed_job


def test_save_paragraph_translation_preserves_original_html(
    isolated_app,
    make_epub,
    tmp_path,
):
    job_id = seed_job(make_epub(tmp_path / "paragraph.epub"))
    paragraph = _content_paragraphs(job_id)[0]

    db.save_paragraph_translation(
        paragraph["id"],
        "<p>Translated paragraph.</p>",
    )

    with db.get_connection() as connection:
        saved = connection.execute(
            """
            SELECT original_html, translated_html, status
            FROM paragraphs
            WHERE id = ?
            """,
            (paragraph["id"],),
        ).fetchone()

    assert saved["original_html"] == paragraph["original_html"]
    assert saved["translated_html"] == "<p>Translated paragraph.</p>"
    assert saved["status"] == "Completed"


def test_progress_does_not_round_incomplete_work_to_100_percent(
    isolated_app,
    make_epub,
    tmp_path,
):
    job_id = seed_job(make_epub(tmp_path / "progress.epub"))
    paragraphs = _content_paragraphs(job_id)

    db.save_paragraph_translation(paragraphs[0]["id"], "<p>Translated.</p>")

    progress = db.get_job_progress(job_id)

    assert progress["completed_paragraphs"] < progress["total_paragraphs"]
    assert progress["progress_percent"] < 100


def test_translation_timer_runs_and_freezes_on_completion(
    isolated_app,
    make_epub,
    tmp_path,
):
    job_id = seed_job(make_epub(tmp_path / "timer.epub"))
    assert db.claim_job_for_translation(job_id)

    with db.get_connection() as connection:
        connection.execute(
            """
            UPDATE jobs
            SET
                started_at = datetime('now', '-65 seconds'),
                active_started_at = datetime('now', '-65 seconds')
            WHERE id = ?
            """,
            (job_id,),
        )

    running = db.get_job_progress(job_id)
    assert 64 <= running["elapsed_seconds"] <= 66

    db.set_job_status(job_id, "Completed")
    completed = db.get_job_progress(job_id)

    assert completed["elapsed_seconds"] >= running["elapsed_seconds"]
    assert db.get_job(job_id)["completed_at"] is not None


def test_existing_database_adds_job_timing_columns(tmp_path, monkeypatch):
    database_path = tmp_path / "legacy.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", database_path)

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO jobs (id, status, updated_at)
            VALUES (1, 'Translating', '2026-06-22 10:00:00')
            """
        )

    db.init_db()

    with db.get_connection() as connection:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(jobs)")
        }
        job = connection.execute(
            "SELECT started_at, completed_at FROM jobs WHERE id = 1"
        ).fetchone()

    assert {
        "started_at",
        "completed_at",
        "active_seconds",
        "active_started_at",
        "pause_requested",
        "run_mode",
    } <= columns
    assert job["started_at"] == "2026-06-22 10:00:00"
    assert job["completed_at"] is None


def test_startup_recovers_interrupted_translation_as_paused(
    isolated_app,
    make_epub,
    tmp_path,
):
    job_id = seed_job(make_epub(tmp_path / "interrupted.epub"))
    assert db.claim_job_for_translation(job_id)

    db.init_db()
    recovered = db.get_job(job_id)

    assert recovered["status"] == "Paused"
    assert recovered["active_started_at"] is None
    assert recovered["pause_requested"] == 0
    assert db.claim_job_for_resume(job_id) == "full"


def test_pause_and_resume_preserve_completed_paragraphs(
    isolated_app,
    make_epub,
    tmp_path,
    monkeypatch,
):
    job_id = seed_job(make_epub(tmp_path / "pause.epub"))
    paragraphs = _content_paragraphs(job_id)
    assert len(paragraphs) == 2
    assert db.claim_job_for_translation(job_id)

    completion_calls: list[list[str]] = []

    async def pause_after_first_completion(api_key, model, messages):
        payload = json.loads(messages[1]["content"])
        completion_calls.append([item["id"] for item in payload])
        db.request_job_pause(job_id)
        return json.dumps(
            [
                {
                    "id": item["id"],
                    "translation_html": f"<p>Translated {item['id']}.</p>",
                }
                for item in payload
            ]
        )

    async def pausing_translate(
        job,
        selected,
        api_key,
        should_pause=None,
    ):
        await translator.translate_paragraphs(
            job,
            selected,
            api_key,
            completion=pause_after_first_completion,
            should_pause=should_pause,
        )

    monkeypatch.setattr(translator, "BATCH_SIZE", 1)
    monkeypatch.setattr(job_runner, "translate_paragraphs", pausing_translate)
    asyncio.run(job_runner.run_translation_job(job_id, "first-request-key"))

    paused = db.get_job_progress(job_id)
    remaining = _content_paragraphs(job_id)

    assert paused["status"] == "Paused"
    assert paused["completed_paragraphs"] >= 1
    assert [paragraph["id"] for paragraph in remaining] == [paragraphs[1]["id"]]
    assert completion_calls == [[str(paragraphs[0]["id"])]]

    frozen_seconds = paused["elapsed_seconds"]
    assert db.claim_job_for_resume(job_id) == "full"

    resumed_calls: list[list[int]] = []

    async def finish_translation(
        job,
        selected,
        api_key,
        should_pause=None,
    ):
        resumed_calls.append([paragraph["id"] for paragraph in selected])
        for paragraph in selected:
            db.save_paragraph_translation(
                paragraph["id"],
                "<p>Resumed translation.</p>",
            )

    monkeypatch.setattr(job_runner, "translate_paragraphs", finish_translation)
    asyncio.run(job_runner.run_translation_job(job_id, "second-request-key"))

    completed = db.get_job_progress(job_id)
    assert resumed_calls == [[paragraphs[1]["id"]]]
    assert completed["status"] == "Completed"
    assert completed["completed_paragraphs"] == completed["total_paragraphs"]
    assert completed["elapsed_seconds"] >= frozen_seconds


def test_pause_and_resume_http_controls(
    isolated_app,
    make_epub,
    tmp_path,
    monkeypatch,
):
    with TestClient(main.app) as client:
        job_id = seed_job(make_epub(tmp_path / "pause-controls.epub"))
        assert db.claim_job_for_translation(job_id)

        pause_response = client.post(
            f"/jobs/{job_id}/pause",
            follow_redirects=False,
        )
        assert pause_response.status_code == 303
        assert db.get_job(job_id)["status"] == "Pausing"

        db.mark_job_paused(job_id)
        paused_page = client.get(f"/jobs/{job_id}")
        assert "Resume translation" in paused_page.text

        async def finish_resumed_job(resumed_job_id, api_key):
            assert api_key == "resume-request-key"
            db.set_job_status(resumed_job_id, "Completed")

        monkeypatch.setattr(main, "run_translation_job", finish_resumed_job)
        resume_response = client.post(
            f"/jobs/{job_id}/resume",
            data={"api_key": "resume-request-key"},
            follow_redirects=False,
        )

    assert resume_response.status_code == 303
    assert db.get_job(job_id)["status"] == "Completed"


def test_chapter_selection_skips_unselected_chapters(
    isolated_app,
    make_epub,
    tmp_path,
    monkeypatch,
):
    job_id = seed_job(make_epub(tmp_path / "selection.epub"))
    job = db.get_job(job_id)
    first_chapter = next(
        chapter
        for chapter in db.get_job_chapters(job_id)
        if chapter["title"] == "Chapter One"
    )

    with db.get_connection() as connection:
        skipped_chapter_id = connection.execute(
            """
            INSERT INTO chapters (
                book_id, order_index, title, href, original_html, status
            )
            VALUES (
                ?, 99, 'Do Not Translate', 'skip.xhtml',
                '<p>Skip me.</p>', 'Pending'
            )
            """,
            (job["book_id"],),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO paragraphs (
                chapter_id, order_index, original_html, original_text, status
            )
            VALUES (?, 0, '<p>Skip me.</p>', 'Skip me.', 'Pending')
            """,
            (skipped_chapter_id,),
        )

    db.save_chapter_selection(job["book_id"], [first_chapter["id"]])
    translated_chapters: list[int] = []

    async def translate_selected(
        job,
        paragraphs,
        api_key,
        should_pause=None,
    ):
        translated_chapters.append(paragraphs[0]["chapter_id"])
        for paragraph in paragraphs:
            db.save_paragraph_translation(
                paragraph["id"],
                "<p>Translated.</p>",
            )

    monkeypatch.setattr(job_runner, "translate_paragraphs", translate_selected)
    assert db.claim_job_for_translation(job_id)
    asyncio.run(job_runner.run_translation_job(job_id, "request-key"))

    progress = db.get_job_progress(job_id)
    chapters = {
        chapter["title"]: chapter for chapter in progress["chapters"]
    }

    assert translated_chapters == [first_chapter["id"]]
    assert chapters["Do Not Translate"]["status"] == "Skipped"
    assert progress["skipped_chapters"] == 1
    assert progress["total_chapters"] == 1
    assert progress["progress_percent"] == 100


def test_settings_page_saves_selected_chapters(
    isolated_app,
    make_epub,
    tmp_path,
):
    job_id = seed_job(make_epub(tmp_path / "selection-form.epub"))
    job = db.get_job(job_id)
    chapters = db.get_job_chapters(job_id)
    selected_id = chapters[0]["id"]

    with TestClient(main.app) as client:
        page = client.get(f"/books/{job['book_id']}/settings")
        assert page.status_code == 200
        assert 'name="selected_chapter_ids"' in page.text

        response = client.post(
            f"/books/{job['book_id']}/settings",
            data={
                "source_language": "English",
                "target_language": "Chinese",
                "model": "test/model",
                "text_direction": "horizontal-ltr",
                "layout": "original-first",
                "selected_chapter_ids": str(selected_id),
            },
            follow_redirects=False,
        )

        empty_response = client.post(
            f"/books/{job['book_id']}/settings",
            data={
                "source_language": "English",
                "target_language": "Chinese",
                "model": "test/model",
                "text_direction": "horizontal-ltr",
                "layout": "original-first",
            },
        )

    assert response.status_code == 303
    assert empty_response.status_code == 400
    assert "Select at least one chapter" in empty_response.text
    refreshed = db.get_job_chapters(job_id)
    assert next(
        chapter for chapter in refreshed if chapter["id"] == selected_id
    )["selected"] == 1


def test_failed_chapter_continues_and_retry_only_translates_unfinished_work(
    isolated_app,
    make_epub,
    tmp_path,
    monkeypatch,
):
    job_id = seed_job(make_epub(tmp_path / "failure.epub"))
    job = db.get_job(job_id)

    with db.get_connection() as connection:
        second_chapter = connection.execute(
            """
            INSERT INTO chapters (
                book_id, order_index, title, href, original_html, status
            )
            VALUES (?, 99, 'Later Chapter', 'later.xhtml', '<p>Later.</p>', 'Pending')
            """,
            (job["book_id"],),
        ).lastrowid
        later_paragraph = connection.execute(
            """
            INSERT INTO paragraphs (
                chapter_id, order_index, original_html, original_text, status
            )
            VALUES (?, 0, '<p>Later.</p>', 'Later.', 'Pending')
            """,
            (second_chapter,),
        ).lastrowid

    content_chapter = next(
        chapter
        for chapter in db.get_job_chapters(job_id)
        if chapter["title"] == "Chapter One"
    )
    content_ids = [
        paragraph["id"]
        for paragraph in db.get_pending_chapter_paragraphs(content_chapter["id"])
    ]
    calls: list[int] = []

    async def fail_first_chapter(
        job,
        paragraphs,
        api_key,
        should_pause=None,
    ):
        chapter_id = paragraphs[0]["chapter_id"]
        calls.append(chapter_id)

        if chapter_id == content_chapter["id"]:
            db.save_paragraph_translation(
                paragraphs[0]["id"],
                "<p>Saved before failure.</p>",
            )
            db.set_paragraphs_failed(
                [paragraphs[1]["id"]],
                "translation failed",
            )
            raise RuntimeError("translation failed")

        for paragraph in paragraphs:
            db.save_paragraph_translation(paragraph["id"], "<p>Translated.</p>")

    monkeypatch.setattr(job_runner, "translate_paragraphs", fail_first_chapter)
    db.claim_job_for_translation(job_id)
    asyncio.run(job_runner.run_translation_job(job_id, "request-only-key"))

    progress = db.get_job_progress(job_id)
    chapter_statuses = {
        chapter["title"]: chapter["status"] for chapter in progress["chapters"]
    }

    assert chapter_statuses["Chapter One"] == "Partial"
    assert chapter_statuses["Later Chapter"] == "Completed"
    assert progress["status"] == "CompletedWithErrors"
    assert second_chapter in calls

    retry_calls: list[list[int]] = []

    async def succeed_retry(
        job,
        paragraphs,
        api_key,
        should_pause=None,
    ):
        retry_calls.append([paragraph["id"] for paragraph in paragraphs])
        for paragraph in paragraphs:
            db.save_paragraph_translation(paragraph["id"], "<p>Retried.</p>")

    monkeypatch.setattr(job_runner, "translate_paragraphs", succeed_retry)
    db.claim_job_for_retry(job_id)
    asyncio.run(job_runner.retry_failed_chapters(job_id, "new-request-key"))

    final_progress = db.get_job_progress(job_id)
    assert retry_calls == [[content_ids[1]]]
    assert later_paragraph not in retry_calls[0]
    assert final_progress["status"] == "Completed"
    assert final_progress["failed_paragraphs"] == 0


def _content_paragraphs(job_id: int) -> list[dict]:
    chapter = next(
        chapter
        for chapter in db.get_job_chapters(job_id)
        if chapter["title"] == "Chapter One"
    )
    return db.get_pending_chapter_paragraphs(chapter["id"])
