"""Convert DPMG_User_Manual.md (or another plain Markdown file) to .docx.

Handles headings, paragraphs, bullet and numbered lists, pipe tables, fenced
code blocks, horizontal rules, images (![caption](path), relative to the
Markdown file), and inline **bold**, *italic* and `code`.

    python tools/md_to_docx.py DPMG_User_Manual.md DPMG_User_Manual.docx
"""
import os
import re
import sys

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt

INLINE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*)")


def add_runs(par, text):
    for part in INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            add_runs_bold(par, part[2:-2])
        elif part.startswith("`") and part.endswith("`"):
            r = par.add_run(part[1:-1])
            r.font.name = "Consolas"
            r.font.size = Pt(9.5)
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            par.add_run(part[1:-1]).italic = True
        else:
            par.add_run(part)


def add_runs_bold(par, text):
    for part in re.split(r"(`[^`]+`)", text):
        if not part:
            continue
        if part.startswith("`"):
            r = par.add_run(part[1:-1])
            r.font.name = "Consolas"
            r.font.size = Pt(9.5)
        else:
            r = par.add_run(part)
        r.bold = True


def split_row(line):
    cells = line.strip().strip("|").split("|")
    return [c.strip() for c in cells]


def convert(src, dst):
    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)
    lines = open(src, encoding="utf-8").read().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if s.startswith("```"):
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            p = doc.add_paragraph()
            r = p.add_run("\n".join(code))
            r.font.name = "Consolas"
            r.font.size = Pt(9.5)
            i += 1
            continue
        if not s:
            i += 1
            continue
        if s == "---":
            i += 1
            continue
        m = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)$", s)
        if m:
            path = os.path.join(os.path.dirname(os.path.abspath(src)), m.group(2))
            doc.add_picture(path, width=Inches(6.5))
            doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
            if m.group(1):
                cap = doc.add_paragraph()
                cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
                r = cap.add_run(m.group(1))
                r.italic = True
                r.font.size = Pt(9)
            i += 1
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        if m:
            level = len(m.group(1)) - 1
            h = doc.add_heading(level=level)
            add_runs(h, m.group(2))
            i += 1
            continue
        if s.startswith("|") and i + 1 < len(lines) and re.match(r"^\|?\s*:?-{3,}", lines[i + 1].strip()):
            header = split_row(s)
            rows = []
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(split_row(lines[i]))
                i += 1
            t = doc.add_table(rows=1, cols=len(header))
            t.style = "Table Grid"
            t.alignment = WD_TABLE_ALIGNMENT.CENTER
            for c, txt in enumerate(header):
                add_runs_bold(t.rows[0].cells[c].paragraphs[0], txt)
            for row in rows:
                cells = t.add_row().cells
                for c in range(len(header)):
                    add_runs(cells[c].paragraphs[0], row[c] if c < len(row) else "")
            doc.add_paragraph()
            continue
        m = re.match(r"^(\s*)[-*]\s+(.*)$", line)
        if m:
            style = "List Bullet 2" if len(m.group(1)) >= 2 else "List Bullet"
            add_runs(doc.add_paragraph(style=style), m.group(2))
            i += 1
            continue
        m = re.match(r"^\s*\d+\.\s+(.*)$", line)
        if m:
            add_runs(doc.add_paragraph(style="List Number"), m.group(1))
            i += 1
            continue
        para = [s]
        i += 1
        while i < len(lines):
            n = lines[i].strip()
            if not n or n.startswith(("#", "|", "```", "- ", "* ", "![")) or n == "---" or re.match(r"^\d+\.\s", n):
                break
            para.append(n)
            i += 1
        add_runs(doc.add_paragraph(), " ".join(para))
    doc.save(dst)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    convert(sys.argv[1], sys.argv[2])
