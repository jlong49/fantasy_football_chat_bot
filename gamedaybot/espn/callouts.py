"""
Discord callouts: the @mention lines that live outside the code block.

Every report in functionality.py is plain text that each chat platform wraps
in a code block, and Discord does not render mentions inside a code block. So
anything meant to ping a person is generated here, as a separate short block
of "mention text" that Discord.send_message appends after the code block.
Slack and GroupMe never see it.

Team-to-user mapping comes from DISCORD_TEAM_MENTIONS (ESPN team id -> Discord
user id); league-wide announcements ping DISCORD_ANNOUNCE_ROLE_ID.
"""
import json
import logging
import os
import time

import gamedaybot.espn.functionality as espn

logger = logging.getLogger(__name__)

# Live scoreboard callouts are built from the live projection (actual points
# for starters who have played, projections for the rest -- the same numbers
# as the "Approximate Projected Scores" block) so they never contradict it.
#
# No live callouts at all until this share of the week's starters has played.
# Friday morning is one game in; Sunday 4 PM ET is the early window done.
LIVE_MIN_PLAYED_FRACTION = 0.5
# "DESTROYING": projected margin at least this, the leader is also the
# projected winner, and the trailer has at most this many starters left.
DESTROYING_MARGIN = 30
DESTROYING_MAX_LEFT = 2
# "Comeback watch": the team ahead on the board by at least this much is
# still projected to lose.
COMEBACK_MIN_LEAD = 15
# "Coin flip": projected margin under this, with at least this share of the
# matchup's starters already played and someone still left to play.
COIN_FLIP_MARGIN = 5
COIN_FLIP_MIN_PLAYED = 0.75

# Activity-feed topics scanned per trade check. ESPN returns newest first, so
# this only needs to cover the trades that can clear between two hourly runs.
TRADE_FEED_SIZE = 10
# With no saved state (first run on a fresh install) announce trades from this
# far back rather than everything ESPN has.
TRADE_BOOTSTRAP_MS = 24 * 60 * 60 * 1000
# Re-scan this far behind the last run. ESPN's feed can lag the moment a trade
# clears; the announced-id list keeps the overlap from repeating anything.
TRADE_OVERLAP_MS = 2 * 60 * 60 * 1000
# Announced trade ids kept in the state file.
TRADE_HISTORY = 50
TRADE_STATE_FILE = 'gamedaybot_trades.json'


def parse_team_mentions(raw):
    """
    Parse DISCORD_TEAM_MENTIONS into {espn_team_id: discord_user_id}.

    Parameters
    ----------
    raw : str
        Comma-separated ``team_id:user_id`` pairs, e.g.
        ``1:393143738374946817,2:192444104804794368``. Whitespace around
        either side is ignored. Malformed entries are logged and skipped so
        one typo does not silence every mention.

    Returns
    -------
    dict
        Team id (int) to Discord user id (str, kept as a string because the
        snowflake is used verbatim in the mention markup).
    """
    mapping = {}
    if not raw:
        return mapping
    for entry in raw.split(','):
        entry = entry.strip()
        if not entry:
            continue
        team_id, sep, user_id = entry.partition(':')
        team_id, user_id = team_id.strip(), user_id.strip()
        if not sep or not team_id.isdigit() or not user_id.isdigit():
            logger.warning("Ignoring malformed DISCORD_TEAM_MENTIONS entry %r (want team_id:user_id)", entry)
            continue
        mapping[int(team_id)] = user_id
    return mapping


class Mentions(object):
    """
    Renders teams and the league role as Discord mentions.

    Parameters
    ----------
    team_mentions : dict, optional
        {espn_team_id: discord_user_id}, as parse_team_mentions returns.
    role_id : str, optional
        Discord role id to ping for league-wide announcements.
    """

    def __init__(self, team_mentions=None, role_id=None):
        self.team_mentions = dict(team_mentions or {})
        self.role_id = str(role_id).strip() if role_id else ''

    @property
    def has_teams(self):
        return bool(self.team_mentions)

    def team(self, team):
        """The team's owner as ``<@id>``, or the plain team name if unmapped."""
        user_id = self.team_mentions.get(getattr(team, 'team_id', None))
        if user_id:
            return '<@%s>' % user_id
        return team.team_name

    def role(self):
        """The announce role as ``<@&id>``, or '' if none is configured."""
        return '<@&%s>' % self.role_id if self.role_id else ''


def _matchup_margins(box_scores):
    """
    Yield (margin, leader, trailer) for every matchup with a non-zero margin.

    A zero margin is either a tie or a matchup nobody has scored in yet; there
    is nothing to say about either.
    """
    for box in box_scores:
        if espn.is_bye_box(box):
            continue
        margin = box.home_score - box.away_score
        if margin == 0:
            continue
        if margin > 0:
            yield margin, box.home_team, box.away_team
        else:
            yield -margin, box.away_team, box.home_team


def _is_starter(player):
    return player.slot_position not in ('BE', 'IR')


def _starters_left(lineup):
    """Starters whose game has not finished. Bye players count as finished."""
    return sum(1 for p in lineup if _is_starter(p) and p.game_played < 100)


def _starter_count(lineup):
    return sum(1 for p in lineup if _is_starter(p))


def _players_left(n):
    if n == 0:
        return 'out of players'
    if n == 1:
        return '1 player left'
    return '%d players left' % n


class _LiveMatchup(object):
    """One in-progress matchup, read through the live projection."""

    def __init__(self, box):
        self.home = box.home_team
        self.away = box.away_team
        self.home_actual = box.home_score
        self.away_actual = box.away_score
        self.home_proj = espn.get_projected_total(box.home_lineup)
        self.away_proj = espn.get_projected_total(box.away_lineup)
        self.home_left = _starters_left(box.home_lineup)
        self.away_left = _starters_left(box.away_lineup)
        self.starters = _starter_count(box.home_lineup) + _starter_count(box.away_lineup)

    @property
    def left(self):
        return self.home_left + self.away_left

    @property
    def played_fraction(self):
        return (self.starters - self.left) / self.starters if self.starters else 0.0

    @property
    def proj_margin(self):
        return abs(self.home_proj - self.away_proj)

    @property
    def actual_margin(self):
        return abs(self.home_actual - self.away_actual)

    def proj_leader(self):
        """(leader, trailer, trailer_left) by live projection, or None if level."""
        if self.home_proj == self.away_proj:
            return None
        if self.home_proj > self.away_proj:
            return self.home, self.away, self.away_left
        return self.away, self.home, self.home_left

    def actual_leader(self):
        """(leader, trailer) by points on the board, or None if level."""
        if self.home_actual == self.away_actual:
            return None
        if self.home_actual > self.away_actual:
            return self.home, self.away
        return self.away, self.home


def live_callouts(box_scores, mentions):
    """
    Trash talk for an in-progress scoreboard, built from the live projection
    rather than the raw score so a Thursday-night lead is never mistaken for
    a win.

    At most three lines, in this order, each from the single matchup that best
    fits it:

    - DESTROYING: the leader is also the projected winner, by at least
      DESTROYING_MARGIN, and the trailer has DESTROYING_MAX_LEFT or fewer
      starters left.
    - Comeback watch: the team ahead on the board by COMEBACK_MIN_LEAD or
      more is still projected to lose.
    - Coin flip: projected margin under COIN_FLIP_MARGIN, most of the
      matchup already played, and someone still left to play.

    Nothing is said before LIVE_MIN_PLAYED_FRACTION of the week's starters
    have played, and finished matchups are left for Tuesday's final.

    Parameters
    ----------
    box_scores : list
        Box scores for the week being reported.
    mentions : Mentions

    Returns
    -------
    str
        The lines, or '' when there is no mapping or nothing qualifies.
    """
    if not mentions.has_teams:
        return ''
    matchups = [_LiveMatchup(box) for box in box_scores if not espn.is_bye_box(box)]
    starters = sum(m.starters for m in matchups)
    if not starters:
        return ''
    played = starters - sum(m.left for m in matchups)
    if played / starters < LIVE_MIN_PLAYED_FRACTION:
        return ''
    # A finished matchup gets its verdict with the trophies.
    matchups = [m for m in matchups if m.left > 0]

    lines = []

    destroying = None
    for m in matchups:
        proj = m.proj_leader()
        actual = m.actual_leader()
        if proj is None or actual is None or proj[0] is not actual[0]:
            continue
        if m.proj_margin < DESTROYING_MARGIN or proj[2] > DESTROYING_MAX_LEFT:
            continue
        if destroying is None or m.proj_margin > destroying[0].proj_margin:
            destroying = (m, proj)
    if destroying is not None:
        m, (leader, trailer, trailer_left) = destroying
        lines.append('🔥 %s is DESTROYING %s, up %.2f with %s %s' %
                     (mentions.team(leader), mentions.team(trailer), m.actual_margin,
                      mentions.team(trailer), _players_left(trailer_left)))

    comeback = None
    for m in matchups:
        proj = m.proj_leader()
        actual = m.actual_leader()
        if proj is None or actual is None or proj[0] is actual[0]:
            continue
        if m.actual_margin < COMEBACK_MIN_LEAD:
            continue
        if comeback is None or m.actual_margin > comeback[0].actual_margin:
            comeback = (m, proj, actual)
    if comeback is not None:
        m, (proj_leader, _, _), (board_leader, _) = comeback
        proj_left = m.home_left if proj_leader is m.home else m.away_left
        lines.append('🔄 %s is up %.2f on %s, but %s is still projected to win by %.2f with %s' %
                     (mentions.team(board_leader), m.actual_margin, mentions.team(proj_leader),
                      mentions.team(proj_leader), m.proj_margin, _players_left(proj_left)))

    coin_flip = None
    for m in matchups:
        if m.proj_margin >= COIN_FLIP_MARGIN or m.played_fraction < COIN_FLIP_MIN_PLAYED:
            continue
        if coin_flip is None or m.proj_margin < coin_flip.proj_margin:
            coin_flip = m
    if coin_flip is not None:
        m = coin_flip
        lines.append('😬 %s vs %s is a coin flip, projected within %.2f with %s between them' %
                     (mentions.team(m.home), mentions.team(m.away), m.proj_margin, _players_left(m.left)))

    return '\n'.join(lines)


def worst_manager(league, week, box_scores):
    """
    The team that left the most on its bench, mirroring optimal_team_scores'
    Worst Manager pick (lowest percentage of its optimal score).

    Returns
    -------
    tuple or None
        (team, bench_points) or None when nothing was scored.
    """
    starter_counts = espn.get_starter_counts(league)
    worst = None
    for box in box_scores:
        if espn.is_bye_box(box):
            continue
        for team, lineup in ((box.home_team, box.home_lineup), (box.away_team, box.away_lineup)):
            optimal, actual, _, pct = espn.optimal_lineup_score(lineup, starter_counts)
            if worst is None or pct < worst[0]:
                worst = (pct, team, optimal - actual)
    if worst is None:
        return None
    return worst[1], worst[2]


def final_callouts(league, week, box_scores, mentions):
    """
    Pings for the Tuesday final: top and bottom score, the biggest win, and the
    worst lineup manager. The numbers repeat the trophies block above them;
    the point of these lines is who gets notified.

    Returns
    -------
    str
        Up to four lines, or '' when there is no mapping or nothing was scored.
    """
    if not mentions.has_teams:
        return ''
    high = low = None
    for box in box_scores:
        if espn.is_bye_box(box):
            continue
        for team, score in ((box.home_team, box.home_score), (box.away_team, box.away_score)):
            if high is None or score > high[0]:
                high = (score, team)
            if low is None or score < low[0]:
                low = (score, team)
    if high is None:
        return ''

    lines = ['👑 %s put up %.2f. Top score of the week.' % (mentions.team(high[1]), high[0]),
             '💩 %s scraped together %.2f. Yikes.' % (mentions.team(low[1]), low[0])]

    margins = list(_matchup_margins(box_scores))
    if margins:
        margin, winner, loser = max(margins, key=lambda m: m[0])
        if margin >= DESTROYING_MARGIN:
            lines.append('😱 %s DESTROYED %s by %.2f' % (mentions.team(winner), mentions.team(loser), margin))
        else:
            lines.append('😅 %s beat %s by %.2f, the biggest win of a tight week' %
                         (mentions.team(winner), mentions.team(loser), margin))

    try:
        worst = worst_manager(league, week, box_scores)
    except Exception:
        # A lineup the optimizer cannot score should cost this one line, not
        # the whole Tuesday final.
        logger.warning('Could not work out the worst manager', exc_info=True)
        worst = None
    if worst is not None:
        team, bench = worst
        lines.append('🤡 %s left %.2f points on the bench' % (mentions.team(team), bench))
    return '\n'.join(lines)


def _trade_state_path(state_dir):
    return os.path.join(state_dir, TRADE_STATE_FILE)


def _load_trade_state(state_dir):
    try:
        with open(_trade_state_path(state_dir)) as f:
            state = json.load(f)
        if isinstance(state, dict):
            return state
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        logger.warning('Unreadable trade state in %s; starting over', state_dir, exc_info=True)
    return {}


def _save_trade_state(state_dir, state):
    try:
        with open(_trade_state_path(state_dir), 'w') as f:
            json.dump(state, f)
    except OSError:
        # Without state the next run re-announces from the bootstrap window,
        # which is annoying but not worth losing the announcement over.
        logger.warning('Could not save trade state to %s', state_dir, exc_info=True)


def _player_label(player):
    name = getattr(player, 'name', None) or str(player)
    position = getattr(player, 'position', None)
    return '%s (%s)' % (name, position) if position else name


def format_trade(activity):
    """
    One executed trade as 'Team sends: players' lines, one per side.

    Parameters
    ----------
    activity : espn_api Activity
        A TRADED activity: its actions are (team, 'TRADE_SENT'|'TRADE_RECEIVED',
        player, 0) tuples, one pair per player. Only the SENT rows are needed
        to describe the deal, since every player sent is received by someone.
    """
    sent = {}
    order = []
    for team, action, player, _ in activity.actions:
        if action != 'TRADE_SENT' or team is None:
            continue
        if team not in sent:
            sent[team] = []
            order.append(team)
        sent[team].append(_player_label(player))
    return '\n'.join('%s sends: %s' % (team.team_name, ', '.join(sent[team])) for team in order)


def trade_announcements(league, state_dir, now_ms=None):
    """
    Trades that cleared since the last run, from ESPN's activity feed.

    The mTransactions2 view drops an executed trade's proposal (the record
    that carries the players) and keeps only the empty accept/uphold rows, so
    the league communication feed is the only place the deal itself can be
    read back. espn_api's recent_activity(msg_type='TRADED') parses it.

    State (last run time plus the ids of announced trades) is kept in a small
    JSON file under state_dir so nothing is announced twice across runs.

    Parameters
    ----------
    league : espn_api.football.League
    state_dir : str
        Directory for the state file. Must be writable.
    now_ms : int, optional
        Current time in epoch milliseconds, for tests.

    Returns
    -------
    str
        A 'Trade Alert' block, or '' when nothing new cleared.
    """
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    state = _load_trade_state(state_dir)
    last_seen = state.get('last_seen')
    since = (last_seen - TRADE_OVERLAP_MS) if last_seen else (now_ms - TRADE_BOOTSTRAP_MS)
    announced = [str(i) for i in state.get('announced', [])]

    try:
        activities = league.recent_activity(size=TRADE_FEED_SIZE, msg_type='TRADED')
    except Exception:
        # espn_api raises on an empty feed (no 'topics' key) as well as on a
        # real failure. Either way there is nothing to announce this hour, and
        # the state is left alone so the next run looks again.
        logger.info('No trade activity readable this run', exc_info=True)
        return ''

    # espn_api's Activity keeps only the topic date, so that is the id. Two
    # trades clearing in the same millisecond is not a realistic collision.
    fresh = [a for a in activities if a.date > since and str(a.date) not in announced]
    fresh.sort(key=lambda a: a.date)

    blocks = [format_trade(a) for a in fresh]
    blocks = [b for b in blocks if b]

    state = {
        'last_seen': max(now_ms, last_seen or 0),
        'announced': (announced + [str(a.date) for a in fresh])[-TRADE_HISTORY:],
    }
    _save_trade_state(state_dir, state)

    if not blocks:
        return ''
    header = 'Trade Alert' if len(blocks) == 1 else 'Trade Alert (%d trades)' % len(blocks)
    return '\n\n'.join([header] + blocks)


def trade_mention(mentions):
    """The line that pings the league about a trade, or '' with no role set."""
    role = mentions.role()
    if not role:
        return ''
    return '🔁 %s a trade just went through' % role
