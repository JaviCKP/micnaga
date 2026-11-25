"""Markdown del «Acta completa» -> HTML sencillo para Google Docs (Drive lo convierte).

Mismo subconjunto que la vista (``web/fullacta.js``): titulos, parrafos, listas anidadas,
tablas, citas, codigo, reglas, el bloque «**Campo:** valor» y en linea negrita, cursiva, codigo y
enlaces. Todo el texto se escapa; solo se enlazan URLs ``http(s)``. Las marcas ``[mm:ss]`` quedan
como texto. Salida compacta (sin saltos entre etiquetas): la entrada viaja tal cual en una llamada
de herramienta que se compara exacta (``google.guard``).
"""

from __future__ import annotations

import html
import re

_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_META = re.compile(r"^\s*\*\*([^*]{1,40}?):?\*\*:?\s+(.+)$")
_LIST = re.compile(r"^(\s*)([-*+•]|\d+[.)])\s+(.*)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_RULE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
_INLINE = re.compile(
    r"(\*\*[^*]+\*\*|__[^_]+__|`[^`]+`|\*[^*\s][^*]*\*|\b_[^_\s][^_]*_\b|\[[^\]]+\]\([^)]+\))"
)


def inline(text: str) -> str:
    out: list[str] = []
    pos = 0
    for m in _INLINE.finditer(text):
        out.append(html.escape(text[pos : m.start()], quote=False))
        t = m.group(0)
        if t.startswith(("**", "__")):
            out.append(f"<b>{inline(t[2:-2])}</b>")
        elif t.startswith("`"):
            out.append(f"<code>{html.escape(t[1:-1], quote=False)}</code>")
        elif t.startswith(("*", "_")):
            out.append(f"<i>{inline(t[1:-1])}</i>")
        else:
            lm = re.match(r"^\[([^\]]+)\]\(([^)]+)\)$", t)
            label, url = (lm.group(1), lm.group(2).strip()) if lm else (t, "")
            if url.startswith(("http://", "https://")):
                out.append(f'<a href="{html.escape(url)}">{inline(label)}</a>')
            else:
                out.append(inline(label))
        pos = m.end()
    out.append(html.escape(text[pos:], quote=False))
    return "".join(out)


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", s)]


def _list(items: list[tuple[int, bool, str]]) -> str:
    """``items``: ``(sangria, ordenada, texto)``. Una sangria mayor abre una sublista dentro del
    ultimo ``<li>``."""
    out: list[str] = []
    stack: list[tuple[int, str]] = []  # (sangria, etiqueta)
    for indent, ordered, text in items:
        tag = "ol" if ordered else "ul"
        while stack and indent < stack[-1][0]:
            out.append(f"</li></{stack.pop()[1]}>")
        if not stack or indent > stack[-1][0]:
            out.append(f"<{tag}>")
            stack.append((indent, tag))
        else:
            out.append("</li>")
        task = re.match(r"^\[([ xX])\]\s+(.*)$", text)
        if task:
            text = ("☑ " if task.group(1) != " " else "☐ ") + task.group(2)
        out.append(f"<li>{inline(text)}")
    while stack:
        out.append(f"</li></{stack.pop()[1]}>")
    return "".join(out)


def markdown_to_html(md: str) -> str:
    lines = md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            out.append("<p>" + " ".join(inline(x) for x in para) + "</p>")
            para.clear()

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].rstrip()
        if not line.strip():
            flush()
            i += 1
            continue
        if line.lstrip().startswith(("```", "~~~")):
            flush()
            fence = line.strip()[:3]
            buf: list[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith(fence):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append("<pre>" + html.escape("\n".join(buf), quote=False) + "</pre>")
            continue
        m = _HEADING.match(line)
        if m:
            flush()
            level = min(6, len(m.group(1)))
            out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
            i += 1
            continue
        if _RULE.match(line):
            flush()
            out.append("<hr>")
            i += 1
            continue
        if "|" in line and i + 1 < n and _TABLE_SEP.match(lines[i + 1]):
            flush()
            head = _cells(line)
            i += 2
            rows: list[list[str]] = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1
            t = ['<table border="1" cellpadding="6" style="border-collapse:collapse"><tr>']
            t += [f"<th>{inline(h)}</th>" for h in head]
            t.append("</tr>")
            for r in rows:
                t.append("<tr>")
                t += [f"<td>{inline(r[k] if k < len(r) else '')}</td>" for k in range(len(head))]
                t.append("</tr>")
            t.append("</table>")
            out.append("".join(t))
            continue
        if line.lstrip().startswith(">"):
            flush()
            buf = []
            while i < n and lines[i].lstrip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>" + markdown_to_html("\n".join(buf)) + "</blockquote>")
            continue
        if _LIST.match(line):
            flush()
            items: list[tuple[int, bool, str]] = []
            while i < n:
                ln = lines[i]
                if not ln.strip():
                    if i + 1 < n and _LIST.match(lines[i + 1]):
                        i += 1
                        continue
                    break
                lm = _LIST.match(ln)
                if lm:
                    indent = len(lm.group(1).replace("\t", "    "))
                    items.append((indent, lm.group(2)[0].isdigit(), lm.group(3)))
                elif re.match(r"^\s{2,}\S", ln) and items:
                    ind, ordered, text = items[-1]
                    items[-1] = (ind, ordered, text + " " + ln.strip())
                else:
                    break
                i += 1
            out.append(_list(items))
            continue
        nxt = lines[i + 1] if i + 1 < n else ""
        if _META.match(line) and (_META.match(nxt) or not nxt.strip()):
            flush()
            rows_meta: list[str] = []
            while i < n and (mm := _META.match(lines[i])):
                rows_meta.append(f"<b>{inline(mm.group(1).rstrip(':'))}:</b> {inline(mm.group(2))}")
                i += 1
            out.append("<p>" + "<br>".join(rows_meta) + "</p>")
            continue
        para.append(line.strip())
        i += 1
    flush()
    return "".join(out)


def document(md: str) -> str:
    """El documento HTML completo que se sube a Drive."""
    return "<html><body>" + markdown_to_html(md) + "</body></html>"
