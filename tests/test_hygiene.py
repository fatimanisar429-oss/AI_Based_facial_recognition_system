"""Housekeeping checks on the source files themselves."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = [p for folder in ("app", "tests") for p in (ROOT / folder).rglob("*")
          if p.is_file() and p.suffix in (".py", ".html", ".css", ".js", ".sql") and "__pycache__" not in p.parts]


def test_no_file_starts_with_a_byte_order_mark():
    """A BOM in a template ends up in the page. Windows PowerShell's Set-Content adds one, so it is easy to introduce."""
    assert [str(p.relative_to(ROOT)) for p in SOURCE if p.read_bytes().startswith(b"\xef\xbb\xbf")] == []


def test_no_garbled_characters():
    """'Â·' is what a UTF-8 dot becomes when a file is re-encoded by mistake."""
    garbled = [str(p.relative_to(ROOT)) for p in SOURCE if p.name != "test_hygiene.py" and "\u00c2\u00b7" in p.read_text(encoding="utf-8")]
    assert garbled == []

def test_documentation_links_point_to_existing_files():
    """README and docs/*.md link to each other; a renamed file must not leave a dead link."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    pages = [root / "README.md"] + sorted((root / "docs").glob("*.md"))
    assert len(pages) >= 9
    for page in pages:
        for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", page.read_text(encoding="utf-8")):
            if not target.startswith(("http://", "https://", "mailto:")):
                assert (page.parent / target).exists(), f"{page.name}: broken link {target}"
