"""Writes a one-page PDF of plain text, for the mock scenario's invented notices.

It is the smallest PDF a reader opens: one page, one built-in font, and the text
uncompressed, so the file can be told apart and read back without a PDF library. The
same lines always give the same bytes.
"""

_PAGE_WIDTH, _PAGE_HEIGHT = 612, 792
_LEFT, _TOP, _LEADING = 72, 720, 16


def text_pdf(lines: list[str]) -> bytes:
    """A PDF whose one page shows `lines`, the first in bold as its title. The lines
    are ASCII; anything else is written as a question mark."""
    content = ["BT", f"{_LEADING} TL", f"{_LEFT} {_TOP} Td"]
    for number, line in enumerate(lines):
        font = "/F2 14 Tf" if number == 0 else "/F1 11 Tf"
        content += [font, f"({_escape(line)}) Tj", "T*"]
    content.append("ET")
    stream = "\n".join(content).encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_PAGE_WIDTH} {_PAGE_HEIGHT}] "
            "/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>"
        ).encode("ascii"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objects) + 1)
    out += b"startxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


def _escape(line: str) -> str:
    text = line.encode("ascii", "replace").decode("ascii")
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
