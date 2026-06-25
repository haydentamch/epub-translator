from pathlib import Path

import pytest
from ebooklib import epub

import app.db as db
import app.main as main
import app.services.exporter as exporter


@pytest.fixture
def isolated_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    upload_dir = tmp_path / "uploads"
    export_dir = tmp_path / "exports"
    database_path = tmp_path / "test.sqlite3"

    upload_dir.mkdir()
    export_dir.mkdir()

    monkeypatch.setattr(db, "DB_PATH", database_path)
    monkeypatch.setattr(main, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(main, "EXPORT_DIR", export_dir)
    monkeypatch.setattr(exporter, "EXPORT_DIR", export_dir)
    db.init_db()

    return {
        "database": database_path,
        "uploads": upload_dir,
        "exports": export_dir,
    }


@pytest.fixture
def make_epub():
    def _make_epub(
        path: Path,
        paragraphs: tuple[str, ...] = ("First paragraph.", "Second paragraph."),
    ) -> Path:
        book = epub.EpubBook()
        book.set_identifier("test-book")
        book.set_title("Test Book")
        book.set_language("en")
        book.add_author("Test Author")

        chapter = epub.EpubHtml(
            title="Chapter One",
            file_name="chapter.xhtml",
            lang="en",
        )
        paragraph_html = "".join(f"<p>{text}</p>" for text in paragraphs)
        chapter.content = (
            "<html><head><title>Chapter One</title></head>"
            f"<body><h1>Chapter One</h1>{paragraph_html}</body></html>"
        )

        book.add_item(chapter)
        book.add_item(epub.EpubNcx())
        book.spine = [chapter]
        book.toc = (chapter,)
        epub.write_epub(str(path), book)
        return path

    return _make_epub


def seed_job(
    source_path: Path,
    *,
    source_language: str = "English",
    target_language: str = "Chinese",
) -> int:
    from app.services.epub_parser import parse_epub

    parsed = parse_epub(source_path)
    book_id = db.save_parsed_book(parsed, source_path.name, source_path)
    return db.save_job_settings(
        book_id=book_id,
        source_language=source_language,
        target_language=target_language,
        model="test/model",
        text_direction="horizontal-ltr",
        layout="original-first",
    )
