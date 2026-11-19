"""Acciones de la interfaz para Google (``BrainService`` las hereda).

Ajustes › Secretaria › Google: ``google_status`` (cache + si hay una comprobacion en marcha),
``google_check`` («Comprobar», en un hilo: ``claude mcp list`` tarda ~5-10 s) y
``google_open`` («Conectar»: abre ``claude.ai/settings/connectors`` en el navegador).

Biblioteca: ``open_link`` (solo enlaces de Google del acta).
"""

from __future__ import annotations

import logging
import threading
import webbrowser
from pathlib import Path
from typing import Any

from micnaga.brain import google as g

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
        """Abre en el navegador un enlace del acta: Google o el Odoo configurado, nada mas."""
        url = args.get("url")
        odoo = self.config.secretaria.odoo_url.rstrip("/")
        hosts = (*LINK_HOSTS, f"{odoo}/web#") if odoo.startswith("http") else LINK_HOSTS
        if not isinstance(url, str) or not url.startswith(hosts) or len(url) > 2000:
            return {"ok": False, "error": "secretaria.err.invalid"}
        webbrowser.open(url)
        return {"ok": True}

    def _google_applier(self, path: Path) -> Any:
        from micnaga.brain.google.apply import GoogleApplier

        cfg = self.config
        odoo = self._odoo_client if cfg.secretaria.odoo_url else None
        return GoogleApplier(path, runner=self._runner(), db_factory=self._db, odoo=odoo)
