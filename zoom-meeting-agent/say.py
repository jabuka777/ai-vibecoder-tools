#!/usr/bin/env python3
"""Send a UTF-8 file to one explicit running SDK session."""
import argparse
import json
from pathlib import Path
import urllib.request


def say(session, text):
    config = json.loads((Path(session) / 'control.json').read_text(encoding='utf-8'))
    if config.get('host') != '127.0.0.1' or not isinstance(config.get('port'), int):
        raise ValueError('Invalid local session endpoint')
    if not 1 <= config['port'] <= 65535 or not isinstance(config.get('token'), str):
        raise ValueError('Invalid local session credential')
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise ValueError('Message must contain 1 to 4000 characters')
    request = urllib.request.Request(f"http://127.0.0.1:{config['port']}/say", method='POST',
              data=json.dumps({'message': text}).encode(), headers={
              'Authorization': 'Bearer ' + config['token'], 'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        return response.status == 200


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', required=True, type=Path)
    parser.add_argument('--file', required=True, type=Path)
    args = parser.parse_args(argv)
    say(args.session, args.file.read_text(encoding='utf-8'))
    print('Queued for the selected session')


if __name__ == '__main__':
    main()
