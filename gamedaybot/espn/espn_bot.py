import os
if os.environ.get("AWS_EXECUTION_ENV") is not None:
    # For use in lambda function
    import utils.util as util
    from chat.groupme import GroupMe
    from chat.slack import Slack
    from chat.discord import Discord
else:
    # For local use
    import sys
    sys.path.insert(1, os.path.abspath('.'))
    import gamedaybot.utils.util as util
    from gamedaybot.chat.groupme import GroupMe
    from gamedaybot.chat.slack import Slack
    from gamedaybot.chat.discord import Discord
    from gamedaybot.espn.env_vars import get_env_vars
    import gamedaybot.espn.functionality as espn
    import gamedaybot.espn.season_recap as recap
    import gamedaybot.espn.callouts as callouts


from espn_api.football import League
import json
import logging

logger = logging.getLogger(__name__)
# logger.setLevel(logging.INFO)
logger.setLevel(logging.DEBUG)


def espn_bot(function):
    """
    This function is used to send messages to a messaging platform (e.g. Slack, Discord, or GroupMe) with information
    about a fantasy football league.

    Parameters
    ----------
    function: str
        A string that specifies which type of information to send (e.g. "get_matchups", "get_power_rankings").

    Returns
    -------
    None

    Notes
    -----
    The function uses the following information from the data dictionary:

    str_limit: the character limit for messages on slack.
    bot_id: the id of the GroupMe bot.
        If not provided, defaults to 1.
    slack_webhook_url: the webhook url for the slack bot.
        If not provided, defaults to 1.
    discord_webhook_url: the webhook url for the discord bot.
        If not provided, defaults to 1.
    league_id: the id of the fantasy football league.
    year: the year of the league.
        If not provided, defaults to current year.
    swid: the swid of the league.
        If not provided, defaults to '{1}'.
    espn_s2: the espn s2 of the league.
        If not provided, defaults to '1'.
    close_scores_threshold: the largest projected point difference that still
        counts as a close matchup.
        If not provided, defaults to functionality.CLOSE_SCORES_DEFAULT_THRESHOLD.

    The function creates GroupMe, Slack, and Discord objects, and a League object using the provided information.
    It then uses the specified function to generate a message and sends it through the appropriate messaging platform.

    Possible function values:

    get_matchups: sends the current week's matchups and the projected scores for the remaining games.
    get_monitor: sends a message with a summary of the current week's scores.
    get_scoreboard_short: sends a short version of the current week's scores.
    get_projected_scoreboard: sends the projected scores for the remaining games.
    get_close_scores: sends a message with the scores of games that have a difference of less than 7 points.
    get_power_rankings: sends a message with the power rankings for the league.
    get_trophies: sends a message with the trophies for the league.
    get_standings: sends a message with the standings for the league.
    get_final: sends the final scores and trophies for the previous week.
    get_waiver_report: sends a message with the waiver report for the league.
    season_recap: once the season is over, posts the end-of-season recap (standings, superlatives,
        trophy case, luck, all-play, bench points) to the mention channel if there is one.
    season_recap_now: the same recap for the season so far, on demand.
    get_trade_announcements: sends any trade accepted since the last check (pinging the
        announce role, since the league's veto window is open) and any trade that has
        since completed.
    init: sends a message to confirm that the bot has been set up.

    On Discord, some functions also send a short block of mention text after
    the code block (see callouts.py), so the people involved get pinged.
    """

    data = get_env_vars()
    str_limit = data['str_limit']  # slack char limit

    try:
        bot_id = data['bot_id']
    except KeyError:
        bot_id = 1

    try:
        slack_webhook_url = data['slack_webhook_url']
    except KeyError:
        slack_webhook_url = 1

    try:
        discord_webhook_url = data['discord_webhook_url']
    except KeyError:
        discord_webhook_url = 1

    if (len(str(bot_id)) <= 1 and
        len(str(slack_webhook_url)) <= 1 and
            len(str(discord_webhook_url)) <= 1):
        # Ensure that there's info for at least one messaging platform,
        # use length of str in case of blank but non null env variable
        raise Exception("No messaging platform info provided. Be sure one of BOT_ID, SLACK_WEBHOOK_URL, or DISCORD_WEBHOOK_URL env variables are set")

    league_id = data['league_id']

    try:
        year = int(data['year'])
    except KeyError:
        year = 2026

    try:
        swid = data['swid']
    except KeyError:
        swid = '{1}'

    if swid.find("{", 0) == -1:
        swid = "{" + swid
    if swid.find("}", -1) == -1:
        swid = swid + "}"

    try:
        espn_s2 = data['espn_s2']
    except KeyError:
        espn_s2 = '1'

    try:
        close_scores_threshold = data['close_scores_threshold']
    except KeyError:
        close_scores_threshold = espn.CLOSE_SCORES_DEFAULT_THRESHOLD

    groupme_bot = GroupMe(bot_id)
    slack_bot = Slack(slack_webhook_url)
    mentions = callouts.Mentions(data.get('discord_team_mentions'), data.get('discord_announce_role_id'),
                                 data.get('discord_mention_style', callouts.DEFAULT_MENTION_STYLE))
    discord_bot = Discord(discord_webhook_url, data.get('discord_mention_webhook_url'), mentions.notify_users)

    if swid == '{1}' or espn_s2 == '1':
        league = League(league_id=league_id, year=year)
    else:
        league = League(league_id=league_id, year=year, espn_s2=espn_s2, swid=swid)

    try:
        broadcast_message = data['broadcast_message']
    except KeyError:
        broadcast_message = None

    # always let init and broadcast run; the season recap is for after the season
    if function not in ["init", "broadcast", "win_matrix", "trophy_recap", "season_recap", "season_recap_now"] and (league.scoringPeriodId > league.finalScoringPeriod or league.scoringPeriodId < league.firstScoringPeriod):
        logger.info("Not in active season")
        return

    text = ''
    mention_text = ''
    logger.info("Function: " + function)

    if function == "get_matchups":
        box_scores = espn.fetch_box_scores(league)
        text = espn.get_matchups(league, box_scores=box_scores)
        if text != util.NO_MATCHUP_DATA:
            text = text + "\n\n" + espn.get_projected_scoreboard(league, box_scores=box_scores)
    elif function == "get_monitor":
        text = espn.get_monitor(league)
    elif function == "get_scoreboard_short":
        box_scores = espn.fetch_box_scores(league)
        text = espn.get_scoreboard_short(league, box_scores=box_scores)
        if text != util.NO_MATCHUP_DATA:
            text = text + "\n\n" + espn.get_projected_scoreboard(league, box_scores=box_scores)
            mention_text = callouts.live_callouts(box_scores, mentions)
    elif function == "get_projected_scoreboard":
        text = espn.get_projected_scoreboard(league)
    elif function == "get_close_scores":
        text = espn.get_close_scores(league, threshold=close_scores_threshold)
    elif function == "get_power_rankings":
        text = espn.get_power_rankings(league)
    elif function == "get_trophies":
        text = espn.get_trophies(league)
    elif function == "get_standings":
        text = espn.get_standings(league)
    elif function in ("season_recap", "season_recap_now"):
        # Scheduled daily once the window opens; fires once per season, the
        # first day ESPN reports the final period as over. The _now variant
        # posts a to-date recap on demand and does not touch the state.
        state = callouts._load_trade_state(data['state_dir'])
        if function == "season_recap":
            if not recap.season_is_over(league) or recap.recap_already_posted(state, year):
                return
        stats = recap.gather_season(league)
        sections = recap.season_recap_sections(league, stats)
        ping = recap.season_recap_mentions(league, mentions, stats)
        for index, section in enumerate(sections):
            _broadcast(section, ping if index == len(sections) - 1 else '',
                       str_limit, groupme_bot, slack_bot, discord_bot, discord_channel='mentions')
        if function == "season_recap":
            callouts._save_trade_state(data['state_dir'], recap.mark_recap_posted(state, year))
        return
    elif function == "win_matrix":
        text = recap.win_matrix(league)
    elif function == "trophy_recap":
        text = recap.trophy_recap(league)
        # groupme_bot.send_message(text, file_path='/tmp/season_recap.png')
        # slack_bot.send_message(text, file_path='/tmp/season_recap.png')
        # discord_bot.send_message(text, file_path='/tmp/season_recap.png')
    elif function == "get_final":
        # on Tuesday we need to get the scores of last week
        week = league.current_week - 1
        box_scores = espn.fetch_box_scores(league, week=week)
        scores = espn.get_scoreboard_short(league, week=week, box_scores=box_scores)
        if scores == util.NO_MATCHUP_DATA:
            text = scores
        else:
            text = "Final " + scores
            text = text + "\n\n" + espn.get_trophies(league, week=week, box_scores=box_scores)
            mention_text = callouts.final_callouts(league, week, box_scores, mentions)
    elif function == "get_waiver_report":
        faab = league.settings.faab
        text = espn.get_waiver_report(league, faab)
    elif function == "get_trade_announcements":
        accepted = callouts.accepted_trade_alerts(league, data['state_dir'])
        if accepted:
            # The deal rides along in the ping when that goes to another channel.
            split = bool(data.get('discord_mention_webhook_url'))
            _broadcast(accepted, callouts.trade_mention(mentions, accepted if split else None),
                       str_limit, groupme_bot, slack_bot, discord_bot)
        # Completed trades are a quiet confirmation; the ping went out on acceptance.
        text = callouts.trade_announcements(league, data['state_dir'])
    elif function == "broadcast":
        try:
            text = broadcast_message
        except KeyError:
            # do nothing here, empty broadcast message
            pass
    elif function == "init":
        try:
            text = data["init_msg"]
        except KeyError:
            # do nothing here, empty init message
            pass
    else:
        text = "Something bad happened. HALP"

    logger.debug(data)
    _broadcast(text, mention_text, str_limit, groupme_bot, slack_bot, discord_bot)


def _broadcast(text, mention_text, str_limit, groupme_bot, slack_bot, discord_bot, discord_channel='reports'):
    """Send one report to every configured platform, mention text to Discord only."""
    if not util.has_sendable_content(text):
        return
    logger.debug(text)
    messages = util.str_limit_check(text, str_limit)
    for index, message in enumerate(messages):
        groupme_bot.send_message(message)
        slack_bot.send_message(message)
        # Mentions ride on the last chunk so they land under the report.
        discord_bot.send_message(message, mention_text if index == len(messages) - 1 else None,
                                 channel=discord_channel)


if __name__ == '__main__':
    from gamedaybot.espn.scheduler import scheduler

    espn_bot("init")
    scheduler()
