import os
import sys
sys.path.insert(1, os.path.abspath('.'))

import gamedaybot.espn.season_recap as recap
import gamedaybot.espn.callouts as callouts


_counter = [0]


class FakePlayer:
    def __init__(self, position, slot, points, projected=None):
        self.position = position
        self.slot_position = slot
        self.points = points
        self.projected_points = points if projected is None else projected
        self.game_played = 100
        self.on_bye_week = False
        self.injuryStatus = 'ACTIVE'
        # optimal_lineup_score keys players by name, so every fake needs its own.
        _counter[0] += 1
        self.name = 'P%d' % _counter[0]


def lineup(qb, rb, bench_rb=0.0, projected=None):
    """One QB, one RB starter, one RB on the bench. Projection defaults to actual."""
    pj = projected
    return [FakePlayer('QB', 'QB', qb, pj / 2 if pj is not None else None),
            FakePlayer('RB', 'RB', rb, pj / 2 if pj is not None else None),
            FakePlayer('RB', 'BE', bench_rb)]


class FakeTeam:
    def __init__(self, team_id, abbrev, name, final_standing=0):
        self.team_id = team_id
        self.team_abbrev = abbrev
        self.team_name = name
        self.final_standing = final_standing


class FakeBox:
    def __init__(self, home, home_lineup, away, away_lineup):
        self.home_team, self.away_team = home, away
        self.home_lineup, self.away_lineup = home_lineup, away_lineup
        self.home_score = sum(p.points for p in home_lineup if p.slot_position != 'BE')
        self.away_score = sum(p.points for p in away_lineup if p.slot_position != 'BE')
        self.home_projected = sum(p.projected_points for p in home_lineup if p.slot_position != 'BE')
        self.away_projected = sum(p.projected_points for p in away_lineup if p.slot_position != 'BE')


class FakeSettings:
    name = 'Test League'
    reg_season_count = 2
    position_slot_counts = {'QB': 1, 'RB': 1, 'BE': 1}


class FakeLeague:
    year = 2025
    settings = FakeSettings()
    firstScoringPeriod = 1

    def __init__(self, weeks, teams, scoring_period, final_period, current_week):
        self._weeks = weeks
        self.teams = teams
        self.scoringPeriodId = scoring_period
        self.finalScoringPeriod = final_period
        self.current_week = current_week

    def box_scores(self, week=None):
        return self._weeks.get(week, [])


A = FakeTeam(1, 'AAA', 'Alpha', final_standing=1)
B = FakeTeam(2, 'BBB', 'Bravo', final_standing=3)
C = FakeTeam(3, 'CCC', 'Charlie', final_standing=2)
D = FakeTeam(4, 'DDD', 'Delta', final_standing=4)
MAP = {1: '111', 2: '222', 3: '333', 4: '444'}


def league_with_two_weeks(final_known=True, scoring_period=4, final_period=3, current_week=3):
    """
    Week 1: A 100 beats B 50 (blowout 50); C 80 beats D 79 (closest, 1).
            A left 30 on the bench; everyone else 0.
    Week 2: B 120 beats A 60; D 70 beats C 65.
    Week 3 (playoffs): A 90 beats C 85, B/D idle (bye box).
    """
    weeks = {
        1: [FakeBox(A, lineup(50, 50, bench_rb=80), B, lineup(25, 25)),
            FakeBox(C, lineup(40, 40), D, lineup(40, 39))],
        2: [FakeBox(B, lineup(60, 60), A, lineup(30, 30)),
            FakeBox(D, lineup(35, 35), C, lineup(30, 35))],
        3: [FakeBox(A, lineup(45, 45), C, lineup(40, 45)),
            FakeBox(B, lineup(10, 10), None, [])],
    }
    teams = [A, B, C, D]
    if not final_known:
        teams = [FakeTeam(t.team_id, t.team_abbrev, t.team_name, 0) for t in teams]
    return FakeLeague(weeks, teams, scoring_period, final_period, current_week)


class TestLastCompletedWeek:
    def test_mid_season_is_week_before_current(self):
        assert recap.last_completed_week(FakeLeague({}, [], 5, 17, 5)) == 4

    def test_season_over_counts_the_final_week(self):
        assert recap.last_completed_week(FakeLeague({}, [], 18, 17, 17)) == 17

    def test_before_week_one(self):
        assert recap.last_completed_week(FakeLeague({}, [], 1, 17, 1)) == 0


class TestGatherSeason:
    def test_records_and_points(self):
        stats = recap.gather_season(league_with_two_weeks())
        a, b, c, d = (stats.teams[i] for i in (1, 2, 3, 4))
        assert (a.wins, a.losses) == (2, 1) and a.points_for == 250 and a.points_against == 255
        assert (b.wins, b.losses) == (1, 1)
        assert (c.wins, c.losses) == (1, 2)
        assert (d.wins, d.losses) == (1, 1)
        assert stats.weeks_counted == 3

    def test_bench_points(self):
        stats = recap.gather_season(league_with_two_weeks())
        # Week 1: A started a 50 RB with an 80 on the bench.
        assert stats.teams[1].bench == 30
        assert stats.teams[2].bench == 0

    def test_superlatives(self):
        stats = recap.gather_season(league_with_two_weeks())
        assert stats.high == (120, 2, 2)
        assert stats.low == (50, 2, 1)
        assert stats.blowout == (60, 2, 1, 2)
        assert stats.closest == (1, 3, 4, 1)

    def test_allplay_and_luck_use_regular_season_only(self):
        stats = recap.gather_season(league_with_two_weeks())
        a = stats.teams[1]
        # Week 1 A scored 100: beat all 3. Week 2 A scored 60: beat nobody (120, 70, 65).
        assert (a.allplay_wins, a.allplay_losses) == (3, 3)
        assert a.expected_wins == 1.0
        # Two regular-season wins would be expected 1.0; A actually went 1-1 in
        # the regular season and 2-1 overall, so luck counts the playoff win too.
        assert a.luck == 1.0
        c = stats.teams[3]
        # Week 1 C 80: beat 50 and 79, lost to 100. Week 2 C 65: beat 60, lost to 120 and 70.
        assert (c.allplay_wins, c.allplay_losses) == (3, 3)

    def test_trophies(self):
        stats = recap.gather_season(league_with_two_weeks())
        t = {i: stats.teams[i].trophies for i in (1, 2, 3, 4)}
        assert t[1]['HI'] == 2 and t[2]['HI'] == 1          # A weeks 1 and 3, B week 2
        assert t[2]['LO'] == 1 and t[1]['LO'] == 1 and t[3]['LO'] == 1
        assert t[2]['BLW'] == 1 and t[1]['BLW'] == 1        # B blown out wk1, A wk2
        assert t[3]['CLS'] == 1 and t[4]['CLS'] == 1 and t[1]['CLS'] == 1   # C wk1, D wk2, A wk3
        assert t[1]['WST'] == 1                              # only bench points all season
        assert sum(t[i]['BST'] for i in t) == 3

    def test_missing_week_is_skipped(self):
        league = league_with_two_weeks()
        league._weeks.pop(2)
        stats = recap.gather_season(league)
        assert stats.weeks_counted == 2
        assert stats.teams[1].wins == 2


class TestFinalOrder:
    def test_uses_espn_final_rank_when_known(self):
        order, is_final = recap.final_order(recap.gather_season(league_with_two_weeks()))
        assert is_final and [t.team.team_abbrev for t in order] == ['AAA', 'CCC', 'BBB', 'DDD']

    def test_falls_back_to_wins_then_points(self):
        order, is_final = recap.final_order(recap.gather_season(league_with_two_weeks(final_known=False)))
        # C's 230 PF outranks B's 170 among the one-win teams.
        assert not is_final and [t.team.team_abbrev for t in order] == ['AAA', 'CCC', 'BBB', 'DDD']


class TestSections:
    def test_four_sections_under_discord_limit(self):
        sections = recap.season_recap_sections(league_with_two_weeks())
        assert len(sections) == 4
        assert all(len(s) < 1900 for s in sections)

    def test_header_section(self):
        first = recap.season_recap_sections(league_with_two_weeks())[0].splitlines()
        assert first[0] == '🏆 2025 Season Recap: Test League'
        assert first[1:4] == ['Champion: Alpha', 'Runner-up: Charlie', 'Last place: Delta']
        assert first[5] == 'Final standings'
        assert first[6] == ' 1. AAA   2-1       250.00 PF'

    def test_header_when_playoffs_not_final(self):
        first = recap.season_recap_sections(league_with_two_weeks(final_known=False))[0].splitlines()
        assert first[1] == 'Standings through week 3 (playoffs not final)'

    def test_superlatives_section(self):
        lines = recap.season_recap_sections(league_with_two_weeks())[1].splitlines()
        assert lines[0] == 'Season Superlatives'
        assert '👑 Week of the year: BBB 120.00 (week 2)' in lines
        assert '💩 Dud of the year: BBB 50.00 (week 1)' in lines
        assert '😱 Biggest blowout: BBB over AAA by 60.00 (week 2)' in lines
        assert '😅 Closest game: CCC over DDD by 1.00 (week 1)' in lines
        assert '🤡 Most bench points: AAA left 30.00 on the bench' in lines

    def test_trophy_case_section(self):
        lines = recap.season_recap_sections(league_with_two_weeks())[2].splitlines()
        assert lines[0] == 'Trophy Case (weekly trophies, 3 weeks)'
        assert lines[1] == 'Team   HI  LO BLW CLS LCK UNL OVR UND BST WST'
        assert lines[-1] == recap.TROPHY_LEGEND

    def test_tables_section(self):
        text = recap.season_recap_sections(league_with_two_weeks())[3]
        assert text.startswith('Fortune Index')
        assert 'Win Matrix (if everyone played everyone every week)' in text
        assert ' 1. AAA   (3-3)' in text
        assert 'Points Left on the Bench' in text
        assert ' 1. AAA     30.00  (89.3% of optimal)' in text

    def test_nothing_played_yet(self):
        league = FakeLeague({}, [A, B], 1, 3, 1)
        sections = recap.season_recap_sections(league)
        assert len(sections) == 1
        assert recap.season_recap_mentions(league, callouts.Mentions(MAP)) == ''


class TestMentions:
    def test_pings(self):
        text = recap.season_recap_mentions(league_with_two_weeks(), callouts.Mentions(MAP))
        lines = text.splitlines()
        assert lines[0] == '🏆 <@111> is your 2025 champion'
        assert lines[1] == '💀 <@444> finished dead last'
        assert lines[2] == '👑 <@222> had the week of the year: 120.00 in week 2'
        assert lines[3].startswith('🍀 <@')
        assert lines[4].endswith('🤡 <@111> left 30.00 on the bench')

    def test_no_champion_line_before_playoffs_end(self):
        text = recap.season_recap_mentions(league_with_two_weeks(final_known=False), callouts.Mentions(MAP))
        assert '🏆' not in text and '💀' not in text

    def test_no_mapping(self):
        assert recap.season_recap_mentions(league_with_two_weeks(), callouts.Mentions({})) == ''


class TestRecapState:
    def test_season_is_over(self):
        assert recap.season_is_over(FakeLeague({}, [], 18, 17, 17))
        assert not recap.season_is_over(FakeLeague({}, [], 17, 17, 17))

    def test_posted_flag(self):
        state = {}
        assert not recap.recap_already_posted(state, 2026)
        state = recap.mark_recap_posted(state, 2026)
        assert recap.recap_already_posted(state, 2026)
        assert not recap.recap_already_posted(state, 2027)
        assert recap.mark_recap_posted(state, 2026)[recap.RECAP_STATE_KEY] == ['2026']
