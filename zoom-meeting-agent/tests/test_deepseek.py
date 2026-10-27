import io
import json
from pathlib import Path
import sys
import subprocess
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deepseek_text as api
import llm


class DeepSeekTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(api, 'load_env', return_value={'DEEPSEEK_API_KEY': 'SYNTHETIC_KEY'})
        p.start()
        self.addCleanup(p.stop)

    def response(self, text='Result', finish='stop', **message):
        return {'choices': [{'finish_reason': finish, 'message': {'role': 'assistant', 'content': text, **message}}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'private_payload': 'SYNTHETIC_SECRET'}}

    def call(self, payload=None):
        response = io.BytesIO(json.dumps(payload if payload is not None else self.response()).encode())
        with patch.object(api.urllib.request, 'build_opener') as build:
            build.return_value.open.return_value = response
            result, _ = api._request_http('Synthetic meeting', role='text-only', model='deepseek-flash', timeout=10, key='SYNTHETIC_KEY')
            request = build.return_value.open.call_args.args[0]
        return result, request

    def test_explicit_text_only_request_uses_fixed_endpoint_and_no_cli(self):
        with patch.object(llm, 'executable', side_effect=AssertionError('must not launch CLI')):
            result, request = self.call()
        self.assertEqual(result, 'Result')
        self.assertEqual(request.full_url, api.ENDPOINT)
        body = json.loads(request.data)
        self.assertEqual(body['model'], 'deepseek-flash')
        self.assertNotIn('tools', body)
        self.assertNotIn('functions', body)
        self.assertEqual(body['messages'][1]['content'], 'Synthetic meeting')

    def test_tool_truncated_error_and_invalid_outputs_are_rejected(self):
        cases = [self.response(finish='length'), self.response(tool_calls=[{'name': 'shell'}]),
                 self.response(function_call={'name': 'shell'}), self.response(text=''),
                 None, [], {'error': {'message': 'SYNTHETIC_SECRET'}}, {'choices': []},
                 self.response(text={'invalid': 'shape'})]
        for payload in cases:
            with self.subTest(payload=payload), patch.object(api.urllib.request, 'build_opener') as build:
                build.return_value.open.return_value = io.BytesIO(json.dumps(payload).encode())
                with self.assertRaises(api.TextAPIError) as exc:
                    api._request_http('Synthetic meeting', role='text', model='model', timeout=10, key='SYNTHETIC_KEY')
                self.assertNotIn('SYNTHETIC_SECRET', str(exc.exception))

    def test_api_failures_never_echo_body_key_or_prompt(self):
        error = urllib.error.HTTPError(api.ENDPOINT, 429, 'SYNTHETIC_SECRET', {}, io.BytesIO(b'SYNTHETIC_SECRET'))
        with patch.object(api.urllib.request, 'build_opener') as build:
            build.return_value.open.side_effect = error
            with self.assertRaisesRegex(api.TextAPIError, 'rate_limit') as exc:
                api._request_http('SYNTHETIC_SECRET', role='text', model='model', timeout=10, key='SYNTHETIC_KEY')
            self.assertNotIn('SYNTHETIC_SECRET', str(exc.exception))
            self.assertEqual(build.return_value.open.call_count, 1)

    def test_response_limit_and_redirect_are_rejected(self):
        with patch.object(api.urllib.request, 'build_opener') as build:
            build.return_value.open.return_value = io.BytesIO(b' ' * (api.MAX_RESPONSE + 1))
            with self.assertRaisesRegex(api.TextAPIError, 'response_limit'):
                api._request_http('meeting', role='text', model='model', timeout=10, key='SYNTHETIC_KEY')
        with self.assertRaises(api.TextAPIError):
            api.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.invalid')

    def test_no_key_no_network_and_usage_is_numeric_only(self):
        with patch.object(api, 'load_env', return_value={}), patch.object(api.urllib.request, 'build_opener') as build:
            with self.assertRaises(llm.ModelError):
                llm.call('meeting', backend='deepseek')
            build.assert_not_called()
        with patch.object(api.urllib.request, 'build_opener') as build:
            build.return_value.open.return_value = io.BytesIO(json.dumps(self.response()).encode())
            _, usage = api._request_http('meeting', role='text', model='model', timeout=10, key='SYNTHETIC_KEY')
        self.assertEqual(usage, {'prompt_tokens': 10, 'completion_tokens': 2})

    def test_total_deadline_is_enforced_by_owned_worker_and_secrets_are_stdin_only(self):
        def run(args, **kwargs):
            self.assertNotIn('SYNTHETIC_KEY', ' '.join(args))
            self.assertNotIn('Synthetic meeting', ' '.join(args))
            self.assertNotIn('DEEPSEEK_API_KEY', kwargs['env'])
            self.assertEqual(json.loads(kwargs['input'])['key'], 'SYNTHETIC_KEY')
            self.assertEqual(kwargs['timeout'], 0.1)
            raise subprocess.TimeoutExpired(args, 0.1)
        with patch('runtime.run_process', side_effect=run):
            with self.assertRaisesRegex(llm.ModelError, 'deadline_exceeded'):
                llm.call('Synthetic meeting', backend='deepseek', timeout=0.1)

    def test_worker_result_reaches_llm_without_cli_and_non_text_failures_are_redacted(self):
        result = subprocess.CompletedProcess([], 0, json.dumps({'ok': True, 'answer': 'Result', 'usage': {'total_tokens': 2}}), '')
        with patch('runtime.run_process', return_value=result), patch.object(llm, 'executable', side_effect=AssertionError):
            self.assertEqual(llm.call('Synthetic meeting', backend='deepseek'), 'Result')


if __name__ == '__main__':
    unittest.main()
