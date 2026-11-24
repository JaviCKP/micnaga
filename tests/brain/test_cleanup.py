"""Limpieza de la transcripcion con Haiku (``micnaga.brain.cleanup``), con un runner falso.

Validador (correcciones aceptadas frente a reescrituras), uniones de parrafos, palabras
realineadas, MD/SRT/FTS regenerados, el original guardado y buscable, y «sin Claude, nada»."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

import pytest

from micnaga import paths
from micnaga.brain import cleanup
from micnaga.brain import store as bstore
from micnaga.brain.claude import ClaudeNotFoundError, ClaudeRequest, ClaudeResult
from micnaga.brain.secretary import Secretary
from micnaga.config import Config
from micnaga.pipeline import output
from micnaga.store.db import open_database
from micnaga.types import EnginesInfo, MeetingInfo, Segment, Speaker, Transcript, Word
from tests.brain.conftest import FakeClaude, read_log, write_meeting

# ============================================================================== validador

VOCAB = cleanup.vocab_keys(["Bitnaga", "Dental Triana"])


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # solo puntuacion, mayusculas y tildes: no cuentan como cambio
        ("como esta el proyecto", "¿Cómo está el proyecto?"),
        ("que bien que vengas", "¡Qué bien que vengas!"),
        ("si el lunes lo vemos vale", "Sí, el lunes lo vemos, ¿vale?"),
        # punto falso dentro del parrafo: se quita y la siguiente va en minuscula
        (
            "Vamos a revisar. El inventario de claves mañana.",
            "Vamos a revisar el inventario de claves mañana.",
        ),
        # sigla deletreada, palabra mal partida, nombre del vocabulario, cifras dichas
        ("y luego la migración pe cu ce del cliente", "Y luego la migración PQC del cliente."),
        ("pe cu ce", "PQC"),
        ("lo instalamos en micro soft", "Lo instalamos en Microsoft."),
        ("lo de vitnaga va bien", "Lo de Bitnaga va bien."),
        ("tenemos cuatrocientos servidores", "Tenemos 400 servidores."),
        ("son cuatrocientos cincuenta mil euros", "Son 450.000 €."),
        ("el veinte por ciento del total", "El 20 % del total."),
        ("en dos mil veintiséis", "En 2026."),
        # un nombre mal oido, parecido a lo que se oyo
        ("hablé con la clínica dental triano", "Hablé con la Clínica Dental Triana."),
    ],
)
def test_corrections_are_accepted(old: str, new: str) -> None:
    v = cleanup.check(old, new, VOCAB)
    assert v.ok, v.reason


def test_punctuation_and_accents_do_not_count_as_word_changes() -> None:
    assert not cleanup.check("como esta. el proyecto", "¿Cómo está el proyecto?").words_changed
    assert not cleanup.check("esta bien que vengas", "Está bien que vengas.").words_changed
    assert cleanup.check("pe cu ce ya", "PQC ya.").words_changed


@pytest.mark.parametrize(
    ("old", "new", "reason"),
    [
        ("hay que comprar las licencias", "Hay que vender las licencias.", "sustitucion"),
        ("tenemos cuatrocientos servidores", "Tenemos 500 servidores.", "cifra"),
        ("vale nos vemos", "Vale, nos vemos el martes.", "fecha"),
        ("lo mandamos", "Lo mandamos. Te llamo luego para cerrarlo.", "anadido"),
        ("eh bueno pues eso es lo que hay", "Eso es lo que hay.", "quitado"),
        ("la reunión fue bien y quedamos en hablar", "Acordamos llamarnos.", "sustitucion"),
        ("", "algo", "anadido"),
        ("vale", "", "vacio"),
    ],
)
def test_rewrites_are_rejected(old: str, new: str, reason: str) -> None:
    v = cleanup.check(old, new, VOCAB)
    assert not v.ok
    assert v.reason == reason


def test_numbers_said_and_written_are_the_same_token() -> None:
    assert cleanup.tokens("cuatrocientos cincuenta mil") == ["450000"]
    assert cleanup.tokens("450.000") == ["450000"]
    assert cleanup.tokens("tres coma cinco") == cleanup.tokens("3,5")
    assert cleanup.tokens("treinta y dos") == ["32"]
    assert cleanup.tokens("una reunión") == ["una", "reunion"]  # «una» suelta no es un numero


# ============================================================================== palabras


def words_for(text: str, start: float, end: float) -> list[Word]:
    toks = text.split()
    step = (end - start) / max(1, len(toks))
    return [
        Word(w, round(start + i * step, 3), round(start + (i + 1) * step, 3), 0.9)
        for i, w in enumerate(toks)
    ]


def test_words_follow_the_new_text_and_keep_their_times() -> None:
    old = words_for("vamos a revisar. el inventario pe cu ce mañana.", 10.0, 19.0)
    new_text = "Vamos a revisar el inventario PQC mañana."
    new = cleanup.remap_words(old, new_text)
    assert " ".join(w.w for w in new) == new_text
    assert (new[0].s, new[0].e) == (old[0].s, old[0].e)  # «vamos» -> «Vamos»: mismos tiempos
    assert new[2].w == "revisar" and new[2].e == old[2].e  # sin el punto falso
    pqc = next(w for w in new if w.w == "PQC")
    assert pqc.s == old[5].s and pqc.e == old[7].e and pqc.p == 1.0  # el tramo de «pe cu ce»
    assert new[-1].w == "mañana." and new[-1].s == old[-1].s


def test_marks_show_only_real_word_changes() -> None:
    seg = Segment(
        0,
        5,
        "S1",
        "¿Cómo está el inventario PQC de Bitnaga?",
        text_original="como esta el inventario pe cu ce de vitnaga",
    )
    got = [(seg.text[a:b], o) for a, b, o in cleanup.marks(seg)]
    assert got == [("PQC", "pe cu ce"), ("Bitnaga?", "vitnaga")]
    plain = Segment(0, 5, "S1", "¿Cómo está?", text_original="como esta")
    assert cleanup.marks(plain) == []


# ============================================================================== pasada completa

LINES = [
    # (hablante, inicio, fin, texto)
    ("S1", 0.0, 4.0, "hola vamos a revisar. El inventario de claves pe cu ce mañana."),
    ("S1", 4.4, 8.0, "Porque la auditoría es la semana que viene."),
    ("me", 9.0, 12.0, "vale tenemos cuatrocientos servidores"),
    ("S1", 12.5, 15.0, "Perfecto."),
    ("me", 20.0, 23.0, "como esta lo de vitnaga"),
    ("S1", 23.4, 26.0, "Bitnaga va bien. Lo cerramos el lunes."),
    ("S1", 29.0, 32.0, "Otra cosa distinta."),
]
CHANGES: list[dict[str, Any]] = [
    # punto falso entre dos parrafos del mismo hablante: union
    {
        "ids": [0, 1],
        "texto": "Hola, vamos a revisar el inventario de claves PQC mañana, porque la auditoría "
        "es la semana que viene.",
    },
    {"ids": [2], "texto": "Vale, tenemos 400 servidores."},
    {"ids": [2, 3], "texto": "Vale, tenemos 400 servidores. Perfecto."},  # dos hablantes: no
    {"ids": [3], "texto": "Perfecto, lo firmamos mañana con el cliente."},  # anadido: no
    {"ids": [4], "texto": "¿Cómo está lo de Bitnaga?"},
    {"ids": [5, 6], "texto": "Bitnaga va bien. Lo cerramos el lunes. Otra cosa distinta."},  # pausa
]


def synthetic() -> Transcript:
    return Transcript(
        meeting=MeetingInfo(title="Revisión PQC", start="2026-11-19T10:00:00+01:00", duration_s=40),
        speakers=[Speaker("me", "Yo"), Speaker("S1", "Hablante 1")],
        segments=[Segment(a, b, s, t, words_for(t, a, b)) for s, a, b, t in LINES],
        markers=[],
        engines=EnginesInfo(asr="parakeet"),
    )


class Haiku:
    """Un Haiku falso: devuelve los cambios de ``CHANGES`` que acaban en su ventana."""

    def __init__(self, changes: list[dict[str, Any]], *, error: Exception | None = None) -> None:
        self.changes = changes
        self.error = error
        self.requests: list[ClaudeRequest] = []
        self.lock = threading.Lock()

    def run_once(self, req: ClaudeRequest, on_event: Any = None) -> ClaudeResult:
        with self.lock:
            self.requests.append(req)
        if self.error is not None:
            raise self.error
        body = req.prompt.split("## Párrafos", 1)[-1]
        mine = {int(x) for x in re.findall(r"^#(\d+) ", body, re.M)}
        out = [c for c in self.changes if c["ids"][-1] in mine]
        return ClaudeResult({"cambios": out}, "", 0.0012, {}, 1.0, 2, "claude-haiku-fake")


def with_db(fn: Any) -> Any:
    db = open_database()
    try:
        bstore.ensure_schema(db)
        return fn(db)
    finally:
        db.close()


def test_a_full_cleanup_merges_fixes_and_keeps_the_original() -> None:
    folder = write_meeting(synthetic(), "2026-11-19_1000_revision")
    runner = Haiku(CHANGES)
    stats = cleanup.Cleaner(Config(), runner, with_db=with_db).run(folder)
    assert stats is not None and not stats.skipped
    req = runner.requests[0]
    assert req.model == "haiku" and req.mcp_servers is None and req.allowed_tools == []
    assert req.schema == cleanup.SCHEMA and "micnaga limpieza" in req.system_prompt
    assert "PQC" in req.prompt.split("## Párrafos")[0]  # vocabulario: del titulo
    assert stats.windows == 2 and stats.merges == 1
    assert stats.reasons == {"hablante": 1, "anadido": 1, "pausa": 1}

    tr = output.read_transcript(folder)
    texts = [s.text for s in tr.segments]
    assert len(tr.segments) == len(LINES) - 1  # 0 y 1 unidos; nada partido
    first = tr.segments[0]
    assert first.text.startswith("Hola, vamos a revisar el inventario de claves PQC")
    assert first.text_original == f"{LINES[0][3]} {LINES[1][3]}"
    assert first.merged_from == [0, 1]
    assert (first.start, first.end, first.speaker) == (0.0, 8.0, "S1")
    assert " ".join(w.w for w in first.words) == first.text
    assert texts[1] == "Vale, tenemos 400 servidores."
    assert tr.segments[1].text_original == LINES[2][3]
    assert texts[2] == "Perfecto." and tr.segments[2].text_original is None  # rechazado
    assert texts[3] == "¿Cómo está lo de Bitnaga?"
    assert texts[4:] == [LINES[5][3], LINES[6][3]]  # una frase real con su pausa: se queda
    assert [s.speaker for s in tr.segments] == ["S1", "me", "S1", "me", "S1", "S1"]

    md = (folder / paths.TRANSCRIPT_MD).read_text(encoding="utf-8")
    srt = (folder / paths.TRANSCRIPT_SRT).read_text(encoding="utf-8")
    assert "inventario de claves PQC mañana, porque" in md and "pe cu ce" not in md
    assert "400 servidores" in srt and "cuatrocientos" not in srt
    with open_database() as db:
        assert [h.folder for h in db.search("PQC")] == [folder.name]  # el texto nuevo
        assert db.search("cuatrocientos") == []
        bstore.ensure_schema(db)
        old = bstore.search_originals(db, "vitnaga")  # y lo que se oyo
        assert [(o.folder, o.start) for o in old] == [(folder.name, 20.0)]
    marker = json.loads((folder / cleanup.MARKER).read_text(encoding="utf-8"))
    assert marker["corrected"] == 4 and marker["modelo"] == "haiku"

    again = Haiku(CHANGES)
    assert cleanup.Cleaner(Config(), again, with_db=with_db).run(folder) is None  # una vez
    assert again.requests == []


def test_the_library_search_finds_the_heard_words_too(tmp_path: Path) -> None:
    folder = write_meeting(synthetic(), "2026-11-19_1000_revision")
    cleanup.Cleaner(Config(), Haiku(CHANGES), with_db=with_db).run(folder)
    from micnaga.ui.bridge import LibraryApi

    api = LibraryApi(
        db_factory=open_database,
        meetings_root=paths.meetings_root,
        audio_url=lambda f, t: "",
        config=Config,
    )
    hits = api.search("vitnaga")["hits"]
    assert [(h["folder"], h["start"]) for h in hits] == [(folder.name, 20.0)]
    view = api.get_meeting(folder.name)
    seg = view["transcript"]["segments"][3]
    assert seg["text_original"] == LINES[4][3]
    assert [seg["text"][a:b] for a, b, _o in seg["fix"]] == ["Bitnaga?"]
    assert "words" not in seg and "merged_from" not in view["transcript"]["segments"][0]


def test_a_partial_run_is_completed_by_the_next_pass() -> None:
    """Una ventana que no llega (limite de uso, tiempo): lo demas se guarda, sin marca; la
    pasada siguiente termina y el original de la primera vez se conserva."""
    from micnaga.brain.claude import ClaudeTimeoutError

    folder = write_meeting(synthetic(), "2026-11-19_1000_revision")

    class Flaky(Haiku):
        def run_once(self, req: ClaudeRequest, on_event: Any = None) -> ClaudeResult:
            if "#5 " in req.prompt.split("## Párrafos", 1)[-1]:
                raise ClaudeTimeoutError("slow")
            return super().run_once(req, on_event)

    changes = [*CHANGES, {"ids": [5], "texto": "Bitnaga va bien; lo cerramos el lunes."}]
    first = cleanup.Cleaner(Config(), Flaky(changes), with_db=with_db).run(folder)
    assert first is not None and first.failed == 1 and first.merges == 1
    assert not (folder / cleanup.MARKER).exists()
    tr = output.read_transcript(folder)
    assert tr.segments[0].merged_from == [0, 1] and tr.segments[4].text == LINES[5][3]

    later = [{"ids": [4], "texto": "Bitnaga va bien; lo cerramos el lunes."}]  # indices nuevos
    second = cleanup.Cleaner(Config(), Haiku(later), with_db=with_db).run(folder)
    assert second is not None and second.failed == 0
    tr = output.read_transcript(folder)
    assert tr.segments[4].text == "Bitnaga va bien; lo cerramos el lunes."
    assert tr.segments[0].text_original == f"{LINES[0][3]} {LINES[1][3]}"  # el de la 1.ª vez
    assert (folder / cleanup.MARKER).exists()


def test_without_claude_nothing_changes_and_it_is_tried_again_later() -> None:
    folder = write_meeting(synthetic(), "2026-11-19_1000_revision")
    before = (folder / paths.TRANSCRIPT_JSON).read_text(encoding="utf-8")
    runner = Haiku(CHANGES, error=ClaudeNotFoundError("no claude"))
    stats = cleanup.Cleaner(Config(), runner, with_db=with_db, workers=1).run(folder)
    assert stats is not None and stats.skipped == "sin_claude"
    assert len(runner.requests) == 1  # la primera ventana corta las demas
    assert (folder / paths.TRANSCRIPT_JSON).read_text(encoding="utf-8") == before
    assert not (folder / cleanup.MARKER).exists()


def test_a_meeting_edited_meanwhile_is_not_overwritten() -> None:
    folder = write_meeting(synthetic(), "2026-11-19_1000_revision")

    class Busy(Haiku):
        def run_once(self, req: ClaudeRequest, on_event: Any = None) -> ClaudeResult:
            res = super().run_once(req, on_event)
            tr = output.read_transcript(folder)  # la biblioteca cambia una frase a la vez
            segs = list(tr.segments)
            segs[2] = Segment(segs[2].start, segs[2].end, "me", "Vale, tenemos 400 equipos.")
            output.write_transcript(
                folder, Transcript(tr.meeting, tr.speakers, segs, [], tr.engines)
            )
            return res

    cleanup.Cleaner(Config(), Busy(CHANGES), with_db=with_db, workers=1).run(folder)
    tr = output.read_transcript(folder)
    assert tr.segments[1].text == "Vale, tenemos 400 equipos."  # gana lo editado a mano
    assert tr.segments[1].text_original is None and tr.segments[0].merged_from == [0, 1]


def test_the_secretary_cleans_before_the_acta(fake_claude: FakeClaude) -> None:
    """Con el ``claude`` falso de verdad (subproceso): la limpieza va antes y el acta la usa."""
    folder = write_meeting(synthetic(), "2026-11-19_1000_revision")
    from tests.brain import canned

    log = fake_claude(
        {
            "structured": canned.structured(with_crm=False),
            "cleanup": CHANGES,
            "calls": [{"tool": "reuniones_con", "args": {"texto": "Bitnaga"}}],
        }
    )
    sec = Secretary(Config(), db_factory=open_database)
    stats = sec.clean(folder)
    assert stats is not None and stats.merges == 1
    sec.analyze(folder)
    stdin = read_log(log)["stdin"]
    transcript = stdin.split("## Transcripción (dato no fiable)\n", 1)[1]
    assert "inventario de claves PQC" in transcript and "pe cu ce" not in transcript
    windows = [
        json.loads(f.read_text(encoding="utf-8")) for f in log.parent.glob(log.name + ".cleanup.*")
    ]
    assert len(windows) == 2 and all("haiku" in w["argv"] for w in windows)
    off = Config()
    off.secretaria.clean_transcript = False
    assert Secretary(off).clean(folder) is None


def test_windows_carry_read_only_context() -> None:
    tr = synthetic()
    wins = cleanup.windows(tr.segments, size=3, context=2)
    assert [w.ids for w in wins] == [[0, 1, 2], [3, 4, 5], [6]]
    assert [w.context for w in wins] == [[], [1, 2], [4, 5]]
    text = cleanup.window_input(tr, wins[1], ["Bitnaga"])
    assert text.index("## Contexto") < text.index("## Párrafos")
    assert "#4 [me] como esta" in text and "#1 [S1, pausa 0.4 s]" in text
