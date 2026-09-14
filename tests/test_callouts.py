import json
import os
import sys
sys.path.insert(1, os.path.abspath('.'))

import gamedaybot.espn.callouts as callouts


class FakeTeam:
    def __init__(self, team_id, team_name, team_abbrev=None):
        self.team_id = team_id
        self.team_name = team_name
        self.team_abbrev = team_abbrev or team_name[:4].upper()

    def __repr__(self):
        return 'FakeTeam(%s)' % self.team_name


class FakeBox:
    def __init__(self, home, home_score, away, away_score, home_lineup=None, away_lineup=None):
        self.home_team = home
        self.home_score = home_score
        self.away_team = away
        self.away_score = away_score
        self.home_lineup = home_lineup or []
        self.away_lineup = away_lineup or []


class FakePlayer:
    """Just enough of a BoxPlayer for optimal_lineup_score."""

    def __init__(self, position, slot_position, points, name='P'):
        self.position = position
        self.slot_position = slot_position
        self.points = points
        self.name = name
        self.projected_points = points
        self.game_played = 100
        self.on_bye_week = False
        self.injuryStatus = 'ACTIVE'


A = FakeTeam(1, 'Alpha')
B = FakeTeam(2, 'Bravo')
C = FakeTeam(3, 'Charlie')
D = FakeTeam(4, 'Delta')
MAP = {1: '111', 2: '222', 3: '333'}  # Delta is deliberately unmapped


class TestParseTeamMentions:
    def test_parses_pairs(self):
        assert callouts.parse_team_mentions('1:111,2:222') == {1: '111', 2: '222'}

    def test_tolerates_whitespace_and_blanks(self):
        assert callouts.parse_team_mentions(' 1 : 111 , ,2:222, ') == {1: '111', 2: '222'}

    def test_skips_malformed_entries(self):
        assert callouts.parse_team_mentions('1:111,nope,3:abc,4') == {1: '111'}

    def test_empty(self):
        assert callouts.parse_team_mentions('') == {}
        assert callouts.parse_team_mentions(None) == {}

    def test_user_id_kept_as_string(self):
        # Snowflakes exceed 2**53; keeping them as text avoids any float detour.
        assert callouts.parse_team_mentions('1:719929655318020147') == {1: '719929655318020147'}


class TestMentions:
    def test_mapped_team_becomes_user_mention(self):
        assert callouts.Mentions(MAP).team(A) == '<@111>'

    def test_unmapped_team_falls_back_to_name(self):
        assert callouts.Mentions(MAP).team(D) == 'Delta'

    def test_role(self):
        assert callouts.Mentions(MAP, '999').role() == '<@&999>'
        assert callouts.Mentions(MAP).role() == ''
        assert callouts.Mentions(MAP, '  ').role() == ''

    def test_silent_style_keeps_markup_but_not_notifications(self):
        m = callouts.Mentions(MAP, style='silent')
        assert m.team(A) == '<@111>' and not m.notify_users and m.has_teams

    def test_names_style_uses_team_names(self):
        m = callouts.Mentions(MAP, style='names')
        assert m.team(A) == 'Alpha' and not m.notify_users and m.has_teams

    def test_ping_style_default(self):
        assert callouts.Mentions(MAP).notify_users
        assert callouts.Mentions(MAP, style='bogus').style == 'ping'

    def test_parse_mention_style(self):
        assert callouts.parse_mention_style(' Silent ') == 'silent'
        assert callouts.parse_mention_style('') == 'ping'
        assert callouts.parse_mention_style('nope') == 'ping'

    def test_has_teams(self):
        assert callouts.Mentions(MAP).has_teams
        assert not callouts.Mentions({}).has_teams
        assert not callouts.Mentions(None).has_teams


def P(points, projected=None, played=True, slot='WR'):
    """A starter: points scored (if played) and a projection."""
    p = FakePlayer('WR', slot, points if played else 0.0)
    p.projected_points = projected if projected is not None else points
    p.game_played = 100 if played else 0
    return p


def live(home, home_players, away, away_players):
    """A matchup whose scores are the sum of the played starters' points."""
    def total(players):
        return sum(p.points for p in players if p.slot_position not in ('BE', 'IR'))
    return FakeBox(home, total(home_players), away, total(away_players), home_players, away_players)


def played(n, points=10.0):
    return [P(points) for _ in range(n)]


def left(n, projected=10.0):
    return [P(0, projected, played=False) for _ in range(n)]


class TestLiveCallouts:
    """Nine starters a side, like a standard lineup."""

    M = callouts.Mentions(MAP)

    def test_no_mapping_means_no_callouts(self):
        boxes = [live(A, played(9, 20), B, played(7, 5) + left(2))]
        assert callouts.live_callouts(boxes, callouts.Mentions({})) == ''

    def test_silent_until_half_the_week_has_played(self):
        # Friday morning: one Thursday-night player a side has played and
        # the team ahead is projected to lose. Nothing is said.
        boxes = [live(A, [P(29.7)] + left(8, 10), B, [P(0.0)] + left(8, 14)),
                 live(C, played(1) + left(8), D, played(1) + left(8))]
        assert callouts.live_callouts(boxes, self.M) == ''

    def test_silent_when_nothing_played(self):
        boxes = [live(A, left(9), B, left(9))]
        assert callouts.live_callouts(boxes, self.M) == ''

    def test_destroying_when_projected_winner_leads_and_trailer_is_nearly_done(self):
        # A: 8 played x 20 = 160, one left projected 10 -> proj 170.
        # B: 7 played x 10 = 70, two left projected 10 -> proj 90.
        boxes = [live(A, played(8, 20) + left(1), B, played(7) + left(2))]
        assert callouts.live_callouts(boxes, self.M) == \
            '🔥 <@111> is DESTROYING <@222>, up 90.00 with <@222> 2 players left'

    def test_destroying_trailer_out_of_players(self):
        boxes = [live(A, played(8, 20) + left(1), B, played(9, 10))]
        assert callouts.live_callouts(boxes, self.M) == \
            '🔥 <@111> is DESTROYING <@222>, up 70.00 with <@222> out of players'

    def test_no_destroying_when_trailer_has_too_many_left(self):
        # Same margin, but B still has three to play.
        boxes = [live(A, played(8, 20) + left(1), B, played(6) + left(3))]
        assert '🔥' not in callouts.live_callouts(boxes, self.M)

    def test_no_destroying_when_projected_margin_too_small(self):
        # A: 90 done. B: 35 on the board, two left projected 13 -> 61. Margin 29, not 30.
        boxes = [live(A, played(9, 10), B, played(7, 5.0) + left(2, 13.0))]
        assert '🔥' not in callouts.live_callouts(boxes, self.M)

    def test_no_destroying_when_board_leader_is_projected_to_lose(self):
        # A leads 90-35 on the board but B has two 40-point players left.
        boxes = [live(A, played(9, 10), B, played(7, 5.0) + left(2, 40.0))]
        text = callouts.live_callouts(boxes, self.M)
        assert '🔥' not in text
        assert text == '🔄 <@111> is up 55.00 on <@222>, but <@222> is still projected to win by 25.00 with 2 players left'

    def test_comeback_needs_the_board_leader_to_be_nearly_done(self):
        # Week is 69% played, but A's 100-20 lead is only because A had five
        # on the early slate and B two. A still has four to play: no line.
        boxes = [live(A, played(5, 20) + left(4, 10.0), B, played(2, 10) + left(7, 20.0)),
                 live(C, played(9), D, played(9))]
        assert callouts.live_callouts(boxes, self.M) == ''

    def test_comeback_needs_most_of_the_matchup_played(self):
        # A is done and projected to lose, but B has seven left (10 of 18 played).
        boxes = [live(A, played(9, 5), B, played(1, 10) + left(7, 10.0) + [P(0, 10.0, played=False)]),
                 live(C, played(9), D, played(9))]
        assert '🔄' not in callouts.live_callouts(boxes, self.M)

    def test_comeback_needs_a_real_lead(self):
        # A up 10 on the board, projected to lose: not worth a line.
        boxes = [live(A, played(9, 10), B, played(7, 80 / 7) + left(2, 40.0))]
        assert '🔄' not in callouts.live_callouts(boxes, self.M)

    def test_finished_matchup_stays_quiet(self):
        boxes = [live(A, played(9, 20), B, played(9, 5))]
        assert callouts.live_callouts(boxes, self.M) == ''

    def test_coin_flip_when_mostly_played_and_tight(self):
        # 16 of 18 played, projected 91 vs 90.
        boxes = [live(A, played(8, 10) + left(1, 11.0), B, played(8, 10) + left(1, 10.0))]
        assert callouts.live_callouts(boxes, self.M) == \
            '😬 <@111> vs <@222> is a coin flip, projected within 1.00 with 2 players left between them'

    def test_coin_flip_needs_most_of_the_matchup_played(self):
        # Tight projection but only 10 of 18 have played (56%).
        boxes = [live(A, played(5, 10) + left(4, 10.0), B, played(5, 10) + left(4, 10.25))]
        assert callouts.live_callouts(boxes, self.M) == ''

    def test_coin_flip_needs_a_tight_projection(self):
        boxes = [live(A, played(8, 10) + left(1, 16.0), B, played(8, 10) + left(1, 10.0))]
        assert callouts.live_callouts(boxes, self.M) == ''

    def test_coin_flip_singular_player(self):
        boxes = [live(A, played(9, 10), B, played(8, 10) + left(1, 11.0))]
        assert callouts.live_callouts(boxes, self.M) == \
            '😬 <@111> vs <@222> is a coin flip, projected within 1.00 with 1 player left between them'

    def test_all_three_lines_in_order_from_different_matchups(self):
        boxes = [
            live(C, played(8, 10) + left(1, 11.0), D, played(8, 10) + left(1, 10.0)),   # coin flip
            live(A, played(8, 20) + left(1), B, played(9, 10)),                         # destroying
            live(C, played(9, 10), A, played(7, 5.0) + left(2, 40.0)),                  # comeback
        ]
        assert callouts.live_callouts(boxes, self.M).splitlines() == [
            '🔥 <@111> is DESTROYING <@222>, up 70.00 with <@222> out of players',
            '🔄 <@333> is up 55.00 on <@111>, but <@111> is still projected to win by 25.00 with 2 players left',
            '😬 <@333> vs Delta is a coin flip, projected within 1.00 with 2 players left between them',
        ]

    def test_picks_the_biggest_blowout(self):
        boxes = [live(A, played(8, 20) + left(1), B, played(9, 10)),
                 live(C, played(8, 30) + left(1), D, played(9, 10))]
        text = callouts.live_callouts(boxes, self.M)
        assert text.startswith('🔥 <@333> is DESTROYING Delta')

    def test_bench_and_ir_do_not_count(self):
        bench = [P(0, 50.0, played=False, slot='BE'), P(0, 50.0, played=False, slot='IR')]
        boxes = [live(A, played(8, 20) + left(1) + bench, B, played(9, 10) + bench)]
        assert callouts.live_callouts(boxes, self.M) == \
            '🔥 <@111> is DESTROYING <@222>, up 70.00 with <@222> out of players'

    def test_bye_matchup_ignored(self):
        boxes = [FakeBox(A, 100, None, 0, played(9), []),
                 live(C, played(8, 20) + left(1), D, played(9, 10))]
        assert '🔥 <@333> is DESTROYING Delta' in callouts.live_callouts(boxes, self.M)


class FakeSettings:
    position_slot_counts = {'QB': 1, 'RB': 1, 'BE': 1}


class FakeLeague:
    """Enough of a League for get_starter_counts: one QB, one RB, one bench."""

    settings = FakeSettings()

    def __init__(self, boxes):
        self._boxes = boxes

    def box_scores(self, week=None):
        return self._boxes


def lineup(qb, rb, bench_rb):
    return [FakePlayer('QB', 'QB', qb), FakePlayer('RB', 'RB', rb), FakePlayer('RB', 'BE', bench_rb)]


class TestFinalCallouts:
    def make(self):
        # Alpha: 20+10=30, bench 25 -> optimal 45, 15 left on bench (worst)
        # Bravo: 20+30=50, bench 5 -> optimal
        # Charlie 100 vs Delta 20 -> blowout by 80
        boxes = [FakeBox(A, 30, B, 50, lineup(20, 10, 25), lineup(20, 30, 5)),
                 FakeBox(C, 100, D, 20, lineup(60, 40, 0), lineup(10, 10, 0))]
        return FakeLeague(boxes), boxes

    def test_no_mapping_means_no_callouts(self):
        league, boxes = self.make()
        assert callouts.final_callouts(league, 1, boxes, callouts.Mentions({})) == ''

    def test_full_set_of_lines(self):
        league, boxes = self.make()
        text = callouts.final_callouts(league, 1, boxes, callouts.Mentions(MAP))
        assert text.splitlines() == [
            '👑 <@333> put up 100.00. Top score of the week.',
            '💩 Delta scraped together 20.00. Yikes.',
            '😱 <@333> DESTROYED Delta by 80.00',
            '🤡 <@111> left 15.00 points on the bench',
        ]

    def test_tight_week_uses_beat_wording(self):
        boxes = [FakeBox(A, 100, B, 95, lineup(50, 50, 0), lineup(50, 45, 0))]
        text = callouts.final_callouts(FakeLeague(boxes), 1, boxes, callouts.Mentions(MAP))
        assert '😅 <@111> beat <@222> by 5.00, the biggest win of a tight week' in text
        assert '😱' not in text

    def test_nothing_played(self):
        assert callouts.final_callouts(FakeLeague([]), 1, [], callouts.Mentions(MAP)) == ''

    def test_worst_manager_failure_drops_only_that_line(self, monkeypatch):
        league, boxes = self.make()
        monkeypatch.setattr(callouts, 'worst_manager', lambda *a: (_ for _ in ()).throw(RuntimeError('boom')))
        text = callouts.final_callouts(league, 1, boxes, callouts.Mentions(MAP))
        assert '🤡' not in text
        assert '👑' in text


class FakePlayerObj:
    def __init__(self, name, position):
        self.name = name
        self.position = position


class FakeActivity:
    def __init__(self, date, actions):
        self.date = date
        self.actions = actions


def trade(date, sends):
    """sends: list of (from_team, to_team, player) -> a TRADED Activity."""
    actions = []
    for frm, to, player in sends:
        actions.append((frm, 'TRADE_SENT', player, 0))
        actions.append((to, 'TRADE_RECEIVED', player, 0))
    return FakeActivity(date, actions)


class FeedLeague:
    def __init__(self, activities=None, raise_exc=None):
        self.activities = activities or []
        self.raise_exc = raise_exc
        self.calls = []

    def recent_activity(self, size=25, msg_type=None, offset=0):
        self.calls.append((size, msg_type))
        if self.raise_exc:
            raise self.raise_exc
        return list(self.activities)


HOUR = 60 * 60 * 1000
NOW = 1_800_000_000_000


class TestFormatTrade:
    def test_two_sided(self):
        act = trade(NOW, [(A, B, FakePlayerObj('Josh Allen', 'QB')),
                          (A, B, FakePlayerObj('Gus Edwards', 'RB')),
                          (B, A, FakePlayerObj('Puka Nacua', 'WR'))])
        assert callouts.format_trade(act) == \
            'Alpha sends: Josh Allen (QB), Gus Edwards (RB)\nBravo sends: Puka Nacua (WR)'

    def test_unresolved_player_uses_str(self):
        act = trade(NOW, [(A, B, 12345)])
        assert callouts.format_trade(act) == 'Alpha sends: 12345'

    def test_missing_from_team_skipped(self):
        act = trade(NOW, [(None, B, FakePlayerObj('X', 'TE')), (B, A, FakePlayerObj('Y', 'K'))])
        assert callouts.format_trade(act) == 'Bravo sends: Y (K)'


class TestTradeAnnouncements:
    def state(self, tmp_path):
        with open(os.path.join(str(tmp_path), callouts.TRADE_STATE_FILE)) as f:
            return json.load(f)

    def test_asks_feed_for_trades_only(self, tmp_path):
        league = FeedLeague()
        callouts.trade_announcements(league, str(tmp_path), now_ms=NOW)
        assert league.calls == [(callouts.TRADE_FEED_SIZE, 'TRADED')]

    def test_first_run_announces_recent_trade_and_saves_state(self, tmp_path):
        act = trade(NOW - HOUR, [(A, B, FakePlayerObj('Josh Allen', 'QB')), (B, A, FakePlayerObj('Puka', 'WR'))])
        text = callouts.trade_announcements(FeedLeague([act]), str(tmp_path), now_ms=NOW)
        assert text == 'Trade Complete\n\nAlpha sends: Josh Allen (QB)\nBravo sends: Puka (WR)'
        assert self.state(tmp_path) == {'last_seen': NOW, 'announced': [str(NOW - HOUR)]}

    def test_first_run_ignores_trades_older_than_bootstrap(self, tmp_path):
        old = trade(NOW - callouts.TRADE_BOOTSTRAP_MS - 1, [(A, B, FakePlayerObj('Old', 'QB'))])
        assert callouts.trade_announcements(FeedLeague([old]), str(tmp_path), now_ms=NOW) == ''

    def test_second_run_does_not_repeat(self, tmp_path):
        act = trade(NOW - HOUR, [(A, B, FakePlayerObj('Josh Allen', 'QB'))])
        league = FeedLeague([act])
        assert callouts.trade_announcements(league, str(tmp_path), now_ms=NOW) != ''
        assert callouts.trade_announcements(league, str(tmp_path), now_ms=NOW + HOUR) == ''

    def test_trade_inside_overlap_window_still_announced_once(self, tmp_path):
        # Feed lag: a trade stamped before the previous run shows up only now.
        league = FeedLeague([])
        callouts.trade_announcements(league, str(tmp_path), now_ms=NOW)
        late = trade(NOW - 10 * 60 * 1000, [(A, B, FakePlayerObj('Late', 'RB'))])
        league.activities = [late]
        assert 'Late (RB)' in callouts.trade_announcements(league, str(tmp_path), now_ms=NOW + HOUR)
        assert callouts.trade_announcements(league, str(tmp_path), now_ms=NOW + 2 * HOUR) == ''

    def test_trade_older_than_overlap_is_not_resurrected(self, tmp_path):
        league = FeedLeague([])
        callouts.trade_announcements(league, str(tmp_path), now_ms=NOW)
        stale = trade(NOW - callouts.TRADE_OVERLAP_MS - 1, [(A, B, FakePlayerObj('Stale', 'RB'))])
        league.activities = [stale]
        assert callouts.trade_announcements(league, str(tmp_path), now_ms=NOW + HOUR) == ''

    def test_multiple_trades_oldest_first(self, tmp_path):
        newer = trade(NOW - HOUR, [(C, D, FakePlayerObj('Second', 'WR'))])
        older = trade(NOW - 2 * HOUR, [(A, B, FakePlayerObj('First', 'QB'))])
        text = callouts.trade_announcements(FeedLeague([newer, older]), str(tmp_path), now_ms=NOW)
        assert text == 'Trades Complete (2)\n\nAlpha sends: First (QB)\n\nCharlie sends: Second (WR)'

    def test_feed_error_announces_nothing_and_keeps_state(self, tmp_path):
        good = FeedLeague([])
        callouts.trade_announcements(good, str(tmp_path), now_ms=NOW)
        before = self.state(tmp_path)
        bad = FeedLeague(raise_exc=Exception('No topics'))
        assert callouts.trade_announcements(bad, str(tmp_path), now_ms=NOW + HOUR) == ''
        assert self.state(tmp_path) == before

    def test_corrupt_state_starts_over(self, tmp_path):
        with open(os.path.join(str(tmp_path), callouts.TRADE_STATE_FILE), 'w') as f:
            f.write('{not json')
        act = trade(NOW - HOUR, [(A, B, FakePlayerObj('Josh', 'QB'))])
        assert 'Josh (QB)' in callouts.trade_announcements(FeedLeague([act]), str(tmp_path), now_ms=NOW)

    def test_announced_history_is_capped(self, tmp_path):
        acts = [trade(NOW - i, [(A, B, FakePlayerObj('P%d' % i, 'QB'))]) for i in range(1, 60)]
        callouts.trade_announcements(FeedLeague(acts), str(tmp_path), now_ms=NOW)
        assert len(self.state(tmp_path)['announced']) == callouts.TRADE_HISTORY

    def test_unwritable_state_dir_still_announces(self, tmp_path):
        act = trade(NOW - HOUR, [(A, B, FakePlayerObj('Josh', 'QB'))])
        missing = os.path.join(str(tmp_path), 'does', 'not', 'exist')
        assert 'Josh (QB)' in callouts.trade_announcements(FeedLeague([act]), missing, now_ms=NOW)


class TestTradeMention:
    def test_with_role(self):
        assert callouts.trade_mention(callouts.Mentions(MAP, '999')) == \
            '🔁 <@&999> a trade was accepted and is up for review'

    def test_without_role(self):
        assert callouts.trade_mention(callouts.Mentions(MAP)) == ''
        assert callouts.trade_mention(callouts.Mentions(MAP), 'Trade Accepted\nAlpha sends: X') == ''

    def test_with_block_includes_the_deal(self):
        block = 'Trade Accepted - 24h to veto\n\nAlpha sends: Josh Allen\nBravo sends: Puka Nacua'
        assert callouts.trade_mention(callouts.Mentions(MAP, '999'), block) == \
            '🔁 <@&999> a trade was accepted and is up for review:\nAlpha sends: Josh Allen\nBravo sends: Puka Nacua'

    def test_with_header_only_block(self):
        assert callouts.trade_mention(callouts.Mentions(MAP, '999'), 'Trade Accepted') == \
            '🔁 <@&999> a trade was accepted and is up for review'


class FakeRequest:
    """Stands in for league.espn_request: canned responses per view."""

    def __init__(self, by_view):
        self.by_view = by_view
        self.calls = []

    def league_get(self, params=None, headers=None, extend=''):
        self.calls.append(dict(params or {}))
        view = (params or {}).get('view')
        value = self.by_view.get(view)
        if callable(value):
            return value(params)
        if isinstance(value, Exception):
            raise value
        return value


class RawLeague:
    """A League with raw ESPN rows behind espn_request, for the accepted-trade tests."""

    def __init__(self, transactions_by_period, pending=None, settings=None, period=3):
        self.scoringPeriodId = period
        self.player_map = {10: 'Josh Allen', 11: 'Puka Nacua', 12: 'Drake Maye'}
        self.teams = [A, B, C, D]
        by_view = {
            'mTransactions2': lambda params: {'transactions': transactions_by_period.get(params.get('scoringPeriodId'), [])},
            'mPendingTransactions': {'pendingTransactions': pending or []},
            'mSettings': settings if settings is not None else {'settings': {'tradeSettings': {'revisionHours': 24}}},
        }
        self.espn_request = FakeRequest(by_view)

    def get_team_data(self, team_id):
        for t in self.teams:
            if t.team_id == team_id:
                return t
        return None


def accept_row(proposal_id, team_id, when, row_id='acc-' + '1'):
    return {'id': row_id + proposal_id, 'type': 'TRADE_ACCEPT', 'status': None, 'teamId': team_id,
            'relatedTransactionId': proposal_id, 'proposedDate': when, 'items': []}


def proposal_row(proposal_id, items, status='PENDING'):
    return {'id': proposal_id, 'type': 'TRADE_PROPOSAL', 'status': status, 'teamId': 1, 'items': items}


ITEMS = [{'type': 'TRADE', 'fromTeamId': 1, 'toTeamId': 2, 'playerId': 10},
         {'type': 'TRADE', 'fromTeamId': 2, 'toTeamId': 1, 'playerId': 11},
         {'type': 'DROP', 'fromTeamId': 2, 'toTeamId': 0, 'playerId': 12}]


class TestFormatProposal:
    def test_players_and_drops(self):
        league = RawLeague({})
        assert callouts.format_proposal(league, proposal_row('p1', ITEMS)) == \
            'Alpha sends: Josh Allen\nBravo sends: Puka Nacua\nBravo drops: Drake Maye'

    def test_draft_pick_and_unknown_player(self):
        league = RawLeague({})
        items = [{'type': 'DRAFT_TRADE', 'fromTeamId': 3, 'toTeamId': 1, 'playerId': 0, 'overallPickNumber': 7},
                 {'type': 'TRADE', 'fromTeamId': 1, 'toTeamId': 3, 'playerId': 999}]
        assert callouts.format_proposal(league, proposal_row('p1', items)) == \
            'Charlie sends: pick #7\nAlpha sends: Unknown'

    def test_unknown_team_and_item_type(self):
        league = RawLeague({})
        items = [{'type': 'TRADE', 'fromTeamId': 9, 'toTeamId': 1, 'playerId': 10},
                 {'type': 'SOMETHING_ELSE', 'fromTeamId': 1, 'toTeamId': 9, 'playerId': 11}]
        assert callouts.format_proposal(league, proposal_row('p1', items)) == 'Team 9 sends: Josh Allen'

    def test_no_items(self):
        assert callouts.format_proposal(RawLeague({}), proposal_row('p1', [])) == ''


class TestAcceptedTradeAlerts:
    def state(self, tmp_path):
        with open(os.path.join(str(tmp_path), callouts.TRADE_STATE_FILE)) as f:
            return json.load(f)

    def test_accepted_trade_with_visible_proposal(self, tmp_path):
        league = RawLeague({3: [accept_row('p1', 2, NOW - HOUR), proposal_row('p1', ITEMS)]})
        text = callouts.accepted_trade_alerts(league, str(tmp_path), now_ms=NOW)
        assert text == 'Trade Accepted - 24h to veto\n\n' \
                       'Alpha sends: Josh Allen\nBravo sends: Puka Nacua\nBravo drops: Drake Maye'
        assert self.state(tmp_path)['accepted'] == ['p1']

    def test_reads_current_and_previous_period(self, tmp_path):
        league = RawLeague({2: [accept_row('p1', 2, NOW - HOUR), proposal_row('p1', ITEMS)], 3: []})
        assert 'Josh Allen' in callouts.accepted_trade_alerts(league, str(tmp_path), now_ms=NOW)
        periods = [c.get('scoringPeriodId') for c in league.espn_request.calls if c.get('view') == 'mTransactions2']
        assert periods == [2, 3]

    def test_period_zero_does_not_go_negative(self, tmp_path):
        league = RawLeague({0: []}, period=0)
        callouts.accepted_trade_alerts(league, str(tmp_path), now_ms=NOW)
        periods = [c.get('scoringPeriodId') for c in league.espn_request.calls if c.get('view') == 'mTransactions2']
        assert periods == [0]

    def test_proposal_found_in_pending_view(self, tmp_path):
        league = RawLeague({3: [accept_row('p1', 2, NOW - HOUR)]}, pending=[proposal_row('p1', ITEMS)])
        assert 'Alpha sends: Josh Allen' in callouts.accepted_trade_alerts(league, str(tmp_path), now_ms=NOW)

    def test_proposal_hidden_still_alerts(self, tmp_path):
        league = RawLeague({3: [accept_row('p1', 2, NOW - HOUR)]})
        assert callouts.accepted_trade_alerts(league, str(tmp_path), now_ms=NOW) == \
            'Trade Accepted - 24h to veto\n\nBravo accepted a trade (ESPN has not shown the players yet)'

    def test_not_repeated(self, tmp_path):
        league = RawLeague({3: [accept_row('p1', 2, NOW - HOUR), proposal_row('p1', ITEMS)]})
        assert callouts.accepted_trade_alerts(league, str(tmp_path), now_ms=NOW) != ''
        assert callouts.accepted_trade_alerts(league, str(tmp_path), now_ms=NOW + HOUR) == ''

    def test_old_acceptances_never_announced(self, tmp_path):
        old = NOW - callouts.TRADE_ACCEPT_MAX_AGE_MS
        league = RawLeague({3: [accept_row('p1', 2, old), proposal_row('p1', ITEMS)]})
        assert callouts.accepted_trade_alerts(league, str(tmp_path), now_ms=NOW) == ''

    def test_two_acceptances_oldest_first(self, tmp_path):
        rows = [accept_row('p2', 4, NOW - HOUR), proposal_row('p2', [{'type': 'TRADE', 'fromTeamId': 3, 'toTeamId': 4, 'playerId': 11}]),
                accept_row('p1', 2, NOW - 2 * HOUR), proposal_row('p1', ITEMS[:1])]
        text = callouts.accepted_trade_alerts(RawLeague({3: rows}), str(tmp_path), now_ms=NOW)
        assert text == 'Trades Accepted (2) - 24h to veto\n\nAlpha sends: Josh Allen\n\nCharlie sends: Puka Nacua'

    def test_unreadable_settings_drops_the_hours(self, tmp_path):
        league = RawLeague({3: [accept_row('p1', 2, NOW - HOUR), proposal_row('p1', ITEMS)]},
                           settings=Exception('nope'))
        assert callouts.accepted_trade_alerts(league, str(tmp_path), now_ms=NOW).startswith('Trade Accepted\n\n')

    def test_transactions_error_is_quiet(self, tmp_path):
        league = RawLeague({})
        league.espn_request.by_view['mTransactions2'] = Exception('down')
        assert callouts.accepted_trade_alerts(league, str(tmp_path), now_ms=NOW) == ''

    def test_state_shares_file_with_completed_trades(self, tmp_path):
        league = RawLeague({3: [accept_row('p1', 2, NOW - HOUR), proposal_row('p1', ITEMS)]})
        callouts.accepted_trade_alerts(league, str(tmp_path), now_ms=NOW)
        done = trade(NOW - HOUR, [(A, B, FakePlayerObj('Josh Allen', 'QB'))])
        callouts.trade_announcements(FeedLeague([done]), str(tmp_path), now_ms=NOW)
        state = self.state(tmp_path)
        assert state['accepted'] == ['p1'] and state['announced'] == [str(NOW - HOUR)]
