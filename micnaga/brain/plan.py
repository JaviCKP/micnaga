"""De la salida del modelo al acta validada: evidencias, correcciones y plan de escritura.

Nada de lo que devuelve el modelo se usa sin pasar por aqui:

* Evidencias por indice de frase (``seg``) -> ``t`` en segundos (local). Fechas ISO validas.
* Correcciones: el hablante debe existir (y no ser «Yo»); la palabra debe aparecer tal cual en
  la transcripcion y no tocar cifras.
* Propuestas de Odoo: cada tipo tiene sus campos; ids que no existen se descartan.
* **Que sale marcado lo decide Micnaga, no el modelo** (design §4.2): por tipo y solo si la
  comprobacion propia (releer el registro, repetir la busqueda de duplicados) coincide con lo
  que dice la propuesta. La ``confianza`` del modelo solo ordena.
"""

from __future__ import annotations

import logging
import re
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


def build_crm(raw: object, transcript: Transcript, odoo: Any | None) -> list[dict[str, Any]]:
    """Valida el plan del modelo. Con ``odoo`` (cliente de lectura) revalida cada registro y
    decide que sale marcado; sin el, nada sale marcado."""
    n = len(transcript.segments)
    blob = _transcript_blob(transcript)
    out: list[dict[str, Any]] = []
    stages: list[dict[str, Any]] | None = None

    def stages_cache() -> list[dict[str, Any]]:
        nonlocal stages
        if stages is None:
            stages = odoo.stages() if odoo is not None else []
        return stages

    for it in sc.as_list(raw, 15):
        if not isinstance(it, dict) or it.get("tipo") not in sc.PROPOSAL_TYPES:
            continue
        d = it.get("datos") if isinstance(it.get("datos"), dict) else {}
        assert isinstance(d, dict)
        seg = sc.seg_ref(it.get("seg"), n)
        p: dict[str, Any] = {
            "grupo": "crm",
            "tipo": it["tipo"],
            "titulo": sc.clean_text(it.get("titulo"), 160),
            "motivo": sc.clean_text(it.get("motivo"), 240),
            "confianza": sc.confidence(it.get("confianza")),
            "seg": seg,
            "t": seg_time(transcript, seg),
            "checked": False,
            "aviso": None,
        }
        try:
            ok = _validate_crm(p, d, blob, odoo, stages_cache)
        except Exception as exc:  # Odoo caido a mitad: se ensena sin marcar
            log.warning("could not verify a proposal: %s", getattr(exc, "kind", type(exc)))
            p["aviso"] = "sin_comprobar"
            ok = p.get("datos") is not None
        if ok and p["titulo"]:
            out.append(p)

    order = {t: i for i, t in enumerate(sc.PROPOSAL_TYPES)}
    out.sort(key=lambda p: (order[p["tipo"]], -p["confianza"]))
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
