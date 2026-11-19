"""Contexto de la Secretaria y automejora (design §5.7).

Contexto que se carga en cada pasada (en este orden):

1. ``micnaga/brain/context/*.md`` (skill base del paquete; p. ej. ``bitnaga.md`` generado al
   adaptar la Secretaria a un Odoo concreto, ver ``docs/ONBOARDING.md`` §6.2);
2. ``<datos>/secretaria/context/*.md`` (lo mismo, fuera del programa: sobrevive a las
   actualizaciones);
3. ``<datos>/secretaria/learned.md``: lo que la Secretaria aprende sola.

``learned.md`` es una lista de lineas ``- [AAAA-MM-DD · fuente] hecho``. Cada pasada puede traer
``aprendizajes``; Micnaga los valida **en local** antes de escribir nada:

* fuera correos, telefonos, importes concretos, secretos (claves, tokens, contrasenas) y textos
  demasiado cortos/largos;
* fuera duplicados (similitud >= 88);
* si contradice una linea existente o la skill base (muy parecida pero con la negacion
  cambiada), se anade marcada ``a revisar`` en vez de aplicarse.

Cada cambio deja antes una copia con fecha en ``<datos>/secretaria/history/``. Al pasar de
``CONSOLIDATE_LINES`` lineas, ``consolidate`` pide a Sonnet una version compacta (que vuelve a
pasar por la misma validacion).
"""

from __future__ import annotations

import logging
import re
import shutil
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from micnaga import paths

log = logging.getLogger(__name__)

LEARNED_FILE = "learned.md"
HEADER = (
    "# Lo que ha aprendido la Secretaria\n\n"
    "<!-- Lo escribe Micnaga tras validar cada aprendizaje. Una linea por hecho:\n"
    "     - [AAAA-MM-DD · fuente] hecho        (fuente: crm | reunion | aprobacion)\n"
    "     «a revisar» = contradice algo anterior; no se aplica hasta revisarlo. -->\n\n"
)
CONSOLIDATE_LINES = 80
MIN_LEN, MAX_LEN = 12, 240
DUPLICATE_RATIO = 88
CONTRADICTION_RATIO = 70

_LINE_RE = re.compile(r"^- \[(\d{4}-\d{2}-\d{2}) · ([a-záéíóúñ]+)(?: · (a revisar))?\] (.+)$")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?:\+?\d[\s.-]?){7,}")
_AMOUNT = re.compile(
    r"(\d[\d.,]*\s*(?:€|eur\b|euros?\b|k€|\$|usd\b))|((?:€|\$)\s*\d)", re.IGNORECASE
)
_SECRET = re.compile(
    r"(sk-[a-z0-9]|api[_ -]?key|token|contrase(ñ|n)a|password|passwd|secret|"
    r"[A-Za-z0-9_\-]{32,})",
    re.IGNORECASE,
)
_NEGATIONS = ("no ", "nunca ", "ningun", "sin ", "jamas ")


def secretaria_dir() -> Path:
    return paths.app_data_dir() / "secretaria"


def learned_path() -> Path:
    return secretaria_dir() / LEARNED_FILE


def history_dir() -> Path:
    return secretaria_dir() / "history"


def package_context_dir() -> Path:
    return Path(__file__).resolve().parent / "context"


def user_context_dir() -> Path:
    return secretaria_dir() / "context"


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", text.casefold())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9ñ ]+", " ", t).split())


# ============================================================================== contexto


def _read_md(folder: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if not folder.is_dir():
        return out
    for f in sorted(folder.glob("*.md")):
        if f.name == LEARNED_FILE:
            continue
        try:
            out.append((f.name, f.read_text(encoding="utf-8").strip()))
        except OSError:
            continue
    return out


def base_context() -> str:
    parts = [text for _n, text in _read_md(package_context_dir()) + _read_md(user_context_dir())]
    return "\n\n".join(p for p in parts if p)


def load_context() -> str:
    """Skill base + negocio + ``learned.md`` (solo lo aplicado; lo «a revisar» no entra)."""
    parts = [base_context()]
    learned = [i.fact for i in list_learned() if not i.review]
    if learned:
        parts.append(
            "## Aprendido en reuniones anteriores\n" + "\n".join(f"- {x}" for x in learned)
        )
    return "\n\n".join(p for p in parts if p)


# ============================================================================== learned.md


@dataclass(frozen=True)
class Learned:
    day: str
    source: str
    fact: str
    review: bool = False

    def line(self) -> str:
        tag = f"{self.day} · {self.source}" + (" · a revisar" if self.review else "")
        return f"- [{tag}] {self.fact}"

    def to_dict(self) -> dict[str, Any]:
        return {"day": self.day, "source": self.source, "fact": self.fact, "review": self.review}


def list_learned(path: Path | None = None) -> list[Learned]:
    file = path or learned_path()
    if not file.is_file():
        return []
    out: list[Learned] = []
    for raw in file.read_text(encoding="utf-8").splitlines():
        m = _LINE_RE.match(raw.strip())
        if m:
            out.append(Learned(m.group(1), m.group(2), m.group(4).strip(), bool(m.group(3))))
    return out


def _snapshot(file: Path) -> None:
    if not file.is_file():
        return
    hist = history_dir()
    hist.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    shutil.copy2(file, hist / f"learned-{stamp}.md")


def _write(items: Iterable[Learned], path: Path | None = None) -> None:
    file = path or learned_path()
    file.parent.mkdir(parents=True, exist_ok=True)
    _snapshot(file)
    body = HEADER + "".join(i.line() + "\n" for i in items)
    tmp = file.with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8", newline="\n")
    tmp.replace(file)


def ensure_learned() -> Path:
    file = learned_path()
    if not file.is_file():
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(HEADER, encoding="utf-8", newline="\n")
    return file


def reject_reason(fact: str) -> str | None:
    """Por que un hecho no puede guardarse (``None`` si vale)."""
    text = " ".join(fact.split())
    if not MIN_LEN <= len(text) <= MAX_LEN:
        return "length"
    if _EMAIL.search(text):
        return "email"
    if _PHONE.search(text):
        return "phone"
    if _AMOUNT.search(text):
        return "amount"
    if _SECRET.search(text):
        return "secret"
    if "\n" in fact or text.startswith(("#", "<", "-")):
        return "format"
    return None


def _negated(text: str) -> bool:
    t = " " + _norm(text) + " "
    return any(" " + n in t for n in _NEGATIONS)


def _compare(fact: str, others: Iterable[str]) -> str | None:
    """``duplicate`` | ``contradiction`` | ``None`` frente a ``others``."""
    from rapidfuzz import fuzz

    a = _norm(fact)
    for other in others:
        b = _norm(other)
        if not b:
            continue
        score = fuzz.token_sort_ratio(a, b)
        if score >= DUPLICATE_RATIO and _negated(fact) == _negated(other):
            return "duplicate"
        if score >= CONTRADICTION_RATIO and _negated(fact) != _negated(other):
            return "contradiction"
    return None


@dataclass(frozen=True)
class LearnOutcome:
    added: list[Learned]
    rejected: list[tuple[str, str]]
    """(hecho, motivo)."""


def add_learnings(
    raw: Iterable[dict[str, Any]],
    *,
    today: str | None = None,
    min_confidence: float = 0.6,
    path: Path | None = None,
) -> LearnOutcome:
    """Valida y anade ``aprendizajes`` del modelo. Nunca lanza por un aprendizaje malo."""
    from micnaga.brain.schema import LEARN_SOURCES, clean_text, confidence

    day = today or datetime.now().date().isoformat()
    current = list_learned(path)
    base_lines = [ln.strip("-* ").strip() for ln in base_context().splitlines() if ln.strip()]
    added: list[Learned] = []
    rejected: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        fact = clean_text(item.get("hecho"), MAX_LEN + 1)
        source = str(item.get("fuente", "reunion"))
        if source not in LEARN_SOURCES:
            source = "reunion"
        reason = reject_reason(fact)
        if reason is None and confidence(item.get("confianza")) < min_confidence:
            reason = "low_confidence"
        if reason is not None:
            rejected.append((fact, reason))
            continue
        existing = [i.fact for i in [*current, *added]]
        verdict = _compare(fact, existing)
        if verdict == "duplicate":
            rejected.append((fact, "duplicate"))
            continue
        review = verdict == "contradiction" or _compare(fact, base_lines) == "contradiction"
        added.append(Learned(day, source, fact, review))
    if added:
        _write([*current, *added], path)
    for fact, why in rejected:
        log.info("learning rejected (%s): %d chars", why, len(fact))  # nunca el contenido
    return LearnOutcome(added, rejected)


def forget(index: int, path: Path | None = None) -> bool:
    items = list_learned(path)
    if not 0 <= index < len(items):
        return False
    del items[index]
    _write(items, path)
    return True


def history_versions() -> list[str]:
    hist = history_dir()
    if not hist.is_dir():
        return []
    return sorted((f.name for f in hist.glob("learned-*.md")), reverse=True)


def restore(version: str) -> bool:
    """«Volver a como estaba»: restaura una copia de ``history/`` (guardando antes la actual)."""
    if not re.fullmatch(r"learned-[0-9-]+\.md", version):
        return False
    src = history_dir() / version
    if not src.is_file():
        return False
    items = list_learned(src)
    _write(items)
    return True


def needs_consolidation(path: Path | None = None) -> bool:
    return len(list_learned(path)) > CONSOLIDATE_LINES


CONSOLIDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"hechos": {"type": "array", "items": {"type": "string"}, "maxItems": 60}},
    "required": ["hechos"],
    "additionalProperties": False,
}


def consolidate(ask: Callable[[str], dict[str, Any] | None], path: Path | None = None) -> int:
    """Reescribe ``learned.md`` compacto. ``ask(texto) -> {"hechos": [...]}`` (Sonnet).

    Lo devuelto pasa la misma validacion; lo «a revisar» se conserva tal cual. Devuelve el
    numero de lineas resultante (o el actual si no se pudo consolidar).
    """
    items = list_learned(path)
    applied = [i for i in items if not i.review]
    if len(applied) <= CONSOLIDATE_LINES // 2:
        return len(items)
    text = "\n".join(f"- {i.fact}" for i in applied)
    try:
        out = ask(text)
    except Exception:
        log.exception("consolidation call failed")
        return len(items)
    facts = out.get("hechos") if isinstance(out, dict) else None
    if not isinstance(facts, list) or not facts:
        return len(items)
    day = datetime.now().date().isoformat()
    fresh: list[Learned] = []
    for f in facts:
        if (
            isinstance(f, str)
            and reject_reason(f) is None
            and _compare(f, [x.fact for x in fresh]) != "duplicate"
        ):
            fresh.append(Learned(day, "reunion", " ".join(f.split())))
    if not fresh:
        return len(items)
    result = fresh + [i for i in items if i.review]
    _write(result, path)
    return len(result)
