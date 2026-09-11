import requests
import json
import logging

logger = logging.getLogger(__name__)


class DiscordException(Exception):
    pass


class Discord(object):
    """
    A class used to send messages to a Discord channel through a webhook.

    Parameters
    ----------
    webhook_url : str
        The URL of the Discord webhook to send messages to.

    Attributes
    ----------
    webhook_url : str
        The URL of the Discord webhook to send messages to.

    Methods
    -------
    send_message(text: str)
        Sends a message to the Discord channel.
    """

    def __init__(self, webhook_url):
        self.webhook_url = webhook_url

    def __repr__(self):
        return "Discord Webhook Url(%s)" % self.webhook_url

    def send_message(self, text, mention_text=None):
        """
        Sends a message to the Discord channel.

        Parameters
        ----------
        text : str
            The message to be sent to the Discord channel. It is wrapped in a
            code block, inside which Discord renders no mentions.
        mention_text : str, optional
            Lines appended after the code block as ordinary text, so that
            ``<@user>`` and ``<@&role>`` markup in them pings people. Only
            user and role mentions are allowed through; @everyone is not.

        Returns
        -------
        r : requests.Response
            The response object of the POST request.

        Raises
        ------
        DiscordException
            If there is an error with the POST request.
        """

        message = "```{0}```".format(text)
        if mention_text:
            message = message + "\n" + mention_text
        template = {
            "content": message,  # limit 3000 chars
            "allowed_mentions": {"parse": ["users", "roles"]},
        }

        headers = {'content-type': 'application/json'}

        if self.webhook_url not in (1, "1", ''):
            r = requests.post(self.webhook_url,
                              data=json.dumps(template), headers=headers)

            if r.status_code != 204:
                print(r.content)
                logger.error(r.content)
                raise DiscordException(r.content)

            return r
