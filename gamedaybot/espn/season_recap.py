import os
# import pandas as pd
# import dataframe_image as dfi
if os.environ.get("AWS_EXECUTION_ENV") is not None:
    import espn.functionality as espn
else:
    # For local use
    import sys
    sys.path.insert(1, os.path.abspath('.'))
    import gamedaybot.espn.functionality as espn


def trophy_recap(league):
    """
    This function takes in a league object and returns a string representing the trophies earned by each team in the league.

    Parameters
    ----------
    league : object
        A league object from the ESPN Fantasy API.

    Returns
    -------
    str
        A string that contains the team names and the number of trophies earned for each team
    """

    ICONS = ['👑', '💩', '😱', '😅', '🍀', '😡', '📈', '📉', '🤡']
    legend = ['*LEGEND*', '👑: Most Points', '💩: Least Points', '😱: Blown out', '😅: Close wins', '🍀: Lucky',
              '😡: Unlucky', '📈: Most over projection', '📉: Most under projection', '🤡: Most points left on bench']
    team_trophies = {}
    team_names = []

    for team in league.teams:
        # Initialize trophy count for each team
        team_trophies[team.team_abbrev] = [0 for i in range(len(ICONS))]
        team_names.append(team.team_abbrev)

    def award(team_abbrev, index):
        # A trophy can go unawarded for a week -- a playoff week where the
        # matchup was a bye, or a week with no completed games -- and that week
        # simply does not count toward anyone's total.
        if team_abbrev is not None:
            team_trophies[team_abbrev][index] += 1

    for week in range(1, league.current_week):
        # All four trophy lookups below read the same week, so fetch it once
        # and hand the same box scores to each of them.
        box_scores = espn.fetch_box_scores(league, week=week)

        # Get high score, low score, blown out, and close win trophies
        high_score_team, low_score_team, blown_out_team, close_win_team = espn.get_trophies(
            league=league, week=week, recap=True, box_scores=box_scores)
        award(high_score_team, 0)
        award(low_score_team, 1)
        award(blown_out_team, 2)
        award(close_win_team, 3)

        # Get lucky and unlucky trophies
        lucky_team, unlucky_team, scores = espn.get_lucky_trophy(
            league=league, week=week, recap=True, box_scores=box_scores)
        award(lucky_team, 4)
        award(unlucky_team, 5)

        # Get overachiever and underachiever trophies
        overachiever_team, underachiever_team = espn.get_achievers_trophy(
            league=league, week=week, recap=True, box_scores=box_scores)
        award(overachiever_team, 6)
        award(underachiever_team, 7)

        # Get most points left on bench trophy
        best_manager_team = espn.optimal_team_scores(
            league=league, week=week, recap=True, box_scores=box_scores)
        award(best_manager_team, 8)

    result = 'Season Recap!\n'
    result += "Team".ljust(7, ' ')
    for icon in ICONS:
        result += icon + ' '
    result += '\n'
    for team_name, trophies in team_trophies.items():
        result += f"{team_name.ljust(5, ' ')}: {trophies}\n"
    result += '\n'.join(legend)

    # Pretty picture
    ### Libraries make lambda size too big
    # df = pd.DataFrame.from_dict(team_trophies, orient='index', columns=ICONS)
    # df_styled = df.style.background_gradient(cmap='Greens')
    # dfi.export(df_styled, '/tmp/season_recap.png')
    return (result)


def win_matrix(league):
    """
    This function takes in a league and returns a string of the standings if every team played every other team every week.
    The standings are sorted by winning percentage, and the string includes the team abbreviation, wins, and losses.

    Parameters
    ----------
    league : object
        A league object from the ESPN Fantasy API.

    Returns
    -------
    str
        A string of the standings in the format of "position. team abbreviation (wins-losses)"
    """

    team_record = {team.team_abbrev: [0, 0] for team in league.teams}

    for week in range(1, league.current_week):
        scores = espn.get_weekly_score_with_win_loss(league=league, week=week)
        losses = 0
        for team in scores:
            team_record[team.team_abbrev][0] += len(scores) - 1 - losses
            team_record[team.team_abbrev][1] += losses
            losses += 1

    team_record = dict(sorted(team_record.items(), key=lambda item: item[1][0] / item[1][1], reverse=True))

    standings_txt = ["Standings if everyone played every team every week"]
    pos = 1
    for team in team_record:
        standings_txt += [f"{pos:2}. {team:4} ({team_record[team][0]}-{team_record[team][1]})"]
        pos += 1

    return '\n'.join(standings_txt)


# ---------------------------------------------------------------------------
# End-of-season recap
#
# Nothing below needs state kept during the season: ESPN serves every week's
# box scores for the whole year, so the recap is recomputed from history
# whenever it runs -- for the current season or a finished one.
# ---------------------------------------------------------------------------

import logging

logger = logging.getLogger(__name__)

# Weekly trophies tallied in the Trophy Case, in column order.
TROPHY_COLUMNS = ['HI', 'LO', 'BLW', 'CLS', 'LCK', 'UNL', 'OVR', 'UND', 'BST', 'WST']
TROPHY_LEGEND = ('HI high score · LO low score · BLW blown out · CLS close win · LCK lucky win · '
                 'UNL unlucky loss · OVR overachiever · UND underachiever · BST best manager · WST worst manager')


def last_completed_week(league):
    """
    The last week with final scores.

    While the season runs, current_week is the week in progress. Once it is
    over, ESPN moves scoringPeriodId past the final period but current_week
    stays parked on the final week, so that week has to be counted too.
    """
    final = league.finalScoringPeriod
    if league.scoringPeriodId > final:
        return final
    return max(league.current_week - 1, 0)


class TeamSeason(object):
    """Season totals for one team."""

    def __init__(self, team):
        self.team = team
        self.wins = self.losses = self.ties = 0
        self.points_for = self.points_against = 0.0
        self.optimal = self.actual = 0.0
        self.allplay_wins = self.allplay_losses = 0
        self.expected_wins = 0.0
        self.trophies = {col: 0 for col in TROPHY_COLUMNS}
        self.best_week = None   # (points, week)
        self.worst_week = None  # (points, week)

    @property
    def bench(self):
        return self.optimal - self.actual

    @property
    def optimal_pct(self):
        return (self.actual / self.optimal * 100) if self.optimal else 0.0

    @property
    def luck(self):
        """Actual wins minus the wins an average schedule would have given."""
        return self.wins - self.expected_wins

    @property
    def record(self):
        if self.ties:
            return '%d-%d-%d' % (self.wins, self.losses, self.ties)
        return '%d-%d' % (self.wins, self.losses)


class SeasonStats(object):
    """Everything the recap reads, gathered from every completed week."""

    def __init__(self, league, last_week):
        self.league = league
        self.last_week = last_week
        self.teams = {t.team_id: TeamSeason(t) for t in league.teams}
        self.by_abbrev = {t.team_abbrev: t.team_id for t in league.teams}
        self.high = None      # (points, team_id, week)
        self.low = None
        self.blowout = None   # (margin, winner_id, loser_id, week)
        self.closest = None
        self.weeks_counted = 0

    def award(self, column, abbrev):
        team_id = self.by_abbrev.get(abbrev)
        if team_id is not None:
            self.teams[team_id].trophies[column] += 1

    def ranked(self, key, reverse=True):
        return sorted(self.teams.values(), key=key, reverse=reverse)


def gather_season(league, last_week=None):
    """
    Walk every completed week's box scores once and accumulate the season.

    All-play records and the luck figure use regular-season weeks only,
    since playoff weeks have byes and partial slates. Everything else counts
    every week that was played.
    """
    if last_week is None:
        last_week = last_completed_week(league)
    stats = SeasonStats(league, last_week)
    starter_counts = espn.get_starter_counts(league)
    regular_weeks = league.settings.reg_season_count

    for week in range(1, last_week + 1):
        boxes = espn.fetch_box_scores(league, week=week)
        played = [b for b in boxes if not espn.is_bye_box(b)]
        if not played:
            continue
        stats.weeks_counted += 1

        weekly = {}  # team_id -> (points, optimal_pct)
        for box in played:
            sides = ((box.home_team, box.home_score, box.away_score, box.home_lineup),
                     (box.away_team, box.away_score, box.home_score, box.away_lineup))
            for team, score, opp_score, lineup in sides:
                s = stats.teams.get(team.team_id)
                if s is None:
                    continue
                optimal, actual, _, pct = espn.optimal_lineup_score(lineup, starter_counts)
                s.points_for += score
                s.points_against += opp_score
                s.optimal += optimal
                s.actual += actual
                if score > opp_score:
                    s.wins += 1
                elif score < opp_score:
                    s.losses += 1
                else:
                    s.ties += 1
                if s.best_week is None or score > s.best_week[0]:
                    s.best_week = (score, week)
                if s.worst_week is None or score < s.worst_week[0]:
                    s.worst_week = (score, week)
                if stats.high is None or score > stats.high[0]:
                    stats.high = (score, team.team_id, week)
                if stats.low is None or score < stats.low[0]:
                    stats.low = (score, team.team_id, week)
                weekly[team.team_id] = (score, pct)

            margin = box.home_score - box.away_score
            if margin != 0:
                winner, loser = (box.home_team, box.away_team) if margin > 0 else (box.away_team, box.home_team)
                entry = (abs(margin), winner.team_id, loser.team_id, week)
                if stats.blowout is None or entry[0] > stats.blowout[0]:
                    stats.blowout = entry
                if stats.closest is None or entry[0] < stats.closest[0]:
                    stats.closest = entry

        if week <= regular_weeks and len(weekly) > 1:
            others = len(weekly) - 1
            for team_id, (score, _) in weekly.items():
                ap_wins = sum(1 for other, (other_score, _) in weekly.items() if other != team_id and score > other_score)
                ap_losses = sum(1 for other, (other_score, _) in weekly.items() if other != team_id and score < other_score)
                s = stats.teams[team_id]
                s.allplay_wins += ap_wins
                s.allplay_losses += ap_losses
                s.expected_wins += ap_wins / others

        high, low, blown_out, close_win = espn.get_trophies(league, week=week, recap=True, box_scores=boxes)
        lucky, unlucky, _ = espn.get_lucky_trophy(league, week=week, recap=True, box_scores=boxes)
        over, under = espn.get_achievers_trophy(league, week=week, recap=True, box_scores=boxes)
        worst = espn.optimal_team_scores(league, week=week, recap=True, box_scores=boxes)
        for column, abbrev in (('HI', high), ('LO', low), ('BLW', blown_out), ('CLS', close_win), ('LCK', lucky),
                               ('UNL', unlucky), ('OVR', over), ('UND', under), ('WST', worst)):
            stats.award(column, abbrev)
        if weekly:
            best_id = max(weekly, key=lambda tid: weekly[tid][1])
            stats.teams[best_id].trophies['BST'] += 1

    return stats


def _abbrev(stats, team_id):
    return stats.teams[team_id].team.team_abbrev


def final_order(stats):
    """
    Teams in finishing order: ESPN's final rank once the playoffs are done,
    otherwise the regular-season table (wins, then points for).
    """
    teams = list(stats.teams.values())
    if teams and all(getattr(t.team, 'final_standing', 0) for t in teams):
        return sorted(teams, key=lambda t: t.team.final_standing), True
    return sorted(teams, key=lambda t: (t.wins, t.points_for), reverse=True), False


def season_recap_sections(league, stats=None):
    """
    The recap as a list of short sections, each its own chat message so none
    of them runs into Discord's per-message limit.
    """
    if stats is None:
        stats = gather_season(league)
    year = league.year
    name = league.settings.name
    order, is_final = final_order(stats)

    # 1. Finish and standings
    lines = ['🏆 %s Season Recap: %s' % (year, name)]
    if order:
        if is_final:
            lines.append('Champion: %s' % order[0].team.team_name)
            if len(order) > 1:
                lines.append('Runner-up: %s' % order[1].team.team_name)
            if len(order) > 2:
                lines.append('Last place: %s' % order[-1].team.team_name)
            lines.append('')
            lines.append('Final standings')
        else:
            lines.append('Standings through week %d (playoffs not final)' % stats.last_week)
        for pos, t in enumerate(order, 1):
            lines.append('%2d. %-5s %-7s %8.2f PF' % (pos, t.team.team_abbrev, t.record, t.points_for))
    sections = ['\n'.join(lines)]

    if not stats.weeks_counted:
        return sections

    # 2. Superlatives
    lines = ['Season Superlatives']
    if stats.high:
        lines.append('👑 Week of the year: %s %.2f (week %d)' % (_abbrev(stats, stats.high[1]), stats.high[0], stats.high[2]))
    if stats.low:
        lines.append('💩 Dud of the year: %s %.2f (week %d)' % (_abbrev(stats, stats.low[1]), stats.low[0], stats.low[2]))
    if stats.blowout:
        m, w, l, wk = stats.blowout
        lines.append('😱 Biggest blowout: %s over %s by %.2f (week %d)' % (_abbrev(stats, w), _abbrev(stats, l), m, wk))
    if stats.closest:
        m, w, l, wk = stats.closest
        lines.append('😅 Closest game: %s over %s by %.2f (week %d)' % (_abbrev(stats, w), _abbrev(stats, l), m, wk))
    most_pf = stats.ranked(lambda t: t.points_for)[0]
    most_pa = stats.ranked(lambda t: t.points_against)[0]
    lines.append('🔥 Most points: %s %.2f' % (most_pf.team.team_abbrev, most_pf.points_for))
    lines.append('🎯 Most points against: %s %.2f' % (most_pa.team.team_abbrev, most_pa.points_against))
    luckiest = stats.ranked(lambda t: t.luck)[0]
    unluckiest = stats.ranked(lambda t: t.luck, reverse=False)[0]
    lines.append('🍀 Luckiest: %s %+.1f wins vs an average schedule' % (luckiest.team.team_abbrev, luckiest.luck))
    lines.append('😡 Unluckiest: %s %+.1f wins vs an average schedule' % (unluckiest.team.team_abbrev, unluckiest.luck))
    best_mgr = stats.ranked(lambda t: t.optimal_pct)[0]
    worst_mgr = stats.ranked(lambda t: t.bench)[0]
    lines.append('🤖 Best manager: %s scored %.1f%% of optimal' % (best_mgr.team.team_abbrev, best_mgr.optimal_pct))
    lines.append('🤡 Most bench points: %s left %.2f on the bench' % (worst_mgr.team.team_abbrev, worst_mgr.bench))
    sections.append('\n'.join(lines))

    # 3. Trophy case
    lines = ['Trophy Case (weekly trophies, %d weeks)' % stats.weeks_counted,
             'Team  ' + ' '.join('%3s' % c for c in TROPHY_COLUMNS)]
    for t in stats.ranked(lambda t: sum(t.trophies[c] for c in ('HI', 'CLS', 'LCK', 'OVR', 'BST'))):
        lines.append('%-5s ' % t.team.team_abbrev + ' '.join('%3d' % t.trophies[c] for c in TROPHY_COLUMNS))
    lines.append(TROPHY_LEGEND)
    sections.append('\n'.join(lines))

    # 4. Luck, all-play, bench
    lines = ['Fortune Index (actual wins minus expected wins, regular season)']
    for pos, t in enumerate(stats.ranked(lambda t: t.luck), 1):
        lines.append('%2d. %-5s %+5.1f  (%s actual, %.1f expected)' % (pos, t.team.team_abbrev, t.luck, t.record, t.expected_wins))
    lines.append('')
    lines.append('Win Matrix (if everyone played everyone every week)')
    for pos, t in enumerate(stats.ranked(lambda t: (t.allplay_wins, -t.allplay_losses)), 1):
        lines.append('%2d. %-5s (%d-%d)' % (pos, t.team.team_abbrev, t.allplay_wins, t.allplay_losses))
    lines.append('')
    lines.append('Points Left on the Bench')
    for pos, t in enumerate(stats.ranked(lambda t: t.bench), 1):
        lines.append('%2d. %-5s %7.2f  (%.1f%% of optimal)' % (pos, t.team.team_abbrev, t.bench, t.optimal_pct))
    sections.append('\n'.join(lines))

    return sections


def season_recap_mentions(league, mentions, stats=None):
    """The ping lines for the recap: who won, who didn't, and the season's superlatives."""
    if not mentions.has_teams:
        return ''
    if stats is None:
        stats = gather_season(league)
    if not stats.weeks_counted:
        return ''
    order, is_final = final_order(stats)
    lines = []
    if is_final and order:
        lines.append('🏆 %s is your %s champion' % (mentions.team(order[0].team), league.year))
        if len(order) > 2:
            lines.append('💀 %s finished dead last' % mentions.team(order[-1].team))
    if stats.high:
        lines.append('👑 %s had the week of the year: %.2f in week %d' %
                     (mentions.team(stats.teams[stats.high[1]].team), stats.high[0], stats.high[2]))
    luckiest = stats.ranked(lambda t: t.luck)[0]
    unluckiest = stats.ranked(lambda t: t.luck, reverse=False)[0]
    lines.append('🍀 %s was the luckiest (%+.1f wins), 😡 %s the unluckiest (%+.1f)' %
                 (mentions.team(luckiest.team), luckiest.luck, mentions.team(unluckiest.team), unluckiest.luck))
    best_mgr = stats.ranked(lambda t: t.optimal_pct)[0]
    worst_mgr = stats.ranked(lambda t: t.bench)[0]
    lines.append('🤖 %s managed best (%.1f%% of optimal), 🤡 %s left %.2f on the bench' %
                 (mentions.team(best_mgr.team), best_mgr.optimal_pct, mentions.team(worst_mgr.team), worst_mgr.bench))
    return '\n'.join(lines)


def season_is_over(league):
    return league.scoringPeriodId > league.finalScoringPeriod


RECAP_STATE_KEY = 'recap_posted'


def recap_already_posted(state, year):
    return str(year) in [str(y) for y in state.get(RECAP_STATE_KEY, [])]


def mark_recap_posted(state, year):
    posted = [str(y) for y in state.get(RECAP_STATE_KEY, [])]
    if str(year) not in posted:
        posted.append(str(year))
    state[RECAP_STATE_KEY] = posted
    return state
