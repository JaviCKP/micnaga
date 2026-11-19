"""Investigacion antes de escribir el acta: reuniones anteriores, fuentes y «desde la ultima vez».

La Secretaria investiga primero (solo lectura, con topes) y luego escribe. Aqui:

* ``meetings_with`` / ``read_acta``: las herramientas locales ``reuniones_con`` y ``leer_acta``
  del servidor MCP ``micnaga`` (reuniones anteriores por participante o empresa, y el acta
  completa de una con sus acuerdos y pendientes).
* ``related`` + ``brief``: candidatas calculadas en Python antes de lanzar (mismos participantes
  que esta reunion), para que el modelo lea las actas en paralelo en su primer turno.
* ``extend``: anade al pedido la seccion «Investigacion» y los campos ``contexto``,
  ``desde_la_ultima`` y ``fuentes`` del esquema.
* ``extract`` + ``prior_by_terms``: lo que dice la propia conversacion (empresas, nombres dichos
  en voz alta, productos, siglas y temas), para investigar **siempre**, aunque nadie tenga
  nombre y no haya evento de Calendar. El brief lleva las llamadas exactas del primer turno y
  ``missing`` dice que falto (la Secretaria repite una vez si el modelo no investigo).
* ``validate``: nada de fuera de la transcripcion entra en el acta sin una fuente que el modelo
  haya **visto** en una herramienta (o que esté en las candidatas): carpetas que existen, URLs de
  Google que aparecen tal cual en un resultado, registros de Odoo cuyo id se leyo.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from micnaga.brain import schema as sc

MAX_MEETINGS = 8
MAX_RELATED = 3
MAX_SINCE = 10
MAX_SOURCES = 12
MAX_CONTEXT = 200
SOURCE_TYPES = ("acta", "gmail", "drive", "calendar", "odoo")
STATES = ("hecho", "abierto", "desconocido")
ODOO_MODELS = (
    "crm.lead",
    "res.partner",
    "project.task",
    "project.project",
    "calendar.event",
    "mail.activity",
)
URL_HOSTS = {
    "gmail": ("https://mail.google.com/",),
    "drive": ("https://docs.google.com/", "https://drive.google.com/"),
    "calendar": ("https://calendar.google.com/", "https://www.google.com/calendar/"),
}
_GENERIC = re.compile(r"^(yo|me|s\d+|hablante \d+|speaker \d+|participante \d+|desconocido)$")
_FOLDER = re.compile(r"^[\w.\- ]{1,120}$")


def norm(text: str) -> str:
    s = unicodedata.normalize("NFKD", text)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.casefold().split())


def _names(speakers: list[Any]) -> list[str]:
    out = []
    for s in speakers:
        name = str(getattr(s, "name", "") or "").strip()
        if name and not _GENERIC.match(norm(name)) and getattr(s, "id", "") != "me":
            out.append(name)
    return out


def _matches(query: str, name: str) -> bool:
    q, n = norm(query), norm(name)
    if len(q) < 3 or len(n) < 3:
        return False
    return q in n or n in q or bool(set(q.split()) & {w for w in n.split() if len(w) > 3})


# ============================================================================== herramientas


def _acta(root: Path, folder: str) -> dict[str, Any] | None:
    from micnaga.brain import acta as acta_io

    if not _FOLDER.match(folder) or folder in (".", ".."):
        return None
    path = root / folder
    try:
        if path.resolve().parent != root.resolve():
            return None
    except OSError:
        return None
    return acta_io.read_acta(path)


def _open_items(acta: dict[str, Any]) -> int:
    since = [x for x in acta.get("desde_la_ultima") or [] if x.get("estado") != "hecho"]
    return len(acta.get("tareas") or []) + len(acta.get("proximos_pasos") or []) + len(since)


def meetings_with(
    db: Any, root: Path, text: str, *, exclude: str | None = None, limit: int = MAX_MEETINGS
) -> list[dict[str, Any]]:
    """Reuniones anteriores con esa persona o empresa (participantes, titulo o acta)."""
    from micnaga.brain import store

    if len(norm(text)) < 3:
        return []
    store.ensure_schema(db)
    in_actas = {h.folder for h in store.search_actas(db, text, 20)}
    out: list[dict[str, Any]] = []
    for rec in db.list_meetings(500):
        if rec.folder == exclude:
            continue
        people = _names(rec.speakers)
        hit = (
            any(_matches(text, n) for n in people)
            or norm(text) in norm(rec.title)
            or rec.folder in in_actas
        )
        if not hit:
            continue
        acta = _acta(root, rec.folder)
        out.append(
            {
                "carpeta": rec.folder,
                "fecha": rec.start[:10],
                "titulo": (acta or {}).get("titulo") or rec.title,
                "participantes": people[:8],
                "con_acta": acta is not None,
                "pendientes": _open_items(acta) if acta else 0,
            }
        )
    out.sort(key=lambda m: str(m["fecha"]), reverse=True)
    return out[:limit]


def _proposal_line(p: dict[str, Any]) -> dict[str, Any]:
    d = p.get("datos") or {}
    u = p.get("deshacer") or {}
    line: dict[str, Any] = {
        "tipo": p.get("tipo"),
        "que": sc.clean_text(p.get("titulo") or d.get("nombre") or d.get("resumen"), 160),
        "estado": p.get("estado"),
    }
    if u.get("creado") and u.get("modelo"):
        line["odoo"] = f"{u['modelo']}:{u['creado']}"
    res = p.get("resultado") or {}
    if isinstance(res, dict) and res.get("url"):
        line["url"] = res["url"]
    return line


def read_acta(root: Path, folder: str, *, exclude: str | None = None) -> dict[str, Any]:
    """Acta completa de una reunion anterior: acuerdos, tareas, pendientes y lo que se aplico."""
    if folder == exclude:
        return {"error": "es la reunion actual"}
    acta = _acta(root, folder)
    if acta is None:
        return {"error": "no hay acta en esa carpeta"}

    def texts(key: str) -> list[str]:
        return [sc.clean_text(x.get("texto"), 300) for x in acta.get(key) or [] if x.get("texto")]

    return {
        "carpeta": folder,
        "fecha": str(acta.get("created") or "")[:10] or folder[:10],
        "titulo": acta.get("titulo"),
        "contexto": acta.get("contexto"),
        "resumen": acta.get("resumen") or [],
        "decisiones": texts("decisiones"),
        "tareas": [
            {
                "texto": sc.clean_text(t.get("texto"), 300),
                "responsable": t.get("responsable"),
                "es_mia": bool(t.get("es_mia")),
                "fecha": t.get("fecha"),
            }
            for t in acta.get("tareas") or []
        ],
        "proximos_pasos": texts("proximos_pasos"),
        "preguntas_abiertas": texts("preguntas_abiertas"),
        "seguia_abierto": [
            sc.clean_text(x.get("texto"), 300)
            for x in acta.get("desde_la_ultima") or []
            if x.get("estado") != "hecho"
        ],
        "propuestas": [
            _proposal_line(p)
            for p in acta.get("propuestas") or []
            if p.get("grupo") not in ("correcciones",)
        ][:20],
    }


# ============================================================================== candidatas


def related(
    db: Any,
    root: Path,
    people: list[str],
    *,
    exclude: str | None,
    limit: int = MAX_RELATED,
) -> list[dict[str, Any]]:
    """Reuniones anteriores (con acta) que comparten participantes con esta, las mas recientes
    y las que mas comparten primero."""
    score: dict[str, int] = {}
    rows: dict[str, dict[str, Any]] = {}
    for name in dict.fromkeys(p for p in people if p):
        for m in meetings_with(db, root, name, exclude=exclude, limit=20):
            if not m["con_acta"] or not any(_same_person(name, p) for p in m["participantes"]):
                continue  # «Carlos» de otro cliente no cuenta: nombre completo si lo hay
            score[m["carpeta"]] = score.get(m["carpeta"], 0) + 1
            rows[m["carpeta"]] = m
    ranked = sorted(rows.values(), key=lambda m: (score[m["carpeta"]], m["fecha"]), reverse=True)
    return ranked[:limit]


def _same_person(a: str, b: str) -> bool:
    wa, wb = norm(a).split(), norm(b).split()
    if len(wa) > 1 and len(wb) > 1:
        return wa[:2] == wb[:2]  # nombre y primer apellido
    return bool(wa and wb and wa[0] == wb[0])


def people_for(transcript: Any, folder: Path) -> list[str]:
    """Nombres de esta reunion: hablantes con nombre y asistentes del evento de Calendar."""
    names = _names(list(transcript.speakers))
    try:
        ctx = json.loads((folder / "contexto_google.json").read_text(encoding="utf-8"))
        for a in ctx.get("asistentes") or []:  # google.context.to_context
            if not isinstance(a, dict) or a.get("self"):
                continue
            n = a.get("nombre")
            if isinstance(n, str) and n.strip():
                names.append(n.strip())
    except (OSError, ValueError, AttributeError):
        pass
    return list(dict.fromkeys(names))


# ============================================================================== terminos


def _wordset(text: str) -> frozenset[str]:
    return frozenset(text.split())


_VOCATIVE = re.compile(
    r"(?:\b(?i:gracias|hola|oye|mira|perdona|buenas|buenos d[ií]as|adi[oó]s|venga|vale|claro|"
    r"hasta luego|un abrazo|encantad[oa]),?\s+|,\s*)"
    r"([A-ZÁÉÍÓÚÑ][a-záéíóúñ]{2,}(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]{2,})?)\s*(?=[.,?!]|$)"
)
_INTRO = re.compile(
    r"\b(?i:soy|me llamo|te habla|le habla)\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñ]{2,}"
    r"(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]{2,})?)"
)
_NOT_NAMES = _wordset(
    "vale bueno perfecto gracias hola venga claro genial exacto nada si no pues entonces ok "
    "okay vamos mira oye adios hasta luego buenas buenos dias tardes noches senor eso esto yo "
    "tu usted ustedes hombre tio chicos chicas todos correcto efectivamente"
)
_TOPIC_STOP = _wordset(
    "porque entonces tambien ahora bueno cuando donde tenemos tienes hacer puede pueden quiero "
    "queremos vamos estamos sobre hemos habia tiene tengo estan seria mucho mucha muchos otros "
    "otras cosas claro nosotros vosotros ellos decir digamos verdad luego antes despues mismo "
    "misma todos todas estas estos bien parte sabes entiendo igual siempre momento necesitamos "
    "podemos tendriamos habria estaba fueron hacemos ningun alguno alguna tambien realmente "
    "solamente simplemente bastante primero segundo tercero cualquier pregunta respuesta "
    "trabajo manera hablando diciendo haciendo tenido estado"
)


def extract(transcript: Any, *, limit: int = 6) -> dict[str, Any]:
    """Candidatos para investigar sacados de la conversacion (heuristica barata, sin modelo).

    ``personas``: nombres dichos en voz alta («gracias, Pedro», «soy Marta»); ``entidades``:
    empresas, productos y siglas con mayuscula fuera del inicio de frase (y el titulo);
    ``temas``: palabras largas frecuentes; ``pistas``: quien dijo que nombre (para nombrar a
    los hablantes)."""
    from micnaga.brain.cleanup import fold, proper_terms, recurring_terms

    people: dict[str, int] = {}
    hints: list[str] = []
    speakers = [s.id for s in transcript.speakers]
    for i, seg in enumerate(transcript.segments):
        for m in _INTRO.finditer(seg.text):
            name = m.group(1)
            if fold(name) in _NOT_NAMES:
                continue
            people[name] = people.get(name, 0) + 2
            hints.append(f"#{i}: `{seg.speaker}` se presenta como «{name}»")
        for m in _VOCATIVE.finditer(seg.text):
            name = m.group(1)
            if fold(name.split()[0]) in _NOT_NAMES:
                continue
            people[name] = people.get(name, 0) + 1
            others = [sid for sid in speakers if sid != seg.speaker]
            to = f" (solo puede ser `{others[0]}`)" if len(others) == 1 else ""
            hints.append(f"#{i}: `{seg.speaker}` llama «{name}» a alguien{to}")
    named = {fold(n) for n in people}
    entities: dict[str, int] = {}
    for term in recurring_terms(transcript, min_count=1):
        k = fold(term)
        if k not in named and k not in _NOT_NAMES and len(term) >= 2:
            entities[term] = entities.get(term, 0) + 1
    title = str(getattr(transcript.meeting, "title", "") or "")
    for term in proper_terms(title):
        entities[term] = entities.get(term, 0) + 3
    text = " ".join(seg.text for seg in transcript.segments)
    words: dict[str, int] = {}
    for tok in re.findall(r"[a-záéíóúñü]{6,}", text.lower()):
        k = fold(tok)
        if k not in _TOPIC_STOP:
            words[k] = words.get(k, 0) + 1
    rank = sorted(entities, key=lambda k: (-entities[k], -len(k)))
    return {
        "personas": sorted(people, key=lambda k: -people[k])[:limit],
        "entidades": _dedupe(rank)[:limit],
        "temas": [w for w in sorted(words, key=lambda k: -words[k]) if words[w] >= 3][:limit],
        "pistas": list(dict.fromkeys(hints))[:12],
    }


def _dedupe(items: list[str]) -> list[str]:
    from micnaga.brain.cleanup import fold

    out: list[str] = []
    for it in items:
        k = fold(it)
        if any(k in fold(o) or fold(o) in k for o in out):
            continue
        out.append(it)
    return out


def prior_by_terms(
    db: Any, root: Path, terms: list[str], *, exclude: str | None, limit: int = MAX_RELATED
) -> list[dict[str, Any]]:
    """Reuniones anteriores con acta que hablan de lo mismo (empresa, persona o tema en su
    titulo, participantes o acta)."""
    score: dict[str, int] = {}
    rows: dict[str, dict[str, Any]] = {}
    for term in dict.fromkeys(t for t in terms if len(norm(t)) >= 3):
        for m in meetings_with(db, root, term, exclude=exclude, limit=10):
            if not m["con_acta"]:
                continue
            score[m["carpeta"]] = score.get(m["carpeta"], 0) + 1
            rows[m["carpeta"]] = m
    ranked = sorted(rows.values(), key=lambda m: (score[m["carpeta"]], m["fecha"]), reverse=True)
    return ranked[:limit]


def search_terms(terms: dict[str, Any]) -> list[str]:
    return [*(terms.get("entidades") or []), *(terms.get("personas") or [])]


def first_calls(terms: dict[str, Any], *, odoo: bool, gmail: bool, mail: bool = False) -> list[str]:
    """Las llamadas concretas del primer turno, con sus argumentos."""
    people = list(terms.get("personas") or [])
    ents = list(terms.get("entidades") or [])
    topics = list(terms.get("temas") or [])
    who = (ents + people)[:3]
    topic = " ".join(ents[:1] + topics[:2]) or (people[:1] or ["la reunión"])[0]
    calls = [f'`buscar_en_reuniones("{topic}")`']
    calls += [f'`reuniones_con("{w}")`' for w in who[:2]]
    if odoo:
        calls += [f'`buscar_empresas("{e}")`' for e in ents[:2]]
        calls += [f'`buscar_contactos("{p}")`' for p in people[:1]]
        calls.append("`proyectos` (el del cliente o el del tema)")
    if gmail or mail:
        q = " OR ".join(f'"{x}"' for x in who[:3]) or " ".join(topics[:2]) or topic
        calls.append(f"una búsqueda de correo: `{q} newer_than:90d`")
    return calls


def missing(
    result: Any, *, odoo: bool, gmail: bool, mail_servers: list[str] | None = None
) -> list[str]:
    """Que parte de la investigacion obligatoria no se hizo en esta ejecucion.

    Solo cuenta lo que se podia hacer: un servidor que no sale ``connected`` en ``system/init``
    no se exige (repetir no lo arreglaria). El correo vale por Gmail o por cualquier servidor
    extra de ``secretaria.mcp_servers`` (``mail_servers``)."""
    from micnaga.brain import google as g
    from micnaga.brain.mcp import EVERY_ODOO_TOOL, LOCAL_TOOLS

    calls = list(getattr(result, "tool_calls", []) or [])
    status = dict(getattr(result, "mcp_servers", {}) or {})
    extra = [s for s in mail_servers or [] if status.get(s) == "connected"]
    local_up = status.get("micnaga") == "connected"
    gmail_up = gmail and status.get(g.SERVER["gmail"]) == "connected"
    out = []
    if local_up and not any(c.server == "micnaga" and c.tool in LOCAL_TOOLS for c in calls):
        out.append("actas")
    in_odoo = any(c.server == "micnaga" and c.tool in EVERY_ODOO_TOOL for c in calls)
    if odoo and local_up and not in_odoo:
        out.append("odoo")
    in_mail = any(g.service_of(c.name) == "gmail" or c.server in extra for c in calls)
    if (gmail_up or extra) and not in_mail:
        out.append("gmail")
    return out


def nudge(gaps: list[str], calls: list[str]) -> str:
    names = {"actas": "reuniones anteriores", "odoo": "Odoo", "gmail": "Gmail"}
    return (
        "\n## Falta investigar\nNo has consultado: "
        + ", ".join(names[g] for g in gaps)
        + ". Antes de responder, haz ahora estas llamadas a la vez, en un solo turno: "
        + "; ".join(calls)
        + ". Después escribe el acta.\n"
    )


def reminder() -> str:
    """Recordatorio al final del pedido (despues de una transcripcion larga)."""
    return (
        "\n## Recuerda\nAntes de responder, haz el primer turno de «Investigación» (las "
        "consultas a la vez). Después escribe el acta con lo que encuentres.\n"
    )


def name_seen(name: str, transcript: Any, seen_text: str = "", extra: str = "") -> bool:
    """El nombre propuesto para un hablante (cada palabra: nombre y apellidos) se dijo en la
    reunion, salio en una herramienta, en el evento de Calendar o entre los participantes de las
    reuniones candidatas (``extra``): nada inventado."""
    from micnaga.brain.cleanup import fold

    words = [w for w in fold(name).split() if len(w) > 2]
    if not words:
        return False
    hay = fold(" ".join(s.text for s in transcript.segments) + "\n" + seen_text + "\n" + extra)
    return all(re.search(rf"\b{re.escape(w)}\b", hay) is not None for w in words)


def brief(
    candidates: list[dict[str, Any]],
    *,
    odoo: bool,
    google: list[str],
    mail: bool = False,
    terms: dict[str, Any] | None = None,
) -> str:
    terms = terms or {}
    first = ["`leer_acta` de las reuniones anteriores candidatas"] if candidates else []
    first += first_calls(terms, odoo=odoo, gmail="gmail" in google, mail=mail)
    if "drive" in google:
        first.append("en Drive, solo los documentos que se mencionen")
    lines = [
        "## Investigación (antes de escribir, obligatoria)",
        "Primero investiga, luego escribe. Solo lectura. Las llamadas independientes van **a la "
        "vez, en el mismo turno**. Presupuesto: 2-3 turnos de investigación, pocos resultados. "
        "Investiga **siempre**, aunque los hablantes no tengan nombre.",
        "1. Quién y de qué: hablantes, nombres que se dicen, empresa y tema.",
        "2. Primer turno, todo a la vez: " + "; ".join(first) + ".",
    ]
    found = [x for k in ("entidades", "personas", "temas") for x in terms.get(k) or []]
    if found:
        lines.append("Dicho en la reunión (para buscar): " + ", ".join(found) + ".")
    if terms.get("pistas"):
        lines.append(
            "Pistas para nombrar a los hablantes sin nombre (propón `hablante` solo con prueba "
            "clara: la frase, una firma de correo o el evento): " + "; ".join(terms["pistas"]) + "."
        )
    if odoo:
        lines.append(
            "3. Segundo turno: lo que cuelga de lo encontrado: las **tareas abiertas** del "
            "proyecto relacionado, historial, actividades y oportunidad (si puedes)."
        )
    if candidates:
        lines += ["", "Reuniones anteriores candidatas (mismos participantes):"]
        for m in candidates:
            who = ", ".join(m.get("participantes") or []) or "—"
            lines.append(f"- `{m['carpeta']}` · {m['fecha']} · {m['titulo']} · {who}")
    lines += [
        "",
        "Al escribir: `contexto` solo si aporta («2.ª reunión con X; la última el 12/11»). "
        "`desde_la_ultima`: cada pendiente de la reunión anterior (tareas y próximos pasos) con "
        "`estado` hecho/abierto/desconocido, `acta` = su carpeta y la prueba (`seg` de esta "
        "transcripción o `evidencia`). `fuentes`: lo que has leído y usas. Nunca afirmes nada de "
        "fuera de la transcripción sin su fuente.",
    ]
    return "\n".join(lines) + "\n"


# ============================================================================== esquema


_SOURCE = {
    "type": "object",
    "properties": {
        "tipo": {"type": "string", "enum": list(SOURCE_TYPES)},
        "ref": {
            "type": "string",
            "description": "acta: carpeta; gmail/drive/calendar: URL tal cual; odoo: modelo:id",
        },
        "titulo": {"type": "string"},
    },
    "required": ["tipo", "ref", "titulo"],
    "additionalProperties": False,
}


def schema_props() -> dict[str, Any]:
    return {
        "contexto": {
            "type": ["string", "null"],
            "description": "una linea, solo si aporta (2.a reunion con X; la ultima el 12/11)",
        },
        "desde_la_ultima": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "texto": {"type": "string"},
                    "estado": {"type": "string", "enum": list(STATES)},
                    "acta": {"type": "string", "description": "carpeta del acta anterior"},
                    "seg": {"type": ["integer", "null"]},
                    "evidencia": {"anyOf": [_SOURCE, {"type": "null"}]},
                },
                "required": ["texto", "estado", "acta", "seg", "evidencia"],
                "additionalProperties": False,
            },
        },
        "fuentes": {"type": "array", "items": _SOURCE},
    }


def extend(req: Any, text: str) -> None:
    """Seccion «Investigacion» antes de la transcripcion y campos nuevos en el esquema."""
    if req.schema is not None:
        schema = dict(req.schema)
        schema["properties"] = {**schema["properties"], **schema_props()}
        schema["required"] = [*schema["required"], "contexto", "desde_la_ultima", "fuentes"]
        req.schema = schema
    marker = "## Transcripción"
    req.prompt = (
        req.prompt.replace(marker, text + "\n" + marker, 1)
        if marker in req.prompt
        else req.prompt + "\n" + text
    )


# ============================================================================== validacion


class Seen:
    """Lo que el modelo ha visto: texto de los resultados de herramientas y las candidatas."""

    def __init__(
        self,
        tool_texts: list[str],
        *,
        root: Path,
        current: str | None,
        candidates: list[dict[str, Any]],
        odoo_url: str = "",
    ) -> None:
        text = "\n".join(tool_texts)  # JSON de las herramientas: URLs con «\/» o «&»
        self.text = text + "\n" + text.replace("\\/", "/").replace("\\u0026", "&")
        self.root = root
        self.current = current
        self.candidates = {m["carpeta"] for m in candidates}
        self.odoo_url = odoo_url.rstrip("/")

    def acta(self, folder: object) -> bool:
        if not isinstance(folder, str) or folder == self.current:
            return False
        if folder not in self.candidates and folder not in self.text:
            return False
        return _acta(self.root, folder) is not None

    def source(self, s: object) -> dict[str, Any] | None:
        if not isinstance(s, dict) or s.get("tipo") not in SOURCE_TYPES:
            return None
        kind, ref = str(s["tipo"]), str(s.get("ref") or "").strip()
        title = sc.clean_text(s.get("titulo"), 120) or ref[:60]
        if kind == "acta":
            return {"tipo": kind, "ref": ref, "titulo": title} if self.acta(ref) else None
        if kind == "odoo":
            model, _, rid = ref.partition(":")
            if model not in ODOO_MODELS or not rid.isdigit():
                return None
            if not re.search(rf'"id":\s*{int(rid)}\b', self.text):
                return None
            out = {"tipo": kind, "ref": f"{model}:{int(rid)}", "titulo": title}
            if self.odoo_url:
                out["url"] = f"{self.odoo_url}/web#id={int(rid)}&model={model}&view_type=form"
            return out
        if not ref.startswith(URL_HOSTS[kind]) or len(ref) > 2000 or ref not in self.text:
            return None
        return {"tipo": kind, "ref": ref, "titulo": title, "url": ref}


def validate(data: dict[str, Any], transcript: Any, seen: Seen) -> dict[str, Any]:
    """``contexto``, ``desde_la_ultima`` y ``fuentes`` del acta, comprobados contra lo visto."""
    from micnaga.brain.plan import seg_time

    n = len(transcript.segments)
    sources: dict[tuple[str, str], dict[str, Any]] = {}

    def keep(src: dict[str, Any] | None) -> dict[str, Any] | None:
        if src is not None:
            sources.setdefault((src["tipo"], src["ref"]), src)
        return src

    since: list[dict[str, Any]] = []
    for it in sc.as_list(data.get("desde_la_ultima"), MAX_SINCE):
        if not isinstance(it, dict):
            continue
        text = sc.clean_text(it.get("texto"), 300)
        folder = it.get("acta")
        if not text or not seen.acta(folder):
            continue
        keep(seen.source({"tipo": "acta", "ref": folder, "titulo": _title(seen, folder)}))
        evidence = keep(seen.source(it.get("evidencia")))
        seg = sc.seg_ref(it.get("seg"), n)
        state = it.get("estado") if it.get("estado") in STATES else "desconocido"
        if state != "desconocido" and seg is None and evidence is None:
            state = "desconocido"  # sin prueba no se afirma
        since.append(
            {
                "texto": text,
                "estado": state,
                "acta": folder,
                "fecha": str(folder)[:10],
                "seg": seg,
                "t": seg_time(transcript, seg),
                "evidencia": evidence,
            }
        )
    for s in sc.as_list(data.get("fuentes"), MAX_SOURCES * 2):
        keep(seen.source(s))
    fuentes = list(sources.values())[:MAX_SOURCES]
    context = sc.clean_text(data.get("contexto"), MAX_CONTEXT) or None
    if context and not any(f["tipo"] in ("acta", "odoo", "gmail", "calendar") for f in fuentes):
        context = None  # el contexto viene de fuera: sin fuente, fuera
    return {"contexto": context, "desde_la_ultima": since, "fuentes": fuentes}


def _title(seen: Seen, folder: object) -> str:
    acta = _acta(seen.root, str(folder)) or {}
    return str(acta.get("titulo") or folder)


def source_counts(fuentes: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for f in fuentes:
        out[f["tipo"]] = out.get(f["tipo"], 0) + 1
    return out
