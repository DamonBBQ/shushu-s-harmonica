import hashlib
import json
import os
import stat
import sys
import subprocess
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError

from core.app_updater import (
    ApplicationRelease,
    ApplicationUpdater,
    PreparedUpdate,
    UpdateError,
    parse_version,
)


class Response:
    def __init__(self, data, headers=None):
        self.data = data
        self.headers = headers or {}
        self.stream = BytesIO(data)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, size=-1):
        return self.stream.read(size)


class ApplicationUpdaterTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.updater = ApplicationUpdater()

    @staticmethod
    def release(data=b'package', version='1.1.0'):
        return ApplicationRelease(
            version=version,
            tag=f'v{version}',
            asset_name=f'HarmonicaPlayer-{version}-windows-x64.zip',
            download_url=(
                f'https://github.com/DamonBBQ/shushu-s-harmonica/releases/download/'
                f'v{version}/HarmonicaPlayer-{version}-windows-x64.zip'),
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )

    @staticmethod
    def metadata(release):
        asset = release.asset_name
        return {
            'tag_name': release.tag,
            'draft': False,
            'prerelease': False,
            'assets': [
                {'name': asset, 'size': release.size,
                 'browser_download_url': release.download_url},
                {'name': f'{asset}.sha256', 'size': 65,
                 'browser_download_url': f'{release.download_url}.sha256'},
            ],
        }

    def test_version_comparison_is_numeric(self):
        self.assertEqual(parse_version('v1.10.0'), (1, 10, 0))
        self.assertGreater(parse_version('1.10.0'), parse_version('1.9.9'))
        with self.assertRaises(UpdateError):
            parse_version('1.1-beta')

    def test_check_selects_versioned_package_and_checksum(self):
        release = self.release()
        with patch('core.app_updater._read_url', side_effect=(
                json.dumps(self.metadata(release)).encode('utf-8'),
                f'{release.sha256}  {release.asset_name}\n'.encode('ascii'))):
            self.assertEqual(self.updater.check_for_update('1.0.0'), release)

    def test_check_returns_none_without_a_release(self):
        error = HTTPError('https://github.com', 404, 'Not Found', {}, None)
        self.addCleanup(error.close)
        with patch('core.app_updater._read_url', side_effect=error):
            self.assertIsNone(self.updater.check_for_update())

    def test_check_returns_none_for_current_or_older_version(self):
        release = self.release()
        with patch('core.app_updater._read_url', return_value=json.dumps(self.metadata(release)).encode()):
            self.assertIsNone(self.updater.check_for_update('1.1.0'))
            self.assertIsNone(self.updater.check_for_update('2.0.0'))

    def test_check_falls_back_to_scores_when_release_has_no_windows_package(self):
        with patch('core.app_updater._read_url', return_value=json.dumps({
                'tag_name': 'v1.1.0', 'draft': False, 'prerelease': False, 'assets': [],
        }).encode()):
            self.assertIsNone(self.updater.check_for_update('1.0.0'))

    def test_check_rejects_untrusted_download_host(self):
        release = self.release()
        metadata = self.metadata(release)
        metadata['assets'][0]['browser_download_url'] = 'https://example.com/update.zip'
        with patch('core.app_updater._read_url', return_value=json.dumps(metadata).encode()):
            with self.assertRaisesRegex(UpdateError, '不安全'):
                self.updater.check_for_update('1.0.0')

    def test_check_requires_checksum_asset(self):
        release = self.release()
        metadata = self.metadata(release)
        metadata['assets'].pop()
        with patch('core.app_updater._read_url', return_value=json.dumps(metadata).encode()):
            with self.assertRaisesRegex(UpdateError, 'SHA-256'):
                self.updater.check_for_update('1.0.0')

    def test_download_verifies_size_and_sha256(self):
        data = b'verified package'
        release = self.release(data)
        destination = self.root / 'update.zip'
        progress = []
        with patch('core.app_updater.urlopen', return_value=Response(
                data, {'Content-Length': str(len(data))})):
            self.updater.download(release, destination, lambda *args: progress.append(args))
        self.assertEqual(destination.read_bytes(), data)
        self.assertEqual(progress, [(len(data), len(data), release.asset_name)])

    def test_download_rejects_checksum_mismatch(self):
        release = self.release(b'expected')
        with patch('core.app_updater.urlopen', return_value=Response(b'tampered')):
            with self.assertRaisesRegex(UpdateError, 'SHA-256'):
                self.updater.download(release, self.root / 'update.zip')

    def make_package(self):
        install_dir = self.root / 'HarmonicaPlayer'
        scores_dir = install_dir / 'scores'
        scores_dir.mkdir(parents=True)
        (install_dir / 'HarmonicaPlayer.exe').write_bytes(b'old executable')
        (scores_dir / '.git-scores.json').write_text(json.dumps({
            'version': 1, 'scores': ['built-in.txt'],
        }), encoding='utf-8')
        (scores_dir / 'built-in.txt').write_text('TITLE=old\nBPM=60\n1\n', encoding='utf-8')
        (scores_dir / 'custom.txt').write_text('TITLE=custom\nBPM=60\n2\n', encoding='utf-8')
        (scores_dir / 'new.txt').write_text('TITLE=local conflict\nBPM=60\n3\n', encoding='utf-8')
        (scores_dir / '.update-backups').mkdir()
        (scores_dir / '.update-backups/previous.txt').write_text('backup', encoding='utf-8')

        archive = BytesIO()
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as package:
            package.writestr('HarmonicaPlayer/HarmonicaPlayer.exe', b'new executable')
            package.writestr('HarmonicaPlayer/_internal/python312.dll', b'dependency')
            package.writestr('HarmonicaPlayer/app-version.txt', '1.1.0\n')
            package.writestr('HarmonicaPlayer/scores/.git-scores.json', json.dumps({
                'version': 1, 'scores': ['built-in.txt', 'new.txt'],
            }))
            package.writestr('HarmonicaPlayer/scores/built-in.txt', 'TITLE=new\nBPM=60\n4\n')
            package.writestr('HarmonicaPlayer/scores/new.txt', 'TITLE=remote\nBPM=60\n5\n')
        return install_dir, scores_dir, archive.getvalue()

    def test_prepare_replaces_managed_scores_and_preserves_local_files(self):
        install_dir, scores_dir, data = self.make_package()
        release = self.release(data)

        def download(_release, destination, progress):
            destination.write_bytes(data)
            if progress:
                progress(len(data), len(data), release.asset_name)

        with patch.object(self.updater, 'download', side_effect=download):
            prepared = self.updater.prepare_update(release, install_dir, scores_dir)
        self.addCleanup(prepared.cleanup)
        app_dir = prepared.staging_dir / 'payload' / 'HarmonicaPlayer'
        self.assertEqual((app_dir / 'HarmonicaPlayer.exe').read_bytes(), b'new executable')
        self.assertEqual((app_dir / '_internal/python312.dll').read_bytes(), b'dependency')
        self.assertIn('TITLE=new', (app_dir / 'scores/built-in.txt').read_text(encoding='utf-8'))
        self.assertIn('TITLE=custom', (app_dir / 'scores/custom.txt').read_text(encoding='utf-8'))
        self.assertIn('TITLE=local conflict', (app_dir / 'scores/new.txt').read_text(encoding='utf-8'))
        self.assertEqual((app_dir / 'scores/.update-backups/previous.txt').read_text(encoding='utf-8'), 'backup')
        manifest = json.loads((app_dir / 'scores/.git-scores.json').read_text(encoding='utf-8'))
        self.assertEqual(set(manifest['scores']), {'built-in.txt'})
        self.assertEqual((scores_dir / 'built-in.txt').read_text(encoding='utf-8'),
                         'TITLE=old\nBPM=60\n1\n')
        self.assertTrue(prepared.script_path.read_bytes().startswith(b'\xef\xbb\xbf'))
        config = json.loads(prepared.config_path.read_text(encoding='utf-8'))
        self.assertEqual(Path(config['install_dir']), install_dir)
        self.assertTrue(Path(config['backup_dir']).is_relative_to(install_dir.parent))

    def test_prepare_cleans_staging_when_local_manifest_is_corrupt(self):
        install_dir, scores_dir, data = self.make_package()
        (scores_dir / '.git-scores.json').write_text('{broken', encoding='utf-8')
        release = self.release(data)

        def download(_release, destination, _progress):
            destination.write_bytes(data)

        with patch.object(self.updater, 'download', side_effect=download):
            with self.assertRaisesRegex(UpdateError, '清单格式损坏'):
                self.updater.prepare_update(release, install_dir, scores_dir)
        self.assertEqual(list(install_dir.parent.glob('.HarmonicaPlayer-update-*')), [])

    def test_extract_rejects_path_traversal(self):
        archive = self.root / 'bad.zip'
        with zipfile.ZipFile(archive, 'w') as package:
            package.writestr('HarmonicaPlayer/../escape.txt', b'bad')
        with self.assertRaisesRegex(UpdateError, '不安全'):
            self.updater._extract_package(archive, self.root / 'extracted')
        self.assertFalse((self.root / 'escape.txt').exists())

    def test_extract_rejects_symbolic_links(self):
        archive = self.root / 'link.zip'
        with zipfile.ZipFile(archive, 'w') as package:
            entry = zipfile.ZipInfo('HarmonicaPlayer/link')
            entry.create_system = 3
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            package.writestr(entry, b'../outside')
        with self.assertRaisesRegex(UpdateError, '符号链接'):
            self.updater._extract_package(archive, self.root / 'extracted')

    def test_extract_rejects_case_insensitive_duplicates(self):
        archive = self.root / 'duplicate.zip'
        with zipfile.ZipFile(archive, 'w') as package:
            package.writestr('HarmonicaPlayer/File.txt', b'one')
            package.writestr('HarmonicaPlayer/file.txt', b'two')
        with self.assertRaisesRegex(UpdateError, '重复'):
            self.updater._extract_package(archive, self.root / 'extracted')

    def test_validate_requires_runtime_dependencies(self):
        app_dir = self.root / 'HarmonicaPlayer'
        app_dir.mkdir()
        (app_dir / 'HarmonicaPlayer.exe').write_bytes(b'exe')
        (app_dir / 'app-version.txt').write_text('1.1.0', encoding='utf-8')
        (app_dir / 'scores').mkdir()
        (app_dir / 'scores/.git-scores.json').write_text(
            '{"version": 1, "scores": []}', encoding='utf-8')
        with self.assertRaisesRegex(UpdateError, '运行依赖'):
            self.updater._validate_package(app_dir, '1.1.0')

    @unittest.skipUnless(sys.platform == 'win32', 'Windows update helper only')
    def test_prepared_update_launches_detached_powershell(self):
        script = self.root / 'apply-update.ps1'
        config = self.root / 'update.json'
        script.write_text('', encoding='utf-8')
        config.write_text('{}', encoding='utf-8')
        install_dir = self.root / 'HarmonicaPlayer'
        install_dir.mkdir()
        prepared = PreparedUpdate(self.root, install_dir, script, config)
        with patch('core.app_updater.subprocess.Popen') as popen:
            prepared.launch()
        command = popen.call_args.args[0]
        self.assertEqual(command[0], str(Path(os.environ['SystemRoot']) /
                                        'System32/WindowsPowerShell/v1.0/powershell.exe'))
        self.assertIn('-File', command)
        self.assertEqual(command[-2:], [str(script), str(config)])
        self.assertTrue(popen.call_args.kwargs['creationflags'] & subprocess.CREATE_NO_WINDOW)


if __name__ == '__main__':
    unittest.main()
