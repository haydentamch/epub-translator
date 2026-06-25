from pathlib import Path

from fastapi.testclient import TestClient

import app.db as db
from app.main import app
from tests.conftest import seed_job


def test_history_lists_completed_jobs_and_creates_download(
    isolated_app,
    make_epub,
    tmp_path,
):
    source_path = make_epub(tmp_path / "history.epub")
    job_id = seed_job(source_path)

    _complete_job(job_id)

    with TestClient(app) as client:
        history = client.get("/history")
        assert history.status_code == 200
        assert "Test Book" in history.text
        assert "Download bilingual EPUB" in history.text

        response = client.post(
            f"/history/jobs/{job_id}/download/bilingual",
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"].startswith("/downloads/")

        filename = Path(response.headers["location"]).name
        assert (isolated_app["exports"] / filename).is_file()

        refreshed = client.get("/history")
        assert refreshed.status_code == 200
        assert "Download bilingual EPUB" in refreshed.text


def test_history_delete_removes_book_and_export(
    isolated_app,
    make_epub,
    tmp_path,
):
    uploaded_path = isolated_app["uploads"] / "uploaded.epub"
    make_epub(uploaded_path)
    job_id = seed_job(uploaded_path)
    _complete_job(job_id)

    with TestClient(app) as client:
        download = client.post(
            f"/history/jobs/{job_id}/download/translated-only",
            follow_redirects=False,
        )
        export_path = isolated_app["exports"] / Path(download.headers["location"]).name
        assert uploaded_path.is_file()
        assert export_path.is_file()

        response = client.post(
            "/history/books/1/delete",
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/history"
        assert not uploaded_path.exists()
        assert not export_path.exists()

        history = client.get("/history")
        assert "Test Book" not in history.text
        assert "No completed translations yet" in history.text


def _complete_job(job_id: int) -> None:
    for chapter in db.get_job_chapters(job_id):
        for paragraph in db.get_pending_chapter_paragraphs(chapter["id"]):
            db.save_paragraph_translation(
                paragraph["id"],
                "<p>Translated paragraph.</p>",
            )
        db.set_chapter_status(chapter["id"], "Completed")
    db.set_job_status(job_id, "Completed")
