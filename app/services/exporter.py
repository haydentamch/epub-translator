import re
from posixpath import dirname, join, normpath
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from bs4 import BeautifulSoup, Tag

from app.db import get_export_data


EXPORT_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "exports"
FAILED_BILINGUAL = "[Translation failed. Original text kept.]"
FAILED_TRANSLATED_ONLY = "[Translation failed. Original text kept below.]"

EXPORT_STYLE = """
.epub-translator-pair {
  display: flex;
  gap: 1em;
  margin: 0 0 1em;
}
.epub-translator-original,
.epub-translator-translation {
  flex: 1 1 0;
  min-width: 0;
}
.epub-translator-warning {
  border-left: 0.25em solid #b45309;
  color: #7c2d12;
  padding-left: 0.75em;
}
.epub-translator-vertical {
  writing-mode: vertical-rl;
}
@media (max-width: 40em) {
  .epub-translator-pair {
    display: block;
  }
}
"""


class ExportError(Exception):
    pass


def export_epub(job_id: int, mode: str) -> Path:
    if mode not in {"bilingual", "translated-only"}:
        raise ExportError("Unsupported export mode.")

    data = get_export_data(job_id)
    if data is None:
        raise ExportError("Translation job not found.")

    job = data["job"]
    if job["status"] not in {"Completed", "CompletedWithErrors"}:
        raise ExportError("Finish processing all chapters before exporting.")

    source_path = Path(job["uploaded_path"])
    if not source_path.is_file():
        raise ExportError("The uploaded EPUB file is no longer available.")

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stem = _safe_stem(job["book_title"])
    suffix = "bilingual" if mode == "bilingual" else "translated-only"
    output_path = EXPORT_DIR / f"{stem}-{suffix}-{uuid4().hex[:10]}.epub"
    export_identifier = f"urn:uuid:{uuid4()}"

    try:
        _copy_epub_with_chapters(
            source_path,
            output_path,
            data["chapters"],
            job,
            mode,
            export_identifier,
        )
    except ExportError:
        _remove_partial_export(output_path)
        raise
    except (BadZipFile, OSError, ElementTree.ParseError) as exc:
        _remove_partial_export(output_path)
        raise ExportError("The translated EPUB could not be written.") from exc

    return output_path


def _copy_epub_with_chapters(
    source_path: Path,
    output_path: Path,
    chapters: list[dict],
    job: dict,
    mode: str,
    export_identifier: str,
) -> None:
    with ZipFile(source_path, "r") as source:
        package_path = _package_path(source)
        package_dir = dirname(package_path)
        replacements = {
            normpath(join(package_dir, chapter["href"])).lstrip("/"): (
                _render_chapter(chapter, job, mode).encode("utf-8")
            )
            for chapter in chapters
        }
        replacements[package_path] = _render_package_metadata(
            source.read(package_path),
            job["book_title"],
            mode,
            export_identifier,
        )
        source_names = set(source.namelist())
        missing = sorted(set(replacements) - source_names)
        if missing:
            raise ExportError(
                f"Chapter file is missing from the source EPUB: {missing[0]}"
            )

        with ZipFile(output_path, "w") as output:
            for info in source.infolist():
                content = replacements.get(info.filename)
                if content is None:
                    content = source.read(info.filename)
                output.writestr(info, content)


def _render_package_metadata(
    package_content: bytes,
    original_title: str,
    mode: str,
    export_identifier: str,
) -> bytes:
    root = ElementTree.fromstring(package_content)
    package_namespace = "http://www.idpf.org/2007/opf"
    dc_namespace = "http://purl.org/dc/elements/1.1/"
    ElementTree.register_namespace("", package_namespace)
    ElementTree.register_namespace("dc", dc_namespace)

    metadata = root.find(f"{{{package_namespace}}}metadata")
    if metadata is None:
        raise ExportError("The source EPUB metadata could not be found.")

    title = metadata.find(f"{{{dc_namespace}}}title")
    if title is None:
        title = ElementTree.SubElement(metadata, f"{{{dc_namespace}}}title")
    title_suffix = "Bilingual" if mode == "bilingual" else "Translated"
    title.text = f"{original_title} - {title_suffix}"

    unique_identifier_id = root.get("unique-identifier")
    identifier = None
    if unique_identifier_id:
        identifier = metadata.find(
            f"{{{dc_namespace}}}identifier[@id='{unique_identifier_id}']"
        )
    if identifier is None:
        identifier = metadata.find(f"{{{dc_namespace}}}identifier")
    if identifier is None:
        identifier = ElementTree.SubElement(
            metadata,
            f"{{{dc_namespace}}}identifier",
        )
        identifier.set("id", "epub-translator-id")
        root.set("unique-identifier", "epub-translator-id")
    identifier.text = export_identifier

    return ElementTree.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
    )


def _package_path(source: ZipFile) -> str:
    container = ElementTree.fromstring(source.read("META-INF/container.xml"))
    rootfile = container.find(
        ".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile"
    )
    if rootfile is None or not rootfile.get("full-path"):
        raise ExportError("The source EPUB package document could not be found.")
    return str(rootfile.get("full-path"))


def _render_chapter(
    chapter: dict,
    job: dict,
    mode: str,
) -> str:
    soup = BeautifulSoup(chapter["original_html"], "html.parser")
    nodes = _paragraph_nodes(soup)
    paragraphs = chapter["paragraphs"]

    if len(nodes) != len(paragraphs):
        raise ExportError(
            f"Chapter structure changed and cannot be exported: {chapter['title']}"
        )

    _inject_style(soup)

    for node, paragraph in zip(nodes, paragraphs):
        if mode == "bilingual":
            replacement = _bilingual_node(soup, node, paragraph, job)
        else:
            replacement = _translated_only_node(soup, node, paragraph, job)
        node.replace_with(replacement)

    return str(soup)


def _paragraph_nodes(soup: BeautifulSoup) -> list[Tag]:
    nodes = [
        node
        for node in soup.select("p")
        if node.get_text(" ", strip=True)
    ]
    if nodes:
        return nodes

    body = soup.body or soup
    return [
        child
        for child in body.find_all(recursive=False)
        if isinstance(child, Tag) and child.get_text(" ", strip=True)
    ]


def _bilingual_node(
    soup: BeautifulSoup,
    original_node: Tag,
    paragraph: dict,
    job: dict,
) -> Tag:
    container = soup.new_tag("div")
    container["class"] = ["epub-translator-pair"]

    original = soup.new_tag("div")
    original["class"] = ["epub-translator-original"]
    original.append(_clone_tag(original_node))

    translation = soup.new_tag("div")
    translation["class"] = [
        "epub-translator-translation",
        *_direction_classes(job["text_direction"]),
    ]
    _apply_direction(translation, job["text_direction"])

    if paragraph["status"] == "Completed" and paragraph["translated_html"]:
        translation.append(_html_fragment(paragraph["translated_html"]))
    else:
        warning = soup.new_tag("p")
        warning["class"] = ["epub-translator-warning"]
        warning.string = FAILED_BILINGUAL
        translation.append(warning)

    if job["layout"] == "translation-first":
        container.extend([translation, original])
    else:
        container.extend([original, translation])

    if job["layout"] != "side-by-side":
        container["style"] = "display: block;"

    return container


def _translated_only_node(
    soup: BeautifulSoup,
    original_node: Tag,
    paragraph: dict,
    job: dict,
) -> Tag:
    container = soup.new_tag("div")
    container["class"] = [
        "epub-translator-translation",
        *_direction_classes(job["text_direction"]),
    ]
    _apply_direction(container, job["text_direction"])

    if paragraph["status"] == "Completed" and paragraph["translated_html"]:
        container.append(_html_fragment(paragraph["translated_html"]))
        return container

    warning = soup.new_tag("p")
    warning["class"] = ["epub-translator-warning"]
    warning.string = FAILED_TRANSLATED_ONLY
    container.extend([warning, _clone_tag(original_node)])
    return container


def _clone_tag(tag: Tag) -> Tag:
    return _html_fragment(str(tag))


def _html_fragment(html: str) -> Tag:
    fragment = BeautifulSoup(html, "html.parser")
    tag = fragment.find(True)
    if tag is None:
        wrapper = fragment.new_tag("p")
        wrapper.string = fragment.get_text()
        return wrapper
    return tag


def _inject_style(soup: BeautifulSoup) -> None:
    head = soup.head
    if head is None:
        html = soup.html
        if html is None:
            html = soup.new_tag("html")
            html.extend(list(soup.contents))
            soup.append(html)
        head = soup.new_tag("head")
        html.insert(0, head)

    style = soup.new_tag("style")
    style["type"] = "text/css"
    style.string = EXPORT_STYLE
    head.append(style)


def _direction_classes(text_direction: str) -> list[str]:
    return (
        ["epub-translator-vertical"]
        if text_direction == "vertical-rtl"
        else []
    )


def _apply_direction(tag: Tag, text_direction: str) -> None:
    directions = {
        "horizontal-ltr": "ltr",
        "horizontal-rtl": "rtl",
        "vertical-rtl": "rtl",
        "auto-detect": "auto",
    }
    tag["dir"] = directions.get(text_direction, "auto")


def _safe_stem(title: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", title).strip("-._")
    return stem[:80] or "translated-book"


def _remove_partial_export(output_path: Path) -> None:
    try:
        output_path.unlink(missing_ok=True)
    except OSError:
        pass
