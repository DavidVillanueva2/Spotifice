# Reporte: implementación del Ejercicio 1 (v1) en Spotifice

## 1. Resumen ejecutivo

Se ha ampliado `media_render` con control de reproducción completo (pausa/reanudar, estado,
pista anterior, repetición) según `spotifice_v1.ice`, y los tres componentes han migrado del
slice v0 al v1. Se eliminó el contexto de "playing/not playing" basado en `is_playing()` y se
sustituyó por una **máquina de estados explícita** con **historial tipo pila**. Resultado:
**56 tests pasan** (25 originales + 31 nuevos) y una prueba end-to-end reproduce exactamente
la secuencia del enunciado.

---

## 2. Inventario de cambios

| Archivo | Tipo | Cambio |
|---|---|---|
| `spotifice_v0/spotifice_v1.ice` (raíz) | borrado | Eliminado el duplicado de la raíz; se usa el de `spotifice_v0/` |
| `spotifice_v0/media_render.py` | modificado | **+167/−51** — núcleo de la v1 |
| `spotifice_v0/media_control.py` | modificado | **+50/−21** — demo de la secuencia del enunciado |
| `spotifice_v0/media_provider.py` | modificado | **+1/−1** — carga el slice v1 |
| `spotifice_v0/Makefile` | modificado | `check-slice` valida la v1 |
| `spotifice_v0/test/test_render_v1.py` | nuevo | 31 tests |
| `e1_en.md` | sin trackear | El enunciado (no se ha tocado) |

Sin commit todavía; el árbol tiene 4 ficheros modificados y 2 sin trackear.

---

## 3. Teoría base: qué es ZeroC Ice (para leer el código)

- **IDL / slice**: los `.ice` son *declaraciones* de interfaces (contratos), no implementación.
  `Ice.loadSlice(...)` genera en runtime las clases Python (`Spotifice`,
  `Spotifice.MediaRenderPrx`, etc.). El `module Spotifice` del slice se convierte en el módulo
  Python `Spotifice`.
- **Servant / proxy / adapter / identidad**: cada proceso publica un *servant* (la clase que
  implementa la interfaz, p. ej. `MediaRenderI`) en un *object adapter* con una **identidad**
  (`mediaRender0`). Los clientes hablan con un **proxy** (`mediaRender0:tcp -p 10001`). Aquí
  `media_control` es un cliente puro que obtiene los proxies de `control.config` mediante
  `propertyToProxy`.
- **`idempotent`**: en el slice, marca operaciones que pueden reejecutarse sin daño (Ice puede
  usarlo para reintentos). En la v1 son idempotentes `stop()`, `pause()`, `get_status()`,
  `set_repeat()`; **`play()` y `previous()` no lo son** (su segundo efecto no es neutro).
- **`Current`**: dentro de una invocación, Ice inyecta `current`; `current.id` es la
  **identidad del objeto invocado**. Ese truco es clave: el render usa su propia identidad como
  *clave* del stream en el provider (`open_stream(current.id, ...)`), sin necesitar conocerla de
  antemano.
- **Clases vs structs y nulabilidad**: `TrackInfo` y `PlaybackStatus` son `class`, que en Ice
  son **referencias que pueden ser nulas**. Por eso `current_track = None` es válido y
  `get_status()` puede devolver "sin pista".
- **Excepciones**: `Error` con `item` (opcional) y `reason` son *postcondiciones de contrato*:
  la Tabla 2 fija el texto exacto de `reason`, y los tests lo verifican literalmente.

---

## 4. Teoría central: máquina de estados

La base modelaba solo dos situaciones con `player.is_playing()`. La v1 introduce tres estados,
es decir, un **autómata finito determinista**:

```
        play() [pista cargada]            pause()
STOPPED ──────────────────────► PLAYING ──────────► PAUSED
   ▲                               ▲   ◄────────────  │
   │            stop()             │      play()      │
   └───────────────────────────────┴──────────────────┘
                          stop()
```

**Por qué no derivar el estado del pipeline**: `GstPipeline.set_state()` es **asíncrono**
(devuelve `ASYNC`) y consultar `pipeline.get_state()` bloquea hasta 1 s. Usar eso como fuente de
verdad daría carreras y lentitud. Solución: el **servant mantiene la verdad** (`self.state`) y
solo *ordena* cambios al player; el player confirma con un `Future` (patrón *command + ack*).

**Invariante principal**: `self.state` y la realidad del player se reconcilian tras cada
operación, y en caso de fin de pista mediante callback.

---

## 5. Cambios en `media_render.py`, uno a uno (con la teoría)

### 5.1 Estado e historial en `__init__`

```python
self.render_id = None
self.history = []
self.repeat = False
self.state = Spotifice.PlaybackState.STOPPED
```

- **Single source of truth**: el estado y el historial viven en el servant (memoria del
  servidor), no en el cliente ni en GStreamer.
- **Historial = pila (LIFO)**: la última pista cargada es la actual; `previous()` equivale a
  hacer *pop* conceptual. Teoría: una pila modela "volver al estado anterior" y **descarta el
  camino** que dejas (no hay *redo*), que es justo lo que pide la Sección 3 del enunciado.

### 5.2 `open_and_play(render_id)` — extraer la "acción" del autómata

Unifica los tres sitios que debían empezar reproducción (play desde STOPPED, al cambiar de pista
estando en PLAYING, y al repetir). Evita **duplicación de lógica** (DRY) y garantiza que todos
pasen por la misma secuencia:

```
open_stream → player.play(chunk_reader, on_exhausted) → confirm_play_starts() → state = PLAYING
```

Teoría: es la **transición** del autómata "hacia PLAYING"; concentrarla hace el sistema más fácil
de razonar.

### 5.3 `exhausted_handler(render_id)` — concurrencia y repetición

```python
def on_track_exhausted():
    if not self.repeat:
        self.state = STOPPED
        return
    try: self.open_and_play(render_id)
    except Ice.Exception: self.state = STOPPED
```

- `GstPlayer` avisa del fin de pista en **otro hilo** (no en el hilo del pipeline), por eso el
  manejador es un *closure* y debe ser robusto: si el reinicio falla, se deja el estado en STOPPED
  en vez de "mentir".
- Teoría: **callback asíncrono** con *error containment* (no propagar excepciones a un hilo sin
  gestor, que abortaría el proceso).
- `render_id` se **captura** en la clausura: el callback no tiene `Current` (no es una invocación
  remota), así que no puede obtener la identidad de Ice.

### 5.4 `teardown_playback()` — liberación de recursos

Cierra el stream en el provider y para el pipeline. Teoría: **manejo determinista de recursos**
(evitar fugas: streams abiertos en el provider, pipelines vivos en GST). El `close_stream` es
idempotente por contrato, así que llamarlo dos veces es seguro.

### 5.5 `switch_track(track)` — cambio de pista como transacción

```python
previous_state = self.state
if previous_state != STOPPED: self.teardown_playback()
self.current_track = track
if previous_state == PLAYING: self.open_and_play(self.render_id)
else: self.state = previous_state          # STOPPED o PAUSED
```

- Implementa la Tabla 1 para `load_track()`/`previous()`: **preserva el estado** (PLAYING →
  reproduce; PAUSED → queda pausada; STOPPED → queda parada).
- **Por qué se elimina `keep_playing_state`**: el `contextmanager` original solo distinguía
  "sonaba / no sonaba". Con un tercer estado (PAUSED) ya no es suficiente: pausar y luego cargar
  pista debe quedar en pausa, no en parada ni en reproducción. Se sustituye por un patrón
  *"guardar estado → mutar → restaurar"*.

### 5.6 `load_track()` — atomicidad y regla de recarga

```python
track = self.provider.get_track_info(track_id)   # 1º consultar (puede fallar)
if self.current_track and track.id == self.current_track.id:
    return                                        # recargar la actual: no-op
self.switch_track(track)                          # 2º mutar
self.history.append(track.id)                     # 3º registrar
```

- **Fetch-before-mutate** = *fail-fast / all-or-nothing*: si el provider lanza `TrackError`, no
  se ha tocado ni el estado ni el historial.
- **Recargar la pista actual** no toca el historial (excepción de la Sección 3). Es una forma de
  **idempotencia a nivel de historial**.
- Se **añade al historial incluso si repite** una pista anterior (solo se excluye la pista
  *actual*).

### 5.7 `play()` — orden de validación y "reanudar vs reiniciar"

Validaciones en orden contractual (Tabla 2): provider → pista → estado.

```python
if state == PLAYING: raise PlayerError("Already playing")
if state == PAUSED and player.is_playing():
    player.resume(); state = PLAYING; return
self.open_and_play(current.id)
```

- **Dos naturalezas de PAUSED**: (a) *pausa real*, con pipeline vivo → `resume()` reanuda **en el
  mismo punto** (requisito); (b) *pausa lógica*, tras cambiar de pista estando en pausa (sin
  pipeline) → hay que **empezar de cero** (la pista nueva siempre suena desde el principio).
- Teoría: distinguir "estado lógico" de "recurso materializado"; el `and
  self.player.is_playing()` es la comprobación de si el recurso existe.

### 5.8 `pause()` / `stop()` — idempotencia y contrato

- `pause()`: STOPPED → `PlayerError("Cannot pause when stopped")`; PAUSED → *no-op*; PLAYING →
  pausa.
- `stop()`: STOPPED → *no-op* (idempotente, ya no lanza error como antes); en PLAYING/PAUSED →
  teardown → STOPPED **conservando la pista** (`current_track` no se toca) para que `play()`
  posterior la reinicie.

### 5.9 `previous()` — modelo de la pila

```python
if len(self.history) < 2: return                 # no hay anterior: no-op
track = self.provider.get_track_info(self.history[-2])   # puede fallar
self.switch_track(track)                          # preserva estado
self.history.pop()                                # descarta la actual
```

- `history[-2]` es la candidata; `pop()` implementa "la pista que dejas atrás se descarta".
- De nuevo **fetch-before-mutate**: "si `previous()` falla, el historial no cambia".

### 5.10 `get_status()` / `set_repeat()`

- `get_status()` devuelve un **snapshot inmutable** (`PlaybackStatus`) del estado, pista actual
  (o null) y `is_repeating`. Es idempotente y de solo lectura.
- `set_repeat()` solo cambia una bandera; el efecto se materializa al **fin de pista** (en el
  callback).

### 5.11 Historial en `bind`/`unbind`

`self.history.clear()` en ambos: el enunciado lo exige. Teoría: el historial es *contexto ligado
al provider*; al cambiar de provider, ese contexto deja de ser válido (pistas de otra
biblioteca).

---

## 6. Teoría de concurrencia ya presente y cómo la usa la v1

- `GstPlayer` es un **hilo con cola de comandos** y `Future`: patrón *command queue* + *ack*
  (`send_command`/`wait_result`). El servant pide "PLAY/STOP/PAUSE/RESUME" y **espera
  confirmación** con timeout; así no asume que la orden se aplicó.
- **Detección de comandos obsoletos**: `GstPlayer` guarda `self.appsrc` y compara `src is not
  self.appsrc` para ignorar eventos de streams viejos (*stale*). Teoría: es un **token/epoch**
  que resuelve el problema "ABA" de eventos asíncronos.
- **Fin de pista vs error**: `EXHAUSTED` (bien) ≠ `ABORTED` (fallo de lectura). Solo el primero
  dispara la repetición. Teoría: separar "evento esperado de dominio" de "error".

---

## 7. Teoría de streaming (contexto, no modificado)

- **Modelo pull**: el `appsrc` pide datos (`need-data`) → el render pide un `get_chunk` al
  provider por Ice → el provider transcodifica mp3→Ogg/Opus a 64 kbps y entrega bytes. Es
  *backpressure*: nadie produce más rápido de lo que el sink consume.
- **Multiplexación por identidad**: el provider mantiene un `dict` `render_id → StreamedFile`;
  por eso el render manda su identidad y cada render tiene su propio stream.

---

## 8. Trazabilidad de requisitos → código

### Tabla 1 (transiciones)

| Operación | Estado | Código |
|---|---|---|
| `play()` | STOPPED | `open_and_play` |
| `play()` | PLAYING | `raise PlayerError("Already playing")` |
| `play()` | PAUSED | `player.resume()` |
| `pause()` | STOPPED | `raise PlayerError("Cannot pause when stopped")` |
| `pause()` | PLAYING | `player.pause()` → PAUSED |
| `pause()` | PAUSED | `return` (idempotente) |
| `stop()` | STOPPED | `return` |
| `stop()` | PLAYING/PAUSED | `teardown_playback()` → STOPPED |
| `load_track()` | cualquiera | `switch_track()` preserva estado |
| `previous()` | cualquiera | `switch_track()` preserva estado |

### Tabla 2 (mensajes exactos)

`"No MediaProvider bound"`, `"No track loaded"`, `"Already playing"`,
`"Cannot pause when stopped"` — literales en el código y verificados por tests.

### Tabla 3 (historial) — evolución de la pila

| Secuencia | Pila final | Actual |
|---|---|---|
| `A,B,prev` | `[A]` | A |
| `A,B,C,prev,prev` | `[A]` | A |
| `A,B,prev,prev` | `[A]` | A |
| `A,B,B,prev` | `[A]` (el 2º B no se apila) | A |
| `A,B,prev,C,prev` | `[A]` | A |

---

## 9. Teoría de los tests añadidos

- **Tests de integración reales**, no mocks: `icetest.py` levanta los servidores Ice **en hilos**
  (`ThreadIceServer`) y crea proxys; se ejercita la pila completa (Ice + GStreamer).
- **Espera por condición, no por `sleep` fijo**: `pool_condition(fn, ntries, interval)` reintenta
  hasta que se cumple. Teoría: evita *flakiness* por **condiciones de carrera** y por tiempos no
  deterministas (arranque del pipeline, decodificación).
- Cobertura: cada celda relevante de la Tabla 1, los 4 mensajes exactos de la Tabla 2, repetición
  (fin de pista con/sin `repeat`), preservación de estado en `load_track`/`previous`, y los 5
  ejemplos de la Tabla 3.
- El caso "repite" es intrínsecamente temporal: se comprueba que tras varios segundos sigue en
  PLAYING (con `repeat`) y que sin `repeat` acaba en STOPPED.

---

## 10. Verificación realizada

| Comprobación | Resultado |
|---|---|
| `pytest test/` (suite completa) | **56 passed** (25 base + 31 nuevos) |
| End-to-end provider + render + control | Reproduce el Listing 2 exactamente |
| `make check-slice` (`slice2html` sobre v1) | OK |
| Compilación de los módulos (`py_compile`) | OK |
| `ruff` | **No ejecutado**: no está instalado en el sistema (dependencia opcional `pipx:ruff`) |

Salida real del e2e:

```
after load_track(track1): STOPPED, track=1s, repeat=False
after play():             PLAYING, track=1s, repeat=False
after load_track(track2): PLAYING, track=2s, repeat=False
after pause():            PAUSED,  track=2s, repeat=False
after previous():         PAUSED,  track=1s, repeat=False
after play():             PLAYING, track=1s, repeat=False
after set_repeat(True):   PLAYING, track=1s, repeat=True
after stop():             STOPPED, track=1s, repeat=True
```

---

## 11. Decisiones y notas

- **Corregí un typo** existente: `"Strean setup failed"` → `"Stream setup failed"` (no lo fija la
  Tabla 2). Si se quiere máxima fidelidad al original, revertirlo.
- **`media_control.main()`** se ha adaptado (la Sección 5 del enunciado lo pide), **manteniendo su
  firma y los nombres de las propiedades de config**, que es lo que el enunciado protege.
- **`main()` de provider y render intactos** (verificado con `git diff`).
- El `spotifice_v0.ice` sigue en el repo como referencia, pero ya **no lo usa nadie**;
  `check-slice` ahora valida la v1.
- Pendiente: **commit y push** a `DavidVillanueva2/Spotifice`.
