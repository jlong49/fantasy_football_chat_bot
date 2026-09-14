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
# The comeback and coin-flip lines also need this share of the matchup's
# own starters played, so an uneven Thursday/Sunday split between the two
# teams cannot masquerade as a comeback or a nail-biter.
MATCHUP_MIN_PLAYED = 0.75
# A team with this many starters or fewer left is "nearly done".
NEARLY_DONE_LEFT = 2
# "DESTROYING": projected margin at least this, the leader is also the
# projected winner, and the trailer is nearly done.
DESTROYING_MARGIN = 30
# "Comeback watch": the team ahead on the board by at least this much is
# nearly done and still projected to lose.
COMEBACK_MIN_LEAD = 15
# "Coin flip": projected margin under this, with someone still left to play.
COIN_FLIP_MARGIN = 5

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

# An accepted trade sits in the league's review window (tradeSettings.
# revisionHours, 24h by default) before it executes. The acceptance is the
# moment to ping the league: that is when a veto vote can still happen.
# Accept records this old or older are never announced, so a fresh install
# does not replay the season's history.
TRADE_ACCEPT_MAX_AGE_MS = 48 * 60 * 60 * 1000
TRADE_ACCEPT_TYPE = 'TRADE_ACCEPT'
TRADE_PROPOSAL_TYPE = 'TRADE_PROPOSAL'


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


# How people are named in the callout lines (DISCORD_MENTION_STYLE):
#   ping   - <@id> markup and a notification to that person
#   silent - the same <@id> markup, rendered as a blue @name, but Discord is
#            told not to notify anyone (allowed_mentions without users)
#   names  - the team name, no Discord markup at all
MENTION_STYLES = ('ping', 'silent', 'names')
DEFAULT_MENTION_STYLE = 'ping'


def parse_mention_style(raw):
    style = (raw or '').strip().lower()
    if style in MENTION_STYLES:
        return style
    if style:
        logger.warning("Unknown DISCORD_MENTION_STYLE %r; using %s", raw, DEFAULT_MENTION_STYLE)
    return DEFAULT_MENTION_STYLE


class Mentions(object):
    """
    Renders teams and the league role as Discord mentions.

    Parameters
    ----------
    team_mentions : dict, optional
        {espn_team_id: discord_user_id}, as parse_team_mentions returns.
    role_id : str, optional
        Discord role id to ping for league-wide announcements.
    style : str, optional
        One of MENTION_STYLES; see the comment above.
    """

    def __init__(self, team_mentions=None, role_id=None, style=DEFAULT_MENTION_STYLE):
        self.team_mentions = dict(team_mentions or {})
        self.role_id = str(role_id).strip() if role_id else ''
        self.style = style if style in MENTION_STYLES else DEFAULT_MENTION_STYLE

    @property
    def has_teams(self):
        """Whether the callout lines are on at all. They need a mapping, even in 'names' style."""
        return bool(self.team_mentions)

    @property
    def notify_users(self):
        """Whether Discord should actually notify the people named."""
        return self.style == 'ping'

    def team(self, team):
        """The team's owner as ``<@id>``, or the plain team name if unmapped or in 'names' style."""
        user_id = self.team_mentions.get(getattr(team, 'team_id', None))
        if user_id and self.style != 'names':
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
      DESTROYING_MARGIN, and the trailer has NEARLY_DONE_LEFT or fewer
      starters left.
    - Comeback watch: the team ahead on the board by COMEBACK_MIN_LEAD or
      more is nearly done and still projected to lose, in a matchup that is
      at least MATCHUP_MIN_PLAYED played.
    - Coin flip: projected margin under COIN_FLIP_MARGIN, the matchup at
      least MATCHUP_MIN_PLAYED played, and someone still left to play.

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
        if m.proj_margin < DESTROYING_MARGIN or proj[2] > NEARLY_DONE_LEFT:
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
        if m.actual_margin < COMEBACK_MIN_LEAD or m.played_fraction < MATCHUP_MIN_PLAYED:
            continue
        # Only a lead the leader can no longer add much to. Otherwise this is
        # just one side having had more players on the early slate.
        board_leader_left = m.home_left if actual[0] is m.home else m.away_left
        if board_leader_left > NEARLY_DONE_LEFT:
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
        if m.proj_margin >= COIN_FLIP_MARGIN or m.played_fraction < MATCHUP_MIN_PLAYED:
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

    state['last_seen'] = max(now_ms, last_seen or 0)
    state['announced'] = (announced + [str(a.date) for a in fresh])[-TRADE_HISTORY:]
    _save_trade_state(state_dir, state)

    if not blocks:
        return ''
    header = 'Trade Complete' if len(blocks) == 1 else 'Trades Complete (%d)' % len(blocks)
    return '\n\n'.join([header] + blocks)


def _raw_trade_records(league):
    """
    Raw TRADE_* rows from mTransactions2 for the current and previous scoring
    periods, keyed by transaction id. Two periods because an acceptance late
    on a Monday night is stamped with the week that just ended.
    """
    current = getattr(league, 'scoringPeriodId', 0) or 0
    records = {}
    for period in sorted({max(current - 1, 0), current}):
        try:
            data = league.espn_request.league_get(params={'view': 'mTransactions2', 'scoringPeriodId': period})
        except Exception:
            logger.warning('Could not read transactions for scoring period %s', period, exc_info=True)
            continue
        for row in (data or {}).get('transactions', []) or []:
            if str(row.get('type', '')).startswith('TRADE') and row.get('id'):
                records[row['id']] = row
    return records


def _pending_trade_records(league):
    """Raw rows from mPendingTransactions, keyed by id. Often empty."""
    try:
        data = league.espn_request.league_get(params={'view': 'mPendingTransactions'})
    except Exception:
        logger.info('Could not read pending transactions', exc_info=True)
        return {}
    return {row['id']: row for row in (data or {}).get('pendingTransactions', []) or [] if row.get('id')}


def _review_hours(league):
    """The league's trade review window in hours, or None if unreadable."""
    try:
        data = league.espn_request.league_get(params={'view': 'mSettings'})
        hours = data['settings']['tradeSettings']['revisionHours']
        return int(hours) if hours else None
    except Exception:
        return None


def _team_name(league, team_id):
    team = league.get_team_data(team_id) if team_id else None
    return team.team_name if team else 'Team %s' % team_id


def format_proposal(league, proposal):
    """
    A raw TRADE_PROPOSAL row as 'Team sends: ...' lines, one per side, with a
    'Team drops: ...' line for any player cut to make room.

    Items are ``{type, fromTeamId, toTeamId, playerId, overallPickNumber}``:
    TRADE for a player, DRAFT_TRADE for a pick (playerId 0), DROP for a
    player released to make room (toTeamId 0). Player names come from
    league.player_map; an id ESPN did not resolve shows as 'Unknown'.
    """
    sends, drops, order = {}, {}, []
    for item in proposal.get('items', []) or []:
        kind = item.get('type')
        team_id = item.get('fromTeamId')
        if kind == 'DRAFT_TRADE':
            pick = item.get('overallPickNumber')
            label = 'pick #%s' % pick if pick else 'a draft pick'
            bucket = sends
        elif kind == 'TRADE':
            label = league.player_map.get(item.get('playerId'), 'Unknown')
            bucket = sends
        elif kind == 'DROP':
            label = league.player_map.get(item.get('playerId'), 'Unknown')
            bucket = drops
        else:
            continue
        if team_id not in order:
            order.append(team_id)
        bucket.setdefault(team_id, []).append(str(label))

    lines = []
    for team_id in order:
        name = _team_name(league, team_id)
        if team_id in sends:
            lines.append('%s sends: %s' % (name, ', '.join(sends[team_id])))
        if team_id in drops:
            lines.append('%s drops: %s' % (name, ', '.join(drops[team_id])))
    return '\n'.join(lines)


def accepted_trade_alerts(league, state_dir, now_ms=None):
    """
    Trades accepted since the last run and now sitting in the league's review
    window, from the raw transaction rows.

    ESPN records an acceptance as a TRADE_ACCEPT row whose
    relatedTransactionId names the TRADE_PROPOSAL that carries the players.
    The proposal is looked up in the same rows and in mPendingTransactions;
    if ESPN has already hidden it, the alert still goes out naming the team
    that accepted, since the point is the veto window, not the box score.

    Announced proposal ids are kept in the trade state file under
    'accepted' so nothing is repeated.

    Returns
    -------
    str
        A 'Trade Accepted' block, or '' when nothing new was accepted.
    """
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    state = _load_trade_state(state_dir)
    announced = [str(i) for i in state.get('accepted', [])]

    records = _raw_trade_records(league)
    accepts = []
    for row in records.values():
        if row.get('type') != TRADE_ACCEPT_TYPE:
            continue
        proposal_id = row.get('relatedTransactionId')
        stamp = row.get('processDate') or row.get('proposedDate') or 0
        if not proposal_id or proposal_id in announced:
            continue
        if stamp <= now_ms - TRADE_ACCEPT_MAX_AGE_MS:
            continue
        accepts.append((stamp, proposal_id, row))
    if not accepts:
        return ''
    accepts.sort()

    pending = None
    blocks = []
    for stamp, proposal_id, row in accepts:
        proposal = records.get(proposal_id)
        if proposal is None:
            if pending is None:
                pending = _pending_trade_records(league)
            proposal = pending.get(proposal_id)
        body = format_proposal(league, proposal) if proposal else ''
        if not body:
            body = '%s accepted a trade (ESPN has not shown the players yet)' % _team_name(league, row.get('teamId'))
        blocks.append(body)

    state['accepted'] = (announced + [proposal_id for _, proposal_id, _ in accepts])[-TRADE_HISTORY:]
    _save_trade_state(state_dir, state)

    hours = _review_hours(league)
    header = 'Trade Accepted' if len(blocks) == 1 else 'Trades Accepted (%d)' % len(blocks)
    if hours:
        header += ' - %dh to veto' % hours
    return '\n\n'.join([header] + blocks)


def trade_mention(mentions, block=None):
    """
    The line that pings the league about an accepted trade, or '' with no
    role set. With ``block`` (the text accepted_trade_alerts returned) the
    deal itself is included, for when the ping goes to a channel that does
    not also get the report.
    """
    role = mentions.role()
    if not role:
        return ''
    line = '🔁 %s a trade was accepted and is up for review' % role
    if block:
        details = block.split('\n', 1)[1] if '\n' in block else ''
        if details.strip():
            line = line + ':\n' + details.strip()
    return line


# ---------------------------------------------------------------------------
# Discussion-channel extras
# ---------------------------------------------------------------------------

# Streaks shorter than this are not worth a line.
STREAK_MIN = 3


def _remaining_starters(lineup):
    return [p for p in lineup if _is_starter(p) and p.game_played < 100]


def night_watch(box_scores):
    """
    Every matchup still undecided, with the players each side has left.

    Meant for Monday evening, when only the Monday night game remains, but it
    works any time: a matchup is listed as long as either side has a starter
    whose game has not finished. Closest game first.

    Returns
    -------
    str
        A block for a code fence, or '' when every matchup is final.
    """
    entries = []
    for box in box_scores:
        if espn.is_bye_box(box):
            continue
        home_left = _remaining_starters(box.home_lineup)
        away_left = _remaining_starters(box.away_lineup)
        if not home_left and not away_left:
            continue
        margin = box.home_score - box.away_score
        if margin > 0:
            status = '%s up %.2f' % (box.home_team.team_abbrev, margin)
        elif margin < 0:
            status = '%s up %.2f' % (box.away_team.team_abbrev, -margin)
        else:
            status = 'tied'
        lines = ['%s %.2f - %.2f %s  (%s)' % (box.home_team.team_abbrev, box.home_score,
                                            box.away_score, box.away_team.team_abbrev, status)]
        for team, left in ((box.home_team, home_left), (box.away_team, away_left)):
            if left:
                names = ', '.join('%s %s' % (getattr(p, 'position', ''), p.name) for p in left)
                proj = sum((p.projected_points or 0) for p in left)
                lines.append('  %s: %s  (%.1f proj)' % (team.team_abbrev, names, proj))
            else:
                lines.append('  %s: nobody left' % team.team_abbrev)
        entries.append((abs(margin), '\n'.join(lines)))
    if not entries:
        return ''
    entries.sort(key=lambda e: e[0])
    return '\n'.join(['Monday Night Watch'] + [text for _, text in entries])


def lineup_regret(box_scores, mentions):
    """
    The bench decision that cost someone their matchup this week: a benched
    player who outscored a started player at the same position by more than
    the losing margin. The biggest such swing in the league gets the line.

    Only same-position swaps count (a bench WR for a started WR, including a
    WR in a flex slot), so every regret named was a legal lineup.

    Returns
    -------
    str
        One line, or '' when no swap would have flipped a result.
    """
    if not mentions.has_teams:
        return ''
    best = None  # (gain, loser, bench_player, starter, margin)
    for box in box_scores:
        if espn.is_bye_box(box):
            continue
        margin = box.home_score - box.away_score
        if margin == 0:
            continue
        loser, lineup = (box.away_team, box.away_lineup) if margin > 0 else (box.home_team, box.home_lineup)
        margin = abs(margin)
        starters = [p for p in lineup if _is_starter(p)]
        for bench in (p for p in lineup if p.slot_position == 'BE'):
            for starter in starters:
                if getattr(starter, 'position', None) != getattr(bench, 'position', None):
                    continue
                gain = bench.points - starter.points
                if gain > margin and (best is None or gain > best[0]):
                    best = (gain, loser, bench, starter, margin)
    if best is None:
        return ''
    gain, loser, bench, starter, margin = best
    return '🤦 %s benched %s (%.1f) for %s (%.1f) and lost by %.2f' % (
        mentions.team(loser), bench.name, bench.points, starter.name, starter.points, margin)


def player_of_the_week(box_scores, mentions):
    """
    The started player who beat his projection by the most, and the one who
    missed it by the most. Bench and IR players are not considered: a waiver
    pickup going off for nobody is not interesting.

    Returns
    -------
    str
        Two lines, or '' when nothing was started.
    """
    if not mentions.has_teams:
        return ''
    best = worst = None  # (diff, player, team)
    for box in box_scores:
        if espn.is_bye_box(box):
            continue
        for team, lineup in ((box.home_team, box.home_lineup), (box.away_team, box.away_lineup)):
            for p in lineup:
                if not _is_starter(p) or getattr(p, 'projected_points', None) is None:
                    continue
                diff = p.points - p.projected_points
                if best is None or diff > best[0]:
                    best = (diff, p, team)
                if worst is None or diff < worst[0]:
                    worst = (diff, p, team)
    if best is None:
        return ''
    lines = []
    diff, p, team = best
    lines.append('⭐ Player of the week: %s (%s) %.1f for %s, projected %.1f' %
                 (p.name, getattr(p, 'position', ''), p.points, mentions.team(team), p.projected_points))
    diff, p, team = worst
    if worst[1] is not best[1]:
        lines.append('🧊 Bust of the week: %s (%s) %.1f for %s, projected %.1f' %
                     (p.name, getattr(p, 'position', ''), p.points, mentions.team(team), p.projected_points))
    return '\n'.join(lines)


def streak_watch(league, box_scores, mentions):
    """
    Teams riding a win or loss streak of STREAK_MIN or more into this week,
    with who they play. For the Thursday matchups post. Longest first.
    """
    if not mentions.has_teams:
        return ''
    opponent = {}
    for box in box_scores:
        if espn.is_bye_box(box):
            continue
        opponent[box.home_team.team_id] = box.away_team
        opponent[box.away_team.team_id] = box.home_team
    streaks = []
    for team in league.teams:
        length = getattr(team, 'streak_length', 0) or 0
        kind = getattr(team, 'streak_type', '')
        if length >= STREAK_MIN and kind in ('WIN', 'LOSS'):
            streaks.append((length, kind, team))
    streaks.sort(key=lambda s: -s[0])
    lines = []
    for length, kind, team in streaks:
        opp = opponent.get(team.team_id)
        versus = ' against %s' % mentions.team(opp) if opp else ''
        if kind == 'WIN':
            lines.append('🔥 %s rides a %d-game win streak into this week%s' % (mentions.team(team), length, versus))
        else:
            lines.append('🧊 %s has dropped %d straight%s' % (mentions.team(team), length, versus))
    return '\n'.join(lines)
