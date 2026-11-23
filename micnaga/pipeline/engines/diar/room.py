"""Personas de la sala: quien es «Yo» entre los hablantes que el diarizador encuentra en el micro.

En una reunion presencial todos hablan por el mismo micro. La etapa 7 diariza tambien esa pista
y aqui se decide la etiqueta de cada hablante del micro (funciones puras, sin modelos):

* **«Yo»** es el hablante con mas **energia** total en el micro (suma de la energia de sus turnos):
  el dueno del portatil es quien esta mas cerca del micro. Con un solo hablante, es «Yo».
* Un hablante del micro que **se parece a un hablante remoto** (coseno >= ``ECHO_SIMILARITY``)
  es eco que el cancelador no quito: pasa a «Yo» y la etapa 9 descarta sus frases si repiten lo
  remoto.
* Un hablante que **se parece a «Yo»** (coseno >= ``SAME_VOICE``) se queda en «Yo».
* Un hablante con menos de ``MIN_ROOM_SPEECH_S`` s de voz **en turnos de al menos**
  ``LONG_TURN_S`` s no es una persona sino **trocitos** (asentimientos, risas, respiraciones y
  cortes de varias voces que el diarizador junta; medido con voz real: un tercer «hablante» de
  18 s hecho de turnos de 0,4-1,5 s): cada turno suyo pasa al hablante del turno mas cercano en
  el tiempo. Es mas estricto que el post-filtro general: en una llamada normal, con una sola
  persona en el micro, nunca debe aparecer una «persona de la sala» inventada.
* El resto son ``P<n>``, numerados por primera aparicion a partir de ``first_number`` (el numero
  siguiente al ultimo hablante remoto: «Hablante 3» no se repite entre ``S3`` y ``P3``).

Umbrales sin calibrar con un banco de reuniones presenciales reales (el usuario puede fusionar o
renombrar hablantes en la biblioteca).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
from numpy.typing import NDArray

from micnaga.types import ROOM_PREFIX, SPEAKER_ME, AudioArray, Turn

MIN_MIC_SPEECH_S = 20.0
"""Por debajo de esto (voz total en el micro) ni se intenta diarizar el micro."""
MIN_ROOM_SPEECH_S = 8.0
LONG_TURN_S = 2.0
ECHO_SIMILARITY = 0.6
SAME_VOICE = 0.55


def speech_energy(audio: AudioArray, sr: int, turns: Sequence[Turn]) -> dict[str, float]:
    """Energia total (suma de cuadrados) del audio de los turnos de cada hablante."""
    out: dict[str, float] = {}
    for t in turns:
        a, b = max(0, int(t.start * sr)), min(len(audio), int(t.end * sr))
        if b > a:
            seg = audio[a:b].astype(np.float64)
            out[t.speaker] = out.get(t.speaker, 0.0) + float(seg @ seg)
    return out


def _cos(a: NDArray[np.float32], b: NDArray[np.float32]) -> float:
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    return float(a @ b) / (na * nb) if na > 0 and nb > 0 else 0.0


def label_room_speakers(
    turns: Sequence[Turn],
    energy: Mapping[str, float],
    speech_s: Mapping[str, float],
    mic_embeddings: Mapping[str, NDArray[np.float32]],
    remote_embeddings: Mapping[str, NDArray[np.float32]],
    first_number: int,
    *,
    min_room_speech_s: float = MIN_ROOM_SPEECH_S,
    long_turn_s: float = LONG_TURN_S,
    echo_similarity: float = ECHO_SIMILARITY,
    same_voice: float = SAME_VOICE,
) -> tuple[list[Turn], dict[str, str]]:
    """Turnos del micro reetiquetados (``me`` / ``P<n>``) y por que cada hablante quedo asi.

    ``turns`` son los turnos del diarizador sobre el micro (etiquetas cualesquiera). Devuelve
    ``(turnos, motivo)`` con ``motivo[etiqueta_original]`` en ``me`` (el elegido), ``echo``,
    ``same_voice``, ``small`` (trocitos: van al turno vecino) o ``room``.
    """
    long_speech: dict[str, float] = {}
    for t in turns:
        if t.end - t.start >= long_turn_s:
            long_speech[t.speaker] = long_speech.get(t.speaker, 0.0) + (t.end - t.start)
    labels = list(dict.fromkeys(t.speaker for t in sorted(turns, key=lambda t: t.start)))
    if not labels:
        return [], {}
    me = max(labels, key=lambda s: (energy.get(s, 0.0), speech_s.get(s, 0.0)))
    reason: dict[str, str] = {me: "me"}
    me_vec = mic_embeddings.get(me)
    mapping: dict[str, str] = {me: SPEAKER_ME}
    number = first_number
    for lab in labels:
        if lab == me:
            continue
        vec = mic_embeddings.get(lab)
        if vec is not None and any(
            _cos(vec, r) >= echo_similarity for r in remote_embeddings.values()
        ):
            reason[lab] = "echo"
        elif vec is not None and me_vec is not None and _cos(vec, me_vec) >= same_voice:
            reason[lab] = "same_voice"
        elif long_speech.get(lab, 0.0) < min_room_speech_s:
            reason[lab] = "small"
            continue  # sus turnos se reparten al final
        else:
            reason[lab] = "room"
            mapping[lab] = f"{ROOM_PREFIX}{number}"
            number += 1
            continue
        mapping[lab] = SPEAKER_ME
    anchors = sorted((t for t in turns if t.speaker in mapping), key=lambda t: t.start)
    out: list[Turn] = []
    for t in turns:
        label = mapping.get(t.speaker)
        if label is None:  # trocito: hablante del turno conservado mas cercano en el tiempo
            near = min(anchors, key=lambda o: max(o.start - t.end, t.start - o.end, 0.0))
            label = mapping[near.speaker]
        out.append(Turn(label, t.start, t.end))
    out.sort(key=lambda t: (t.start, t.end))
    return out, reason


def next_speaker_number(remote_turns: Sequence[Turn]) -> int:
    """Numero siguiente al mayor ``S<n>`` remoto (1 si no hay)."""
    nums = [int(t.speaker[1:]) for t in remote_turns if t.speaker[1:].isdigit()]
    return max(nums, default=0) + 1
