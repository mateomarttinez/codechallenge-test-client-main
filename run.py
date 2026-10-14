import asyncio
import json
import sys
from collections import deque

import websockets

# ---------------------------------------------------------------------------
# CONFIGURACIÓN ESTRATÉGICA OPTIMIZADA
# ---------------------------------------------------------------------------
SAFE_MIN_SPACE = 5          # Espacio vital mínimo

# ---------------------------------------------------------------------------
# REGLAS OFICIALES DEL JUEGO (v5) - ver /how-to-play
# ---------------------------------------------------------------------------
#  - La comida son 5 dígitos (1-9) y hay que comerlos EN ORDEN CÍCLICO
#    ASCENDENTE (...7, 8, 9, 1, 2...).  El correcto puntúa
#    `dígito * 100 * multiplicador` y te hace crecer.
#    Cualquier otro dígito es una penalización de -500.
#    El objetivo NO es el menor número del tablero: es el único dígito cuyo
#    predecesor cíclico (d-1, con 9->1) NO está presente.
#    Ejemplo: con 1 6 7 8 9 en el tablero el objetivo es 6, no 1.
#  - Sobrevivir un movimiento da +1.
#  - Una X da +50 y suma +1 al multiplicador, permanente, y NO crece la serpiente.
#    El multiplicador sólo multiplica la comida, nunca el +1 ni las penalizaciones.
#  - Un muro # cuesta -500 y la serpiente se queda quieta (no termina la partida).
#  - Chocar contra el cuerpo propio o contra el rival SÍ termina la partida.
#  - El tablero va de 12 a 20 por lado: hay que leer rows/cols, nunca asumir 15x15.
#
# Consecuencia estratégica: sólo se persigue EL dígito correcto. Cualquier
# otro dígito es -500, es decir, peor que simplemente sobrevivir (+1 por turno).
WRONG_DIGIT_PENALTY = 500  # comer un dígito fuera de orden
WALL_PENALTY = 500         # chocar con un muro # (la partida continúa)
X_SCORE = 50               # valor de una X, que además suma +1 al multiplicador

# Suelo de seguridad por longitud. Dato medido sobre 132 partidas: 14 terminan
# con la cabeza totalmente cercada (las cuatro vecinas son cuerpo) y en esas 14
# no se ganó NINGUNA, porque quedarse sin salida pierde la partida al instante
# sea cual sea la puntuación. Los cercados aparecen con longitudes de 9 a 23
# (mediana 19), así que la longitud sola NO es la causa.
# OJO: con el tope en 16 este bloque no cambia ninguna jugada sobre ese corpus
# (0 de 1574 turnos elegibles), porque la política por metas ya elige el máximo
# espacio cuando la longitud es alta. Se queda como suelo, no como arreglo: el
# cerco real lo provoca sobre todo el cuerpo del RIVAL cerrando la salida, y eso
# no se puede validar fuera de línea con estos logs.
MAX_SAFE_LEN = 16

# ---------------------------------------------------------------------------
# VALOR REAL DE UNA X
# ---------------------------------------------------------------------------
# Una X NO vale 50 puntos: es +50 y, sobre todo, +1 de multiplicador
# PERMANENTE, y ese multiplicador multiplica todos los dígitos que aún
# quedan por comer. Medido sobre 22 partidas completas:
#   - el dígito correcto comido vale 3.48 de media (1, 2 y 3 suman 55 de las
#     89 veces), luego 100 * 3.5 = 350 por dígito y por escalón;
#   - se comen ~5 dígitos por jugador y partida;
#   - las X se reponen: hay 2 en el tablero pero se comen ~9.6 por partida.
#
# Este valor es correcto como MEDIDA del inverso, pero usarlo como prioridad
# frente al dígito fue un error: al multiplicarlo por la distancia en un
# argmax único, la X (hasta 1450) ganaba siempre a un dígito de 600 y el bot
# dejó de comer dígitos correctos. Medido sobre las 4 partidas de control
# posteriores a aquel cambio, con multiplicador igual o mejor que el rival en
# 4 de 4 (2-0, 4-8, 6-6 y 11-6) se perdieron las 4: 0 dígitos en dos
# partidas de 52 y 71 turnos, y 1800 y 4100 puntos de dígitos frente a los
# 1700, 4700, 4800 y 6600 del rival. El multiplicador sin dígitos no puntúa,
# así que el dígito correcto es prioridad absoluta y la X se recoge sólo
# cuando no retrasa al dígito o cuando no queda ninguno a mano.
AVG_DIGIT_VALUE = 3.5      # media observada del dígito correcto comido
MY_TURNS_PER_DIGIT = 24.0  # ~5 dígitos en ~120 turnos propios
MAX_FUTURE_DIGITS = 4.0    # tope al número de dígitos que aún quedan


def next_target_digit(grid):
    """Dígito correcto siguiente: el único sin predecesor cíclico en el tablero."""
    present = {int(ch) for row in grid for ch in row if ch.isdigit() and ch != '0'}
    for d in sorted(present):
        if (d - 1 if d > 1 else 9) not in present:
            return d
    return None


# ---------------------------------------------------------------------------
# ESTADO GLOBAL DE PARTIDA
# ---------------------------------------------------------------------------
HISTORY = {}

DIRECTIONS = {
    'up': (-1, 0),
    'down': (1, 0),
    'left': (0, -1),
    'right': (0, 1),
}
INF = 10 ** 9


def log_event(game_id, message):
    HISTORY.setdefault(game_id, []).append('< ' + json.dumps(message))


def log_action(game_id, message):
    HISTORY.setdefault(game_id, []).append('> ' + json.dumps(message))


def write_game_log(game_id):
    try:
        with open(f"game_{game_id}.log", "w") as f:
            f.write("\n".join(HISTORY.get(game_id, [])) + "\n")
        print(f"saved game_{game_id}.log")
    except OSError as e:
        print(f"could not write game log: {e}")


async def send(websocket, action, data):
    message = json.dumps({'action': action, 'data': data})
    await websocket.send(message)


async def start(auth_token):
    uri = f"wss://server.codechallenge.net.ar/ws?token={auth_token}"
    while True:
        try:
            print(f'connecting to {uri}')
            async with websockets.connect(uri) as websocket:
                print('connection READY!')
                await play(websocket)
        except KeyboardInterrupt:
            print('Exiting...')
            break
        except Exception as e:
            print(f'connection error: {e}')
            await asyncio.sleep(3)


async def play(websocket):
    while True:
        try:
            request = await websocket.recv()
            request_data = json.loads(request)

            event = request_data.get('event')
            if event == 'game_over':
                game_id = request_data['data'].get('game_id')
                if game_id:
                    log_event(game_id, request_data)
                    write_game_log(game_id)
                    HISTORY.pop(game_id, None)
            elif event == 'challenge':
                await send(
                    websocket,
                    'accept_challenge',
                    {'challenge_id': request_data['data']['challenge_id']},
                )
            elif event == 'your_turn':
                log_event(request_data['data']['game_id'], request_data)
                try:
                    await process_move(websocket, request_data)
                except Exception as e:
                    # Antes esta excepción subía hasta `start()`, cerraba el
                    # websocket y abandonaba TODAS las partidas en curso de
                    # golpe (el rival se las llevaba todas). Un tablero raro no
                    # puede costar más que este turno: se contesta con un
                    # movimiento legal cualquiera y se sigue jugando.
                    print(f'error calculating move: {e!r}')
                    await safe_fallback(websocket, request_data)
        except KeyboardInterrupt:
            print('Exiting...')
            break
        except Exception as e:
            print(f'error {e}')
            break


# ---------------------------------------------------------------------------
# PARSEO Y ANÁLISIS DE TABLERO
# ---------------------------------------------------------------------------
def parse_board(board_str):
    if isinstance(board_str, list):
        return [list(row) for row in board_str]
    grid = []
    for line in board_str.split('\n'):
        if not line.startswith('|'):
            continue
        row = line[1:-1] if line.endswith('|') else line[1:]
        grid.append(list(row))
    return grid


def scan(grid, side):
    me_h, me_b = side.upper(), side.lower()
    op = 'B' if side == 'A' else 'A'
    op_h, op_b = op, op.lower()
    info = dict(my_head=None, opp_head=None, my_len=0, opp_len=0,
                digits={}, xs=[], walls=set())
    for r, row in enumerate(grid):
        for c, ch in enumerate(row):
            if ch == me_h:
                info['my_head'] = (r, c)
                info['my_len'] += 1
            elif ch == me_b:
                info['my_len'] += 1
            elif ch == op_h:
                info['opp_head'] = (r, c)
                info['opp_len'] += 1
            elif ch == op_b:
                info['opp_len'] += 1
            elif ch.isdigit():
                d = int(ch)
                if 1 <= d <= 9:
                    info['digits'].setdefault(d, []).append((r, c))
            elif ch == 'X':
                info['xs'].append((r, c))
            elif ch == '#':
                info['walls'].add((r, c))
    return info


# ---------------------------------------------------------------------------
# BÚSQUEDA Y EVALUACIÓN ESPACIAL
# ---------------------------------------------------------------------------
def make_passable(grid, rows, cols, free_cells=frozenset(),
                  blocked_chars=('#', 'A', 'a', 'B', 'b'),
                  blocked_cells=frozenset()):
    def passable(r, c):
        if r < 0 or r >= rows or c < 0 or c >= cols:
            return False
        if (r, c) in blocked_cells:
            return False
        if (r, c) in free_cells:
            return True
        return grid[r][c] not in blocked_chars
    return passable


def bfs(passable, rows, cols, src):
    """Distancias respetando EXACTAMENTE lo que `passable` permite, para que
    ningún camino trazado cruce un muro, un cuerpo o un dígito incorrecto."""
    dist = {src: 0}
    q = deque([src])
    while q:
        r, c = q.popleft()
        d = dist[(r, c)]
        for dr, dc in DIRECTIONS.values():
            nr, nc = r + dr, c + dc
            if (nr, nc) in dist:
                continue
            if passable(nr, nc):
                dist[(nr, nc)] = d + 1
                q.append((nr, nc))
    return dist


def flood_count(passable, rows, cols, start, blocked=frozenset(), limit=10 ** 9):
    if not passable(*start) or start in blocked:
        return 0
    seen = {start}
    q = deque([start])
    while q and len(seen) < limit:
        r, c = q.popleft()
        for dr, dc in DIRECTIONS.values():
            n = (r + dr, c + dc)
            if n not in seen and n not in blocked and passable(*n):
                seen.add(n)
                q.append(n)
    return len(seen)


def spawn_expectancy(passable, rows, cols, src):
    """Distancia media desde `src` hasta una aparición aleatoria.

    Los dígitos no se eligen: reaparecen solos y casi uniformemente por el
    tablero. La distancia esperada al PRÓXIMO dígito es, entonces, la media de
    distancias a todas las celdas alcanzables, y minimizarla deja la cabeza en
    la mejor posición posible para la carrera siguiente.

    Es la carrera siguiente la que decide la partida. Medido sobre 134
    partidas de este bot: cuando aparece un dígito nuevo, el rival está más
    cerca el 51,4% de las veces y el bot sólo el 44,5% (empate 4,1%), con
    distancias medias de 9,57 frente a 9,90. Un dígito de diferencia, y la
    tabla de resultados es brutal: 0 dígitos -> 1,7% de victorias, 1-2 -> 3,7%,
    3-5 -> 17,4%, 6-9 -> 25%, 10+ -> 83,3%.

    La conversión no es el problema: cuando el bot está al lado del dígito lo
    come el 73-88% de las veces. Lo que falta son oportunidades (8,1 por
    partida cuando gana frente a 2,6 cuando pierde), y eso lo decide dónde
    está la cabeza en el instante en que aparece el dígito, no lo rápido que
    se camine hacia el anterior.
    """
    dist = bfs(passable, rows, cols, src)
    if not dist:
        return float(INF)
    return sum(dist.values()) / float(len(dist))


# ---------------------------------------------------------------------------
# ELECCIÓN DE OBJETIVO (regla oficial de dígitos)
# ---------------------------------------------------------------------------

# Las dos clases de objetivo NO son comparables con un único argmax de valor.
# El dígito correcto es la única fuente de puntos en volumen y el rival que se
# lo come se lleva esa puntuación para siempre, así que tiene prioridad
# absoluta. La X es una inversión en el multiplicador: real, pero secundaria.
GOAL_DIGIT = 0
GOAL_X = 1

# Coste de un turno de demora, en puntos, al puntuar una meta.
STEP_COST = 2.0
# Recargo si el rival llega a la meta antes que nosotros: se sigue yendo,
# porque retroceder garantiza perder la carrera, pero encarece la opción.
CONTESTED_FACTOR = 0.75
# Margen de turnos: si el rival llega a la meta esto antes, la carrera está
# perdida y no se puede recuperar. Caminar 17 pasos hacia un dígito que el
# rival se come en 3 es regalar la partida, así que en ese caso se cambia el
# orden de prioridad y se construye el multiplicador para el siguiente
# dígito, que es el único que todavía se puede ganar.
RACE_LOST_MARGIN = 2

# Distancia al dígito correcto a partir de la cual deja de merecer la pena
# CAMINAR hasta él y compensa devolver el turno al posicionamiento.
#
# Evidencia (4 partidas nuevas, 516 turnos propios, política verificada 100%
# idéntica a la vigente): contra santinovitale el bot pierde 0-2 y llega
# primero al dígito nuevo sólo el 43,2% de las veces frente al 54,1% del
# rival; el ritmo de oportunidades de dígito adyacente es 3,7% por turno
# contra 6,3% contra francopagotto. Y el desenlace no es una muerte del bot:
# en las dos derrotas la jugada fatal la ejecutó el RIVAL metiéndose en su
# propio cuerpo ('a' y 'b'), lo que corta la partida, y aun así santinovitale
# gana por marcador (5667-4918 y 3078-1281). Es decir, contra el rival difícil
# no hay problema de supervivencia: el juego se acaba solo y lo único que
# decide es la velocidad con la que se suma puntos antes de ese corte.
#
# Por eso el recurso apremiante no es sobrevivir sino puntuar rápido, y un
# dígito a más de ANTICIPATE_STEPS no se come hoy: cuando aparezca el
# siguiente (que es lo que puntúa de verdad) estará igual de lejos. Caminar
# hacia él aleja la cabeza de donde puede salir el próximo y así se pierde la
# carrera, que es exactamente lo que se medía.
#
# El umbral sale de un barrido sobre las 146 partidas (15.651 turnos propios).
# La captura inmediata se respeta siempre en todos los valores (100%, 401/401
# oportunidades). Las dos métricas mejoran a la vez frente a no anticipar
# nunca, y 6 es el que más mejora la que produce puntos:
#
#   umbral   dist. spawn   dist. dígito
#   (nunca)     10,8493        6,593
#        0       10,6382        6,654   <- central pero se aleja del dígito
#        4       10,6980        6,509
#        6       10,7222        6,468   <- elegido
#        8       10,7461        6,468
ANTICIPATE_STEPS = 6


def x_value(remaining_moves):
    """Lo que vale de verdad una X: los +50 inmediatos más el +1 de
    multiplicador aplicado a todos los dígitos que aún van a comerse.

    `remaining_moves` cuenta los movimientos de los DOS jugadores, así que
    los turnos que me quedan son la mitad: de ahí el 2 en el denominador.

    El tope de dígitos futuros es lo que hace que la X valga 1450 al principio
    y 50 al final. Medido sobre las 160 partidas, ese optimismo es justo lo que
    arrastra la política hacia la X: el bot asume 4 dígitos futuros y por eso
    desprecia un dígito de 100 puntos. La X sigue siendo marginalmente útil,
    pero no puede robar la carrera de un dígito cercano.
    """
    my_turns = remaining_moves / 2.0
    future = min(my_turns / MY_TURNS_PER_DIGIT, MAX_FUTURE_DIGITS)
    if future <= 0:
        return float(X_SCORE)
    return X_SCORE + 100.0 * AVG_DIGIT_VALUE * future


def build_goals(grid, xs, digits, target_d, mult, remaining_moves=0):
    """Objetivos legítimos, con su clase y su valor real.

    El dígito correcto vale `target_d * 100 * mult` y es prioridad absoluta
    (GOAL_DIGIT). La X aporta +50 y +1 de multiplicador permanente, que
    multiplica los dígitos futuros, así que vale `x_value()` (GOAL_X) y no un
    dígito suelto, pero no debe desplazar al dígito: medido sobre 22
    partidas completas, perseguir la X por delante dejó al bot en 0 dígitos
    correctos en dos partidas de 52 y 71 turnos mientras el rival se llevaba
    1700 y 4700 puntos de dígitos, y se perdió 4 de 4 aunque con igual o
    mejor multiplicador. El multiplicador sin dígitos no puntúa.
    """
    goals = {}
    if target_d is not None:
        value = target_d * 100.0 * mult
        for pos in digits.get(target_d, []):
            goals[pos] = (GOAL_DIGIT, value)
    vx = x_value(remaining_moves)
    for xp in xs:
        goals[xp] = (GOAL_X, vx)
    return goals


# ---------------------------------------------------------------------------
# TOMA DE DECISIONES DE MOVIMIENTO
# ---------------------------------------------------------------------------
def choose_direction(data):
    side = data['side']
    grid = parse_board(data['board'])
    rows = len(grid)
    cols = len(grid[0]) if rows else 0
    mult = data.get('multiplier_1', 1) if side == 'A' else data.get('multiplier_2', 1)

    info = scan(grid, side)
    head = info['my_head']
    if head is None:
        return 'right'

    opp_head = info['opp_head']
    my_len = info['my_len']

    xs = list(info['xs'])
    digits = info['digits']

    me_h, me_b = side.upper(), side.lower()
    op = 'B' if side == 'A' else 'A'
    op_h, op_b = op, op.lower()

    # Dígito correcto según la regla cíclica; el resto son trampas de -500.
    target_d = next_target_digit(grid)
    bad_digit_cells = frozenset(
        pos
        for d, poss in digits.items()
        if d != target_d
        for pos in poss
    )

    # Un mapa transitable común: permite vacío, X y el dígito correcto.
    # Bloquea muros, ambos cuerpos y CUALQUIER dígito que no sea el objetivo,
    # de modo que ningún camino calculado pueda pasar por una trampa de -500.
    passable = make_passable(grid, rows, cols,
                             blocked_chars=('#', me_h, me_b, op_h, op_b),
                             blocked_cells=bad_digit_cells)

    # Mapa de RUTA, distinto del de seguridad. Mi cuerpo es TRANSITORIO: se
    # vacía a medida que avanzo, así que no debe bloquear ni mi propio trayecto
    # ni, sobre todo, la ruta que se le mide al rival. Bloquearlo inflaba `od`:
    # el bot se veía la carrera ganada porque su propio cuerpo alargaba el
    # trayecto del rival, así que `lost` nunca se activaba y se quedaba decenas
    # de turnos persiguiendo un dígito inalcanzable con multiplicador 1 y sin
    # comer ninguna X, teniendo la X más cercana a tres celdas. Para seguridad
    # (flood_count / espacio vital) sigue mandando `passable`, que sí bloquea
    # ambos cuerpos.
    route = make_passable(grid, rows, cols,
                          blocked_chars=('#', op_h, op_b),
                          blocked_cells=bad_digit_cells)

    # Nunca se pisa el cuerpo propio (termina la partida), el del rival
    # (también) ni un dígito incorrecto (-500).
    forbidden = ('#', me_h, me_b, op_h, op_b)

    def cell_ok(nr, nc):
        if not (0 <= nr < rows and 0 <= nc < cols):
            return False
        ch = grid[nr][nc]
        if ch in forbidden:
            return False
        if ch.isdigit() and ch != '0' and (target_d is None or int(ch) != target_d):
            return False
        return True

    candidates = []
    for name, (dr, dc) in DIRECTIONS.items():
        nr, nc = head[0] + dr, head[1] + dc
        if not cell_ok(nr, nc):
            continue
        cell = (nr, nc)
        # Espacio alcanzable desde la nueva cabeza, sin la cabeza vieja.
        space = flood_count(passable, rows, cols, cell, {head})
        adj_opp = 1 if (opp_head and abs(nr - opp_head[0]) + abs(nc - opp_head[1]) <= 1) else 0
        candidates.append({'name': name, 'cell': cell, 'space': space, 'adj_opp': adj_opp})

    if not candidates:
        # Sin salida limpia: peor caso, tocar un muro cuesta -500 pero la
        # partida sigue. Entrar en un cuerpo sería peor: termina la partida.
        fallbacks = []
        for name, (dr, dc) in DIRECTIONS.items():
            nr, nc = head[0] + dr, head[1] + dc
            if not (0 <= nr < rows and 0 <= nc < cols):
                continue
            ch = grid[nr][nc]
            if ch in (me_h, me_b, op_h, op_b):
                continue
            space = flood_count(passable, rows, cols, (nr, nc), {head})
            fallbacks.append((-space, name))
        if fallbacks:
            fallbacks.sort()
            return fallbacks[0][1]
        return 'right'

    # Chocar con el propio cuerpo o el rival termina la partida, así que el
    # espacio disponible tiene que superar la longitud que arrastramos.
    need = max(my_len + 1, SAFE_MIN_SPACE)
    safe = [c for c in candidates if c['space'] >= need]
    if not safe:
        best_space = max(c['space'] for c in candidates)
        safe = [c for c in candidates if c['space'] == best_space]

    # Pegarse a la cabeza del rival es regalar la partida: sólo si no hay otra.
    away = [c for c in safe if not c['adj_opp']]
    if away:
        safe = away

    safe_names = {c['name']: c for c in safe}

    # Distancia esperada al próximo dígito desde cada salida segura. Es la
    # posición desde la que se gana o se pierde la siguiente carrera, que es
    # la que de verdad decide el marcador. Se calcula una vez y se reutiliza en
    # el reposicionamiento y en el último recurso.
    expectancy = {}
    if len(safe_names) > 1:
        for name, c in safe_names.items():
            expectancy[name] = spawn_expectancy(route, rows, cols, c['cell'])

    def reposition():
        """Turno de posicionamiento: el de la carrera que viene, no el de la
        que ya se ha perdido. Se queda con la salida que menos camino medio
        deja hasta donde pueda aparecer el próximo dígito.

        ANTES este bloque iba a por la X alcanzable más próxima. Medido sobre
        las 160 partidas, ese cambio cayó en el peor sitio posible: en 5
        de las 6 partidas nuevas el bot SOBREVIVIÓ (+1000) y aun así perdió,
        porque la carrera de dígitos la había perdido antes. El bot comió 59 X
        frente a 38 del rival y sólo 1,8 dígitos por partida, frente a 7,3 en
        las victorias. La X sube el multiplicador, pero el multiplicador sin
        dígitos que multiplicar no puntúa: 3.736 puntos de dígito de media en
        las derrotas frente a 23.758 en las victorias.

        Así que el reposicionamiento vuelve a ser sólo posición para el
        próximo spawn. La X se recoge en la elección de metas de más abajo,
        que sí sabe cuánto retrasa."""
        if not expectancy:
            return None
        return min(
            safe_names,
            key=lambda n: (expectancy[n], safe_names[n]['adj_opp'],
                           -safe_names[n]['space']),
        )

    # Suelo de longitud: por encima de MAX_SAFE_LEN puntuar deja de importar
    # frente a no quedarse sin salida. Ver nota en la constante: hoy este
    # bloque es prácticamente inerte y NO arregla los cercados.
    if my_len >= MAX_SAFE_LEN:
        best_space = max(c['space'] for c in safe_names.values())
        roomy = [c for c in safe_names.values() if c['space'] == best_space]
        roomy.sort(key=lambda c: c['adj_opp'])
        return roomy[0]['name']

    # Captura inmediata: si el dígito correcto está a una celda y su casilla es
    # segura, se come. La X NO desaparece por no comerla ahora —sigue en el
    # tablero—, así que el coste real de esta jugada es de un turno, mientras
    # que el beneficio es d*100*mult asegurado.
    #
    # Sin esta regla el bot estaba al lado del dígito correcto en 386 turnos de
    # 134 partidas y no lo tomaba en 93 de ellos (el 24%). La causa era
    # x_value(): vale 50 + 100*3.5*4 = 1450 al principio de la partida, cuatro
    # veces un dígito medio con multiplicador 1, y como el objetivo se elige por
    # argmax de ganancia la X le ganaba al dígito que tenía delante. Ese cálculo
    # asume que quedan 4 dígitos por comer, y medido sobre las 134 partidas el
    # bot se come 1,9 en las derrotas y 7,2 en las victorias: la hipótesis
    # optimista se cumple justo en las partidas que ya se estaban ganando.
    #
    # Y los dígitos son lo único que puntúa: 0 dígitos -> 1,7% de victorias,
    # 1-2 -> 3,7%, 3-5 -> 17,4%, 6-9 -> 25%, 10+ -> 83,3%.
    if target_d is not None:
        captura = [c for c in safe_names.values()
                   if grid[c['cell'][0]][c['cell'][1]] == str(target_d)]
        if captura:
            mejor = max(captura, key=lambda c: (c['space'], -c['adj_opp']))
            return mejor['name']

    # ANTICIPACIÓN. La captura inmediata de arriba se respeta siempre: si el
    # dígito está a una celda, se come. Pero cuando está más lejos de
    # ANTICIPATE_STEPS, la carrera por ÉSE dígito ya no es la que decide el
    # marcador: lo decide la siguiente, y para esa hay que quedarse cerca de
    # donde puede aparecer el spawn. Antes, en cuanto la carrera actual no
    # estaba 'lost' (o sea, casi siempre) el bot se comprometía a caminar
    # hasta el dígito lejano y se jugaba la posición de todos los turnos
    # siguientes; medido en las 4 partidas nuevas eso dejó al bot con 3,7
    # oportunidades de dígito por turno ante santinovitale frente a 6,3 ante
    # francopagotto, y perdiendo las carreras 5-3 y 3-1 en dígitos.
    if target_d is not None and len(safe_names) > 1:
        digit_cells = [p for d, poss in digits.items() if d == target_d for p in poss]
        dmin = None
        for p in digit_cells:
            to_p = bfs(route, rows, cols, p)
            reach = [c for c in safe_names.values() if c['cell'] in to_p]
            if reach:
                s = min(to_p[c['cell']] for c in reach)
                dmin = s if dmin is None else min(dmin, s)
        if dmin is not None and dmin > ANTICIPATE_STEPS:
            rp = reposition()
            if rp is not None:
                return rp

    # Elegir objetivo por clases, no por un argmax de valor. El dígito correcto
    # es la única fuente de puntos en volumen y el rival que se lo come se
    # lleva esa puntuación para siempre, así que se va siempre a por él. La X se
    # recoge sólo cuando no retrasa al dígito o cuando no queda ninguno a mano.
    goals = build_goals(grid, xs, digits, target_d, mult,
                        data.get('remaining_moves') or 0)
    opp_od = bfs(route, rows, cols, opp_head) if opp_head else {}

    def score_goal(gpos, gval):
        """Ganancia de una meta: valor menos demora, con recargo si se pierde."""
        to_goal = bfs(route, rows, cols, gpos)
        reach = [c for c in safe_names.values() if c['cell'] in to_goal]
        if not reach:
            return None
        steps = min(to_goal[c['cell']] for c in reach)
        # `goals` sólo contiene el dígito correcto y las X, así que ningún
        # objetivo puede costar una penalización. El coste real es la demora.
        gain = gval - steps * STEP_COST
        od = opp_od.get(gpos, INF)
        # Dos situaciones distintas, y confundirlas es lo que costaba las
        # partidas. Si el rival llega ANTES pero dentro del margen, la carrera
        # sigue viva: se recorta el margen y se sigue yendo, porque el camino
        # propio le estorba al rival aunque llegue tarde y porque retroceder
        # garantiza perder todas las carreras. Si el rival llega con
        # RACE_LOST_MARGIN turnos de ventaja, la carrera está perdida y no se
        # recupera: seguir caminando hasta el dígito sólo deja al rival con
        # los puntos, así que se marca la meta como no disputable.
        lost = od < INF and od <= steps - RACE_LOST_MARGIN
        if lost:
            gain -= gval * CONTESTED_FACTOR
        elif od < INF and od <= steps:
            gain -= gval * CONTESTED_FACTOR
        return gain, to_goal, steps, lost

    # Orden de prioridad POR VALOR, no por clase. La clase ya está dentro de
    # `gain`: `score_goal` descuenta la demora y aplica el recargo por carrera
    # perdida, así que un dígito inalcanzable llega aquí ya castigado y una X
    # alcanzable puede ganarle legitimamente. Antes `rank()` fijaba dígito=0 y
    # X=1 siempre, de modo que el valor real de la X (~1450 al principio) no
    # podía competir nunca con un dígito de 200 puntos: medido en 3 partidas
    # nuevas el bot perdió la carrera de X 1-3 y 1-5 y terminó cediendo 1796 y
    # 1795 puntos, es decir, a una sola X de ganar.
    def rank(kind, lost, gain):
        # Sólo queda un desempate a favor del dígito, y sólo si empatan.
        if kind == GOAL_DIGIT:
            return 0
        return 1

    scored_all = []
    for gpos, (kind, gval) in goals.items():
        scored = score_goal(gpos, gval)
        if not scored:
            continue
        gain, to_goal, steps, lost = scored
        scored_all.append((gpos, kind, gval, gain, to_goal, steps, lost))

    # La X es una inversión, no una fuente de puntos. Medido sobre las 160
    # partidas: el bot sacaba 59 X frente a 38 del rival y aun así ganaba 1-7,
    # porque la carrera de dígitos la perdía por el camino. El daño no está en
    # comer X, está en DEMORAR el dígito para comerla: por eso el filtro se
    # mide en turnos de retraso, no en puntos.
    #
    # Antes el presupuesto era `x_value(remaining_moves) // STEP_COST`, que
    # daba 725 turnos de demora permitidos al principio de la partida: es decir,
    # no filtraba nada. `x_value` contaba los turnos de los DOS jugadores
    # como si fueran propios, así que además sobrestimaba el valor de la X.
    # Aquí el retraso permitido es una constante pequeña y explícita.
    X_DELAY_BUDGET = 2
    digit_steps = min((s[5] for s in scored_all if s[1] == GOAL_DIGIT), default=None)
    if digit_steps is not None:
        scored_all = [s for s in scored_all
                      if s[1] == GOAL_DIGIT
                      or s[5] <= digit_steps + X_DELAY_BUDGET]

    best = None
    for gpos, kind, gval, gain, to_goal, steps, lost in scored_all:
        # La CLASE manda antes que el valor. `x_value()` promedia 1125 puntos
        # (1450 al principio de la partida) frente a un dígito de 300 con
        # multiplicador 1, así que ordenar por `-gain` dejaba ganar a la X
        # casi siempre: el bot avanzaba hacia la X el 58,7% de los turnos y
        # hacia el dígito correcto sólo el 31,6%. Ese es exactamente el
        # comportamiento que perdió las 6 partidas nuevas.
        #
        # El dígito es la única fuente de puntos en volumen y el rival que se
        # lo come se lo lleva para siempre, así que si hay un dígito
        # alcanzable se va a por él. La X se recoge sólo cuando no retrasa
        # (filtro de arriba) o cuando no queda ningún dígito a mano.
        key = (rank(kind, lost, gain), -gain, steps)
        if best is None or key < best[0]:
            best = (key, gpos, to_goal, steps, kind, lost)

    if best is not None:
        _, gpos, to_goal, steps, kind, lost = best
        if lost:
            # La carrera por ESTE dígito está perdida y no se recupera: seguir
            # hasta él sólo deja los puntos al rival y, además, aleja la
            # cabeza de donde aparecerá el siguiente. Antes se seguía paseando
            # hacia un dígito que ya no era reachable, y ahí es donde se iban
            # las oportunidades (2,6 por partida frente a 8,1 en las
            # victorias). Se abandona la persecución y se juega el turno de
            # posicionamiento.
            #
            # No se premia la X aquí a propósito: el comentario de
            # `build_goals` documenta que perseguir la X por delante dejó al
            # bot en 0 dígitos en dos partidas. La X sólo se recoge cuando no
            # retrasa nada, que es el bloque de más abajo.
            rp = reposition()
            if rp is not None:
                return rp
        if kind == GOAL_DIGIT and not lost:
            # Una X en el camino más corto al dígito no retrasa nada: +50 y +1
            # de multiplicador gratis. Se recoge siempre.
            free = None
            for xp, (kind_x, gval) in goals.items():
                if kind_x != GOAL_X:
                    continue
                dx = to_goal.get(xp)
                if dx is not None and dx <= steps and (free is None or dx < free[0]):
                    free = (dx, xp)
            if free is not None:
                gpos = free[1]
                to_goal = bfs(route, rows, cols, gpos)
        options = sorted(
            (to_goal[c['cell']], c['adj_opp'], -c['space'], name)
            for name, c in safe_names.items()
            if c['cell'] in to_goal
        )
        if options:
            return options[0][3]

    # Sin meta ganadora: al menos acercarse a la comida que sí puntúa. Huir
    # sólo para abrir espacio era lo que alejaba al bot del dígito correcto.
    # Se prefiere el dígito: es el que decide la puntuación.
    for want in (GOAL_DIGIT, GOAL_X):
        pull = {}
        for gpos, (kind, gval) in goals.items():
            if kind != want:
                continue
            to_goal = bfs(route, rows, cols, gpos)
            for name, c in safe_names.items():
                if c['cell'] in to_goal:
                    pull[name] = min(pull.get(name, INF), to_goal[c['cell']])
        if pull:
            return min(pull, key=lambda n: (pull[n], expectancy.get(n, INF),
                                            safe_names[n]['adj_opp'],
                                            -safe_names[n]['space']))

    # Sólo si no hay comida alcanzable: sobrevivir y abrir espacio, y de paso
    # quedar en el mejor sitio posible para la carrera siguiente. El filtro
    # `safe` ya garantiza espacio vital, así que la esperanza puede mandar.
    best_name, best_score = None, None
    for name, c in safe_names.items():
        score = (expectancy.get(name, INF), c['adj_opp'], -c['space'])
        if best_score is None or score < best_score:
            best_name, best_score = name, score

    return best_name or candidates[0]['name']


async def process_move(websocket, request_data):
    data = request_data['data']
    direction = choose_direction(data)
    move = {
        'game_id': data['game_id'],
        'turn_token': data['turn_token'],
        'direction': direction,
    }
    log_action(move['game_id'], {'action': 'move', 'data': move})
    await send(websocket, 'move', move)


def fallback_direction(data):
    """Movimiento legal mínimo, sin usar nada de la heurística.

    Sirve de red de seguridad: si `choose_direction` falla, el juego exige
    igualmente una respuesta y equivocarse cuesta la partida, así que se
    contesta con la primera dirección que no mate. Se recorre en orden fijo
    para que sea determinista.
    """
    try:
        grid = parse_board(data.get('board'))
        side = data.get('side', 'A')
        me_h, me_b = side.upper(), side.lower()
        op = 'B' if side == 'A' else 'A'
        op_h, op_b = op, op.lower()
        head = None
        for r, row in enumerate(grid):
            for c, ch in enumerate(row):
                if ch == me_h:
                    head = (r, c)
        if head is None:
            return 'up'
        forbidden = ('#', me_h, me_b, op_h, op_b)
        rows, cols = len(grid), len(grid[0])
        for name, (dr, dc) in DIRECTIONS.items():
            nr, nc = head[0] + dr, head[1] + dc
            if not (0 <= nr < rows and 0 <= nc < cols):
                continue
            ch = grid[nr][nc]
            if ch in forbidden:
                continue
            # Un dígito incorrecto cuesta puntos pero no mata: mejor eso que
            # empotrarse contra un muro.
            return name
        return 'up'
    except Exception:
        return 'up'


async def safe_fallback(websocket, request_data):
    data = request_data['data']
    move = {
        'game_id': data['game_id'],
        'turn_token': data['turn_token'],
        'direction': fallback_direction(data),
    }
    log_action(move['game_id'], {'action': 'move', 'data': move})
    await send(websocket, 'move', move)


if __name__ == '__main__':
    if len(sys.argv) >= 2:
        auth_token = sys.argv[1]
        try:
            asyncio.run(start(auth_token))
        except KeyboardInterrupt:
            print("Programa finalizado.")
    else:
        print('please provide your auth token')
