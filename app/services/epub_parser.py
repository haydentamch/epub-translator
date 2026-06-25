from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup
from ebooklib import ITEM_DOCUMENT, epub


@dataclass(frozen=True)
class ParsedParagraph:
    order_index: int
    original_html: str
    original_text: str


@dataclass(frozen=True)
class ParsedChapter:
    order_index: int
    title: str
    href: str
    original_html: str
    paragraphs: list[ParsedParagraph]


@dataclass(frozen=True)
class ParsedBook:
    title: str
    author: str | None
    chapters: list[ParsedChapter]

    @property
    def estimated_word_count(self) -> int:
        return sum(
            len(paragraph.original_text.split())
            for chapter in self.chapters
            for paragraph in chapter.paragraphs
        )


def parse_epub(file_path: Path) -> ParsedBook:
    try:
        book = epub.read_epub(str(file_path))
    except Exception as exc:
        raise ValueError("The uploaded file could not be read as a valid EPUB.") from exc

    title = _first_metadata_value(book, "title") or file_path.stem
    author = _first_metadata_value(book, "creator")
    chapters = _extract_chapters(book)

    if not chapters:
        raise ValueError("No readable chapters were found in the EPUB.")

    return ParsedBook(title=title, author=author, chapters=chapters)


def _first_metadata_value(book: epub.EpubBook, name: str) -> str | None:
    values = book.get_metadata("DC", name)

    for value in values:
        text = str(value[0]).strip() if value and value[0] else ""
        if text:
            return text

    return None


def _extract_chapters(book: epub.EpubBook) -> list[ParsedChapter]:
    toc_titles = extract_toc_titles(book)
    documents = {
        item.get_id(): item
        for item in book.get_items_of_type(ITEM_DOCUMENT)
    }
    ordered_items = []
    seen_ids: set[str] = set()

    for spine_entry in book.spine:
        item_id = spine_entry[0] if isinstance(spine_entry, tuple) else spine_entry
        item = documents.get(item_id)

        if item is None or item_id in seen_ids:
            continue

        ordered_items.append(item)
        seen_ids.add(item_id)

    for item in book.get_items_of_type(ITEM_DOCUMENT):
        if item.get_id() not in seen_ids:
            ordered_items.append(item)
            seen_ids.add(item.get_id())

    chapters: list[ParsedChapter] = []

    for item in ordered_items:
        raw_html = item.get_content().decode("utf-8", errors="replace")
        soup = BeautifulSoup(raw_html, "html.parser")
        paragraphs = _extract_paragraphs(soup)

        if not paragraphs:
            continue

        chapters.append(
            ParsedChapter(
                order_index=len(chapters),
                title=(
                    _usable_title(
                        toc_titles.get(_normalize_href(item.get_name()))
                    )
                    or _chapter_title(soup, len(chapters))
                ),
                href=item.get_name(),
                original_html=raw_html,
                paragraphs=paragraphs,
            )
        )

    return chapters


def extract_toc_titles(book: epub.EpubBook) -> dict[str, str]:
    titles: dict[str, str] = {}

    def visit(items) -> None:
        for item in items:
            if isinstance(item, tuple):
                section, children = item
                add_item(section)
                visit(children)
            else:
                add_item(item)

    def add_item(item) -> None:
        href = getattr(item, "href", None) or getattr(item, "file_name", None)
        title = getattr(item, "title", None)
        normalized_href = _normalize_href(href or "")
        normalized_title = _usable_title(title)

        if normalized_href and normalized_title:
            titles.setdefault(normalized_href, normalized_title)

    visit(book.toc)
    return titles


def _normalize_href(href: str) -> str:
    return unquote(urlsplit(str(href)).path).replace("\\", "/").lstrip("./")


def _extract_paragraphs(soup: BeautifulSoup) -> list[ParsedParagraph]:
    nodes = soup.select("p")

    if not nodes:
        body = soup.body or soup
        nodes = [
            child
            for child in body.find_all(recursive=False)
            if child.name and child.get_text(" ", strip=True)
        ]

    paragraphs: list[ParsedParagraph] = []

    for node in nodes:
        text = node.get_text(" ", strip=True)

        if not text:
            continue

        paragraphs.append(
            ParsedParagraph(
                order_index=len(paragraphs),
                original_html=str(node),
                original_text=text,
            )
        )

    return paragraphs


def _chapter_title(soup: BeautifulSoup, zero_based_index: int) -> str:
    for heading in soup.find_all(["h1", "h2", "h3"]):
        title = _usable_title(heading.get_text(" ", strip=True))
        if title:
            return title

    document_title = _usable_title(
        soup.title.get_text(" ", strip=True) if soup.title else None
    )
    if document_title:
        return document_title

    return f"Untitled section {zero_based_index + 1}"


def _usable_title(value: object) -> str | None:
    title = " ".join(str(value or "").split())
    if not title:
        return None
    if len(title) > 160:
        return None
    if len(title.split()) > 24:
        return None
    return title
