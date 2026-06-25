from ebooklib import epub

import app.db as db
import app.main as main
from app.services.epub_parser import parse_epub


def test_epub_parser_extracts_metadata_chapters_and_paragraphs(
    make_epub,
    tmp_path,
):
    source_path = make_epub(
        tmp_path / "parser.epub",
        ("Alpha <em>emphasis</em>.", "Beta paragraph."),
    )

    parsed = parse_epub(source_path)
    chapter = next(item for item in parsed.chapters if item.href == "chapter.xhtml")

    assert parsed.title == "Test Book"
    assert parsed.author == "Test Author"
    assert chapter.title == "Chapter One"
    assert [paragraph.original_text for paragraph in chapter.paragraphs] == [
        "Alpha emphasis .",
        "Beta paragraph.",
    ]
    assert "<em>emphasis</em>" in chapter.paragraphs[0].original_html
    assert parsed.estimated_word_count >= 4


def test_epub_parser_prefers_navigation_title_over_short_heading(tmp_path):
    source_path = tmp_path / "toc-title.epub"
    book = epub.EpubBook()
    book.set_identifier("toc-title-test")
    book.set_title("TOC Title Test")
    book.set_language("en")

    chapter = epub.EpubHtml(
        title="Internal title",
        file_name="chapter.xhtml",
        lang="en",
    )
    chapter.content = (
        "<html><body><h1>1.</h1><p>Chapter text.</p></body></html>"
    )
    book.add_item(chapter)
    book.add_item(epub.EpubNcx())
    book.spine = [chapter]
    book.toc = (
        epub.Link(
            "chapter.xhtml#start",
            "1. Correct Chapter Name",
            "chapter-one",
        ),
    )
    epub.write_epub(str(source_path), book)

    parsed = parse_epub(source_path)
    parsed_chapter = next(
        item for item in parsed.chapters if item.href == "chapter.xhtml"
    )

    assert parsed_chapter.title == "1. Correct Chapter Name"


def test_existing_book_chapter_titles_refresh_from_navigation(
    isolated_app,
    tmp_path,
):
    source_path = tmp_path / "refresh-title.epub"
    book = epub.EpubBook()
    book.set_identifier("refresh-title-test")
    book.set_title("Refresh Title Test")
    book.set_language("en")

    chapter = epub.EpubHtml(
        title="Internal title",
        file_name="chapter.xhtml",
        lang="en",
    )
    chapter.content = "<html><body><h1>1.</h1><p>Text.</p></body></html>"
    book.add_item(chapter)
    book.add_item(epub.EpubNcx())
    book.spine = [chapter]
    book.toc = (
        epub.Link(
            "chapter.xhtml",
            "1. Refreshed Chapter Name",
            "refreshed-chapter",
        ),
    )
    epub.write_epub(str(source_path), book)

    parsed = parse_epub(source_path)
    book_id = db.save_parsed_book(parsed, source_path.name, source_path)
    with db.get_connection() as connection:
        connection.execute(
            "UPDATE chapters SET title = '1.' WHERE book_id = ?",
            (book_id,),
        )

    main._refresh_existing_chapter_titles()

    overview = db.get_book_overview(book_id)
    chapter_row = next(
        item for item in overview["chapters"] if item["href"] == "chapter.xhtml"
    )
    assert chapter_row["title"] == "1. Refreshed Chapter Name"


def test_parser_rejects_body_text_as_toc_title_and_hides_filename(tmp_path):
    source_path = tmp_path / "malformed-toc.epub"
    book = epub.EpubBook()
    book.set_identifier("malformed-toc-test")
    book.set_title("Malformed TOC Test")
    book.set_language("en")

    titled_chapter = epub.EpubHtml(
        title="Internal title",
        file_name="text/part0001.xhtml",
        lang="en",
    )
    titled_chapter.content = (
        "<html><head><title>Internal title</title></head>"
        "<body><h2>Clean Chapter Name</h2>"
        "<p>Chapter body text.</p></body></html>"
    )
    untitled_chapter = epub.EpubHtml(
        title="Untitled internal document",
        file_name="text/part0002.xhtml",
        lang="en",
    )
    untitled_chapter.content = (
        "<html><head></head><body><p>Only body text.</p></body></html>"
    )

    book.add_item(titled_chapter)
    book.add_item(untitled_chapter)
    book.add_item(epub.EpubNcx())
    book.spine = [titled_chapter, untitled_chapter]
    book.toc = (
        epub.Link(
            "text/part0001.xhtml",
            "Bad title " + ("body text " * 100),
            "bad-title",
        ),
    )
    epub.write_epub(str(source_path), book)

    parsed = parse_epub(source_path)
    titles = {
        chapter.href: chapter.title for chapter in parsed.chapters
    }

    assert titles["text/part0001.xhtml"] == "Clean Chapter Name"
    assert titles["text/part0002.xhtml"].startswith("Untitled section ")
    assert "part0002.xhtml" not in titles["text/part0002.xhtml"]
