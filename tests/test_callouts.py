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

    def test_has_teams(self):
        assert callouts.Mentions(MAP).has_teams
        assert not callouts.Mentions({}).has_teams
        assert not callouts.Mentions(None).has_teams


class TestLiveCallouts:
    def test_no_mapping_means_no_callouts(self):
        boxes = [FakeBox(A, 150, B, 80)]
        assert callouts.live_callouts(boxes, callouts.Mentions({})) == ''

    def test_nothing_scored_yet(self):
        boxes = [FakeBox(A, 0, B, 0), FakeBox(C, 0, D, 0)]
        assert callouts.live_callouts(boxes, callouts.Mentions(MAP)) == ''

    def test_destroying_line_for_big_lead(self):
        boxes = [FakeBox(A, 150.5, B, 80.25)]
        assert callouts.live_callouts(boxes, callouts.Mentions(MAP)) == \
            '🔥 <@111> is DESTROYING <@222> by 70.25'

    def test_away_team_can_be_the_one_destroying(self):
        boxes = [FakeBox(A, 80.25, B, 150.5)]
        assert callouts.live_callouts(boxes, callouts.Mentions(MAP)) == \
            '🔥 <@222> is DESTROYING <@111> by 70.25'

    def test_beating_line_under_threshold(self):
        boxes = [FakeBox(A, 100, B, 85)]
        assert callouts.live_callouts(boxes, callouts.Mentions(MAP)) == \
            '💪 <@111> is beating <@222> by 15.00'

    def test_close_game_gets_second_line(self):
        boxes = [FakeBox(A, 150, B, 80), FakeBox(C, 101.5, D, 100)]
        assert callouts.live_callouts(boxes, callouts.Mentions(MAP)) == \
            '🔥 <@111> is DESTROYING <@222> by 70.00\n' \
            '😬 <@333> is barely hanging on against Delta, up 1.50'

    def test_close_line_omitted_when_not_close(self):
        boxes = [FakeBox(A, 150, B, 80), FakeBox(C, 120, D, 100)]
        assert '😬' not in callouts.live_callouts(boxes, callouts.Mentions(MAP))

    def test_single_close_game_is_not_reported_twice(self):
        boxes = [FakeBox(A, 101, B, 100)]
        assert callouts.live_callouts(boxes, callouts.Mentions(MAP)) == \
            '💪 <@111> is beating <@222> by 1.00'

    def test_bye_and_tie_ignored(self):
        boxes = [FakeBox(A, 100, None, 0), FakeBox(C, 90, D, 90)]
        assert callouts.live_callouts(boxes, callouts.Mentions(MAP)) == ''


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
        assert text == 'Trade Alert\n\nAlpha sends: Josh Allen (QB)\nBravo sends: Puka (WR)'
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
        assert text == 'Trade Alert (2 trades)\n\nAlpha sends: First (QB)\n\nCharlie sends: Second (WR)'

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
        assert callouts.trade_mention(callouts.Mentions(MAP, '999')) == '🔁 <@&999> a trade just went through'

    def test_without_role(self):
        assert callouts.trade_mention(callouts.Mentions(MAP)) == ''
