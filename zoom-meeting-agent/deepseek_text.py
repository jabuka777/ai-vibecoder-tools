"""Explicit, text-only DeepSeek API call. No tools, redirects or automatic retries."""
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

from env import load_env

ENDPOINT = 'https://api.deepseek.com/chat/completions'
MAX_RESPONSE = 2 * 1024 * 1024


class TextAPIError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TextAPIError('deepseek: redirect_rejected')


def default_model():
    return load_env().get('DEEPSEEK_MODEL') or 'deepseek-flash'


def _request_http(prompt, *, role, model, timeout, key):
    if not key or any(c.isspace() for c in key):
        raise TextAPIError('deepseek: configure DEEPSEEK_API_KEY locally first')
    if not isinstance(prompt, str) or len(prompt) > 250000:
        raise TextAPIError('deepseek: input_limit')
    if not isinstance(model, str) or not model.strip() or len(model) > 200:
        raise TextAPIError('deepseek: invalid_model')
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 600:
        raise TextAPIError('deepseek: invalid_timeout')
    payload = {'model': model, 'messages': [{'role': 'system', 'content': role},
               {'role': 'user', 'content': prompt}], 'stream': False,
               'thinking': {'type': 'disabled'}, 'max_tokens': 8192}
    req = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key}, method='POST')
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise TextAPIError('deepseek: response_limit')
        body = json.loads(raw)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        category = {401: 'authentication_required', 402: 'quota_exhausted',
                    429: 'rate_limit', 400: 'request_rejected'}.get(code, 'http_error')
        raise TextAPIError(f'deepseek: {category} ({code})') from None
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise TextAPIError(f'deepseek: request_failed ({type(exc).__name__})') from None
    if not isinstance(body, dict) or body.get('error'):
        raise TextAPIError('deepseek: invalid_response')
    choices = body.get('choices')
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise TextAPIError('deepseek: invalid_choices')
    choice = choices[0]
    msg = choice.get('message')
    if choice.get('finish_reason') != 'stop' or not isinstance(msg, dict):
        raise TextAPIError('deepseek: incomplete_response')
    if msg.get('role') != 'assistant' or msg.get('tool_calls') or msg.get('function_call'):
        raise TextAPIError('deepseek: non_text_response')
    answer = msg.get('content')
    if not isinstance(answer, str) or not answer.strip():
        raise TextAPIError('deepseek: empty_response')
    usage = body.get('usage', {})
    safe_usage = {k: v for k, v in usage.items()
                  if k in {'prompt_tokens', 'completion_tokens', 'total_tokens',
                           'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens'}
                  and type(v) is int and v >= 0} if isinstance(usage, dict) else {}
    return answer.strip(), safe_usage


def request_text(prompt, *, role, model, timeout):
    """A separate deterministic process enforces a total wall-clock deadline.

    Socket inactivity timeouts alone do not bound a response that trickles bytes.
    Key and text go through stdin, never through argv or an agent prompt file.
    """
    from runtime import run_process
    key = load_env().get('DEEPSEEK_API_KEY', '')
    if not key or any(c.isspace() for c in key) or len(key) > 8192:
        raise TextAPIError('deepseek: configure DEEPSEEK_API_KEY locally first')
    if not math.isfinite(timeout) or not 0 < timeout <= 600:
        raise TextAPIError('deepseek: invalid_timeout')
    packet = json.dumps({'prompt': prompt, 'role': role, 'model': model, 'timeout': timeout, 'key': key})
    if len(packet) > 2 * 1024 * 1024:
        raise TextAPIError('deepseek: input_limit')
    safe_env = {k: v for k, v in os.environ.items() if k in {'PATH', 'SYSTEMROOT', 'SystemRoot',
        'TEMP', 'TMP', 'LANG', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'}}
    try:
        result = run_process([sys.executable, '-E', str(Path(__file__).resolve()), '--worker'], input=packet,
                             capture_output=True, text=True, encoding='utf-8', timeout=timeout, env=safe_env)
        response = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        raise TextAPIError('deepseek: deadline_exceeded') from None
    except (OSError, ValueError, subprocess.SubprocessError):
        raise TextAPIError('deepseek: worker_failed') from None
    if result.returncode or not isinstance(response, dict) or not response.get('ok'):
        error = response.get('error', '') if isinstance(response, dict) else ''
        # Worker messages are fixed codes, not remote error bodies.
        allowed = {'authentication_required', 'quota_exhausted', 'rate_limit', 'request_rejected',
                   'redirect_rejected', 'response_limit', 'incomplete_response', 'non_text_response', 'empty_response'}
        category = next((code for code in allowed if error.startswith('deepseek: ' + code)), 'request_failed')
        raise TextAPIError('deepseek: ' + category)
    return response['answer'], response['usage']


def _worker():
    try:
        raw = sys.stdin.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError
        packet = json.loads(raw)
        answer, usage = _request_http(**packet)
        print(json.dumps({'ok': True, 'answer': answer, 'usage': usage}))
        return 0
    except TextAPIError as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}))
    except Exception:
        print(json.dumps({'ok': False, 'error': 'deepseek: request_failed'}))
    return 1


if __name__ == '__main__':
    raise SystemExit(_worker() if sys.argv[1:] == ['--worker'] else 2)
