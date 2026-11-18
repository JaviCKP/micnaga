"""Banco de pruebas: metricas, modo sintetico, set de prueba, informe y comparativa."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from micnaga import cli, paths
from micnaga.bench import metrics, report
from micnaga.bench.cli import ENV_TESTSET, run
from micnaga.bench.synthetic import SyntheticSpec, generate_session
from micnaga.config import save_config
from micnaga.types import EnginesInfo, MeetingInfo, Segment, Speaker, Transcript, Word
from tests.pipeline.helpers import relaxed_config

REPO = Path(__file__).resolve().parent.parent.parent


def transcript(segments: list[Segment]) -> Transcript:
    ids = sorted({s.speaker for s in segments} - {"me"})
    return Transcript(
        MeetingInfo("t", "2026-11-18T10:30:00+01:00", 100.0),
        [Speaker("me", "Yo"), *[Speaker(i, i) for i in ids]],
        segments,
        [],
        EnginesInfo("x"),
    )


def seg(speaker: str, start: float, end: float, text: str) -> Segment:
    return Segment(start, end, speaker, text, [Word(w, start, end) for w in text.split()])


# ------------------------------------------------------------------ metricas


def test_wer_ignores_case_and_punctuation() -> None:
    ref = transcript([seg("me", 0, 2, "Hola, ¿qué tal?"), seg("S1", 3, 5, "Muy bien gracias")])
    same = transcript([seg("me", 0, 2, "hola que tal"), seg("S1", 3, 5, "muy bien gracias")])
    assert metrics.wer(ref, same) == pytest.approx(1 / 6)  # «qué» != «que»: 1 de 6
    assert metrics.wer(ref, ref) == 0.0
    worse = transcript([seg("me", 0, 2, "Hola"), seg("S1", 3, 5, "Muy bien")])
    assert metrics.wer(ref, worse) == pytest.approx(3 / 6)
    assert metrics.wer(transcript([]), ref) is None


def test_der_is_zero_for_a_perfect_diarization_and_grows_with_errors() -> None:
    ref = transcript([seg("S1", 0, 10, "a"), seg("S2", 10, 20, "b"), seg("me", 20, 25, "c")])
    perfect = transcript([seg("S2", 0, 10, "a"), seg("S1", 10, 20, "b")])  # etiquetas permutadas
    assert metrics.der(ref, perfect) == pytest.approx(0.0, abs=1e-6)
    wrong = transcript([seg("S1", 0, 20, "a")])
    assert metrics.der(ref, wrong) == pytest.approx(0.5, abs=0.05)
    assert metrics.der(transcript([seg("me", 0, 5, "x")]), perfect) is None


def test_echo_as_me_rate_counts_other_peoples_phrases_attributed_to_me() -> None:
    ref = transcript(
        [
            seg("S1", 10, 14, "necesitamos decidir el proveedor antes del viernes"),
            seg("S2", 20, 24, "yo prefiero la opcion mas sencilla del catalogo"),
            seg("me", 30, 33, "vale lo apunto y se lo comento"),
        ]
    )
    hyp = transcript(
        [
            seg("S1", 10, 14, "necesitamos decidir el proveedor antes del viernes"),
            seg("me", 10.05, 14.1, "necesitamos decidir el proveedor antes del viernes"),  # eco
            seg("S2", 20, 24, "yo prefiero la opcion mas sencilla del catalogo"),
            seg("me", 30, 33, "vale lo apunto y se lo comento"),
        ]
    )
    assert metrics.echo_as_me_rate(ref, hyp) == pytest.approx(0.5)
    clean = transcript([s for i, s in enumerate(hyp.segments) if i != 1])
    assert metrics.echo_as_me_rate(ref, clean) == 0.0
    assert metrics.echo_as_me_rate(transcript([seg("me", 0, 1, "x")]), clean) is None


# ------------------------------------------------------------------ CLI


@pytest.fixture
def fast_config() -> None:
    save_config(relaxed_config())


def bench(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = run(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_missing_testset_gives_a_clear_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(ENV_TESTSET, raising=False)
    code, _out, err = bench([], capsys)
    assert code == 2 and ENV_TESTSET in err and "--synthetic" in err
    monkeypatch.setenv(ENV_TESTSET, str(paths.app_data_dir() / "no-existe"))
    code, _out, err = bench([], capsys)
    assert code == 2 and "no existe" in err


def test_empty_testset_is_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _out, err = bench(["--testset", str(tmp_path)], capsys)
    assert code == 2 and "no hay casos" in err


def test_synthetic_mode_end_to_end_with_report_and_comparison(
    fast_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_file = tmp_path / "report.md"
    args = ["--synthetic", "--cases", "dialogo-altavoces", "nota-de-voz", "--out", str(out_file)]
    code, out, err = bench(args, capsys)
    assert code == 0, err
    text = out_file.read_text(encoding="utf-8")
    assert out_file.read_text(encoding="utf-8") in out
    assert "MOTORES FALSOS" in text and "no** son mediciones" in text
    assert "no hay ejecucion previa" in text
    assert "| dialogo-altavoces | done |" in text and "| nota-de-voz | discarded |" in text
    assert "Casos trampa descartados correctamente: 1/1." in text
    assert "aec=fake-aec" in text
    for stage in ("gates", "asr", "diarize", "echo_dedup", "assemble"):
        assert stage in text  # tiempos por etapa

    saved = json.loads(report.last_run_path().read_text(encoding="utf-8"))
    run_data = saved["synthetic"]
    assert run_data["engines"] == "fake" and run_data["aggregate"]["false_positives"] == 0
    case = run_data["cases"]["dialogo-altavoces"]
    assert case["wer"] == pytest.approx(0.0, abs=0.02) and case["der"] is not None
    assert case["echo_as_me"] == 0.0 and case["peak_rss_mb"] > 0 and case["factor"] is not None
    assert set(case["stage_times"]) >= {"gates", "asr", "assemble"}
    assert run_data["machine"]["cpu_logical"]

    # segunda ejecucion: compara con la anterior
    code, out2, _ = bench(args, capsys)
    assert code == 0
    assert "Anterior: 20" in out2 and "| Metrica | Objetivo | Anterior | Actual | Cambio |" in out2
    assert "= |" in out2  # sin cambios en WER/DER/falsos positivos


def test_no_save_does_not_touch_last_run(
    fast_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _out, _err = bench(
        ["--synthetic", "--cases", "nota-de-voz", "--no-save", "--out", str(tmp_path / "r.md")],
        capsys,
    )
    assert code == 0 and not report.last_run_path().exists()


def test_unknown_synthetic_case(capsys: pytest.CaptureFixture[str]) -> None:
    code, _out, err = bench(["--synthetic", "--cases", "nope"], capsys)
    assert code == 2 and "desconocidos" in err


def test_testset_is_never_modified_and_false_positives_are_counted(
    fast_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    testset = tmp_path / "testset"
    real = generate_session(testset / "reunion-real", SyntheticSpec(duration_s=45.0))
    trap = generate_session(
        testset / "trampa-nota", SyntheticSpec(kind="voice_note", duration_s=45.0)
    )
    # un «caso trampa» que en realidad es una reunion: el pipeline lo procesa -> falso positivo
    (real / "expect.json").write_text('{"discard": true}', encoding="utf-8")
    snapshot = sorted(str(p.relative_to(testset)) for p in testset.rglob("*"))
    sizes = {p: p.stat().st_size for p in testset.rglob("*") if p.is_file()}

    code, out, err = bench(
        ["--testset", str(testset), "--engines", "fake", "--out", str(tmp_path / "r.md")], capsys
    )
    assert code == 0, err
    assert sorted(str(p.relative_to(testset)) for p in testset.rglob("*")) == snapshot
    assert {p: p.stat().st_size for p in testset.rglob("*") if p.is_file()} == sizes
    assert trap.is_dir() and real.is_dir()  # nada borrado ni renombrado

    data = json.loads(report.last_run_path().read_text(encoding="utf-8"))["testset"]
    assert data["aggregate"]["false_positives"] == 1
    assert data["cases"]["reunion-real"]["false_positive"] is True
    assert data["cases"]["trampa-nota"]["status"] == "discarded"
    assert "falso positivo" in out
    # el set no se usa en el sitio: la base de datos del banco es temporal
    assert not (paths.meetings_root() / "micnaga.db").exists() or not list(
        paths.meetings_root().glob("2026-*")
    )


def test_a_broken_case_does_not_sink_the_whole_report(
    fast_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    testset = tmp_path / "ts"
    generate_session(testset / "ok", SyntheticSpec(duration_s=45.0))
    broken = testset / "roto"
    broken.mkdir(parents=True)
    (broken / paths.CAPTURE_JSON).write_text("{no es json", encoding="utf-8")
    code, out, _ = bench(
        ["--testset", str(testset), "--engines", "fake", "--out", str(tmp_path / "r.md")], capsys
    )
    assert code == 0
    assert "| ok | done |" in out and "| roto | failed |" in out and "error:" in out


def test_python_dash_m_entry_point_dispatches_to_the_bench(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["bench", "--synthetic", "--cases", "nope"]) == 2
    assert "desconocidos" in capsys.readouterr().err


def test_testset_format_doc_exists() -> None:
    doc = (REPO / "docs" / "testset_format.md").read_text(encoding="utf-8")
    for needle in (
        "audio_yo.flac",
        "capture.json",
        "reference.json",
        "expect.json",
        "MICNAGA_TESTSET",
    ):
        assert needle in doc
