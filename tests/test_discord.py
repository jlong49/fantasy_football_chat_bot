import pytest
import sys
import os
sys.path.insert(1, os.path.abspath('.'))
from gamedaybot.chat.discord import (Discord, DiscordException, )


@pytest.mark.usefixtures("mock_requests")
class TestDiscord:
    '''Test DiscordBot class'''

    def setup_method(self):
        self.url = "https://discordapp.com/api/webhooks/123/abc"
        self.test_bot = Discord(self.url)
        self.test_text = "This is a test."

    def test_send_message(self, mock_requests):
        '''Does the message send successfully?'''
        mock_requests.post(self.url, status_code=204)
        assert self.test_bot.send_message(self.test_text).status_code == 204

    def test_wraps_text_in_code_block_and_blocks_everyone(self, mock_requests):
        mock_requests.post(self.url, status_code=204)
        self.test_bot.send_message(self.test_text)
        body = mock_requests.last_request.json()
        assert body['content'] == '```This is a test.```'
        assert body['allowed_mentions'] == {'parse': ['users', 'roles']}

    def test_mention_text_lands_outside_the_code_block(self, mock_requests):
        mock_requests.post(self.url, status_code=204)
        self.test_bot.send_message(self.test_text, mention_text='<@1> is DESTROYING <@2>')
        assert mock_requests.last_request.json()['content'] == \
            '```This is a test.```\n<@1> is DESTROYING <@2>'

    def test_empty_mention_text_adds_nothing(self, mock_requests):
        mock_requests.post(self.url, status_code=204)
        self.test_bot.send_message(self.test_text, mention_text='')
        assert mock_requests.last_request.json()['content'] == '```This is a test.```'

    def test_second_webhook_gets_mentions_on_their_own(self, mock_requests):
        other = "https://discordapp.com/api/webhooks/456/def"
        mock_requests.post(self.url, status_code=204)
        mock_requests.post(other, status_code=204)
        Discord(self.url, other).send_message(self.test_text, mention_text='<@1> is DESTROYING <@2>')
        posts = [(r.url, r.json()['content']) for r in mock_requests.request_history]
        assert posts == [(self.url, '```This is a test.```'), (other, '<@1> is DESTROYING <@2>')]
        assert all(r.json()['allowed_mentions'] == {'parse': ['users', 'roles']} for r in mock_requests.request_history)

    def test_second_webhook_unused_without_mention_text(self, mock_requests):
        other = "https://discordapp.com/api/webhooks/456/def"
        mock_requests.post(self.url, status_code=204)
        Discord(self.url, other).send_message(self.test_text)
        assert [r.url for r in mock_requests.request_history] == [self.url]

    def test_placeholder_second_webhook_means_single_channel(self, mock_requests):
        mock_requests.post(self.url, status_code=204)
        Discord(self.url, "1").send_message(self.test_text, mention_text='hi')
        assert mock_requests.last_request.json()['content'] == '```This is a test.```\nhi'

    def test_mentions_channel_gets_the_whole_message(self, mock_requests):
        other = "https://discordapp.com/api/webhooks/456/def"
        mock_requests.post(other, status_code=204)
        Discord(self.url, other).send_message(self.test_text, mention_text='<@1> wins', channel='mentions')
        posts = [(r.url, r.json()['content']) for r in mock_requests.request_history]
        assert posts == [(other, '```This is a test.```\n<@1> wins')]

    def test_mentions_channel_without_second_webhook_uses_main(self, mock_requests):
        mock_requests.post(self.url, status_code=204)
        self.test_bot.send_message(self.test_text, mention_text='<@1> wins', channel='mentions')
        assert [r.url for r in mock_requests.request_history] == [self.url]
        assert mock_requests.last_request.json()['content'] == '```This is a test.```\n<@1> wins'

    def test_silent_mode_allows_roles_only(self, mock_requests):
        mock_requests.post(self.url, status_code=204)
        Discord(self.url, notify_users=False).send_message(self.test_text, mention_text='<@1> wins')
        body = mock_requests.last_request.json()
        assert body['content'] == '```This is a test.```\n<@1> wins'
        assert body['allowed_mentions'] == {'parse': ['roles']}

    def test_bad_bot_id(self, mock_requests):
        '''Does the expected error raise when a bot id is incorrect?'''
        mock_requests.post(self.url, status_code=404)
        with pytest.raises(DiscordException):
            self.test_bot.send_message(self.test_text)
