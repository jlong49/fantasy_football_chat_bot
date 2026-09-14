import os
import gamedaybot.espn.functionality as espn
import gamedaybot.espn.callouts as callouts
import gamedaybot.utils.util as utils


def get_env_vars():
    data = {}
    try:
        ff_start_date = os.environ["START_DATE"]
    except KeyError:
        ff_start_date = '2026-09-10'

    data['ff_start_date'] = ff_start_date

    try:
        ff_end_date = os.environ["END_DATE"]
    except KeyError:
        ff_end_date = '2027-01-10'

    data['ff_end_date'] = ff_end_date

    try:
        my_timezone = os.environ["TIMEZONE"]
    except KeyError:
        my_timezone = 'America/New_York'

    data['my_timezone'] = my_timezone

    try:
        daily_waiver = utils.str_to_bool(os.environ["DAILY_WAIVER"])
    except KeyError:
        daily_waiver = False

    data['daily_waiver'] = daily_waiver

    try:
        monitor_report = utils.str_to_bool(os.environ["MONITOR_REPORT"])
    except KeyError:
        monitor_report = True

    data['monitor_report'] = monitor_report

    try:
        close_scores_threshold = int(os.environ["CLOSE_SCORES_THRESHOLD"])
    except (KeyError, ValueError):
        # Unset, or set to something that is not a whole number. A typo in one
        # optional env var should not take down every scheduled message, so
        # fall back to the default rather than raise.
        close_scores_threshold = espn.CLOSE_SCORES_DEFAULT_THRESHOLD

    data['close_scores_threshold'] = close_scores_threshold

    str_limit = 40000  # slack char limit

    try:
        bot_id = os.environ["BOT_ID"]
        str_limit = 1000
    except KeyError:
        bot_id = 1

    try:
        slack_webhook_url = os.environ["SLACK_WEBHOOK_URL"]
    except KeyError:
        slack_webhook_url = 1

    try:
        discord_webhook_url = os.environ["DISCORD_WEBHOOK_URL"]
        # Discord rejects content over 2000 characters; the code fence and a
        # trailing mention line need room inside that.
        str_limit = 1900
    except KeyError:
        discord_webhook_url = 1

    if (len(str(bot_id)) <= 1 and
        len(str(slack_webhook_url)) <= 1 and
            len(str(discord_webhook_url)) <= 1):
        # Ensure that there's info for at least one messaging platform,
        # use length of str in case of blank but non null env variable
        raise Exception("No messaging platform info provided. Be sure one of BOT_ID, SLACK_WEBHOOK_URL, or DISCORD_WEBHOOK_URL env variables are set")

    data['str_limit'] = str_limit
    data['bot_id'] = bot_id
    data['slack_webhook_url'] = slack_webhook_url
    data['discord_webhook_url'] = discord_webhook_url

    data['league_id'] = os.environ["LEAGUE_ID"]

    try:
        year = int(os.environ["LEAGUE_YEAR"])
    except KeyError:
        year = 2026

    data['year'] = year

    try:
        swid = os.environ["SWID"]
    except KeyError:
        swid = '{1}'

    if swid.find("{", 0) == -1:
        swid = "{" + swid
    if swid.find("}", -1) == -1:
        swid = swid + "}"

    data['swid'] = swid

    try:
        espn_s2 = os.environ["ESPN_S2"]
    except KeyError:
        espn_s2 = '1'

    data['espn_s2'] = espn_s2

    try:
        test = utils.str_to_bool(os.environ["TEST"])
    except KeyError:
        test = False

    data['test'] = test

    try:
        waiver_report = utils.str_to_bool(os.environ["WAIVER_REPORT"])
    except KeyError:
        waiver_report = False

    data['waiver_report'] = waiver_report

    # Discord-only extras. ESPN team id -> Discord user id pairs drive the
    # @mention callouts; the role id is pinged for league-wide announcements
    # such as trades. Both are optional and change nothing when unset.
    data['discord_team_mentions'] = callouts.parse_team_mentions(os.environ.get("DISCORD_TEAM_MENTIONS", ""))
    data['discord_announce_role_id'] = os.environ.get("DISCORD_ANNOUNCE_ROLE_ID", "")
    # ping (notify), silent (blue @name, no notification) or names (team names).
    data['discord_mention_style'] = callouts.parse_mention_style(os.environ.get("DISCORD_MENTION_STYLE", ""))
    # Optional second webhook. When set, the mention lines go there as their
    # own short messages and the reports stay in DISCORD_WEBHOOK_URL's channel.
    data['discord_mention_webhook_url'] = os.environ.get("DISCORD_MENTION_WEBHOOK_URL", "")

    # Where the trade announcer keeps its "already announced" file. /tmp is
    # fine for a long-lived container; point it at a volume to survive restarts.
    data['state_dir'] = os.environ.get("STATE_DIR", "/tmp")

    try:
        data['init_msg'] = os.environ["INIT_MSG"]
    except KeyError:
        # do nothing here, empty init message
        pass

    return data
