"""Verify Orvix Chat Completions connectivity with a single minimal request."""
import getpass
import os
import sys

import openai
from openai import OpenAI


BASE_URL = 'https://api.orvix.id/v1'
MODEL = 'orvix/deepseek-v4.1-flash'
TIMEOUT_SECONDS = 120
USER_AGENT = 'moobby-collector/0.1'


def api_key():
    value = os.environ.get('ORVIX_API_KEY', '').strip()
    if not value:
        value = getpass.getpass('Orvix API key (hidden): ').strip()
    return value


def main():
    key = api_key()
    if not key:
        print('API status: failed (ORVIX_API_KEY is missing).')
        return 1

    client = OpenAI(
        api_key=key,
        base_url=BASE_URL,
        default_headers={'User-Agent': USER_AGENT},
        timeout=TIMEOUT_SECONDS,
        max_retries=0,
    )
    try:
        response = client.chat.completions.with_raw_response.create(
            model=MODEL,
            messages=[{'role': 'user', 'content': 'Reply with exactly: OK'}],
            max_tokens=5,
        )
        completion = response.parse()
        print('HTTP status: {}'.format(response.status_code))
        print('API status: success')
        print('Model: {}'.format(completion.model or MODEL))
        print('Response: {}'.format(completion.choices[0].message.content or '(empty)'))
        return 0
    except openai.AuthenticationError:
        print('API status: authentication failed (check key validity and ai:invoke scope).')
    except openai.PermissionDeniedError:
        print('API status: permission denied (check key scope and project access).')
    except openai.RateLimitError:
        print('API status: rate limited (HTTP 429).')
    except openai.APITimeoutError:
        print('API status: timed out after {} seconds.'.format(TIMEOUT_SECONDS))
    except openai.APIConnectionError:
        print('API status: connection failed (Orvix endpoint could not be reached).')
    except openai.APIStatusError as error:
        print('API status: HTTP {} ({})'.format(error.status_code, type(error).__name__))
    except openai.APIError as error:
        print('API status: failed ({})'.format(type(error).__name__))
    finally:
        client.close()
    return 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print('\nAPI status: cancelled.')
        sys.exit(1)
