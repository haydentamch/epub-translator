from hashlib import sha256
from xml.etree import ElementTree
from zipfile import ZipFile

import app.db as db
from app.services import exporter
from tests.conftest import seed_job


def test_exporter_creates_bilingual_and_translated_only_epubs(
    isolated_app,
    make_epub,
    tmp_path,
):
    source_path = make_epub(tmp_path / "export.epub")
    source_hash = sha256(source_path.read_bytes()).hexdigest()
    job_id = seed_job(source_path)

    chapter = next(
        chapter
        for chapter in db.get_job_chapters(job_id)
        if chapter["title"] == "Chapter One"
    )
    paragraphs = db.get_pending_chapter_paragraphs(chapter["id"])
    db.save_paragraph_translation(
        paragraphs[0]["id"],
        "<p>Translated first paragraph.</p>",
    )
    db.set_paragraphs_failed(
        [paragraphs[1]["id"]],
        "translation failed",
    )
    db.set_chapter_status(chapter["id"], "Partial", "translation failed")

    for other_chapter in db.get_job_chapters(job_id):
        if other_chapter["id"] != chapter["id"]:
            for paragraph in db.get_pending_chapter_paragraphs(other_chapter["id"]):
                db.save_paragraph_translation(
                    paragraph["id"],
                    paragraph["original_html"],
                )
            db.set_chapter_status(other_chapter["id"], "Completed")

    db.set_job_status(job_id, "CompletedWithErrors")

    bilingual_path = exporter.export_epub(job_id, "bilingual")
    translated_path = exporter.export_epub(job_id, "translated-only")

    assert bilingual_path.is_file()
    assert translated_path.is_file()
    assert bilingual_path.parent == isolated_app["exports"]
    assert sha256(source_path.read_bytes()).hexdigest() == source_hash

    source_title, source_identifier = _epub_identity(source_path)
    bilingual_title, bilingual_identifier = _epub_identity(bilingual_path)
    translated_title, translated_identifier = _epub_identity(translated_path)

    assert source_title == "Test Book"
    assert bilingual_title == "Test Book - Bilingual"
    assert translated_title == "Test Book - Translated"
    assert len(
        {source_identifier, bilingual_identifier, translated_identifier}
    ) == 3

    with ZipFile(bilingual_path) as bilingual:
        chapter_html = bilingual.read("EPUB/chapter.xhtml").decode("utf-8")
        assert "Translated first paragraph." in chapter_html
        assert "First paragraph." in chapter_html
        assert exporter.FAILED_BILINGUAL in chapter_html
        assert "Second paragraph." in chapter_html

    with ZipFile(translated_path) as translated:
        chapter_html = translated.read("EPUB/chapter.xhtml").decode("utf-8")
        assert "Translated first paragraph." in chapter_html
        assert exporter.FAILED_TRANSLATED_ONLY in chapter_html
        assert "Second paragraph." in chapter_html


def test_exporter_ignores_empty_spacer_paragraphs(
    isolated_app,
    make_epub,
    tmp_path,
):
    source_path = make_epub(
        tmp_path / "spacer.epub",
        paragraphs=(
            "First paragraph.",
            "<span>&nbsp;</span>",
            "Second paragraph.",
        ),
    )
    job_id = seed_job(source_path)

    for chapter in db.get_job_chapters(job_id):
        for paragraph in db.get_pending_chapter_paragraphs(chapter["id"]):
            db.save_paragraph_translation(
                paragraph["id"],
                "<p>Translated paragraph.</p>",
            )
        db.set_chapter_status(chapter["id"], "Completed")

    db.set_job_status(job_id, "Completed")
    output_path = exporter.export_epub(job_id, "translated-only")

    with ZipFile(output_path) as translated:
        chapter_html = translated.read("EPUB/chapter.xhtml").decode("utf-8")

    assert chapter_html.count("Translated paragraph.") == 2
    assert "\u00a0" in chapter_html or "&nbsp;" in chapter_html


def _epub_identity(path):
    container_namespace = (
        "urn:oasis:names:tc:opendocument:xmlns:container"
    )
    package_namespace = "http://www.idpf.org/2007/opf"
    dc_namespace = "http://purl.org/dc/elements/1.1/"

    with ZipFile(path) as archive:
        container = ElementTree.fromstring(
            archive.read("META-INF/container.xml")
        )
        rootfile = container.find(
            f".//{{{container_namespace}}}rootfile"
        )
        package = ElementTree.fromstring(
            archive.read(rootfile.get("full-path"))
        )

    metadata = package.find(f"{{{package_namespace}}}metadata")
    title = metadata.find(f"{{{dc_namespace}}}title").text
    identifier_id = package.get("unique-identifier")
    identifier = metadata.find(
        f"{{{dc_namespace}}}identifier[@id='{identifier_id}']"
    )
    return title, identifier.text
