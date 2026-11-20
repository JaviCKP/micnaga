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
        google: Callable[[], Any] | None = None,
        today: date | None = None,
    ) -> None:
        self.folder = folder
        self._google_getter = google  # micnaga.brain.google.apply.GoogleApplier
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

    def google(self) -> Any:
        if self._google_getter is None:
            raise ApplyError("google_unavailable")
        return self._google_getter()

    def _apply_one(self, p: dict[str, Any], acta: dict[str, Any]) -> dict[str, Any] | None:
        kind, d = p.get("tipo"), p.get("datos") or {}
        if p.get("grupo") == "google":
            result: dict[str, Any] = self.google().apply(p, acta)
            return result
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
            body = (
                acta_io.text_to_html(str(d["cuerpo"]))
                if d.get("cuerpo")
                else acta_io.render_note_html(acta)
            )
            msg_id = o.message_post(d["modelo"], d["id"], body)
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
            # ``escrito``: lo que pone Micnaga; el «Deshacer» solo restaura si sigue asi
            return {"modelo": "crm.lead", "id": d["id"], "antes": prev, "escrito": values}
        if kind == "programar_actividad":
            values = {
                "res_id": d["id"],
                "summary": d["resumen"],
                "date_deadline": d["fecha"],
                "user_id": o.uid,
            }
            if d.get("tipo_actividad_id"):
                values["activity_type_id"] = d["tipo_actividad_id"]
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
            from micnaga.brain.timesheets import LineError

            book = self._hours_book(o)
            try:
                line = book.validate(d, explicit=bool(d.get("explicito")))
            except LineError as exc:
                raise ApplyError(
                    "duplicate" if str(exc) == "ya_existe" else f"horas_{exc}"
                ) from exc
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
        """Evento propio; los invitados solo si el usuario los activo y **sin correo**
        (``odoo_calendar``, la misma pieza que usa la capa de Google Calendar)."""
        from micnaga.brain import odoo_calendar
        from micnaga.brain.odoo import local_tz

        me = o.user_info()
        partners = [me["partner_id"]] if me.get("partner_id") else []
        partners += [int(i["id"]) for i in d.get("invitados") or [] if i.get("on")]
        values = odoo_calendar.event_values(
            name=d["nombre"],
            day=d["fecha"],
            tz=local_tz(me.get("tz")),
            start=d.get("hora_inicio"),
            duration_h=float(d.get("duracion_h") or 1.0),
            partner_ids=partners,
            user_id=o.uid,
        )
        new_id = odoo_calendar.create_event(o, values)
        return {"modelo": odoo_calendar.MODEL, "creado": new_id}

    # ------------------------------------------------------------------ editar

    def edit(self, pid: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Cambios del usuario en cualquier campo editable de un item pendiente: se revalidan con
        la misma logica que la propuesta original y el item se sustituye (mismo ``id``). Una
        edicion a mano cierra el «Deshacer» de la ultima revision de la IA."""
        acta = self._edit(pid, changes)
        p = next((x for x in acta.get("propuestas") or [] if x.get("id") == pid), None)
        if p is not None and ("previa" in p or "cambiados" in p):
            p.pop("previa", None)
            p.pop("cambiados", None)
            acta_io.write_acta(self.folder, acta)
        return acta

    def _edit(self, pid: str, changes: dict[str, Any]) -> dict[str, Any]:
        from micnaga.brain import plan

        acta = acta_io.read_acta(self.folder)
        if acta is None:
            raise ApplyError("no_acta")
        gp = next((x for x in acta.get("propuestas") or [] if x.get("id") == pid), None)
        if gp is not None and gp.get("grupo") == "google" and gp.get("estado") == "fallida":
            return self._edit_google(acta, gp, changes)  # revisarla es la forma de reintentar
        p = self._pending(acta, pid)
        if p.get("grupo") == "google":
            return self._edit_google(acta, p, changes)
        if p.get("grupo") == "correcciones":
            to = " ".join(str(changes.get("a") or "").split())[:60]
            if not to or re.search(r"\d", to):
                raise ApplyError("invalid")
            p["datos"] = {**(p.get("datos") or {}), "a": to}
            p["checked"] = True
            acta_io.write_acta(self.folder, acta)
            return acta
        invited = changes.get("invitados_on")
        if p.get("tipo") == "crear_evento" and isinstance(invited, list) and len(changes) == 1:
            on = {int(i) for i in invited if isinstance(i, int) and not isinstance(i, bool)}
            d = dict(p.get("datos") or {})
            d["invitados"] = [{**i, "on": int(i["id"]) in on} for i in d.get("invitados") or []]
            p["datos"] = d
            acta_io.write_acta(self.folder, acta)
            return acta
        try:
            raw = plan.apply_changes(plan.to_input(p), changes)
        except ValueError as exc:
            raise ApplyError("invalid") from exc
        return self.replace(pid, raw, acta=acta, user_edit=True)

    def replace(
        self,
        pid: str,
        raw: dict[str, Any],
        *,
        acta: dict[str, Any] | None = None,
        user_edit: bool = False,
    ) -> dict[str, Any]:
        """Sustituye un item pendiente por otra version (forma de salida del modelo), p. ej. la
        que revise la IA a partir de un mensaje del usuario. Conserva ``id``; ``LineError`` /
        ``ApplyError`` si la nueva version no pasa la validacion."""
        from micnaga.brain import capabilities, plan

        acta = acta if acta is not None else acta_io.read_acta(self.folder)
        if acta is None:
            raise ApplyError("no_acta")
        p = self._pending(acta, pid)
        if raw.get("tipo") != p.get("tipo"):
            raise ApplyError("invalid")
        if p.get("grupo") == "google":
            from micnaga.brain.google import items

            try:
                items.replace(acta, pid, raw)
            except items.EditError as exc:
                raise ApplyError("invalid") from exc
            acta_io.write_acta(self.folder, acta)
            return acta
        o = self.odoo()
        try:
            profile = capabilities.ensure(o)
        except Exception:
            profile = None
        transcript = output.read_transcript(self.folder)
        try:
            new = plan.rebuild(
                raw, transcript, o, profile=profile, today=self.today, user_edit=user_edit
            )
        except ValueError as exc:
            from micnaga.brain.timesheets import LineError

            if isinstance(exc, LineError):
                raise
            raise ApplyError("invalid") from exc
        if p.get("tipo") == "crear_evento":  # los invitados que el usuario ya activo siguen
            was_on = {i["id"] for i in (p.get("datos") or {}).get("invitados") or [] if i.get("on")}
            for i in (new.get("datos") or {}).get("invitados") or []:
                i["on"] = i["id"] in was_on
        keep: dict[str, Any] = {k: p[k] for k in ("id", "seg", "t") if k in p}
        revision = int(p.get("revision") or 0) + 1
        p.clear()
        p.update(new)
        p.update(keep, estado="pendiente", deshacer=None, error=None, revision=revision)
        if user_edit:
            p["checked"] = True
        acta_io.write_acta(self.folder, acta)
        return acta

    # ------------------------------------------------------------------ revisar (IA)

    def revise(
        self,
        pid: str,
        changes: dict[str, Any],
        *,
        expect_revision: int | None = None,
        typed: str = "",
    ) -> dict[str, Any]:
        """Una revision de la IA («corrigelo con un mensaje») sobre un item pendiente.

        ``changes``: solo los campos que cambian (ya filtrados por tipo en
        ``micnaga.brain.refine``). Pasa por la misma validacion que una edicion (``replace``),
        conserva ``id`` y la marca (la decide el usuario, no el modelo), sube ``revision`` y guarda
        la version anterior en ``previa`` para un «Deshacer» de un paso. Nunca activa invitados
        ni cambia un borrador por un envio (``plan``/``items``). ``typed``: el mensaje del usuario
        (un correo o telefono que escribio el puede ir en un contacto aunque no se dijera).
        ``ApplyError('stale')`` si el item cambio mientras se pensaba; ``'unchanged'`` si la
        version nueva es igual."""
        from micnaga.brain import plan

        acta = acta_io.read_acta(self.folder)
        if acta is None:
            raise ApplyError("no_acta")
        p = self._pending(acta, pid)
        if expect_revision is not None and int(p.get("revision") or 0) != expect_revision:
            raise ApplyError("stale")
        before = snapshot(p)
        kind = str(p.get("tipo"))
        if p.get("grupo") == "correcciones":
            to = " ".join(str(changes.get("a") or "").split())[:60]
            if not to or re.search(r"\d", to):
                raise ApplyError("invalid")
            p["datos"] = {**(p.get("datos") or {}), "a": to}
        elif p.get("grupo") == "google":
            from micnaga.brain.google import items

            try:
                items.replace(acta, pid, {"tipo": kind, "datos": changes})
            except items.EditError as exc:
                raise ApplyError("invalid") from exc
        else:
            raw = plan.to_input(p)
            datos = dict(raw["datos"])
            if any(k in changes for k in plan.TARGET_KEYS):
                for k in plan.TARGET_KEYS:
                    datos.pop(k, None)
            if kind == "registrar_horas" and "proyecto_id" in changes and "tarea_id" not in changes:
                datos["tarea_id"] = None
            datos.update(changes)
            if kind == "registrar_horas":
                datos["fechas"] = [datos.get("fecha")]
                datos["explicito"] = (p.get("datos") or {}).get("explicito") is True
            typed_contact = any(
                isinstance(changes.get(k), str) and str(changes[k]).strip() in typed
                for k in ("email", "telefono")
                if changes.get(k)
            )
            self.replace(pid, {**raw, "datos": datos}, acta=acta, user_edit=typed_contact)
        p = next(x for x in acta.get("propuestas") or [] if x.get("id") == pid)
        if p.get("datos") == before.get("datos"):
            raise ApplyError("unchanged")  # nada se escribe: la version anterior sigue
        p["checked"] = bool(before.get("checked"))
        p["revision"] = int(before.get("revision") or 0) + 1
        p["previa"] = before
        p["cambiados"] = changed_fields(before, p, acta)
        acta_io.write_acta(self.folder, acta)
        return acta

    def revert(self, pid: str) -> dict[str, Any]:
        """«Deshacer» de una revision: vuelve a la version anterior (un paso). La ``revision``
        sigue subiendo (la clave de idempotencia de Google la incluye)."""
        acta = acta_io.read_acta(self.folder)
        if acta is None:
            raise ApplyError("no_acta")
        p = self._pending(acta, pid)
        prev = p.get("previa")
        if not isinstance(prev, dict):
            raise ApplyError("not_editable")
        restored = dict(prev)
        restored.update(
            id=pid,
            estado="pendiente",
            revision=int(p.get("revision") or 0) + 1,
            cambiados=changed_fields(p, prev, acta),
        )
        props = acta["propuestas"]
        props[props.index(p)] = restored
        acta_io.write_acta(self.folder, acta)
        return acta

    def _edit_google(
        self, acta: dict[str, Any], p: dict[str, Any], changes: dict[str, Any]
    ) -> dict[str, Any]:
        from micnaga.brain.google import items

        try:
            items.edit(p, changes)
        except items.EditError as exc:
            raise ApplyError("invalid") from exc
        acta_io.write_acta(self.folder, acta)
        return acta

    @staticmethod
    def _pending(acta: dict[str, Any], pid: str) -> dict[str, Any]:
        p: dict[str, Any] | None = next(
            (x for x in acta.get("propuestas") or [] if x.get("id") == pid), None
        )
        if p is None or p.get("estado") != "pendiente":
            raise ApplyError("not_editable")
        return p

    # ------------------------------------------------------------------ deshacer

    def undo(self, ids: list[str], *, resolve: str | None = None) -> dict[str, Any]:
        """Deshace lo aplicado. Un campo que cambio en Odoo despues de aplicar no se pisa: el
        item queda en ``deshacer_estado = "conflicto"`` (con ``conflicto``: actual / escrito /
        original) hasta que el usuario elija ``resolve`` (``"forzar"`` u ``"omitir"``, solo para
        un item a la vez). Si un paso falla tras otros hechos queda ``"a_medias"`` y el
        reintento solo repite lo que falto."""
        acta = acta_io.read_acta(self.folder)
        if acta is None:
            raise ApplyError("no_acta")
        wanted = set(ids)
        if len(wanted) != 1 or resolve not in ("forzar", "omitir"):
            resolve = None  # «Deshacer todo» nunca fuerza
        for p in reversed(acta.get("propuestas") or []):
            if p.get("id") not in wanted or p.get("estado") != "aplicada":
                continue
            try:
                done = self._undo_one(p, resolve)
            except Exception as exc:
                p["error"] = _error_key(exc)
                steps = (p.get("deshacer") or {}).get("pasos") or {}
                p["deshacer_estado"] = "a_medias" if "hecho" in steps.values() else None
                log.warning("undo of %s failed: %s", p.get("id"), p["error"])
            else:
                if done:
                    p["estado"] = "deshecha"
                    p["error"] = None
                    p.pop("deshacer_estado", None)
                    p.pop("conflicto", None)
                    if p.get("tipo") == "hablante":
                        self._relabel(acta)
                else:
                    p["error"] = "undo_conflict"
                    p["deshacer_estado"] = "conflicto"
            acta_io.write_acta(self.folder, acta)
        return acta

    def _undo_one(self, p: dict[str, Any], resolve: str | None = None) -> bool:
        """``True`` si quedo deshecho; ``False`` si hay conflicto (no se escribio nada)."""
        u = p.get("deshacer") or {}
        kind = p.get("tipo")
        if p.get("grupo") == "google":
            self.google().undo(p)
            return True
        if p.get("grupo") == "correcciones":
            if kind == "hablante":
                output.rename_speaker(self.folder, u["hablante"], u["nombre"], db=self._db)
            elif kind == "palabra":
                transcript = output.read_transcript(self.folder)
                updated, n = replace_text(transcript, u["a"], u["de"])
                if n:
                    output.write_transcript(self.folder, updated)
                    output._reindex(self.folder, updated, self._db)
            return True
        o = self.odoo()
        if "creado" in u and u.get("creado"):
            unlink_gone_ok(o, str(u["modelo"]), int(u["creado"]))
        elif kind == "actualizar_oportunidad":
            return self._revert_fields(o, p, resolve)
        return True

    @staticmethod
    def _revert_fields(o: Any, p: dict[str, Any], resolve: str | None) -> bool:
        """Restaura los valores anteriores **solo** donde Odoo sigue con lo que escribio
        Micnaga (se relee antes). Lo que ya esta como antes no se toca; lo demas es conflicto."""
        u = p.get("deshacer") or {}
        model, rid = str(u.get("modelo") or "crm.lead"), int(u["id"])
        before: dict[str, Any] = dict(u.get("antes") or {})
        written: dict[str, Any] = dict(u.get("escrito") or _written_from(p.get("datos") or {}))
        if not before:
            return True
        rows = o.read(model, [rid], list(before))
        if not rows:
            raise ApplyError("gone")
        now = rows[0]
        restore: dict[str, Any] = {}
        conflicts: list[dict[str, Any]] = []
        for field, orig in before.items():
            cur = _norm_value(now.get(field))
            if _same(cur, orig):
                continue  # ya esta como antes
            if (field in written and _same(cur, written[field])) or resolve == "forzar":
                restore[field] = orig
            else:
                conflicts.append(
                    {
                        "campo": field,
                        "actual": cur,
                        "escrito": written.get(field),
                        "original": _norm_value(orig),
                    }
                )
        if conflicts and resolve != "omitir":
            p["conflicto"] = conflicts
            return False
        if restore:
            o.write(model, [rid], restore)
        return True


def _written_from(d: dict[str, Any]) -> dict[str, Any]:
    """Lo que escribio una aplicacion anterior a ``escrito`` (actas ya aplicadas)."""
    out: dict[str, Any] = {}
    if d.get("etapa_id") is not None:
        out["stage_id"] = d["etapa_id"]
    if d.get("importe") is not None:
        out["expected_revenue"] = d["importe"]
    if d.get("fecha_cierre"):
        out["date_deadline"] = d["fecha_cierre"]
    return out


def _norm_value(v: Any) -> Any:
    """Valor de Odoo comparable: many2one ``[id, nombre]`` -> id; vacio (False/""/None) -> None."""
    if isinstance(v, list | tuple) and len(v) == 2 and isinstance(v[0], int):
        return v[0]
    if v is False or v == "" or v is None:
        return None
    return v


def _same(a: Any, b: Any) -> bool:
    a, b = _norm_value(a), _norm_value(b)
    if isinstance(a, int | float) and isinstance(b, int | float) and not isinstance(a, bool):
        return abs(float(a) - float(b)) < 1e-6
    return bool(a == b)


def unlink_gone_ok(o: Any, model: str, rid: int) -> None:
    """``unlink`` en el que borrar algo que ya no existe cuenta como hecho (se comprueba
    releyendo, incluso archivados; un fallo con el registro aun presente se relanza)."""
    try:
        o.unlink(model, [rid])
    except Exception:
        try:
            n = o.call(
                model, "search_count", [[("id", "=", rid)]], {"context": {"active_test": False}}
            )
        except Exception:
            n = None
        if n == 0:
            return
        raise


_NOT_DATA = ("previa", "cambiados", "campos", "enlace")


def snapshot(p: dict[str, Any]) -> dict[str, Any]:
    """Copia de un item tal como esta (sin su propia version anterior ni lo que anade la UI)."""
    import copy

    return {k: copy.deepcopy(v) for k, v in p.items() if k not in _NOT_DATA}


def changed_fields(old: dict[str, Any], new: dict[str, Any], acta: dict[str, Any]) -> list[str]:
    """Claves editables (``campos``) cuyo valor cambia entre dos versiones de un item."""
    from micnaga.brain import plan
    from micnaga.brain.google import items

    kind = str(new.get("tipo"))
    if new.get("grupo") == "google":
        keys = [f["clave"] for f in items.SCHEMAS.get(kind, [])]
        a, b = old.get("datos") or {}, new.get("datos") or {}
        return [k for k in keys if a.get(k) != b.get(k)]
    keys = [f["clave"] for f in plan.EDIT_FIELDS.get(kind, [])]
    return [k for k in keys if plan.field_value(old, k, acta) != plan.field_value(new, k, acta)]


def _error_key(exc: Exception) -> str:
    if isinstance(exc, ApplyError):
        return str(exc)
    kind = getattr(exc, "kind", None)
    if isinstance(kind, str):
        return f"odoo_{kind}"
    return "failed"
