"""Acciones de la interfaz para Google y para editar propuestas (``BrainService`` las hereda).

Ajustes › Secretaria › Google: ``google_status`` (cache + si hay una comprobacion en marcha),
``google_check`` («Comprobar», en un hilo: ``claude mcp list`` tarda ~5-10 s) y
``google_open`` («Conectar»: abre ``claude.ai/settings/connectors`` en el navegador).

Biblioteca: ``edit`` (cualquier campo de cualquier propuesta pendiente; conserva el id).
"""

from __future__ import annotations

import logging
import threading
import webbrowser
from pathlib import Path
from typing import Any

from micnaga.brain import acta as acta_io
from micnaga.brain import google as g
from micnaga.brain import items

log = logging.getLogger(__name__)

LINK_HOSTS = (
    "https://mail.google.com/",
    "https://docs.google.com/",
    "https://drive.google.com/",
    "https://calendar.google.com/",
    "https://www.google.com/calendar/",
    "https://meet.google.com/",
)


class GoogleApiMixin:
    _google_checking = False
    _google_lock = threading.Lock()

    # Lo implementa BrainService
    config: Any

    def _folder_path(self, args: dict[str, Any]) -> Path | None:  # pragma: no cover
        raise NotImplementedError

    def _runner(self) -> Any:  # pragma: no cover
        raise NotImplementedError

    def _db(self) -> Any:  # pragma: no cover
        raise NotImplementedError

    def _odoo_client(self) -> Any:  # pragma: no cover
        raise NotImplementedError

    # ------------------------------------------------------------------ ajustes

    def _api_google_status(self, args: dict[str, Any]) -> dict[str, Any]:
        from micnaga.brain.google import detect

        cache = detect.read_cache()
        services = cache.get("services") if isinstance(cache.get("services"), dict) else {}
        assert isinstance(services, dict)
        s = self.config.secretaria
        return {
            "ok": True,
            "checking": self._google_checking,
            "checked": cache.get("checked"),
            "auth": s.auth,
            "services": {
                x: {"state": services.get(x, "unknown"), "enabled": bool(getattr(s, f"google_{x}"))}
                for x in g.SERVICES
            },
        }

    def _api_google_check(self, args: dict[str, Any]) -> dict[str, Any]:
        with self._google_lock:
            if self._google_checking:
                return {"ok": True, "checking": True}
            type(self)._google_checking = True

        def work() -> None:
            from micnaga.brain.google import detect

            try:
                detect.check(self._runner())
            except Exception:
                log.exception("google check failed")
            finally:
                type(self)._google_checking = False

        threading.Thread(target=work, name="micnaga-google-check", daemon=True).start()
        return {"ok": True, "checking": True}

    def _api_google_open(self, args: dict[str, Any]) -> dict[str, Any]:
        webbrowser.open(g.CONNECTORS_URL)
        return {"ok": True}

    def _api_open_link(self, args: dict[str, Any]) -> dict[str, Any]:
        """Abre en el navegador un enlace de Google del acta (solo dominios de Google)."""
        url = args.get("url")
        if not isinstance(url, str) or not url.startswith(LINK_HOSTS) or len(url) > 2000:
            return {"ok": False, "error": "secretaria.err.invalid"}
        webbrowser.open(url)
        return {"ok": True}

    # ------------------------------------------------------------------ propuestas

    def _api_edit(self, args: dict[str, Any]) -> dict[str, Any]:
        """``{folder, id, values: {campo: valor}}`` -> acta con la propuesta editada."""
        path = self._folder_path(args)
        if path is None:
            return {"ok": False, "error": "library.err.not_found"}
        pid, values = args.get("id"), args.get("values")
        if not isinstance(pid, str) or not isinstance(values, dict):
            return {"ok": False, "error": "secretaria.err.invalid"}
        acta = acta_io.read_acta(path)
        if acta is None:
            return {"ok": False, "error": "library.err.not_found"}
        p = next((x for x in acta.get("propuestas") or [] if x.get("id") == pid), None)
        if p is None:
            return {"ok": False, "error": "library.err.not_found"}
        try:
            items.edit(p, values)
        except items.EditError as exc:
            return {"ok": False, "error": "secretaria.err.invalid", "field": str(exc)}
        acta_io.write_acta(path, items.strip(acta))
        return {"ok": True, "acta": acta}

    def _google_applier(self, path: Path) -> Any:
        from micnaga.brain.google.apply import GoogleApplier

        cfg = self.config
        odoo = self._odoo_client if cfg.secretaria.odoo_url else None
        return GoogleApplier(path, runner=self._runner(), db_factory=self._db, odoo=odoo)
