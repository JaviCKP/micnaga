"""De la salida del modelo al acta validada: evidencias, correcciones y plan de escritura.

Nada de lo que devuelve el modelo se usa sin pasar por aqui:

* Evidencias por indice de frase (``seg``) -> ``t`` en segundos (local). Fechas ISO validas.
* Correcciones: el hablante debe existir (y no ser «Yo»); la palabra debe aparecer tal cual en
  la transcripcion y no tocar cifras.
* Propuestas de Odoo: cada tipo tiene sus campos; ids que no existen se descartan. Solo se
  aceptan los tipos que permite el perfil de capacidades del usuario y, para notas y
  actividades, solo sobre registros donde puede escribir (``capabilities.note_targets``).
* Horas (``registrar_horas``): una propuesta del modelo con varias ``fechas`` se expande en una
  linea por dia; cada linea pasa la validacion de ``timesheets`` contra Odoo (proyecto abierto,
  fecha no futura, 0,25-12 h, <= 10 h/dia). Solo sale marcada una peticion explicita de un dia
  («apuntame 2 horas a X hoy») citada en la reunion.
* **Que sale marcado lo decide Micnaga, no el modelo** (design §4.2): por tipo y solo si la
  comprobacion propia (releer el registro, repetir la busqueda de duplicados) coincide con lo
  que dice la propuesta. La ``confianza`` del modelo solo ordena.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from micnaga.brain import schema as sc
from micnaga.types import SPEAKER_ME, Transcript

log = logging.getLogger(__name__)

_DEFAULT_SPEAKER = re.compile(r"^(Hablante \d+|S\d+)$")
_EMAIL = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")
_DIGIT = re.compile(r"\d")
SIMILAR_PARTNER = 0.9


def seg_time(transcript: Transcript, seg: int | None) -> float | None:
    if seg is None:
        return None
    return round(transcript.segments[seg].start, 2)


def _items(raw: object, transcript: Transcript) -> list[dict[str, Any]]:
    n = len(transcript.segments)
    out = []
    for it in sc.as_list(raw):
        if isinstance(it, str):
            it = {"texto": it, "seg": None}
        if not isinstance(it, dict):
            continue
        text = sc.clean_text(it.get("texto"))
        if not text:
            continue
        seg = sc.seg_ref(it.get("seg"), n)
        out.append({"texto": text, "seg": seg, "t": seg_time(transcript, seg)})
    return out


def speaker_name(transcript: Transcript, sid: str | None) -> str | None:
    if sid is None:
        return None
    return next((s.name for s in transcript.speakers if s.id == sid), None)


def build_acta(data: dict[str, Any], transcript: Transcript) -> dict[str, Any]:
    """Partes del acta (sin propuestas) validadas."""
    n = len(transcript.segments)
    tareas = []
    for it in sc.as_list(data.get("tareas")):
        if not isinstance(it, dict):
            continue
        text = sc.clean_text(it.get("texto"))
        if not text:
            continue
        resp = it.get("responsable")
        resp = sc.clean_text(resp, 60) if isinstance(resp, str) else None
        sid = resp if resp and speaker_name(transcript, resp) is not None else None
        seg = sc.seg_ref(it.get("seg"), n)
        tareas.append(
            {
                "texto": text,
                "responsable_id": sid,
                "responsable": speaker_name(transcript, sid) if sid else resp,
                "es_mia": sid == SPEAKER_ME,
                "fecha": sc.iso_date(it.get("fecha")),
                "seg": seg,
                "t": seg_time(transcript, seg),
            }
        )
    resumen = [sc.clean_text(x, 500) for x in sc.as_list(data.get("resumen"), sc.MAX_SUMMARY)]
    return {
        "titulo": sc.clean_title(data.get("titulo")),
        "resumen": [x for x in resumen if x],
        "decisiones": _items(data.get("decisiones"), transcript),
        "tareas": tareas,
        "preguntas_abiertas": _items(data.get("preguntas_abiertas"), transcript),
        "proximos_pasos": _items(data.get("proximos_pasos"), transcript),
    }


def relabel(acta: dict[str, Any], transcript: Transcript) -> dict[str, Any]:
    """Pone los nombres ACTUALES de los hablantes en las tareas (``responsable_id``): tras
    renombrar a «Hablante 2» como «Marta Gil», el acta, ``acta.md`` y la nota de Odoo dicen
    «Marta Gil»."""
    for t in acta.get("tareas") or []:
        sid = t.get("responsable_id")
        if sid:
            name = speaker_name(transcript, sid)
            if name:
                t["responsable"] = name
    return acta


# ============================================================================== correcciones


def count_occurrences(transcript: Transcript, text: str) -> int:
    pat = re.compile(r"(?<!\w)" + re.escape(text) + r"(?!\w)")
    return sum(len(pat.findall(s.text)) for s in transcript.segments)


def build_corrections(raw: object, transcript: Transcript) -> list[dict[str, Any]]:
    n = len(transcript.segments)
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for it in sc.as_list(raw, 40):
        if not isinstance(it, dict):
            continue
        kind = it.get("tipo")
        to = sc.clean_text(it.get("a"), 60)
        seg = sc.seg_ref(it.get("seg"), n)
        base = {
            "grupo": "correcciones",
            "motivo": sc.clean_text(it.get("motivo"), 240),
            "confianza": sc.confidence(it.get("confianza")),
            "seg": seg,
            "t": seg_time(transcript, seg),
        }
        if kind == "hablante":
            sid = it.get("hablante")
            current = speaker_name(transcript, sid) if isinstance(sid, str) else None
            if current is None or sid == SPEAKER_ME or not to or current == to:
                continue
            key = ("hablante", str(sid))
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    **base,
                    "tipo": "hablante",
                    "datos": {"hablante": sid, "de": current, "a": to},
                    "checked": bool(_DEFAULT_SPEAKER.match(current)),
                }
            )
        elif kind == "palabra":
            frm = sc.clean_text(it.get("de"), 60)
            if not frm or not to or frm == to or _DIGIT.search(frm) or _DIGIT.search(to):
                continue
            key = ("palabra", frm)
            if key in seen:
                continue
            seen.add(key)
            hits = count_occurrences(transcript, frm)
            if hits == 0:
                continue
            out.append(
                {
                    **base,
                    "tipo": "palabra",
                    "datos": {"de": frm, "a": to, "n": hits},
                    "checked": True,
                }
            )
    out.sort(key=lambda p: (p["tipo"] != "hablante", -p["confianza"]))
    return out


# ============================================================================== propuestas CRM


def _int(v: object) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def _num(v: object) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _transcript_blob(transcript: Transcript) -> str:
    return " ".join(s.text for s in transcript.segments).casefold().replace(" ", "")


MAX_RAW = 30
MAX_HOUR_LINES = 80
TASK_SIMILAR = 85


def build_crm(
    raw: object,
    transcript: Transcript,
    odoo: Any | None,
    *,
    profile: dict[str, Any] | None = None,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Valida el plan del modelo. Con ``odoo`` (cliente de lectura) revalida cada registro y
    decide que sale marcado; sin el, nada sale marcado. ``profile``: perfil de capacidades
    (sin el, solo los tipos de CRM de siempre)."""
    from micnaga.brain import capabilities

    n = len(transcript.segments)
    blob = _transcript_blob(transcript)
    out: list[dict[str, Any]] = []
    stages: list[dict[str, Any]] | None = None
    allowed = set(capabilities.actions_for(profile, sc.PROPOSAL_TYPES))
    targets = (
        capabilities.note_targets(profile) if profile is not None else ["crm.lead", "res.partner"]
    )
    ctx = _Ctx(odoo, today or date.today(), targets, blob)

    def stages_cache() -> list[dict[str, Any]]:
        nonlocal stages
        if stages is None:
            stages = odoo.stages() if odoo is not None else []
        return stages

    for it in sc.as_list(raw, MAX_RAW):
        if not isinstance(it, dict) or it.get("tipo") not in allowed:
            continue
        d = it.get("datos") if isinstance(it.get("datos"), dict) else {}
        assert isinstance(d, dict)
        seg = sc.seg_ref(it.get("seg"), n)
        p: dict[str, Any] = {
            "grupo": sc.PROPOSAL_GROUP.get(str(it["tipo"]), "crm"),
            "tipo": it["tipo"],
            "titulo": sc.clean_text(it.get("titulo"), 160),
            "motivo": sc.clean_text(it.get("motivo"), 240),
            "confianza": sc.confidence(it.get("confianza")),
            "seg": seg,
            "t": seg_time(transcript, seg),
            "checked": False,
            "aviso": None,
        }
        if p["tipo"] == "registrar_horas":
            hours = sum(1 for x in out if x["tipo"] == "registrar_horas")
            if hours < MAX_HOUR_LINES:
                out += _expand_hours(p, d, ctx)[: MAX_HOUR_LINES - hours]
            continue
        try:
            if p["tipo"] in _BUSINESS or _business_target(d):
                ok = _validate_business(p, d, ctx)
            else:
                ok = _validate_crm(p, d, blob, odoo, stages_cache)
                datos = p.get("datos") or {}
                if ok and p["tipo"] in ("nota_en_historial", "programar_actividad"):
                    ok = datos.get("modelo") in targets
        except Exception as exc:  # Odoo caido a mitad: se ensena sin marcar
            log.warning("could not verify a proposal: %s", getattr(exc, "kind", type(exc)))
            p["aviso"] = "sin_comprobar"
            p["checked"] = False
            ok = p.get("datos") is not None
        if ok and p["titulo"]:
            if p["tipo"] == "nota_en_historial" and (p["datos"] or {}).get("modelo") in (
                "res.partner",
                "crm.lead",
            ):
                p["grupo"] = "crm"
            out.append(p)

    groups = {g: i for i, g in enumerate(("horas", "agenda", "crm"))}
    order = {t: i for i, t in enumerate(sc.PROPOSAL_TYPES)}
    out.sort(
        key=lambda p: (
            groups.get(p["grupo"], 9),
            order[p["tipo"]],
            str((p.get("datos") or {}).get("fecha") or ""),
            -p["confianza"],
        )
    )
    return out


# ============================================================================== negocio

_BUSINESS = ("crear_tarea", "crear_evento", "crear_gasto")
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class _Ctx:
    def __init__(self, odoo: Any | None, today: date, targets: list[str], blob: str) -> None:
        self.odoo = odoo
        self.today = today
        self.targets = targets
        self.blob = blob
        self._book: Any = None

    def book(self) -> Any:
        from micnaga.brain.horas import HoursBook

        if self._book is None:
            self._book = HoursBook(self.odoo, self.today)
        return self._book


def _business_target(d: dict[str, Any]) -> bool:
    return bool(_int(d.get("tarea_id")) or _int(d.get("proyecto_id")))


def _record(ctx: _Ctx, model: str, rid: int) -> dict[str, Any] | None:
    """El registro (con ``nombre``) o ``None`` si no existe / no es legible."""
    o: Any = ctx.odoo
    if model == "project.task":
        return o.read_task(rid)  # type: ignore[no-any-return]
    if model == "project.project":
        return o.read_project(rid)  # type: ignore[no-any-return]
    if model == "crm.lead":
        return o.read_lead(rid)  # type: ignore[no-any-return]
    rows = o.read(model, [rid], ["id", "name"])
    return {"id": rid, "nombre": rows[0].get("name")} if rows else None


def _resolve_target(d: dict[str, Any], ctx: _Ctx) -> tuple[str, int] | None:
    """Registro de la nota/actividad: tarea > oportunidad > proyecto > contacto, entre los que el
    usuario puede escribir (si el contacto es de solo lectura, se usa la tarea o el proyecto)."""
    order = (
        ("project.task", "tarea_id"),
        ("crm.lead", "oportunidad_id"),
        ("project.project", "proyecto_id"),
        ("res.partner", "partner_id"),
    )
    for model, key in order:
        rid = _int(d.get(key))
        if rid and model in ctx.targets:
            return model, rid
    return None


def _validate_business(p: dict[str, Any], d: dict[str, Any], ctx: _Ctx) -> bool:
    kind = p["tipo"]
    if kind in ("nota_en_historial", "programar_actividad"):
        target = _resolve_target(d, ctx)
        if target is None:
            return False
        datos: dict[str, Any] = {"modelo": target[0], "id": target[1]}
        if kind == "programar_actividad":
            fecha = sc.iso_date(d.get("fecha"))
            resumen = sc.clean_text(d.get("resumen"), 160)
            if not fecha or not resumen:
                return False
            datos.update(fecha=fecha, resumen=resumen)
        p["datos"] = datos
        if ctx.odoo is None:
            return True
        rec = _record(ctx, target[0], target[1])
        if rec is None:
            return False
        datos["nombre"] = rec.get("nombre")
        if kind == "programar_actividad":
            pending = ctx.odoo.pending_activities(target[1], model=target[0])
            if _similar_activity(datos["resumen"], pending):
                p["aviso"] = "parecida_pendiente"
                return True
        p["checked"] = True
        return True
    if kind == "crear_tarea":
        pid = _int(d.get("proyecto_id"))
        name = sc.clean_text(d.get("nombre") or d.get("resumen"), 160)
        if not pid or not name:
            return False
        datos = {
            "proyecto_id": pid,
            "nombre": name,
            "fecha": sc.iso_date(d.get("fecha")),
            "descripcion": sc.clean_text(d.get("descripcion"), 600) or None,
        }
        p["datos"] = datos
        if ctx.odoo is None:
            return True
        proj = ctx.odoo.read_project(pid)
        if proj is None or not proj.get("activo", True):
            return False
        datos["proyecto"] = proj.get("nombre")
        from rapidfuzz import fuzz

        similar = [
            t
            for t in ctx.odoo.tasks(project_id=pid, mine=False, open_only=True, text=None, limit=80)
            if fuzz.token_set_ratio(name.casefold(), str(t.get("nombre") or "").casefold())
            >= TASK_SIMILAR
        ]
        if similar:
            datos["parecidas"] = [{"id": t["id"], "nombre": t["nombre"]} for t in similar[:3]]
            p["aviso"] = "tarea_parecida"
        else:
            p["checked"] = True
        return True
    if kind == "crear_evento":
        name = sc.clean_text(d.get("nombre") or d.get("resumen"), 160)
        fecha = sc.iso_date(d.get("fecha"))
        if not name or not fecha or date.fromisoformat(fecha) < ctx.today:
            return False
        start = d.get("hora_inicio")
        hhmm = None
        if isinstance(start, str) and _HHMM.match(start.strip()):
            h, m = start.strip().split(":")
            hhmm = f"{int(h):02d}:{m}"
        dur = _num(d.get("duracion_h"))
        dur = 1.0 if dur is None else max(0.25, min(12.0, round(dur * 4) / 4))
        datos = {
            "nombre": name,
            "fecha": fecha,
            "hora_inicio": hhmm,
            "duracion_h": dur,
            "invitados": [],
            "tarea_id": _int(d.get("tarea_id")),
            "proyecto_id": _int(d.get("proyecto_id")),
        }
        p["datos"] = datos
        ids = [i for i in sc.as_list(d.get("asistentes_ids"), 10) if _int(i)]
        if ctx.odoo is None:
            return True
        if ids:
            rows = ctx.odoo.read("res.partner", ids, ["id", "name"])
            # invitados: SIEMPRE apagados; el usuario los activa uno a uno (no se envia correo)
            datos["invitados"] = [
                {"id": r["id"], "nombre": r.get("name"), "on": False} for r in rows
            ]
        clash = [
            e
            for e in ctx.odoo.calendar_events(fecha, fecha)
            if _similar_text(name, str(e.get("nombre") or ""))
        ]
        if clash:
            p["aviso"] = "evento_parecido"
        else:
            p["checked"] = hhmm is not None
        return True
    if kind == "crear_gasto":
        name = sc.clean_text(d.get("nombre") or d.get("descripcion"), 160)
        amount = _num(d.get("importe"))
        fecha = sc.iso_date(d.get("fecha")) or ctx.today.isoformat()
        if not name or amount is None or amount <= 0 or date.fromisoformat(fecha) > ctx.today:
            return False
        p["datos"] = {"nombre": name, "importe": round(amount, 2), "fecha": fecha}
        return True  # nunca marcado: el importe lo revisa el usuario
    return False


def _similar_text(a: str, b: str) -> bool:
    from rapidfuzz import fuzz

    return fuzz.token_set_ratio(a.casefold(), b.casefold()) >= 85


def _expand_hours(p: dict[str, Any], d: dict[str, Any], ctx: _Ctx) -> list[dict[str, Any]]:
    """Una propuesta ``registrar_horas`` del modelo -> una linea validada por dia."""
    from micnaga.brain.timesheets import LineError

    dates = [x for x in sc.as_list(d.get("fechas"), 60) if isinstance(x, str)]
    if not dates and isinstance(d.get("fecha"), str):
        dates = [str(d["fecha"])]
    explicit = d.get("explicito") is True
    out: list[dict[str, Any]] = []
    for day in dict.fromkeys(dates):
        raw = {
            "fecha": day,
            "proyecto_id": _int(d.get("proyecto_id")),
            "tarea_id": _int(d.get("tarea_id")),
            "horas": d.get("horas"),
            "descripcion": sc.clean_text(d.get("descripcion"), 200),
        }
        line = dict(p)
        line["aviso"] = None
        if ctx.odoo is None:
            if sc.iso_date(day) is None or not raw["proyecto_id"]:
                continue
            line["datos"] = {**raw, "explicito": explicit}
            line["checked"] = False
            out.append(line)
            continue
        book = ctx.book()
        try:
            v = book.validate(raw, explicit=explicit)
        except LineError as exc:
            log.info("hours line dropped: %s", exc)
            continue
        except Exception as exc:
            log.warning("could not verify an hours line: %s", getattr(exc, "kind", type(exc)))
            line["datos"] = {**raw, "explicito": explicit}
            line["aviso"] = "sin_comprobar"
            line["checked"] = False
            out.append(line)
            continue
        book.accept(v)
        v["explicito"] = explicit
        v["ya_en_el_dia"] = round(book.day_total(v["fecha"]) - v["horas"], 2)
        line["datos"] = v
        line["checked"] = bool(explicit and len(dates) == 1 and p.get("seg") is not None)
        out.append(line)
    return out


def _validate_crm(
    p: dict[str, Any], d: dict[str, Any], blob: str, odoo: Any | None, stages: Any
) -> bool:
    kind = p["tipo"]
    lead_id, partner_id = _int(d.get("oportunidad_id")), _int(d.get("partner_id"))
    if kind in ("vincular_a_oportunidad", "nota_en_historial", "programar_actividad"):
        target = ("crm.lead", lead_id) if lead_id else ("res.partner", partner_id)
        if target[1] is None or (kind == "vincular_a_oportunidad" and not lead_id):
            return False
        datos: dict[str, Any] = {"modelo": target[0], "id": target[1]}
        if kind == "programar_actividad":
            fecha = sc.iso_date(d.get("fecha"))
            resumen = sc.clean_text(d.get("resumen"), 160)
            if not fecha or not resumen:
                return False
            datos.update(fecha=fecha, resumen=resumen)
        p["datos"] = datos
        if odoo is None:
            return True
        if target[0] == "crm.lead":
            lead = odoo.read_lead(target[1])
            if lead is None:
                return False
            datos["nombre"] = lead.get("nombre")
            if kind == "programar_actividad":
                dup = _similar_activity(datos["resumen"], lead.get("actividades_pendientes") or [])
                if dup:
                    p["aviso"] = "parecida_pendiente"
                    return True
        else:
            rows = odoo.read("res.partner", [target[1]], ["id", "name"])
            if not rows:
                return False
            datos["nombre"] = rows[0].get("name")
        p["checked"] = True
        return True
    if kind == "crear_contacto":
        name = sc.clean_text(d.get("nombre"), 80)
        if not name:
            return False
        email = sc.clean_text(d.get("email"), 120) or None
        if email and (not _EMAIL.match(email) or email.casefold().replace(" ", "") not in blob):
            email = None  # solo si se dicto en la reunion
        phone = sc.clean_text(d.get("telefono"), 40) or None
        if phone and re.sub(r"\D", "", phone) not in re.sub(r"\D", "", blob):
            phone = None
        datos = {
            "nombre": name,
            "email": email,
            "telefono": phone,
            "cargo": sc.clean_text(d.get("cargo"), 80) or None,
            "empresa_id": _int(d.get("empresa_id")),
            "empresa": None,
        }
        p["datos"] = datos
        if odoo is None:
            return True
        if datos["empresa_id"]:
            rows = odoo.read("res.partner", [datos["empresa_id"]], ["id", "name", "is_company"])
            if not rows or not rows[0].get("is_company"):
                datos["empresa_id"] = None
            else:
                datos["empresa"] = rows[0].get("name")
        similar = [
            r
            for r in odoo.search_partners(name, companies=False)
            if (r.get("parecido") or 0) >= SIMILAR_PARTNER
        ]
        datos["parecidos"] = [{"id": r["id"], "nombre": r["nombre"]} for r in similar[:3]]
        if similar:
            p["aviso"] = "parecido_existe"
        else:
            p["checked"] = True
        return True
    if kind == "actualizar_oportunidad":
        if not lead_id:
            return False
        new_stage = _int(d.get("etapa_id"))
        amount = _num(d.get("importe"))
        close = sc.iso_date(d.get("fecha_cierre"))
        if new_stage is None and amount is None and close is None:
            return False
        datos = {"id": lead_id, "etapa_id": new_stage, "importe": amount, "fecha_cierre": close}
        p["datos"] = datos
        if odoo is None:
            return True
        lead = odoo.read_lead(lead_id)
        if lead is None:
            return False
        stage = lead.get("etapa") or {}
        datos["nombre"] = lead.get("nombre")
        datos["anterior"] = {
            "etapa_id": stage.get("id"),
            "etapa": stage.get("nombre"),
            "importe": lead.get("importe"),
            "fecha_cierre": lead.get("fecha_cierre"),
        }
        if new_stage is not None:
            names = {s["id"]: s["nombre"] for s in stages()}
            if new_stage not in names:
                return False
            datos["etapa"] = names[new_stage]
        changes = (
            (new_stage is not None and new_stage != stage.get("id"))
            or (amount is not None and amount != lead.get("importe"))
            or (close is not None and close != lead.get("fecha_cierre"))
        )
        if not changes:
            return False
        claimed_amount = _num(d.get("importe_anterior"))
        claimed_stage = d.get("etapa_anterior")
        mismatch = (claimed_amount is not None and claimed_amount != lead.get("importe")) or (
            isinstance(claimed_stage, str)
            and claimed_stage.strip()
            and claimed_stage.strip().casefold() != str(stage.get("nombre", "")).casefold()
        )
        if mismatch:
            p["aviso"] = "cambio_en_odoo"
        else:
            p["checked"] = True
        return True
    return False


def _similar_activity(summary: str, pending: list[dict[str, Any]]) -> bool:
    from rapidfuzz import fuzz

    return any(
        fuzz.token_set_ratio(summary.casefold(), str(a.get("resumen") or "").casefold()) >= 80
        for a in pending
    )


def number(proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for i, p in enumerate(proposals, 1):
        p["id"] = f"p{i}"
        p.setdefault("estado", "pendiente")
    return proposals
