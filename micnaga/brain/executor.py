"""Ejecuta las propuestas aprobadas (codigo determinista, nunca el modelo) y las deshace.

* Correcciones: ``hablante`` -> ``pipeline.output.rename_speaker`` (regenera MD/SRT y
  reindexa); ``palabra`` -> sustitucion exacta en ``transcripcion.json`` (texto y palabras) +
  regenerado + reindexado + variante al glosario (``Canonico: variante``) para que no vuelva a
  pasar. Si el texto ya no aparece (editado a mano), la correccion se descarta en silencio.
* Odoo: ``crear_contacto`` (``res.partner``), ``vincular_a_oportunidad`` / ``nota_en_historial``
  (nota interna con el acta: ``message_post`` subtipo nota), ``actualizar_oportunidad``
  (``write`` de esos campos tras **releer** el registro: si cambio en Odoo desde la propuesta, se
  avisa en vez de pisar) y ``programar_actividad`` (``mail.activity`` para el usuario).
* Negocio: ``nota_en_historial`` tambien sobre tareas/proyectos; ``crear_tarea``
  (``project.task`` asignada al usuario), ``programar_actividad`` sobre cualquier registro
  permitido, ``crear_evento`` (``calendar.event`` con contexto ``no_mail_to_attendees``: no se
  envia ninguna invitacion; los invitados externos solo si el usuario los activa),
  ``registrar_horas`` (``account.analytic.line``, revalidada con ``horas.HoursBook`` justo antes)
  y ``crear_gasto`` (``hr.expense`` en borrador). Todo con «Deshacer» (``unlink`` de lo creado).
* Cada propuesta guarda en ``acta.json`` su estado y lo necesario para deshacer (id creado o
  valores anteriores). Reaplicar no duplica: lo ya ``aplicada`` se salta.
* ``edit``: cambios del usuario en una propuesta pendiente (horas, proyecto y descripcion de una
  linea de horas; nombre y fecha de una tarea; invitados de un evento), validados en Python.
"""

from __future__ import annotations

import dataclasses
import html
import logging
import re
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from micnaga.brain import acta as acta_io
from micnaga.pipeline import output
from micnaga.types import Transcript, Word

log = logging.getLogger(__name__)


class ApplyError(RuntimeError):
    pass


# ============================================================================== texto


def replace_text(transcript: Transcript, frm: str, to: str) -> tuple[Transcript, int]:
    """Sustituye ``frm`` por ``to`` (palabra completa) en el texto y en las palabras."""
    pat = re.compile(r"(?<!\w)" + re.escape(frm) + r"(?!\w)")
    total = 0
    segments = []
    for seg in transcript.segments:
        new_text, n = pat.subn(to, seg.text)
        if not n:
            segments.append(seg)
            continue
        total += n
        words = list(seg.words)
        if words and " ".join(w.w for w in words) == seg.text:
            words = _replace_words(words, frm, to)
        segments.append(dataclasses.replace(seg, text=new_text, words=words))
    return dataclasses.replace(transcript, segments=segments), total


def _replace_words(words: list[Word], frm: str, to: str) -> list[Word]:
    target = frm.split()
    repl = to.split()
    out: list[Word] = []
    i = 0
    while i < len(words):
        window = [w.w for w in words[i : i + len(target)]]
        core = [re.sub(r"^\W+|\W+$", "", x) for x in window]
        if len(window) == len(target) and core == target:
            first, last = words[i], words[i + len(target) - 1]
            lead = re.match(r"^\W*", first.w)
            tail = re.search(r"\W*$", last.w)
            text = (
                (lead.group(0) if lead else "") + " ".join(repl) + (tail.group(0) if tail else "")
            )
            out.append(Word(text, first.s, last.e, 1.0))
            i += len(target)
        else:
            out.append(words[i])
            i += 1
    return out


def add_to_glossary(canonical: str, variant: str) -> None:
    from micnaga.glossary import glossary_path, normalize, parse_entries

    path = glossary_path()
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    entries = parse_entries(lines)
    for i, e in enumerate(entries):
        if normalize(e.canonical) == normalize(canonical):
            if any(normalize(v) == normalize(variant) for v in e.variants):
                return
            # reescribe esa linea con la variante nueva
            for j, raw in enumerate(lines):
                head = raw.split(":", 1)[0].strip()
                if normalize(head) == normalize(canonical) and not raw.strip().startswith("#"):
                    variants = [*entries[i].variants, variant]
                    lines[j] = f"{entries[i].canonical}: {', '.join(variants)}"
                    break
            break
    else:
        lines.append(f"{canonical}: {variant}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ============================================================================== ejecutor


OdooGetter = Callable[[], Any]


class Executor:
    def __init__(
        self,
        folder: Path,
        *,
        odoo: OdooGetter | None = None,
        db: Any = None,
        today: date | None = None,
    ) -> None:
        self.folder = folder
        self._odoo_getter = odoo
        self._odoo: Any = None
        self._db = db
        self.today = today or date.today()
        self._book: Any = None

    def odoo(self) -> Any:
        if self._odoo is None:
            if self._odoo_getter is None:
                raise ApplyError("odoo_unavailable")
            self._odoo = self._odoo_getter()
            if self._odoo is None:
                raise ApplyError("odoo_unavailable")
        return self._odoo

    # ------------------------------------------------------------------ aplicar

    def apply(self, ids: list[str]) -> dict[str, Any]:
        acta = acta_io.read_acta(self.folder)
        if acta is None:
            raise ApplyError("no_acta")
        wanted = set(ids)
        for p in acta.get("propuestas") or []:
            if p.get("id") not in wanted or p.get("estado") == "aplicada":
                continue
            try:
                undo = self._apply_one(p, acta)
            except Exception as exc:
                p["estado"] = "fallida"
                p["error"] = _error_key(exc)
                log.warning("proposal %s (%s) failed: %s", p.get("id"), p.get("tipo"), p["error"])
            else:
                p["estado"] = "aplicada" if undo is not None else "descartada"
                p["deshacer"] = undo
                p["error"] = None
                if p.get("tipo") == "hablante":
                    self._relabel(acta)  # la nota de Odoo que venga despues ya lleva el nombre
            acta_io.write_acta(self.folder, acta)  # cada linea queda registrada al momento
        return acta

    def _relabel(self, acta: dict[str, Any]) -> None:
        from micnaga.brain.plan import relabel

        relabel(acta, output.read_transcript(self.folder))

    def _apply_one(self, p: dict[str, Any], acta: dict[str, Any]) -> dict[str, Any] | None:
        kind, d = p.get("tipo"), p.get("datos") or {}
        if p.get("grupo") == "correcciones":
            if kind == "hablante":
                transcript = output.read_transcript(self.folder)
                current = next((s.name for s in transcript.speakers if s.id == d["hablante"]), None)
                if current is None:
                    return None
                output.rename_speaker(self.folder, d["hablante"], d["a"], db=self._db)
                return {"hablante": d["hablante"], "nombre": current}
            if kind == "palabra":
                transcript = output.read_transcript(self.folder)
                updated, n = replace_text(transcript, d["de"], d["a"])
                if n == 0:
                    return None  # el texto ya no esta: se descarta en silencio
                output.write_transcript(self.folder, updated)
                output._reindex(self.folder, updated, self._db)
                add_to_glossary(d["a"], d["de"])
                return {"de": d["de"], "a": d["a"], "n": n}
            return None
        o = self.odoo()
        if kind == "crear_contacto":
            dup = [
                r
                for r in o.search_partners(d["nombre"], companies=False)
                if (r.get("parecido") or 0) >= 0.97
            ]
            if dup:
                raise ApplyError("duplicate")
            values: dict[str, Any] = {"name": d["nombre"], "is_company": False}
            for src, dst in (("email", "email"), ("telefono", "phone"), ("cargo", "function")):
                if d.get(src):
                    values[dst] = d[src]
            if d.get("empresa_id"):
                values["parent_id"] = d["empresa_id"]
            new_id = o.create("res.partner", values)
            return {"modelo": "res.partner", "creado": new_id}
        if kind in ("vincular_a_oportunidad", "nota_en_historial"):
            msg_id = o.message_post(d["modelo"], d["id"], acta_io.render_note_html(acta))
            return {"modelo": "mail.message", "creado": msg_id, "registro": [d["modelo"], d["id"]]}
        if kind == "actualizar_oportunidad":
            lead = o.read_lead(d["id"])
            if lead is None:
                raise ApplyError("gone")
            before = d.get("anterior") or {}
            stage = lead.get("etapa") or {}
            if before and (
                stage.get("id") != before.get("etapa_id")
                or lead.get("importe") != before.get("importe")
            ):
                raise ApplyError("changed_in_odoo")
            values = {}
            prev: dict[str, Any] = {}
            if d.get("etapa_id") is not None:
                values["stage_id"] = d["etapa_id"]
                prev["stage_id"] = stage.get("id")
            if d.get("importe") is not None:
                values["expected_revenue"] = d["importe"]
                prev["expected_revenue"] = lead.get("importe")
            if d.get("fecha_cierre"):
                values["date_deadline"] = d["fecha_cierre"]
                prev["date_deadline"] = lead.get("fecha_cierre") or False
            o.write("crm.lead", [d["id"]], values)
            return {"modelo": "crm.lead", "id": d["id"], "antes": prev}
        if kind == "programar_actividad":
            values = {
                "res_id": d["id"],
                "summary": d["resumen"],
                "date_deadline": d["fecha"],
                "user_id": o.uid,
            }
            model_id = o.model_id(d["modelo"])
            if model_id is not None:
                values["res_model_id"] = model_id
            else:  # sin acceso a ir.model (sin verificar en Odoo 19)
                values["res_model"] = d["modelo"]
            new_id = o.create("mail.activity", values)
            return {"modelo": "mail.activity", "creado": new_id}
        if kind == "crear_tarea":
            values = {
                "name": d["nombre"],
                "project_id": d["proyecto_id"],
                "user_ids": [(6, 0, [o.uid])],
            }
            if d.get("fecha"):
                values["date_deadline"] = f"{d['fecha']} 12:00:00"  # datetime en Odoo 19
            if d.get("descripcion"):
                values["description"] = f"<p>{html.escape(str(d['descripcion']))}</p>"
            new_id = o.create("project.task", values)
            return {"modelo": "project.task", "creado": new_id}
        if kind == "crear_evento":
            return self._create_event(o, d)
        if kind == "registrar_horas":
            from micnaga.brain import horas

            book = self._hours_book(o)
            line = book.validate(d, explicit=bool(d.get("explicito")))
            if book.exists(line):
                raise ApplyError("duplicate")
            new_id = o.create(horas.TIMESHEET_MODEL, horas.create_values(line))
            book.accept(line)
            return {"modelo": horas.TIMESHEET_MODEL, "creado": new_id}
        if kind == "crear_gasto":
            values = {
                "name": d["nombre"],
                "date": d["fecha"],
                "total_amount_currency": d["importe"],
                "payment_mode": "own_account",
            }
            new_id = o.create("hr.expense", values)  # borrador; sin verificar en un 19 real
            return {"modelo": "hr.expense", "creado": new_id}
        return None

    def _hours_book(self, o: Any) -> Any:
        from micnaga.brain.horas import HoursBook

        if self._book is None:
            self._book = HoursBook(o, self.today)
        return self._book

    def _create_event(self, o: Any, d: dict[str, Any]) -> dict[str, Any]:
        """Evento propio; los invitados solo si el usuario los activo y **sin correo**."""
        from micnaga.brain.odoo import add_hours, local_tz, to_utc

        me = o.user_info()
        partners = [me["partner_id"]] if me.get("partner_id") else []
        partners += [int(i["id"]) for i in d.get("invitados") or [] if i.get("on")]
        values: dict[str, Any] = {
            "name": d["nombre"],
            "user_id": o.uid,
            "partner_ids": [(6, 0, partners)],
        }
        if d.get("hora_inicio"):
            tz = local_tz(me.get("tz"))
            start = f"{d['fecha']} {d['hora_inicio']}"
            values["start"] = to_utc(start, tz)
            values["stop"] = to_utc(add_hours(start, float(d.get("duracion_h") or 1.0)), tz)
        else:
            values.update(
                allday=True, start=f"{d['fecha']} 08:00:00", stop=f"{d['fecha']} 18:00:00"
            )
        new_id = o.create("calendar.event", values, context={"no_mail_to_attendees": True})
        return {"modelo": "calendar.event", "creado": new_id}

    # ------------------------------------------------------------------ editar

    def edit(self, pid: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Cambios del usuario en una propuesta pendiente (validados aqui, nunca en JS)."""
        acta = acta_io.read_acta(self.folder)
        if acta is None:
            raise ApplyError("no_acta")
        p = next((x for x in acta.get("propuestas") or [] if x.get("id") == pid), None)
        if p is None or p.get("estado") != "pendiente":
            raise ApplyError("not_editable")
        d = dict(p.get("datos") or {})
        kind = p.get("tipo")
        if kind == "registrar_horas":
            from micnaga.brain.horas import HoursBook

            raw = {**d}
            for key in ("horas", "proyecto_id", "descripcion", "tarea_id", "fecha"):
                if key in changes:
                    raw[key] = changes[key]
            if "proyecto_id" in changes and "tarea_id" not in changes:
                raw["tarea_id"] = None
            book = HoursBook(self.odoo(), self.today)
            # el total del dia se cuenta sin esta misma linea (ya_en_el_dia = lo de Odoo)
            line = book.validate(raw, explicit=bool(d.get("explicito")))
            p["datos"] = {**d, **line}
            p["checked"] = True
        elif kind == "crear_tarea":
            if isinstance(changes.get("nombre"), str) and changes["nombre"].strip():
                d["nombre"] = " ".join(changes["nombre"].split())[:160]
            if "fecha" in changes:
                from micnaga.brain.schema import iso_date

                d["fecha"] = iso_date(changes["fecha"])
            p["datos"] = d
        elif kind == "crear_evento":
            on = {int(i) for i in changes.get("invitados_on") or [] if isinstance(i, int)}
            d["invitados"] = [{**i, "on": int(i["id"]) in on} for i in d.get("invitados") or []]
            p["datos"] = d
        else:
            raise ApplyError("not_editable")
        acta_io.write_acta(self.folder, acta)
        return acta

    # ------------------------------------------------------------------ deshacer

    def undo(self, ids: list[str]) -> dict[str, Any]:
        acta = acta_io.read_acta(self.folder)
        if acta is None:
            raise ApplyError("no_acta")
        wanted = set(ids)
        for p in reversed(acta.get("propuestas") or []):
            if p.get("id") not in wanted or p.get("estado") != "aplicada":
                continue
            try:
                self._undo_one(p)
            except Exception as exc:
                p["error"] = _error_key(exc)
                log.warning("undo of %s failed: %s", p.get("id"), p["error"])
            else:
                p["estado"] = "deshecha"
                p["error"] = None
                if p.get("tipo") == "hablante":
                    self._relabel(acta)
            acta_io.write_acta(self.folder, acta)
        return acta

    def _undo_one(self, p: dict[str, Any]) -> None:
        u = p.get("deshacer") or {}
        kind = p.get("tipo")
        if p.get("grupo") == "correcciones":
            if kind == "hablante":
                output.rename_speaker(self.folder, u["hablante"], u["nombre"], db=self._db)
            elif kind == "palabra":
                transcript = output.read_transcript(self.folder)
                updated, n = replace_text(transcript, u["a"], u["de"])
                if n:
                    output.write_transcript(self.folder, updated)
                    output._reindex(self.folder, updated, self._db)
            return
        o = self.odoo()
        if "creado" in u and u.get("creado"):
            o.unlink(str(u["modelo"]), [int(u["creado"])])
        elif kind == "actualizar_oportunidad":
            o.write("crm.lead", [int(u["id"])], dict(u.get("antes") or {}))


def _error_key(exc: Exception) -> str:
    if isinstance(exc, ApplyError):
        return str(exc)
    kind = getattr(exc, "kind", None)
    if isinstance(kind, str):
        return f"odoo_{kind}"
    return "failed"
