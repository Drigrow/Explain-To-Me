import io
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from etm import cli
from etm.attachments import AttachmentError, discover_files, load_attachments


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def file(self, name, data=b'hello'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path.resolve()

    def test_exact_duplicate_names_and_prompt_separation(self):
        one = self.file('report.csv')
        two = self.file('sub/report.csv')
        self.file('sub/myreport.csv')
        question, paths = discover_files(['compare', 'report.csv', 'please'], self.root)
        self.assertEqual(question, 'compare please')
        self.assertEqual(set(paths), {one, two})

    def test_absolute_path_with_spaces_and_multiple_files(self):
        one = self.file('my report.txt')
        two = self.file('data.json', b'{}')
        question, paths = discover_files(['summarize', str(one), str(two)], self.root)
        self.assertEqual(question, 'summarize')
        self.assertEqual(paths, [one, two])

    def test_split_space_path(self):
        one = self.file('my report.txt')
        self.assertEqual(discover_files(['explain', *str(one).split()], self.root)[1], [one])

    def test_quoted_full_question_with_absolute_path(self):
        one = self.file('my report.txt')
        self.assertEqual(discover_files([f'summarize {one} please'], self.root), ('summarize please', [one]))

    def test_multiword_basename_in_full_prompt(self):
        one = self.file('my report.txt')
        self.assertEqual(discover_files(['summarize my report.txt please'], self.root), ('summarize please', [one]))

    def test_no_substring_or_hidden_directory_search(self):
        self.file('report.csv')
        self.file('.private/secret.csv')
        self.assertEqual(discover_files(['myreport.csv', 'secret.csv'], self.root)[1], [])

    def test_missing_explicit_path_fails(self):
        with self.assertRaises(AttachmentError):
            discover_files([str(self.root / 'missing.pdf'), 'analyze'], self.root)

    def test_plain_question_does_not_walk(self):
        with patch('etm.attachments.os.walk', side_effect=AssertionError('unexpected scan')):
            self.assertEqual(discover_files(['what is SWOT'], self.root), ('what is SWOT', []))

    def test_multimodal_parts(self):
        text = self.file('a.txt', '中文'.encode())
        pdf = self.file('a.pdf', b'%PDF-1.4\n')
        image = self.file('a.png', b'\x89PNG\r\n\x1a\n')
        parts = [a.part() for a in load_attachments([text, pdf, image])]
        self.assertIn('中文', parts[0]['text'])
        self.assertTrue(parts[1]['file']['file_data'].startswith('data:application/pdf;base64,'))
        self.assertTrue(parts[2]['image_url']['url'].startswith('data:image/png;base64,'))

    def test_reject_unsupported_binary_and_oversize(self):
        for name, data in [('a.docx', b'zip'), ('a.txt', b'\xff'), ('nul.txt', b'\x00'), ('big.txt', b'x' * 200001)]:
            with self.subTest(name=name), self.assertRaises(AttachmentError):
                load_attachments([self.file(name, data)])

    def test_deny_reads_no_content(self):
        path = self.file('a.txt')
        with patch.object(cli.sys.stdin, 'isatty', return_value=True), patch('builtins.input', return_value='n'), patch.object(Path, 'open', side_effect=AssertionError('content read')):
            self.assertEqual(cli.confirm_attachments([path], [('gpt', 'model')], cli.DEFAULTS, 'en'), [])

    def test_approve_only_selected(self):
        one, two = self.file('a.txt'), self.file('b.txt')
        with patch.object(cli.sys.stdin, 'isatty', return_value=True), patch('builtins.input', side_effect=['yes', 'n']):
            self.assertEqual([a.path for a in cli.confirm_attachments([one, two], [('gpt', 'model')], cli.DEFAULTS, 'zh')], [one])

    def test_noninteractive_rejected(self):
        with patch.object(cli.sys.stdin, 'isatty', return_value=False), self.assertRaises(AttachmentError):
            cli.confirm_attachments([self.file('a.txt')], [], cli.DEFAULTS, 'en')

    def test_request_contains_files_and_pdf_plugin(self):
        attachments = load_attachments([self.file('a.pdf', b'%PDF-1.4\n')])
        response = io.BytesIO(b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n')
        with patch.object(cli.urllib.request, 'urlopen', return_value=response) as mock:
            self.assertEqual(''.join(cli.stream_openrouter('summarize', 'model', cli.DEFAULTS, 'dummy', 1, attachments=attachments)), 'ok')
        payload = json.loads(mock.call_args.args[0].data)
        self.assertEqual(payload['messages'][-1]['content'][0], {'type': 'text', 'text': 'summarize'})
        self.assertEqual(payload['messages'][-1]['content'][1]['type'], 'file')
        self.assertEqual(payload['plugins'][0]['pdf']['engine'], 'cloudflare-ai')

    def test_plain_requests_remain_plain(self):
        with patch.object(cli.urllib.request, 'urlopen', return_value=io.BytesIO(b'data: [DONE]\n')) as mock:
            list(cli.stream_openrouter('hi', 'model', cli.DEFAULTS, 'dummy', 1))
        payload = json.loads(mock.call_args.args[0].data)
        self.assertEqual(payload['messages'][-1]['content'], 'hi')
        self.assertNotIn('plugins', payload)

    def test_provider_sse_error_not_silenced(self):
        with patch.object(cli.urllib.request, 'urlopen', return_value=io.BytesIO(b'data: {"error":{"message":"vision unsupported"}}\n')):
            with self.assertRaises(cli.EtmError):
                list(cli.stream_openrouter('hi', 'model', cli.DEFAULTS, 'dummy', 1))

    def test_full_flow_does_not_store_source_content(self):
        source = self.file('secret.txt', b'PRIVATE_SOURCE_MARKER')
        captured = []
        def stream(*args):
            captured.append(args)
            yield 'File analysis.'
        args = Namespace(tokens=['gpt', str(source), 'summarize'], detailed=False, lang=None, synth=None, timeout=1, no_synthesis=True, pdf_engine='cloudflare-ai')
        with patch.object(cli, 'APP_DIR', self.root), patch.object(cli, 'CONFIG_PATH', self.root / 'config.json'), patch.object(cli, 'STATE_PATH', self.root / 'state.json'), patch.object(cli, 'get_api_key', return_value='dummy'), patch.object(cli.sys.stdin, 'isatty', return_value=True), patch('builtins.input', return_value='y'), patch.object(cli, 'stream_openrouter', side_effect=stream):
            self.assertEqual(cli.run_question(args), 0)
            self.assertEqual(captured[0][0], 'summarize')
            self.assertEqual(captured[0][8][0].data, b'PRIVATE_SOURCE_MARKER')
            state = cli.STATE_PATH.read_text()
            self.assertNotIn('PRIVATE_SOURCE_MARKER', state)
            self.assertNotIn('dummy', state)
            self.assertIn('secret.txt', state)


if __name__ == '__main__':
    unittest.main()
