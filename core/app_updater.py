from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from core.updater import ScoreUpdater, UpdateError
from core.version import __version__

_REPOSITORY = 'DamonBBQ/shushu-s-harmonica'
_RELEASE_URL = f'https://api.github.com/repos/{_REPOSITORY}/releases/latest'
_TIMEOUT_SECONDS = 30
_MAX_METADATA_BYTES = 1024 * 1024
_MAX_PACKAGE_BYTES = 512 * 1024 * 1024
_MAX_UNPACKED_BYTES = 2 * 1024 * 1024 * 1024
_MAX_ARCHIVE_ENTRIES = 30_000
_VERSION = re.compile(r'(\d+)\.(\d+)\.(\d+)')
_SHA256 = re.compile(r'[0-9a-fA-F]{64}')
_PACKAGE_ROOT = 'HarmonicaPlayer'

ProgressCallback = Callable[[int, int, str], None]


@dataclass(frozen=True)
class ApplicationRelease:
    version: str
    tag: str
    asset_name: str
    download_url: str
    size: int
    sha256: str


@dataclass(frozen=True)
class PreparedUpdate:
    staging_dir: Path
    install_dir: Path
    script_path: Path
    config_path: Path

    def cleanup(self) -> None:
        shutil.rmtree(self.staging_dir, ignore_errors=True)

    def launch(self) -> None:
        if sys.platform != 'win32':
            raise UpdateError('程序自动更新仅支持 Windows')
        powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / (
            'System32/WindowsPowerShell/v1.0/powershell.exe')
        if not powershell.is_file():
            raise UpdateError('未找到 Windows PowerShell，无法执行程序更新')
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            [str(powershell), '-NoProfile', '-ExecutionPolicy', 'Bypass',
             '-File', str(self.script_path), str(self.config_path)],
            cwd=self.install_dir.parent,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
        )


def parse_version(value: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(value.strip().removeprefix('v'))
    if match is None:
        raise UpdateError(f'版本号格式不正确：{value}')
    return tuple(int(part) for part in match.groups())


def _read_url(
    url: str,
    limit: int,
    *,
    accept: str | None = None,
    allow_missing: bool = False,
) -> bytes:
    headers = {'User-Agent': 'HarmonicaPlayer-App-Updater'}
    if accept:
        headers['Accept'] = accept
    try:
        with urlopen(Request(url, headers=headers), timeout=_TIMEOUT_SECONDS) as response:
            data = response.read(limit + 1)
    except HTTPError as exc:
        if exc.code == 404 and allow_missing:
            raise
        if exc.code in (403, 429):
            raise UpdateError('GitHub 更新接口暂时拒绝访问（可能请求过于频繁），请稍后重试') from exc
        raise UpdateError(f'更新服务器返回 HTTP {exc.code}') from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f'无法连接 GitHub 更新服务器：{getattr(exc, "reason", exc)}') from exc
    if len(data) > limit:
        raise UpdateError('更新数据超过允许大小，已取消更新')
    return data


def _safe_download_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == 'https' and parsed.hostname == 'github.com' and not parsed.username


class ApplicationUpdater:
    def check_for_update(self, current_version: str = __version__) -> ApplicationRelease | None:
        try:
            raw = _read_url(
                _RELEASE_URL,
                _MAX_METADATA_BYTES,
                accept='application/vnd.github+json',
                allow_missing=True,
            )
        except HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        try:
            release = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateError('GitHub 返回的程序版本信息无法解析') from exc
        if not isinstance(release, dict):
            raise UpdateError('GitHub 返回的程序版本信息格式不正确')
        if release.get('draft') or release.get('prerelease'):
            return None

        tag = release.get('tag_name')
        if not isinstance(tag, str):
            raise UpdateError('GitHub 发布版本缺少版本号')
        try:
            remote_version = parse_version(tag)
        except UpdateError as exc:
            raise UpdateError(f'GitHub 发布版本无效：{tag}') from exc
        if remote_version <= parse_version(current_version):
            return None

        assets = release.get('assets')
        if not isinstance(assets, list):
            raise UpdateError('GitHub 发布版本缺少文件清单')
        version = '.'.join(str(part) for part in remote_version)
        asset_name = f'HarmonicaPlayer-{version}-windows-x64.zip'
        package = next((asset for asset in assets
                        if isinstance(asset, dict) and asset.get('name') == asset_name), None)
        if package is None:
            return None

        download_url = package.get('browser_download_url')
        size = package.get('size')
        if not isinstance(download_url, str) or not _safe_download_url(download_url):
            raise UpdateError('程序更新包下载地址不安全，已取消更新')
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= _MAX_PACKAGE_BYTES:
            raise UpdateError('程序更新包大小无效，已取消更新')

        checksum_asset = next((asset for asset in assets
                               if isinstance(asset, dict) and asset.get('name') == f'{asset_name}.sha256'), None)
        checksum_url = checksum_asset.get('browser_download_url') if checksum_asset else None
        if not isinstance(checksum_url, str) or not _safe_download_url(checksum_url):
            raise UpdateError('程序更新包缺少有效的 SHA-256 校验文件')
        try:
            checksum_data = _read_url(checksum_url, 4096).decode('ascii', errors='strict').split()
        except UnicodeDecodeError as exc:
            raise UpdateError('程序更新包的 SHA-256 校验文件无法解析') from exc
        if not checksum_data or _SHA256.fullmatch(checksum_data[0]) is None:
            raise UpdateError('程序更新包的 SHA-256 校验值无效')
        return ApplicationRelease(
            version=version,
            tag=tag,
            asset_name=asset_name,
            download_url=download_url,
            size=size,
            sha256=checksum_data[0].lower(),
        )

    def download(
        self,
        release: ApplicationRelease,
        destination: Path,
        progress: ProgressCallback | None = None,
    ) -> Path:
        if not _safe_download_url(release.download_url):
            raise UpdateError('程序更新包下载地址不安全，已取消下载')
        headers = {'User-Agent': 'HarmonicaPlayer-App-Updater'}
        digest = hashlib.sha256()
        downloaded = 0
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with urlopen(Request(release.download_url, headers=headers), timeout=_TIMEOUT_SECONDS) as response:
                content_length = response.headers.get('Content-Length')
                if (content_length and content_length.isdigit()
                        and int(content_length) != release.size):
                    raise UpdateError('程序更新包大小与发布清单不一致')
                with destination.open('wb') as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        downloaded += len(chunk)
                        if downloaded > release.size or downloaded > _MAX_PACKAGE_BYTES:
                            raise UpdateError('程序更新包超过允许大小，已取消下载')
                        output.write(chunk)
                        digest.update(chunk)
                        if progress:
                            progress(downloaded, release.size, release.asset_name)
        except HTTPError as exc:
            raise UpdateError(f'下载程序更新包失败：HTTP {exc.code}') from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise UpdateError(f'下载程序更新包失败：{getattr(exc, "reason", exc)}') from exc
        if downloaded != release.size:
            raise UpdateError('程序更新包下载不完整')
        if digest.hexdigest() != release.sha256:
            raise UpdateError('程序更新包 SHA-256 校验失败，已取消更新')
        return destination

    def prepare_update(
        self,
        release: ApplicationRelease,
        install_dir: Path,
        scores_dir: Path,
        progress: ProgressCallback | None = None,
    ) -> PreparedUpdate:
        install_dir = install_dir.resolve()
        if not install_dir.is_dir():
            raise UpdateError(f'未找到程序目录：{install_dir}')
        try:
            staging_dir = Path(tempfile.mkdtemp(prefix='.HarmonicaPlayer-update-', dir=install_dir.parent))
        except OSError as exc:
            raise UpdateError(f'无法创建更新目录，请检查程序安装位置是否可写：{exc}') from exc

        try:
            archive = staging_dir / 'update.zip'
            self.download(release, archive, progress)
            extracted = staging_dir / 'payload'
            self._extract_package(archive, extracted)
            app_dir = extracted / _PACKAGE_ROOT
            self._validate_package(app_dir, release.version)
            self._preserve_local_scores(scores_dir, app_dir / 'scores')

            backup_dir = staging_dir.with_name(f'{staging_dir.name}-backup')
            log_path = install_dir.parent / '.HarmonicaPlayer-update.log'
            config_path = staging_dir / 'update-config.json'
            config_path.write_text(json.dumps({
                'process_id': os.getpid(),
                'install_dir': str(install_dir),
                'staged_app': str(app_dir),
                'backup_dir': str(backup_dir),
                'staging_dir': str(staging_dir),
                'log_path': str(log_path),
            }), encoding='utf-8')
            script_path = staging_dir / 'apply-update.ps1'
            script_path.write_text(_APPLY_SCRIPT, encoding='utf-8-sig')
            return PreparedUpdate(staging_dir, install_dir, script_path, config_path)
        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise

    @staticmethod
    def _extract_package(archive: Path, destination: Path) -> None:
        try:
            with zipfile.ZipFile(archive) as package:
                entries = package.infolist()
                if len(entries) > _MAX_ARCHIVE_ENTRIES:
                    raise UpdateError('程序更新包文件数量过多')
                total_size = 0
                seen: set[str] = set()
                for entry in entries:
                    name = entry.filename
                    if '\\' in name or '\0' in name:
                        raise UpdateError('程序更新包包含不安全的路径')
                    clean_name = name[:-1] if name.endswith('/') else name
                    parts = clean_name.split('/')
                    if (not parts or any(part in ('', '.', '..') or ':' in part for part in parts)
                            or parts[0] != _PACKAGE_ROOT):
                        raise UpdateError('程序更新包包含不安全的路径')
                    normalized = '/'.join(parts).casefold()
                    if normalized in seen:
                        raise UpdateError('程序更新包包含重复文件名')
                    seen.add(normalized)
                    mode = entry.external_attr >> 16
                    if stat.S_ISLNK(mode):
                        raise UpdateError('程序更新包包含符号链接，已取消更新')
                    if entry.is_dir():
                        continue
                    total_size += entry.file_size
                    if entry.file_size > _MAX_PACKAGE_BYTES or total_size > _MAX_UNPACKED_BYTES:
                        raise UpdateError('程序更新包解压后超过允许大小')
                    target = destination.joinpath(*parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(entry) as source, target.open('wb') as output:
                        shutil.copyfileobj(source, output)
                destination.mkdir(parents=True, exist_ok=True)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
            raise UpdateError(f'程序更新包损坏或使用了不支持的压缩格式：{exc}') from exc
        except OSError as exc:
            raise UpdateError(f'解压程序更新包失败：{exc}') from exc

    @staticmethod
    def _validate_package(app_dir: Path, version: str) -> None:
        executable = app_dir / 'HarmonicaPlayer.exe'
        if not executable.is_file() or not (app_dir / '_internal').is_dir():
            raise UpdateError('程序更新包缺少 EXE 或运行依赖')
        try:
            packaged_version = (app_dir / 'app-version.txt').read_text(encoding='utf-8').strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise UpdateError('程序更新包缺少有效的版本标记') from exc
        if packaged_version != version:
            raise UpdateError('程序更新包版本与发布版本不一致')
        scores = app_dir / 'scores'
        if not scores.is_dir():
            raise UpdateError('程序更新包缺少内置曲库')
        ScoreUpdater(scores).managed_score_names()

    @staticmethod
    def _preserve_local_scores(scores_dir: Path, packaged_scores: Path) -> None:
        managed = ScoreUpdater(scores_dir).managed_score_names()
        packaged_managed = ScoreUpdater(packaged_scores).managed_score_names()
        if not scores_dir.is_dir():
            return
        local_conflicts: set[str] = set()
        try:
            for source in scores_dir.iterdir():
                if source.name == '.git-scores.json' or source.name in managed:
                    continue
                if source.name == '.update-backups' and source.is_dir():
                    shutil.copytree(source, packaged_scores / source.name, dirs_exist_ok=True)
                    continue
                if source.suffix.lower() not in ('.txt', '.mid', '.midi') or not source.is_file():
                    continue
                shutil.copy2(source, packaged_scores / source.name)
                if source.name in packaged_managed:
                    local_conflicts.add(source.name)
            if local_conflicts:
                (packaged_scores / '.git-scores.json').write_text(
                    ScoreUpdater._serialize_manifest(packaged_managed - local_conflicts),
                    encoding='utf-8',
                )
        except OSError as exc:
            raise UpdateError(f'保留本地曲谱失败：{exc}') from exc


_APPLY_SCRIPT = r'''$ErrorActionPreference = 'Stop'
$configPath = $args[0]
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$moved = $false
try {
    $process = Get-Process -Id ([int]$config.process_id) -ErrorAction SilentlyContinue
    if ($process) { Wait-Process -Id ([int]$config.process_id) -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $config.backup_dir) { throw '更新备份目录已存在，已取消替换' }
    Move-Item -LiteralPath $config.install_dir -Destination $config.backup_dir
    try {
        Move-Item -LiteralPath $config.staged_app -Destination $config.install_dir
        $moved = $true
    } catch {
        Move-Item -LiteralPath $config.backup_dir -Destination $config.install_dir
        throw
    }
    $executable = Join-Path $config.install_dir 'HarmonicaPlayer.exe'
    try {
        Start-Process -FilePath $executable -WorkingDirectory $config.install_dir
    } catch {
        Remove-Item -LiteralPath $config.install_dir -Recurse -Force -ErrorAction SilentlyContinue
        $moved = $false
        Move-Item -LiteralPath $config.backup_dir -Destination $config.install_dir
        throw
    }
    Remove-Item -LiteralPath $config.backup_dir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $config.staging_dir -Recurse -Force -ErrorAction SilentlyContinue
} catch {
    if (-not $moved -and (Test-Path -LiteralPath $config.backup_dir) -and
        -not (Test-Path -LiteralPath $config.install_dir)) {
        Move-Item -LiteralPath $config.backup_dir -Destination $config.install_dir -ErrorAction SilentlyContinue
    }
    $_ | Out-File -LiteralPath $config.log_path -Append -Encoding utf8
    exit 1
}
'''
