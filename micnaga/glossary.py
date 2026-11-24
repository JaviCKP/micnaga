"""Glosario de transcripcion (MIC-16, spec secciones 3 y 8.10).

Lista de nombres propios y siglas que el modelo suele escribir mal. Se aplica como correccion
posterior a la transcripcion (``Glossary.apply_words`` / ``apply_text``) y, si el motor ASR lo
admite, como palabras clave (``Glossary.hotwords`` y ``Glossary.prompt``).

Fichero ``glossary.txt`` en la carpeta de datos de la app (``paths.app_data_dir()``), una entrada
por linea::

    # comentario
    Laura
    Kubernetes: cubernetes, kubernetis
    SQL: sequel, ese cu ele
    San Francisco

La forma canonica va a la izquierda de ``:`` y las variantes (formas que el ASR suele producir,
coincidencia exacta tras normalizar) a la derecha, separadas por comas. Ademas se aceptan las
entradas de ``config.processing.glossary`` (misma sintaxis, una cadena por entrada).
En la transcripcion se añaden términos de Bitnaga detrás de los personales. Sus correcciones
exigen coincidencia exacta o una variante conocida.

Coincidencia
------------
Se normaliza (minusculas, sin acentos, solo letras y cifras) y se compara cada ventana de 1 a
``n+1`` palabras consecutivas con cada forma. La puntuacion y los signos pegados a la palabra
(``Laura,``) se conservan. Para no «corregir» palabras corrientes el umbral depende de la
longitud de la forma canonica normalizada:

* <= 4 caracteres: solo coincidencia exacta (siglas y nombres cortos: ``sql`` -> ``SQL``);
* 5-7: ratio >= 92 y misma primera letra (``Pedro`` no captura ``pero``: 89);
* 8-9: ratio >= 86;
* >= 10: ratio >= 82.

Las coincidencias aproximadas exigen ademas que las longitudes normalizadas difieran menos de un
20 % (``francisco`` no se convierte en ``San Francisco``).

Las variantes solo casan de forma exacta. Una ventana no cruza signos de fin de frase ni comas.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from rapidfuzz import fuzz, process

from micnaga import paths
from micnaga.types import Word

if TYPE_CHECKING:
    from micnaga.config import Config

log = logging.getLogger(__name__)

GLOSSARY_FILENAME = "glossary.txt"
MIN_LENGTH_RATIO = 0.8
PROMPT_PREFIX = "Glosario:"
PROMPT_MAX_CHARS = 600

_TOKEN_RE = re.compile(r"^([^\w]*)(.*?)([^\w]*)$", re.UNICODE | re.DOTALL)
_BREAKING = set(",.;:!?…¿¡")


def glossary_path() -> Path:
    return paths.app_data_dir() / GLOSSARY_FILENAME


def normalize(text: str) -> str:
    """Minusculas, sin acentos, solo caracteres alfanumericos."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if c.isalnum())


@dataclass(frozen=True)
class GlossaryEntry:
    canonical: str
    variants: tuple[str, ...] = ()
    mode: Literal["fuzzy", "exact", "hint"] = "fuzzy"


_BITNAGA_GLOSSARY = (
    GlossaryEntry("Bitnaga", ("vitnaga",), mode="exact"),
    GlossaryEntry("PQC", ("pe cu ce",), mode="exact"),
    GlossaryEntry("CrowdStrike", mode="exact"),
    GlossaryEntry("ciberseguridad", mode="hint"),
    GlossaryEntry("Diputación", mode="hint"),
    GlossaryEntry("postcuántica", mode="hint"),
    GlossaryEntry("Día Q", mode="exact"),
    GlossaryEntry("Shodan", mode="exact"),
    GlossaryEntry("Censys", ("censis",), mode="exact"),
    GlossaryEntry("Dunning-Kruger", ("inkruger", "linkruger"), mode="exact"),
    GlossaryEntry("Odoo", mode="exact"),
    GlossaryEntry("TLS", ("te ele ese",), mode="exact"),
    GlossaryEntry("SSH", ("ese ese hache",), mode="exact"),
    GlossaryEntry("VPN", ("uve pe ene",), mode="exact"),
    GlossaryEntry("SaaS", mode="hint"),
)


def parse_entries(lines: Iterable[str]) -> list[GlossaryEntry]:
    entries: list[GlossaryEntry] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        head, sep, tail = line.partition(":")
        canonical = head.strip()
        if not canonical:
            continue
        variants = tuple(v.strip() for v in tail.split(",") if v.strip()) if sep else ()
        entries.append(GlossaryEntry(canonical, variants))
    return entries


def _split_token(token: str) -> tuple[str, str, str]:
    m = _TOKEN_RE.match(token)
    assert m is not None  # el patron acepta cualquier cadena
    return m.group(1), m.group(2), m.group(3)


@dataclass(frozen=True)
class _Form:
    entry: GlossaryEntry
    norm: str
    n_tokens: int
    threshold: float
    """100 = solo exacta."""
    needs_first_letter: bool


def _threshold(norm_len: int) -> tuple[float, bool]:
    if norm_len <= 4:
        return 100.0, False
    if norm_len <= 7:
        return 92.0, True
    if norm_len <= 9:
        return 86.0, False
    return 82.0, False


@dataclass(frozen=True)
class _Match:
    start: int
    end: int
    """Exclusivo."""
    entry: GlossaryEntry
    score: float


class Glossary:
    def __init__(self, entries: Iterable[GlossaryEntry | str] = ()) -> None:
        self.entries: list[GlossaryEntry] = []
        for e in entries:
            if isinstance(e, str):
                self.entries.extend(parse_entries([e]))
            else:
                self.entries.append(e)
        self._exact: dict[str, GlossaryEntry] = {}
        self._fuzzy_forms: list[_Form] = []
        self._fuzzy_choices: list[str] = []
        self._sizes: set[int] = set()
        for entry in self.entries:
            if entry.mode == "hint":
                continue
            canon_norm = normalize(entry.canonical)
            if not canon_norm:
                continue
            k = len(entry.canonical.split())
            threshold, first = _threshold(len(canon_norm))
            self._exact.setdefault(canon_norm, entry)
            self._sizes.update({max(1, k - 1), k, k + 1})
            if entry.mode == "fuzzy" and threshold < 100.0:
                self._fuzzy_forms.append(_Form(entry, canon_norm, k, threshold, first))
                self._fuzzy_choices.append(canon_norm)
            for variant in entry.variants:
                v_norm = normalize(variant)
                if v_norm:
                    self._exact.setdefault(v_norm, entry)
                    vk = len(variant.split())
                    self._sizes.update({vk, max(1, vk - 1), vk + 1})

    # ------------------------------------------------------------------ carga

    @classmethod
    def load(cls, path: Path | None = None, config: Config | None = None) -> Glossary:
        """Glosario personal y, en transcripcion, el vocabulario habitual de Bitnaga."""
        entries: list[GlossaryEntry] = []
        file = path or glossary_path()
        try:
            if file.is_file():
                entries.extend(parse_entries(file.read_text(encoding="utf-8-sig").splitlines()))
        except (OSError, UnicodeError) as exc:
            if config is None:
                raise
            log.warning("cannot read glossary %s: %s", file, exc)
        if config is not None:
            entries.extend(parse_entries(config.processing.glossary))
            personal = {
                normalize(form) for entry in entries for form in (entry.canonical, *entry.variants)
            }
            entries.extend(e for e in _BITNAGA_GLOSSARY if normalize(e.canonical) not in personal)
        return cls(entries)

    def __len__(self) -> int:
        return len(self.entries)

    def hotwords(self) -> list[str]:
        """Formas canonicas, para motores ASR que admiten palabras clave."""
        seen: dict[str, None] = {}
        for e in self.entries:
            seen.setdefault(e.canonical, None)
        return list(seen)

    def prompt(self, max_chars: int = PROMPT_MAX_CHARS) -> str:
        """Texto para las ``hotwords`` de Whisper: ``"Glosario: A, B, C."`` ("" si vacio).

        Solo formas canonicas (las variantes son lo que el ASR oye mal: no se le sugieren), en
        orden y sin pasar de ``max_chars`` (Whisper admite unos 220 tokens de prompt)."""
        terms: list[str] = []
        size = len(PROMPT_PREFIX) + 2  # un espacio y el punto final
        for term in self.hotwords():
            extra = len(term) + (2 if terms else 0)
            if size + extra > max_chars:
                continue
            terms.append(term)
            size += extra
        return f"{PROMPT_PREFIX} {', '.join(terms)}." if terms else ""

    # ------------------------------------------------------------------ coincidencia

    def _find(self, tokens: Sequence[str]) -> list[_Match]:
        """Coincidencias sin solapes entre ventanas de ``tokens`` (cadenas con su puntuacion)."""
        if not self._sizes or not tokens:
            return []
        parts = [_split_token(t) for t in tokens]
        norms = [normalize(p[1]) for p in parts]
        candidates: list[_Match] = []
        max_size = max(self._sizes)
        for i in range(len(tokens)):
            if not norms[i]:
                continue
            joined = ""
            for size in range(1, max_size + 1):
                j = i + size
                if j > len(tokens):
                    break
                last = j - 1
                if size > 1:
                    # no cruzar comas / fin de frase dentro de la ventana
                    prev_trail = parts[last - 1][2]
                    if any(ch in _BREAKING for ch in prev_trail):
                        break
                if not norms[last]:
                    break
                joined += norms[last]
                if size not in self._sizes:
                    continue
                match = self._match_window(joined)
                if match is not None:
                    entry, score = match
                    candidates.append(_Match(i, j, entry, score))
        candidates.sort(
            key=lambda m: (-m.score, m.entry.mode != "fuzzy", -(m.end - m.start), m.start)
        )
        taken = [False] * len(tokens)
        chosen: list[_Match] = []
        for m in candidates:
            if any(taken[m.start : m.end]):
                continue
            for k in range(m.start, m.end):
                taken[k] = True
            chosen.append(m)
        chosen.sort(key=lambda m: m.start)
        return chosen

    def _match_window(self, joined: str) -> tuple[GlossaryEntry, float] | None:
        exact = self._exact.get(joined)
        if exact is not None:
            return exact, 100.0
        if not self._fuzzy_forms:
            return None
        results = process.extract(
            joined, self._fuzzy_choices, scorer=fuzz.ratio, limit=3, score_cutoff=82.0
        )
        for _choice, score, idx in results:
            form = self._fuzzy_forms[idx]
            if score < form.threshold:
                continue
            if form.needs_first_letter and joined[:1] != form.norm[:1]:
                continue
            if (
                min(len(joined), len(form.norm)) / max(len(joined), len(form.norm))
                < MIN_LENGTH_RATIO
            ):
                continue  # evita que una parte de un nombre compuesto sustituya a la palabra suelta
            return form.entry, float(score)
        return None

    # ------------------------------------------------------------------ aplicacion

    def apply_text(self, text: str) -> str:
        """Aplica el glosario a un texto libre (conserva espacios y puntuacion)."""
        pieces = re.split(r"(\s+)", text)
        words = [p for p in pieces if p and not p.isspace()]
        matches = self._find(words)
        if not matches:
            return text
        by_start = {m.start: m for m in matches}
        out: list[str] = []
        wi = -1
        skip_until = -1
        pending_space = ""
        for piece in pieces:
            if not piece:
                continue
            if piece.isspace():
                pending_space = piece
                continue
            wi += 1
            if wi < skip_until:
                pending_space = ""
                continue
            if wi in by_start:
                m = by_start[wi]
                lead = _split_token(words[m.start])[0]
                trail = _split_token(words[m.end - 1])[2]
                out.append(pending_space + lead + m.entry.canonical + trail)
                skip_until = m.end
            else:
                out.append(pending_space + piece)
            pending_space = ""
        return "".join(out)

    def apply_words(self, words: Sequence[Word]) -> list[Word]:
        """Aplica el glosario a una lista de palabras (conserva tiempos y puntuacion)."""
        matches = self._find([w.w for w in words])
        if not matches:
            return list(words)
        by_start = {m.start: m for m in matches}
        out: list[Word] = []
        i = 0
        while i < len(words):
            m = by_start.get(i)
            if m is None:
                out.append(words[i])
                i += 1
                continue
            window = list(words[m.start : m.end])
            canon_tokens = m.entry.canonical.split()
            if len(canon_tokens) == len(window):
                for w, token in zip(window, canon_tokens, strict=True):
                    lead, _core, trail = _split_token(w.w)
                    out.append(Word(lead + token + trail, w.s, w.e, w.p))
            else:
                lead = _split_token(window[0].w)[0]
                trail = _split_token(window[-1].w)[2]
                p = sum(w.p for w in window) / len(window)
                out.append(Word(lead + m.entry.canonical + trail, window[0].s, window[-1].e, p))
            i = m.end
        return out
