import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import run

VALID_DIRECTIONS = {'up', 'down', 'left', 'right'}


def board(*rows):
    """Arma el tablero en el formato del servidor: '|....|' por línea."""
    return '\n'.join('|' + r + '|' for r in rows)


def make_data(rows, side='A', **extra):
    data = {'board': board(*rows), 'side': side, 'game_id': 'g1', 'turn_token': 't1'}
    data.update(extra)
    return data


def apply(rows, direction, side='A'):
    """Posición a la que iría la cabeza propia tras `direction`."""
    head = run.scan([list(r) for r in rows], side)['my_head']
    dr, dc = run.DIRECTIONS[direction]
    return head[0] + dr, head[1] + dc


class FakeWebSocket:
    """Websocket de mentira: devuelve mensajes guardados y recuerda los envíos."""

    def __init__(self, incoming=()):
        self.incoming = list(incoming)
        self.sent = []

    async def recv(self):
        if not self.incoming:
            raise ConnectionError('closed')
        item = self.incoming.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item if isinstance(item, str) else json.dumps(item)

    async def send(self, message):
        self.sent.append(json.loads(message))


def quiet(func, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return func(*args, **kwargs)


def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return loop.run_until_complete(coro)
    finally:
        loop.close()


class NextTargetDigitTests(unittest.TestCase):
    def test_lowest_digit_without_predecessor(self):
        self.assertEqual(run.next_target_digit(['123', '...']), 1)

    def test_target_is_not_always_the_smallest(self):
        self.assertEqual(run.next_target_digit(['1678', '9...']), 6)

    def test_cyclic_wrap(self):
        self.assertEqual(run.next_target_digit(['89', '1.']), 8)

    def test_no_digits(self):
        self.assertIsNone(run.next_target_digit(['....', '....']))

    def test_every_digit_has_a_predecessor(self):
        self.assertIsNone(run.next_target_digit(['123456789']))


class LogTests(unittest.TestCase):
    def setUp(self):
        run.HISTORY.clear()

    def tearDown(self):
        run.HISTORY.clear()

    def test_log_event_and_action(self):
        run.log_event('x', {'a': 1})
        run.log_action('x', {'b': 2})
        self.assertEqual(run.HISTORY['x'], ['< {"a": 1}', '> {"b": 2}'])

    def test_write_game_log(self):
        run.log_event('abc', {'a': 1})
        old = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                quiet(run.write_game_log, 'abc')
                with open('game_abc.log') as f:
                    self.assertEqual(f.read(), '< {"a": 1}\n')
            finally:
                os.chdir(old)

    def test_write_game_log_survives_os_error(self):
        with mock.patch('builtins.open', side_effect=OSError('disk full')):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                run.write_game_log('abc')
        self.assertIn('could not write game log', out.getvalue())


class ParseAndScanTests(unittest.TestCase):
    def test_parse_board_string(self):
        grid = run.parse_board('header\n|ab.|\n|.X#|\n|12')
        self.assertEqual(grid, [list('ab.'), list('.X#'), list('12')])

    def test_parse_board_list(self):
        self.assertEqual(run.parse_board(['ab', 'cd']), [['a', 'b'], ['c', 'd']])

    def test_scan_side_a(self):
        grid = [list(r) for r in ('Aa.B', 'b#X3', '0..9')]
        info = run.scan(grid, 'A')
        self.assertEqual(info['my_head'], (0, 0))
        self.assertEqual(info['my_len'], 2)
        self.assertEqual(info['opp_head'], (0, 3))
        self.assertEqual(info['opp_len'], 2)
        self.assertEqual(info['digits'], {3: [(1, 3)], 9: [(2, 3)]})
        self.assertEqual(info['xs'], [(1, 2)])
        self.assertEqual(info['walls'], {(1, 1)})

    def test_scan_side_b(self):
        grid = [list(r) for r in ('Aa.B', 'b...')]
        info = run.scan(grid, 'B')
        self.assertEqual(info['my_head'], (0, 3))
        self.assertEqual(info['opp_head'], (0, 0))
        self.assertEqual(info['opp_len'], 2)


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.grid = [list(r) for r in ('....', '.##.', '....')]
        self.rows, self.cols = 3, 4
        self.passable = run.make_passable(self.grid, self.rows, self.cols)

    def test_passable(self):
        self.assertTrue(self.passable(0, 0))
        self.assertFalse(self.passable(1, 1))
        self.assertFalse(self.passable(-1, 0))
        self.assertFalse(self.passable(0, 4))

    def test_passable_free_and_blocked_cells(self):
        p = run.make_passable(self.grid, 3, 4, free_cells={(1, 1)}, blocked_cells={(0, 0)})
        self.assertTrue(p(1, 1))
        self.assertFalse(p(0, 0))

    def test_bfs_goes_around_walls(self):
        dist = run.bfs(self.passable, self.rows, self.cols, (1, 0))
        self.assertEqual(dist[(1, 0)], 0)
        self.assertEqual(dist[(1, 3)], 5)
        self.assertNotIn((1, 1), dist)

    def test_flood_count(self):
        self.assertEqual(run.flood_count(self.passable, 3, 4, (0, 0)), 10)

    def test_flood_count_respects_blocked_and_limit(self):
        self.assertEqual(run.flood_count(self.passable, 3, 4, (0, 0), blocked={(0, 0)}), 0)
        self.assertEqual(run.flood_count(self.passable, 3, 4, (1, 1)), 0)
        self.assertEqual(run.flood_count(self.passable, 3, 4, (0, 0), limit=3), 3)

    def test_spawn_expectancy(self):
        value = run.spawn_expectancy(self.passable, 3, 4, (0, 0))
        self.assertGreater(value, 0)
        self.assertLess(value, 10)

    def test_spawn_expectancy_without_distances(self):
        with mock.patch.object(run, 'bfs', return_value={}):
            self.assertEqual(run.spawn_expectancy(self.passable, 3, 4, (0, 0)), float(run.INF))


class GoalTests(unittest.TestCase):
    def test_x_value_at_end_of_game(self):
        self.assertEqual(run.x_value(0), run.X_SCORE)

    def test_x_value_is_capped(self):
        self.assertEqual(run.x_value(10 ** 6), run.X_SCORE + 100.0 * run.AVG_DIGIT_VALUE * run.MAX_FUTURE_DIGITS)

    def test_build_goals(self):
        goals = run.build_goals(['..'], [(0, 1)], {2: [(0, 0)], 5: [(1, 1)]}, 2, 3)
        self.assertEqual(goals[(0, 0)], (run.GOAL_DIGIT, 600.0))
        self.assertEqual(goals[(0, 1)][0], run.GOAL_X)
        self.assertNotIn((1, 1), goals)

    def test_build_goals_without_target(self):
        goals = run.build_goals(['..'], [(0, 1)], {}, None, 1)
        self.assertEqual(list(goals), [(0, 1)])


class ChooseDirectionTests(unittest.TestCase):
    def check(self, rows, side='A', expect=None, **extra):
        direction = run.choose_direction(make_data(rows, side, **extra))
        self.assertIn(direction, VALID_DIRECTIONS)
        if expect is not None:
            self.assertEqual(direction, expect)
        return direction

    def test_no_head_defaults_to_right(self):
        self.check(['.....', '.....'], expect='right')

    def test_eats_adjacent_correct_digit(self):
        self.check(['.....', '.A3..', '...B.'], expect='right')

    def test_never_steps_on_wrong_digit(self):
        # El objetivo es el 3 (no hay 2); el 7 es una trampa a la derecha.
        rows = ['.......', '.A7...3', '.....B.']
        direction = self.check(rows)
        self.assertNotEqual(direction, 'right')

    def test_never_walks_into_wall_when_free_cells_exist(self):
        rows = ['#####', '#A..#', '#####']
        self.check(rows, expect='right')

    def test_heads_toward_digit(self):
        rows = ['.......', '.A....3', '.......', 'B......']
        self.check(rows, expect='right')

    def test_player_b_uses_second_multiplier(self):
        rows = ['.......', '.B.5...', '..A....']
        self.check(rows, side='B', multiplier_1=1, multiplier_2=4)

    def test_digit_far_away_repositions(self):
        rows = [
            '....................',
            '.A..................',
            '....................',
            '....................',
            '....................',
            '...................3',
            '.................B..',
        ]
        self.check(rows)

    def test_far_digit_with_trailing_body_picks_safe_cell(self):
        rows = [
            '....................',
            '.Aaa................',
            '....................',
            '....................',
            '...................3',
            '.................B..',
        ]
        self.check(rows)

    def test_only_x_on_board(self):
        rows = ['.....', '.A..X', '....B']
        self.check(rows, expect='right')

    def test_x_on_the_way_to_digit_is_collected(self):
        rows = ['.......', '.A.X..4', '.......', '......B']
        self.check(rows, expect='right')

    def test_x_that_delays_digit_is_ignored(self):
        rows = ['..........', '.5........', '..........', '.A.......X', '.........B']
        self.check(rows, expect='up')

    def test_lost_race_repositions(self):
        # El rival está pegado al dígito y nosotros lejos.
        rows = [
            '..................',
            '.A................',
            '..................',
            '..................',
            '..................',
            '..................',
            '...............B3.',
        ]
        self.check(rows)

    def test_no_goals_surviving_picks_open_space(self):
        rows = ['.....', '.A...', '.....', '....B']
        self.check(rows)

    def test_no_food_reachable_pulls_toward_unreachable_space(self):
        # El dígito está tapiado por muros: no hay meta alcanzable.
        rows = [
            '.A....',
            '....B.',
            '######',
            '..3...',
        ]
        self.check(rows)

    def test_digit_walled_in_and_x_reachable(self):
        rows = [
            '.A....',
            '....B.',
            '..X...',
            '######',
            '..3...',
        ]
        self.check(rows)

    def test_corner_with_single_exit(self):
        rows = ['A.#', 'a.#', '###']
        self.check(rows, expect='right')

    def test_avoids_dead_end_pocket(self):
        # Hacia abajo hay una bolsa de 1 celda; hacia la derecha hay sitio.
        rows = [
            '#.#....',
            '#A.....',
            '###....',
            '......B',
        ]
        direction = self.check(rows)
        self.assertNotEqual(direction, 'up')

    def test_no_clean_exit_prefers_wall_over_body(self):
        rows = ['#a#', '#A#', 'aaa']
        direction = self.check(rows)
        # Salidas: arriba es cuerpo, izquierda/derecha son muros, abajo es cuerpo.
        self.assertIn(direction, ('left', 'right'))

    def test_no_clean_exit_picks_wall_with_most_space(self):
        rows = ['#.#', '#A#', 'a#a']
        self.check(rows)

    def test_completely_enclosed_by_bodies(self):
        rows = ['aaa', 'aAa', 'aaa']
        self.check(rows, expect='right')

    def test_stays_away_from_rival_head(self):
        rows = ['.......', '..A.B..', '.......']
        direction = self.check(rows)
        new = apply(rows, direction)
        self.assertGreater(abs(new[0] - 1) + abs(new[1] - 4), 1)

    def test_never_moves_into_rival_head(self):
        # La única casilla libre es el rival: se prefiere un muro (-500) a perder la partida.
        direction = self.check(['#####', '#AB.#', '#####'])
        self.assertNotEqual(direction, 'right')

    def test_long_snake_goes_for_max_space(self):
        body = 'a' * 15
        rows = [
            'A' + body[:5] + '.' * 10,
            '.' * 16,
            '.' * 16,
            body[5:] + '.' * 6,
            '...............B',
        ]
        self.check(rows)

    def test_long_snake_prefers_not_adjacent_to_rival(self):
        rows = [
            'Aaaaaaaaaa.......',
            'aaaaaa..B........',
            '.................',
        ]
        self.check(rows)

    def test_target_none_when_all_digits_chain(self):
        rows = ['A.........', '..........', '123456789.', '.........B']
        self.check(rows)

    def test_digit_equal_to_current_target_on_free_path(self):
        rows = ['.....', '.A.2.', '.....', '....B']
        self.check(rows, expect='right')

    def test_remaining_moves_none(self):
        rows = ['.....', '.A..X', '....B']
        self.check(rows, remaining_moves=None)

    def test_random_boards_always_return_a_direction(self):
        import random
        rng = random.Random(7)
        for _ in range(150):
            rows_n, cols_n = rng.randint(6, 14), rng.randint(6, 14)
            grid = [[rng.choice('.....#X123456789') for _ in range(cols_n)] for _ in range(rows_n)]
            cells = [(r, c) for r in range(rows_n) for c in range(cols_n)]
            rng.shuffle(cells)
            ha, hb = cells.pop(), cells.pop()
            grid[ha[0]][ha[1]] = 'A'
            grid[hb[0]][hb[1]] = 'B'
            for _ in range(rng.randint(0, 20)):
                r, c = cells.pop()
                grid[r][c] = rng.choice('ab')
            rows = [''.join(g) for g in grid]
            for side in 'AB':
                self.check(rows, side=side, remaining_moves=rng.choice([0, 40, 400]))


class FallbackTests(unittest.TestCase):
    def test_first_safe_direction_in_fixed_order(self):
        data = make_data(['.....', '.A...', '.....'])
        self.assertEqual(run.fallback_direction(data), 'up')

    def test_skips_edges_and_bodies(self):
        # Arriba es el borde, izquierda es cuerpo: la primera libre es abajo.
        data = make_data(['aA...', '.....'])
        self.assertEqual(run.fallback_direction(data), 'down')

    def test_wall_above_goes_down(self):
        data = make_data(['.#...', '.A...', '.....'])
        self.assertEqual(run.fallback_direction(data), 'down')

    def test_everything_blocked_returns_up(self):
        data = make_data(['###', '#A#', '###'])
        self.assertEqual(run.fallback_direction(data), 'up')

    def test_no_head_returns_up(self):
        self.assertEqual(run.fallback_direction(make_data(['...', '...'])), 'up')

    def test_garbage_input_returns_up(self):
        self.assertEqual(run.fallback_direction({'board': None}), 'up')
        self.assertEqual(run.fallback_direction({}), 'up')


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        run.HISTORY.clear()

    def tearDown(self):
        run.HISTORY.clear()

    def your_turn(self, **extra):
        data = make_data(['.....', '.A.3.', '....B'], **extra)
        return {'event': 'your_turn', 'data': data}

    def test_send_wraps_action_and_data(self):
        ws = FakeWebSocket()
        run_async(run.send(ws, 'ping', {'x': 1}))
        self.assertEqual(ws.sent, [{'action': 'ping', 'data': {'x': 1}}])

    def test_process_move_replies_with_move(self):
        ws = FakeWebSocket()
        run_async(run.process_move(ws, self.your_turn()))
        sent = ws.sent[0]
        self.assertEqual(sent['action'], 'move')
        self.assertEqual(sent['data']['game_id'], 'g1')
        self.assertEqual(sent['data']['turn_token'], 't1')
        self.assertIn(sent['data']['direction'], VALID_DIRECTIONS)
        self.assertTrue(run.HISTORY['g1'][0].startswith('> '))

    def test_safe_fallback_replies_with_move(self):
        ws = FakeWebSocket()
        run_async(run.safe_fallback(ws, self.your_turn()))
        self.assertEqual(ws.sent[0]['action'], 'move')
        self.assertIn(ws.sent[0]['data']['direction'], VALID_DIRECTIONS)

    def test_play_accepts_challenge(self):
        ws = FakeWebSocket([{'event': 'challenge', 'data': {'challenge_id': 42}}])
        run_async(run.play(ws))
        self.assertEqual(ws.sent, [{'action': 'accept_challenge', 'data': {'challenge_id': 42}}])

    def test_play_answers_your_turn(self):
        ws = FakeWebSocket([self.your_turn()])
        run_async(run.play(ws))
        self.assertEqual(len(ws.sent), 1)
        self.assertEqual(ws.sent[0]['action'], 'move')
        self.assertIn('g1', run.HISTORY)

    def test_play_falls_back_when_move_calculation_fails(self):
        ws = FakeWebSocket([self.your_turn()])
        with mock.patch.object(run, 'choose_direction', side_effect=RuntimeError('boom')):
            run_async(run.play(ws))
        self.assertEqual(len(ws.sent), 1)
        self.assertEqual(ws.sent[0]['action'], 'move')

    def test_play_game_over_writes_log_and_clears_history(self):
        ws = FakeWebSocket([
            self.your_turn(),
            {'event': 'game_over', 'data': {'game_id': 'g1'}},
        ])
        with mock.patch.object(run, 'write_game_log') as write:
            run_async(run.play(ws))
        write.assert_called_once_with('g1')
        self.assertNotIn('g1', run.HISTORY)

    def test_play_game_over_without_game_id_is_ignored(self):
        ws = FakeWebSocket([{'event': 'game_over', 'data': {}}])
        with mock.patch.object(run, 'write_game_log') as write:
            run_async(run.play(ws))
        write.assert_not_called()

    def test_play_ignores_other_events(self):
        ws = FakeWebSocket([{'event': 'list_users', 'data': []}])
        run_async(run.play(ws))
        self.assertEqual(ws.sent, [])

    def test_play_stops_on_keyboard_interrupt(self):
        ws = FakeWebSocket([KeyboardInterrupt()])
        run_async(run.play(ws))
        self.assertEqual(ws.sent, [])

    def test_play_stops_on_connection_error(self):
        ws = FakeWebSocket([])
        run_async(run.play(ws))
        self.assertEqual(ws.sent, [])


class FakeConnect:
    """Sustituto de websockets.connect como context manager asíncrono."""

    def __init__(self, ws=None, error=None):
        self.ws = ws
        self.error = error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self.ws

    async def __aexit__(self, *exc):
        return False


class StartTests(unittest.TestCase):
    def test_start_connects_with_token_and_plays(self):
        ws = FakeWebSocket([{'event': 'challenge', 'data': {'challenge_id': 1}}])
        calls = []

        def connect(uri):
            calls.append(uri)
            if len(calls) == 1:
                return FakeConnect(ws)
            return FakeConnect(error=KeyboardInterrupt())

        with mock.patch.object(run.websockets, 'connect', side_effect=connect):
            run_async(run.start('tok'))
        self.assertTrue(calls[0].endswith('?token=tok'))
        self.assertEqual(ws.sent[0]['action'], 'accept_challenge')

    def test_start_retries_after_error(self):
        results = [FakeConnect(error=OSError('down')), FakeConnect(error=KeyboardInterrupt())]
        sleep = mock.AsyncMock()
        with mock.patch.object(run.websockets, 'connect', side_effect=results), \
                mock.patch.object(run.asyncio, 'sleep', sleep):
            run_async(run.start('tok'))
        sleep.assert_awaited_once_with(3)


if __name__ == '__main__':
    unittest.main()
