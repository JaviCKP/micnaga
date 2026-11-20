"""Limpieza de la transcripcion con Haiku, antes del acta (DECISIONS §23).

Despues del ASR y la diarizacion, y antes de la pasada de la Secretaria, Haiku revisa los
parrafos y corrige lo evidente: puntuacion (los puntos falsos que cortan una frase, ¿? ¡!,
comas), tildes, mayusculas, cifras, palabras mal oidas, siglas y nombres. Tambien puede **unir**
parrafos seguidos del mismo hablante cuando un punto falso los partio.

* ``claude -p --model haiku`` de solo lectura: sin herramientas ni servidores MCP, con
  ``--json-schema``. Ventanas pequenas (``WINDOW`` parrafos, con ``CONTEXT`` parrafos
  anteriores de solo lectura para poder unir en el borde y mantener los nombres), varias a la
  vez (``WORKERS``), con un tope total (``DEADLINE_S``).
* **La seguridad esta en Python** (``check``): se comparan tokens normalizados (minusculas, sin
  tildes ni puntuacion, cifras dichas = cifras escritas), asi que puntuacion, mayusculas y
  tildes no cuentan. Se rechaza un cambio con demasiadas sustituciones de palabra, una
  sustitucion que no se parece a lo oido (salvo siglas deletreadas y vocabulario), palabras
  anadidas o quitadas, cifras, meses o dias que no se dijeron, o un cambio de longitud grande.
  Una union exige mismo hablante, ids seguidos y pausas cortas. Nunca se parte un parrafo ni
  cambian hablantes o tiempos.
* Se guarda el original (``Segment.text_original``; ``merged_from`` en las uniones), las
  palabras se realinean con el texto nuevo (``remap_words``: ``" ".join(w.w) == text`` sigue
  valiendo para el SRT y el prompt), se regeneran MD y SRT, se reindexa el FTS (texto corregido)
  y el texto original de los parrafos con palabras cambiadas va a ``original_fts``.
* Una vez por reunion (``limpieza.json``). Sin Claude, se salta en silencio y sin marca.
"""

from __future__ import annotations

import contextlib
import dataclasses
import itertools
import json
import logging
import re
import threading
import time
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from micnaga.types import Segment, Transcript, Word

log = logging.getLogger(__name__)

VERSION = 1
MARKER = "limpieza.json"
VOCAB_FILE = "vocabulario.json"
MODEL = "haiku"
WINDOW = 5
CONTEXT = 2
WINDOW_CHARS = 3000
WORKERS = 24  # techo; el real lo limita la maquina (default_workers)
CLAUDE_PROC_MB = 300  # memoria aproximada de cada ``claude -p`` (node) en paralelo
BATCH_TIMEOUT_S = 60.0
DEADLINE_S = 90.0
MAX_RATIO = 0.35
MIN_BUDGET = 2
MAX_LEN_CHANGE = 0.30
MAX_INSERT = 2
MAX_DELETE = 2
MIN_SIMILARITY = 0.5
VOCAB_SIMILARITY = 0.4
MERGE_GAP_S = 1.5
MAX_MERGE = 4
MAX_MERGED_CHARS = 1600
MAX_VOCAB = 150
FATAL = ("not_found", "auth", "rate_limit")

StatusCallback = Callable[[dict[str, Any]], None]
WithDb = Callable[[Callable[[Any], Any]], Any]

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cambios": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "un parrafo, o varios seguidos del mismo hablante (union)",
                    },
                    "texto": {"type": "string"},
                },
                "required": ["ids", "texto"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["cambios"],
    "additionalProperties": False,
}


# ============================================================================== normalizacion

_UNITS = {
    "cero": 0,
    "uno": 1,
    "dos": 2,
    "tres": 3,
    "cuatro": 4,
    "cinco": 5,
    "seis": 6,
    "siete": 7,
    "ocho": 8,
    "nueve": 9,
}
_ARTICLE_ONE = {"un", "una"}  # solo cuentan dentro de un numero («un millon», «veintiuna»)
_TEENS = {
    "diez": 10,
    "once": 11,
    "doce": 12,
    "trece": 13,
    "catorce": 14,
    "quince": 15,
    "dieciseis": 16,
    "diecisiete": 17,
    "dieciocho": 18,
    "diecinueve": 19,
    "veinte": 20,
    "veintiun": 21,
    "veintiuno": 21,
    "veintiuna": 21,
    "veintidos": 22,
    "veintitres": 23,
    "veinticuatro": 24,
    "veinticinco": 25,
    "veintiseis": 26,
    "veintisiete": 27,
    "veintiocho": 28,
    "veintinueve": 29,
}
_TENS = {
    "treinta": 30,
    "cuarenta": 40,
    "cincuenta": 50,
    "sesenta": 60,
    "setenta": 70,
    "ochenta": 80,
    "noventa": 90,
}
_HUNDREDS = {
    "cien": 100,
    "ciento": 100,
    "doscientos": 200,
    "trescientos": 300,
    "cuatrocientos": 400,
    "quinientos": 500,
    "seiscientos": 600,
    "setecientos": 700,
    "ochocientos": 800,
    "novecientos": 900,
}
_HUNDREDS.update({k[:-2] + "as": v for k, v in list(_HUNDREDS.items()) if k.endswith("os")})
_SMALL = {**_UNITS, **_TEENS, **_TENS, **_HUNDREDS}
_BIG = {"mil", "millon", "millones"}
MONTHS = {
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "setiembre",
    "octubre",
    "noviembre",
    "diciembre",
}
WEEKDAYS = {"lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"}
_LETTERS = {
    "a": "a",
    "b": "be",
    "c": "ce",
    "d": "de",
    "e": "e",
    "f": "efe",
    "g": "ge",
    "h": "hache",
    "i": "i",
    "j": "jota",
    "k": "ka",
    "l": "ele",
    "m": "eme",
    "n": "ene",
    "o": "o",
    "p": "pe",
    "q": "cu",
    "r": "erre",
    "s": "ese",
    "t": "te",
    "u": "u",
    "v": "uve",
    "w": "uvedoble",
    "x": "equis",
    "y": "ye",
    "z": "zeta",
}
_THOUSANDS = re.compile(r"\b\d{1,3}(?:\.\d{3})+\b")
_DECIMAL = re.compile(r"(?<=\d),(?=\d)")
_NON_WORD = re.compile(r"[^\w]+")
_PERCENT = re.compile(r"\bpor\s+ciento\b")


def fold(text: str) -> str:
    """Minusculas y sin tildes (``n`` de ``ñ`` incluida: solo importa para comparar)."""
    s = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in s if not unicodedata.combining(c))


def _raw_tokens(text: str) -> list[str]:
    t = fold(text).replace("%", " porciento ").replace("€", " euros ")
    t = _PERCENT.sub("porciento", t)
    t = _THOUSANDS.sub(lambda m: m.group(0).replace(".", ""), t)
    t = _DECIMAL.sub("_", t)  # 3,5 -> 3_5 (el «_» sobrevive a la limpieza)
    return [x for x in _NON_WORD.sub(" ", t).split() if x]


def _number_at(toks: list[str], i: int) -> tuple[int, int] | None:
    """``(valor, fin)`` de un numero dicho o escrito que empieza en ``toks[i]``."""
    total, current, j, seen, last_tens, digit = 0, 0, i, False, False, False
    while j < len(toks):
        w = toks[j]
        if w.isdigit() and not seen:
            current, seen, digit, j = int(w), True, True, j + 1
            continue
        if digit and w not in _BIG:
            break
        if w in _SMALL or (w in _ARTICLE_ONE and j + 1 < len(toks) and toks[j + 1] in _BIG):
            current += _SMALL.get(w, 1)
            seen, last_tens, j = True, w in _TENS, j + 1
            continue
        if w == "y" and seen and last_tens and j + 1 < len(toks) and toks[j + 1] in _UNITS:
            j += 1
            continue
        if w == "mil" and (seen or j == i):
            total += (current or 1) * 1000
            current, seen, last_tens, j = 0, True, False, j + 1
            continue
        if w in ("millon", "millones") and seen:
            total = (total + (current or 1)) * 1_000_000
            current, last_tens, j = 0, False, j + 1
            continue
        break
    if not seen or j == i:
        return None
    return total + current, j


def tokens(text: str) -> list[str]:
    """Tokens normalizados: sin tildes, mayusculas ni puntuacion; cada numero (dicho o escrito)
    es un solo token con su valor («cuatrocientos cincuenta mil» = «450.000» = ``450000``)."""
    raw = _raw_tokens(text)
    out: list[str] = []
    i = 0
    while i < len(raw):
        hit = _number_at(raw, i)
        if hit is not None:
            value, i = str(hit[0]), hit[1]
            if i + 1 < len(raw) and raw[i] == "coma":  # «tres coma cinco» = «3,5»
                dec = _number_at(raw, i + 1)
                if dec is not None:
                    value, i = f"{value}_{dec[0]}", dec[1]
            out.append(value)
        else:
            out.append(raw[i])
            i += 1
    return out


def _numbers(toks: list[str]) -> set[str]:
    return {t for t in toks if t[:1].isdigit()}


def _acronym(old: list[str], new: list[str]) -> bool:
    """«pe cu ce» -> «PQC»: una sigla deletreada."""
    if len(new) != 1 or not (2 <= len(new[0]) <= 6) or not new[0].isalpha():
        return False
    spelled = "".join(_LETTERS.get(c, c) for c in new[0])
    return fuzz.ratio("".join(old), spelled) >= 80


# ============================================================================== validacion


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""
    cost: float = 0.0
    words_changed: bool = False
    """Cambio alguna palabra (no solo puntuacion, mayusculas o tildes)."""


def check(old_text: str, new_text: str, vocab: set[str] | frozenset[str] = frozenset()) -> Verdict:
    """¿Es ``new_text`` una correccion (no una reescritura) de ``old_text``?

    ``vocab``: formas normalizadas (``fold`` sin espacios) del vocabulario de la reunion."""
    new_text = " ".join(new_text.split())
    if not new_text:
        return Verdict(False, "vacio")
    if "⟨" in new_text or len(new_text) > 2 * len(old_text) + 40:
        return Verdict(False, "longitud")
    a, b = tokens(old_text), tokens(new_text)
    if not a:
        return Verdict(False, "anadido")
    if a == b:
        return Verdict(True)
    if _numbers(b) - _numbers(a):
        return Verdict(False, "cifra")
    if ((set(b) & (MONTHS | WEEKDAYS)) - set(a)) and "".join(a) != "".join(b):
        return Verdict(False, "fecha")
    cost = 0.0
    inserted = deleted = 0
    for op, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        sa, sb = a[i1:i2], b[j1:j2]
        if op == "insert":
            inserted += len(sb)
            cost += len(sb)
            continue
        if op == "delete":
            deleted += len(sa)
            cost += len(sa)
            continue
        ja, jb = "".join(sa), "".join(sb)
        if ja == jb:
            continue  # palabra mal partida o pegada: «micro soft» -> «Microsoft»
        if _acronym(sa, sb):
            cost += 1
            continue
        sim = fuzz.ratio(ja, jb) / 100.0
        if jb in vocab or any(t in vocab for t in sb):
            if sim < VOCAB_SIMILARITY:
                return Verdict(False, "sustitucion")
            cost += 1
            continue
        if sim < MIN_SIMILARITY:
            return Verdict(False, "sustitucion")
        cost += max(len(sa), len(sb))
    if inserted > MAX_INSERT:
        return Verdict(False, "anadido")
    if deleted > MAX_DELETE:
        return Verdict(False, "quitado")
    if cost > max(MIN_BUDGET, MAX_RATIO * len(a)):
        return Verdict(False, "demasiado")
    la, lb = len(" ".join(a)), len(" ".join(b))
    if abs(lb - la) / max(la, 20) > MAX_LEN_CHANGE:
        return Verdict(False, "longitud")
    return Verdict(True, cost=cost, words_changed=True)


# ============================================================================== palabras


def _key(tok: str) -> str:
    k = _NON_WORD.sub("", fold(tok))
    return k or tok


def remap_words(old: list[Word], new_text: str) -> list[Word]:
    """Las palabras de antes con el texto nuevo: mismos tiempos donde la palabra es la misma
    (aunque cambie la puntuacion), el tramo repartido donde cambio, ``p = 1`` en lo corregido.
    Garantiza ``" ".join(w.w for w in palabras) == new_text`` (si habia palabras)."""
    new_toks = new_text.split()
    if not old:
        return []
    a = [_key(w.w) for w in old]
    b = [_key(t) for t in new_toks]
    out: list[Word] = []
    for op, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            for k in range(i2 - i1):
                w = old[i1 + k]
                out.append(Word(new_toks[j1 + k], w.s, w.e, w.p))
        elif op == "replace":
            s, e = old[i1].s, old[i2 - 1].e
            span = new_toks[j1:j2]
            if len(span) == i2 - i1:
                for k, tok in enumerate(span):
                    out.append(Word(tok, old[i1 + k].s, old[i1 + k].e, 1.0))
                continue
            total = sum(len(t) for t in span) or 1
            t0 = s
            for tok in span:
                t1 = t0 + (e - s) * len(tok) / total
                out.append(Word(tok, round(t0, 3), round(t1, 3), 1.0))
                t0 = t1
        elif op == "insert":
            t = out[-1].e if out else old[min(i1, len(old) - 1)].s
            for tok in new_toks[j1:j2]:
                out.append(Word(tok, t, t, 1.0))
        # delete: la palabra se va con su tiempo
    return out


def marks(seg: Segment) -> list[list[Any]]:
    """Tramos ``[inicio, fin, original]`` (caracteres del texto nuevo) donde cambio una palabra.
    Lo que solo cambia de puntuacion, mayusculas o tildes no se marca."""
    if seg.text_original is None:
        return []
    old = seg.text_original.split()
    spans = [(m.start(), m.end()) for m in re.finditer(r"\S+", seg.text)]
    a = [_key(t) for t in old]
    b = [_key(seg.text[x:y]) for x, y in spans]
    out: list[list[Any]] = []
    for op, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        if op == "delete":
            continue  # lo quitado se ve con «Original»
        if op == "replace" and "".join(a[i1:i2]) == "".join(b[j1:j2]):
            continue  # solo se pego o partio con otra puntuacion
        out.append([spans[j1][0], spans[j2 - 1][1], " ".join(old[i1:i2])])
    return out


# ============================================================================== vocabulario

_PROPER = re.compile(r"\b[A-ZÁÉÍÓÚÑ][\wáéíóúñü]+(?:\s+(?:de\s+)?[A-ZÁÉÍÓÚÑ][\wáéíóúñü]+)*")
_ACRO = re.compile(r"\b[A-ZÑ][A-ZÑ0-9]{1,7}\b")
_SKIP = frozenset(
    str.split(
        "el la los las un una en con para por que se es su sus al del de y si no lo le les hay "
        "cada este esta ese esa eso esto yo tu vale bueno pues entonces claro hola gracias "
        "perfecto vamos mira oye ya ah eh pero como cuando donde muy mas hablante reunion nota "
        "siempre nunca ok okay"
    )
)


def proper_terms(text: str) -> list[str]:
    """Nombres propios y siglas de un texto (heuristica barata)."""
    out: list[str] = []
    for m in _PROPER.finditer(text):
        words = [w for w in m.group(0).split() if fold(w) not in _SKIP]
        if words and not (len(words) == 1 and words[0].lower() == "de"):
            out.append(" ".join(words))
    out += [m.group(0) for m in _ACRO.finditer(text) if fold(m.group(0)) not in _SKIP]
    return [t for t in out if len(t) >= 2]


def recurring_terms(transcript: Transcript, *, min_count: int = 2) -> list[str]:
    """Nombres y siglas que se repiten en la reunion (fuera del inicio de frase)."""
    counts: dict[str, int] = {}
    shown: dict[str, str] = {}
    for seg in transcript.segments:
        toks = seg.text.split()
        for k, tok in enumerate(toks):
            starts = k == 0 or toks[k - 1][-1:] in ".?!¿¡:"
            if starts and not _ACRO.fullmatch(tok.strip(".,;:?!¿¡«»\"'()")):
                continue
            word = tok.strip(".,;:?!¿¡«»\"'()")
            if not word or not (word[0].isupper() or _ACRO.fullmatch(word)):
                continue
            if fold(word) in _SKIP or len(word) < 2:
                continue
            key = fold(word)
            counts[key] = counts.get(key, 0) + 1
            shown.setdefault(key, word)
    ranked = sorted(counts, key=lambda k: -counts[k])
    return [shown[k] for k in ranked if counts[k] >= min_count]


def vocab_path() -> Path:
    from micnaga.brain.learn import secretaria_dir

    return secretaria_dir() / VOCAB_FILE


def load_vocab_cache() -> list[str]:
    try:
        data = json.loads(vocab_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    names = data.get("nombres") if isinstance(data, dict) else None
    return [str(n) for n in names if isinstance(n, str)] if isinstance(names, list) else []


def remember_names(names: list[str], *, limit: int = 300) -> None:
    """Nombres de Odoo vistos (proyectos, empresas, contactos) para las proximas limpiezas."""
    clean = [" ".join(n.split())[:80] for n in names if isinstance(n, str) and n.strip()]
    if not clean:
        return
    merged = list(dict.fromkeys([*clean, *load_vocab_cache()]))[:limit]
    path = vocab_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"nombres": merged}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        log.debug("could not save the vocabulary cache", exc_info=True)


def names_in_results(texts: list[str]) -> list[str]:
    """``"nombre": "..."`` de los resultados de las herramientas de Odoo (JSON compacto)."""
    out: list[str] = []
    for t in texts:
        out += re.findall(r'"nombre":\s*"([^"\\]{2,80})"', t)
    return out


# Terminos de trabajo que el ASR destroza a menudo («Claudio» por Claude, «prometto» por prompt).
BASE_VOCAB = (
    "Claude",
    "Anthropic",
    "prompt",
    "prompts",
    "Odoo",
    "CRM",
    "Micnaga",
    "Bitnaga",
    "PQC",
    "poscuántico",
    "IA",
    "LLM",
    "MCP",
    "API",
    "SaaS",
    "Google Meet",
    "Gmail",
    "Google Drive",
    "nmap",
    "testssl",
    "ssh-audit",
    "TLS",
    "VPN",
    "pentesting",
    "SOC",
    "demo",
    "copiloto",
)


def user_names() -> list[str]:
    """Nombre del propio usuario (perfil de Odoo en cache): nombre completo, nombre de pila y su
    diminutivo habitual, para que «Hola, Javi» no se corrija hacia el nombre del otro hablante."""
    from micnaga.brain.learn import secretaria_dir

    out: list[str] = []
    with contextlib.suppress(OSError, ValueError, AttributeError, TypeError):
        data = json.loads((secretaria_dir() / "odoo_capacidades.json").read_text(encoding="utf-8"))
        for prof in data.values() if isinstance(data, dict) else []:
            full = " ".join(str(prof.get("user") or "").split())
            if not full:
                continue
            first = full.split()[0]
            out += [full, first]
            if len(first) > 4:
                out.append(first[:4])  # Javier -> Javi
    return list(dict.fromkeys(out))


def vocabulary(
    config: Any, folder: Path, transcript: Transcript, *, with_db: WithDb | None = None
) -> list[str]:
    """Pistas de vocabulario: glosario, ``learned.md`` y contexto propio, nombres de Odoo ya
    vistos, asistentes del evento, hablantes con nombre, participantes de otras reuniones y los
    terminos que se repiten en esta."""
    from micnaga.brain import learn
    from micnaga.brain.secretary import glossary_lines, is_generic_title

    found: list[str] = list(BASE_VOCAB)
    with contextlib.suppress(Exception):
        found += [ln.partition(":")[0].strip() for ln in glossary_lines(config)]
    with contextlib.suppress(Exception):
        for item in learn.list_learned():
            found += proper_terms(item.fact)
        for _name, text in learn._read_md(learn.user_context_dir()):
            found += proper_terms(text)
    found += load_vocab_cache()
    found += user_names()
    with contextlib.suppress(OSError, ValueError, AttributeError):
        from micnaga.brain.google.analysis import CONTEXT_JSON

        ctx = json.loads((folder / CONTEXT_JSON).read_text(encoding="utf-8"))
        found += proper_terms(str(ctx.get("titulo") or ""))
        found += [
            str(p.get("nombre"))
            for p in ctx.get("asistentes") or []
            if isinstance(p, dict) and p.get("nombre")
        ]
    from micnaga.brain.research import _names

    found += _names(list(transcript.speakers))
    if not is_generic_title(transcript.meeting.title):
        found += proper_terms(transcript.meeting.title)
    if with_db is not None:
        with contextlib.suppress(Exception):
            recs = with_db(lambda db: db.list_meetings(200))
            for rec in recs:
                found += _names(rec.speakers)
    found += recurring_terms(transcript)
    out: dict[str, str] = {}
    for t in found:
        t = " ".join(str(t).split())
        if 2 <= len(t) <= 60:
            out.setdefault(fold(t), t)
    return list(out.values())[:MAX_VOCAB]


def vocab_keys(vocab: list[str]) -> frozenset[str]:
    keys: set[str] = set()
    for v in vocab:
        toks = tokens(v)
        if toks:
            keys.add("".join(toks))
            keys.update(t for t in toks if len(t) > 2)
    return frozenset(keys)


# ============================================================================== ventanas


@dataclass(frozen=True)
class Window:
    ids: list[int]
    context: list[int]


def windows(segments: list[Segment], size: int = WINDOW, context: int = CONTEXT) -> list[Window]:
    out: list[Window] = []
    i = 0
    while i < len(segments):
        ids: list[int] = []
        chars = 0
        while i < len(segments) and len(ids) < size:
            if ids and chars + len(segments[i].text) > WINDOW_CHARS:
                break
            ids.append(i)
            chars += len(segments[i].text)
            i += 1
        out.append(Window(ids, list(range(max(0, ids[0] - context), ids[0]))))
    return out


def _line(transcript: Transcript, i: int) -> str:
    seg = transcript.segments[i]
    prev = transcript.segments[i - 1] if i > 0 else None
    gap = ""
    if prev is not None and prev.speaker == seg.speaker:
        gap = f", pausa {max(0.0, seg.start - prev.end):.1f} s"
    return f"#{i} [{seg.speaker}{gap}] {seg.text}"


def window_input(transcript: Transcript, win: Window, vocab: list[str]) -> str:
    parts = []
    if vocab:
        parts += ["## Vocabulario (formas correctas)", ", ".join(vocab), ""]
    if win.context:
        parts += ["## Contexto (anterior)", *[_line(transcript, i) for i in win.context], ""]
    parts += ["## Párrafos", *[_line(transcript, i) for i in win.ids]]
    return "\n".join(parts) + "\n"


# ============================================================================== pasada


@dataclass
class Op:
    ids: list[int]
    text: str
    words_changed: bool


@dataclass
class Stats:
    segments: int = 0
    windows: int = 0
    failed: int = 0
    proposed: int = 0
    corrected: int = 0
    """Parrafos de antes que cambiaron (los unidos cuentan uno a uno)."""
    merges: int = 0
    rejected: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0
    window_s: list[float] = field(default_factory=list)
    """Duracion de cada ventana (reloj), para medir."""
    cost_usd: float = 0.0
    skipped: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def validate_ops(
    raw: object,
    transcript: Transcript,
    win: Window,
    vocab: frozenset[str],
    stats: Stats,
) -> list[Op]:
    """Lo que devuelve una ventana, filtrado: ids de la ventana (el contexto solo para unir con
    el primero), uniones de un solo hablante con ids seguidos y pausas cortas, y ``check``."""
    segs = transcript.segments
    allowed = set(win.ids)
    out: list[Op] = []
    used: set[int] = set()
    items = raw.get("cambios") if isinstance(raw, dict) else None
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        ids = it.get("ids")
        text = it.get("texto")
        if not isinstance(ids, list) or not isinstance(text, str) or not ids:
            continue
        if not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
            continue
        if len(ids) == 1 and ids[0] in win.context:
            continue  # el contexto no se corrige aqui (lo hace su propia ventana)
        stats.proposed += 1
        reason = _merge_problem(ids, segs, allowed, set(win.context), used)
        if reason:
            stats.rejected += 1
            stats.reasons[reason] = stats.reasons.get(reason, 0) + 1
            continue
        old = " ".join(segs[i].text for i in ids)
        text = " ".join(text.split())
        if len(ids) == 1 and text == segs[ids[0]].text:
            stats.proposed -= 1
            continue
        verdict = check(old, text, vocab)
        if not verdict.ok:
            stats.rejected += 1
            stats.reasons[verdict.reason] = stats.reasons.get(verdict.reason, 0) + 1
            continue
        used.update(ids)
        out.append(Op(list(ids), text, verdict.words_changed))
    return out


def _merge_problem(
    ids: list[int], segs: list[Segment], allowed: set[int], context: set[int], used: set[int]
) -> str:
    if any(i < 0 or i >= len(segs) for i in ids):
        return "id"
    if ids != list(range(ids[0], ids[0] + len(ids))):
        return "union"
    if len(ids) == 1:
        return ("repetido" if ids[0] in used else "") if ids[0] in allowed else "id"
    if len(ids) > MAX_MERGE or not any(i in allowed for i in ids):
        return "union"
    if any(i not in allowed and i not in context for i in ids):
        return "id"
    if ids[-1] in context:  # el contexto solo se une con el primero de la ventana
        return "union"
    first = segs[ids[0]]
    if any(segs[i].speaker != first.speaker for i in ids):
        return "hablante"
    for x, y in itertools.pairwise(ids):
        if segs[y].start - segs[x].end > MERGE_GAP_S:
            return "pausa"
    if sum(len(segs[i].text) for i in ids) > MAX_MERGED_CHARS:
        return "union"
    return "repetido" if used & set(ids) else ""


def resolve(ops: list[Op]) -> list[Op]:
    """Una sola operacion por parrafo: gana la union (cubre mas) y, a igualdad, la primera."""
    ordered = sorted(ops, key=lambda o: (-len(o.ids), o.ids[0]))
    taken: set[int] = set()
    out: list[Op] = []
    for op in ordered:
        if taken & set(op.ids):
            continue  # la union ya trae la correccion de sus parrafos
        taken.update(op.ids)
        out.append(op)
    return sorted(out, key=lambda o: o.ids[0])


def apply_ops(transcript: Transcript, ops: list[Op]) -> tuple[Transcript, list[int]]:
    """Transcripcion con las operaciones aplicadas y los indices nuevos con palabras cambiadas."""
    by_first = {op.ids[0]: op for op in ops}
    skip: set[int] = set()
    segments: list[Segment] = []
    changed: list[int] = []
    for i, seg in enumerate(transcript.segments):
        if i in skip:
            continue
        op = by_first.get(i)
        if op is None:
            segments.append(seg)
            continue
        group = [transcript.segments[k] for k in op.ids]
        skip.update(op.ids)
        original = " ".join((g.text_original or g.text) for g in group)
        words = [w for g in group for w in g.words]
        merged: list[int] = []
        if len(group) > 1:
            for k, g in zip(op.ids, group, strict=True):
                merged += g.merged_from or [k]
        else:
            merged = list(seg.merged_from)
        new = Segment(
            start=group[0].start,
            end=max(g.end for g in group),
            speaker=seg.speaker,
            text=op.text,
            words=remap_words(words, op.text),
            text_original=original,
            merged_from=merged,
        )
        if op.words_changed:
            changed.append(len(segments))
        segments.append(new)
    return dataclasses.replace(transcript, segments=segments), changed


def read_marker(folder: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((folder / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def default_workers() -> int:
    """Procesos ``claude -p`` simultaneos que aguanta este equipo: hilos logicos y RAM libre
    (cada uno ~300 MB). En un portatil modesto baja de 24 a ~6-8; tarda algo mas, no se ahoga."""
    import os

    cpus = os.cpu_count() or 4
    try:
        import psutil

        free_mb = int(psutil.virtual_memory().available / (1024 * 1024))
    except Exception:
        free_mb = 4096
    by_mem = max(2, (free_mb - 1024) // CLAUDE_PROC_MB)
    return max(2, min(WORKERS, cpus, by_mem))


class Cleaner:
    """``Cleaner(config, runner, with_db=...).run(folder)``."""

    def __init__(
        self,
        config: Any,
        runner: Any,
        *,
        with_db: WithDb | None = None,
        workers: int = WORKERS,
        deadline_s: float = DEADLINE_S,
        window: int = WINDOW,
    ) -> None:
        self.config = config
        self.runner = runner
        self.with_db = with_db
        self.workers = workers
        self.deadline_s = deadline_s
        self.window = window

    def run(
        self, folder: Path, *, on_status: StatusCallback | None = None, force: bool = False
    ) -> Stats | None:
        """Limpia la reunion una vez. ``None`` si ya estaba hecha o no hay nada que limpiar."""
        from micnaga.pipeline import output

        if not force and read_marker(folder) is not None:
            return None
        transcript = output.read_transcript(folder)
        if not transcript.segments:
            return None
        status = on_status or (lambda _s: None)
        started = time.monotonic()
        vocab = vocabulary(self.config, folder, transcript, with_db=self.with_db)
        keys = vocab_keys(vocab)
        wins = windows(transcript.segments, self.window)
        stats = Stats(segments=len(transcript.segments), windows=len(wins))
        lock = threading.Lock()
        abort = threading.Event()
        ops: list[Op] = []
        done = {"n": 0, "ok": 0}
        status({"fase": "corrigiendo", "hechos": 0, "total": len(wins)})

        def work(win: Window) -> None:
            remaining = self.deadline_s - (time.monotonic() - started)
            if abort.is_set() or remaining < 5:
                with lock:
                    stats.failed += 1
                return
            from micnaga.brain.claude import ClaudeError, ClaudeRequest
            from micnaga.brain.prompt import system_prompt

            req = ClaudeRequest(
                prompt=window_input(transcript, win, vocab),
                system_prompt=system_prompt("limpieza"),
                model=MODEL,
                schema=SCHEMA,
                max_turns=3,
                timeout_s=min(BATCH_TIMEOUT_S, remaining),
            )
            try:
                t0 = time.monotonic()
                res = self.runner.run_once(req)
                took = round(time.monotonic() - t0, 1)
            except ClaudeError as exc:
                if exc.kind in FATAL:
                    abort.set()
                log.info("cleanup window failed: %s", exc.kind)
                with lock:
                    stats.failed += 1
                return
            except Exception:
                log.exception("cleanup window crashed")
                with lock:
                    stats.failed += 1
                return
            with lock:
                stats.window_s.append(took)
                stats.cost_usd += float(getattr(res, "cost_usd", 0.0) or 0.0)
                ops.extend(validate_ops(res.structured, transcript, win, keys, stats))
                done["n"] += 1
                done["ok"] += 1
            status({"fase": "corrigiendo", "hechos": done["n"], "total": len(wins)})

        with ThreadPoolExecutor(
            max_workers=max(1, min(self.workers, default_workers(), len(wins)))
        ) as pool:
            list(pool.map(work, wins))
        stats.seconds = round(time.monotonic() - started, 1)
        stats.cost_usd = round(stats.cost_usd, 4)
        if done["ok"] == 0:
            stats.skipped = "sin_claude" if abort.is_set() else "fallo"
            log.info("transcript cleanup skipped (%s)", stats.skipped)
            return stats
        self._save(folder, transcript, resolve(ops), stats)
        return stats

    def _save(self, folder: Path, before: Transcript, ops: list[Op], stats: Stats) -> None:
        from micnaga.pipeline import output

        with output.transcript_lock(folder):  # ni un renombrado ni la mejora se pisan
            current = output.read_transcript(folder)  # la biblioteca pudo cambiar algo mientras
            same = len(current.segments) == len(before.segments)
            fresh = [
                op
                for op in ops
                if same
                and all(
                    (
                        current.segments[i].start,
                        current.segments[i].speaker,
                        current.segments[i].text,
                    )
                    == (
                        before.segments[i].start,
                        before.segments[i].speaker,
                        before.segments[i].text,
                    )
                    for i in op.ids
                )
            ]
            updated, changed = apply_ops(current, fresh)
            stats.corrected = sum(len(op.ids) for op in fresh)
            stats.merges = sum(1 for op in fresh if len(op.ids) > 1)
            if fresh:
                output.write_transcript(folder, updated)
                self._reindex(folder, updated, changed)
        if stats.failed:
            # alguna ventana no llego (limite de uso, tiempo): sin marca, la proxima pasada la
            # completa (lo ya corregido conserva su ``text_original`` de la primera vez)
            log.info("transcript cleanup incomplete: %d windows failed", stats.failed)
            return
        marker = {
            "version": VERSION,
            "modelo": MODEL,
            "fecha": time.strftime("%Y-%m-%dT%H:%M:%S"),
            **stats.to_dict(),
        }
        tmp = folder / (MARKER + ".tmp")
        tmp.write_text(json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(folder / MARKER)

    def _reindex(self, folder: Path, transcript: Transcript, changed: list[int]) -> None:
        from micnaga.brain import store as bstore
        from micnaga.pipeline import output

        rows = [
            (
                transcript.segments[i].start,
                transcript.segments[i].speaker,
                transcript.segments[i].text_original or "",
            )
            for i in changed
        ]

        def index(db: Any) -> None:
            output.index_meeting(db, folder, transcript)
            bstore.index_originals(db, folder.name, rows)

        try:
            if self.with_db is not None:
                self.with_db(index)
            else:
                output._reindex(folder, transcript, None)
        except Exception:
            log.exception("could not reindex the cleaned transcript")
