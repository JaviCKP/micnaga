# Micnaga — dirección de diseño: «cristal sobre la noche»

Referencias: `Pictures/referencias/micnaga/*` (cristal oscuro luminoso 2–3, cristal esmerilado claro y
botón rojo de grabar 4–5, paneles flotantes 1) + identidad Bitnaga (skill `bitnaga-space`).
Regla de desempate: las referencias mandan en el tratamiento de cristal; Bitnaga en color, tipo y marca.

## Principios

1. **Se entiende sin leer.** Estado = forma + color + movimiento: punto rojo que late = grabando;
   anillo hueco = provisional; barras ámbar = pausa privada; el anillo que se vacía alrededor del
   botón de grabar = la cuenta atrás; ondas vivas = te oigo / les oigo.
2. **Nada de frases de relleno.** Fuera subtítulos, «leads» y textos de ayuda permanentes. La ayuda
   que sí importa vive detrás de una marca `?` (tooltip al pasar/enfocar, y `aria-label`).
3. **Un objeto, no una pila de widgets.** Cada superficie es una sola pieza de cristal con jerarquía
   interna (el popup: origen · pregunta · voces | cerrar · grabar).
4. **Silencioso durante la reunión.** Pastilla siempre «de noche», 60 % en reposo, movimiento solo
   donde hay información (punto, ondas, anillo).
5. **Animación con intención.** 180–240 ms con la curva de la spec; entradas de 520 ms con la curva
   de Bitnaga; solo `transform`/`opacity`; todo se apaga con «Efectos de animación» o
   `prefers-reduced-motion`.

## Tokens (`micnaga/ui/web/tokens.css`)

| Grupo | Valor |
| --- | --- |
| Suelo | oscuro `#050507` (noche Bitnaga) · claro `#eceef3`; `.ambient`: 4 focos de luz estáticos (violeta, índigo/azul, coral, violeta profundo) + grano SVG |
| Tinta | `#f4f4f7 / #b9b9c4 / #8c8c99` (oscuro) · `#0b0b0f / #454552 / #5f5f6d` (claro); AA sobre todas las capas |
| Acento | violeta `#a78bfa` (oscuro) / `#6d34e0` (claro): foco, selección, progreso, marca. Violeta del logo `#9d5cb5` |
| Grabar | coral `#e8464d → #c42a31` (degradado brillante); punto `#ff4d55`. Único rojo de la interfaz |
| Pausa | ámbar `#f5a524` (punto) / `#ffc560` (texto en oscuro) / `#a35600` (texto en claro) |
| OK | `#5ad893` / `#13803d` |
| Cristal | capas `--glass .045/.075/.11` (oscuro) · `.58/.78/.92` blanco (claro) + `blur(28px) saturate(150%)`; borde = anillo 1 px + brillo superior 1 px (`--glass-edge`); una sombra suave |
| Radios | paneles 24 · tarjetas 18 · popup 16 (8 con Acrylic) · controles 12 · píldoras 999 |
| Tipo | display **Barlow Semi Condensed 700**, mayúsculas, interlínea .9 (títulos, pregunta, cronómetro, cifras) · texto **Segoe UI Variable** (13,5 px cuerpo, 12,5 meta, 11 versalitas) |
| Escala display | 20 · 28 · 34–40 · 56–76 |
| Movimiento | `--ease: cubic-bezier(.2,.8,.2,1)` (spec) · `--ease-out: cubic-bezier(.16,1,.3,1)` (Bitnaga) · 180 / 220 / 240 / 400 (morph) / 520 (entradas) |

### Receta de cristal

- **Ventanas normales** (biblioteca, ajustes, asistente): suelo ambiental estático + paneles con
  `backdrop-filter` (se compone una vez: nada se anima detrás). Con Mica a nivel de ventana
  (`data-backdrop="mica"`) el suelo se vuelve un tinte al 45 %.
- **Superposiciones** (popup, pastilla): el WebView no desenfoca el escritorio, así que por defecto
  son un cristal opaco «ahumado/esmerilado» (degradado, brillo especular, fugas de luz violeta y
  coral, grano, anillo fino). **Recomendado: Acrylic real de DWM** (verificado en esta máquina,
  ver informe): con `data-backdrop="acrylic"` la tarjeta pasa a ser solo un tinte (62 %) sobre el
  escritorio desenfocado y adopta las esquinas de 8 px de la ventana.

## Por superficie

- **Popup** (372 × 112, antes 400 × 232): fila de origen (icono Meet/app + nombre + marca ámbar de
  «avisa de que grabas» que se despliega al pasar el ratón y se puede ocultar), la pregunta en
  display 28 px, dos ondas vivas (yo / ellos). A la derecha: ✕ en la esquina (= «Ahora no», Esc) y
  el botón rojo redondo (= «Grabar», Intro) con la cuenta atrás como anillo que se vacía y se
  vuelve violeta mientras el ratón está encima. Al confirmar: ✓ + anillo rojo completo → se
  encoge en la pastilla.
- **Pastilla** (200 × 40 → 296 al pasar): siempre de noche; punto que late + cronómetro display +
  dos mini ondas; al pasar, marcador · pausa · parar (disco rojo de la referencia). Provisional =
  anillo hueco + «PROVISIONAL»; pausa = cápsula ámbar con barras y cronómetro congelado.
- **Biblioteca**: dos paneles de cristal flotantes. Lista con grupos en versalitas y hablantes como
  puntos de color (no «3 hablantes»); búsqueda en píldora con resultados resaltados en violeta;
  visor con título display, chips de hablante (renombrar en línea, menú fusionar/huella), índice
  de marcadores en una línea, transcripción por turnos (el nombre solo al cambiar de hablante, la
  hora en el margen) y reproductor en cápsula flotante.
- **Ajustes**: navegación con iconos; título display por sección; grupos = versalita + una tarjeta;
  ayuda tras `?`, «se aplica al reiniciar» como icono ↻; «Avanzado» plegado con animación de
  altura; barra de guardado flotante que solo aparece con cambios; Diagnóstico con puntos vivos;
  «Acerca de» con «MICNAGA» y «Hecho por bitnaga».
- **Asistente**: el momento de marca — anillos violeta (los círculos del logo de Bitnaga) alrededor
  del punto de grabar, «MICNAGA» a 76 px, tres hechos en píldoras, «Hecho por bitnaga». Paso 2 con
  porcentaje en display y una tarjeta de modelos; paso 3 con dos ondas grandes que se ponen
  verdes al oír algo.
- **Bandeja**: discos de color + glifo (grafito/anillo = vigilando, rojo/punto = grabando,
  ámbar/barras = pausa, violeta/ondas = procesando). Icono de la app: disco nocturno con anillos
  violeta y el punto rojo.

## Qué se quitó y por qué

- Subtítulos/leads de cada sección de Ajustes y del asistente: repetían el título.
- El texto de ayuda visible bajo cada opción: ahora bajo demanda (`?`), y vacío donde el nombre basta.
- «Recuerda avisar de que grabas» como franja en el popup: ahora una marca ámbar mínima que se
  despliega (y en Ajustes > Privacidad, y en el asistente).
- Botones con texto del popup («Grabar ↵», «Ahora no Esc»): ✕ y botón de grabar redondo; los
  atajos siguen en `aria-keyshortcuts` y en el `title`.
- «N hablantes», «N reuniones», textos explicativos de estados vacíos y «Sin resultados… prueba…»:
  sustituidos por puntos, cifras display e iconos.
- Párrafo largo del consentimiento de huella: una frase con lo esencial (RGPD, cifrado, borrable,
  pide permiso) + casilla obligatoria.

## Tema claro

Se mantiene (spec: claro/oscuro según el sistema; referencias 4–5 son claras). Mismo sistema con
`light-dark()`: cristal blanco esmerilado, tinta casi negra, violeta más profundo. La pastilla es
siempre oscura en ambos temas.
