from fastapi.testclient import TestClient

from app.main import app


def test_upload_saves_and_parses_epub(isolated_app, make_epub, tmp_path):
    source_path = make_epub(tmp_path / "upload.epub")

    with TestClient(app) as client:
        with source_path.open("rb") as source:
            response = client.post(
                "/upload",
                files={"file": ("my-book.epub", source, "application/epub+zip")},
                follow_redirects=False,
            )

        assert response.status_code == 303
        assert response.headers["location"].startswith("/books/")

        overview = client.get(response.headers["location"])
        assert overview.status_code == 200
        assert "Test Book" in overview.text
        assert "Chapter One" in overview.text

    saved_files = list(isolated_app["uploads"].glob("*.epub"))
    assert len(saved_files) == 1
    assert saved_files[0].name != "my-book.epub"


def test_upload_rejects_non_epub(isolated_app):
    with TestClient(app) as client:
        response = client.post(
            "/upload",
            files={"file": ("notes.txt", b"not an epub", "text/plain")},
        )

    assert response.status_code == 400
    assert "Please upload a valid .epub file." in response.text
    assert list(isolated_app["uploads"].iterdir()) == []
