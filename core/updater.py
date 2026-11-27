from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from core.score import load_score

_REPOSITORY = 'DamonBBQ/shushu-s-harmonica'
_BRANCH = 'main'
_API_URL = f'https://api.github.com/repos/{_REPOSITORY}/contents/scores?ref={_BRANCH}'
_RAW_ROOT = f'https://raw.githubusercontent.com/{_REPOSITORY}/{_BRANCH}/scores'
_SCORE_SUFFIXES = ('.mid', '.midi', '.txt')
_MANIFEST_NAME = '.git-scores.json'
_TIMEOUT_SECONDS = 20
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_SCORE_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_GIT_SHA = re.compile(r'[0-9a-fA-F]{40}')


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class RemoteScore:
    name: str
    blob_sha: str
    size: int


@dataclass(frozen=True)
class UpdateCheck:
    updates: tuple[RemoteScore, ...]
    total: int
    local_conflicts: tuple[str, ...] = ()


@dataclass(frozen=True)
class InstallResult:
    installed: tuple[Path, ...]
    backup_dir: Path | None


ProgressCallback = Callable[[int, int, str], None]


def git_blob_sha(data: bytes) -> str:
    # Git blob IDs include a header, so they differ from a plain SHA-1 of the file.
    header = f'blob {len(data)}\0'.encode('ascii')
    return hashlib.sha1(header + data).hexdigest()


def _read_url(url: str, limit: int, *, accept: str | None = None) -> bytes:
    headers = {'User-Agent': 'HarmonicaPlayer-Score-Updater'}
    if accept:
        headers['Accept'] = accept
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            content_length = response.headers.get('Content-Length')
            if content_length and content_length.isdigit() and int(content_length) > limit:
                raise UpdateError('更新文件超过允许大小，已取消下载')
            data = response.read(limit + 1)
    except HTTPError as exc:
        if exc.code in (403, 429):
            raise UpdateError('GitHub 更新接口暂时拒绝访问（可能请求过于频繁），请稍后重试') from exc
        raise UpdateError(f'更新服务器返回 HTTP {exc.code}') from exc
    except (URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, 'reason', exc)
        raise UpdateError(f'无法连接 GitHub 更新服务器：{reason}') from exc
    if len(data) > limit:
        raise UpdateError('更新文件超过允许大小，已取消下载')
    return data


def _fetch_remote_scores() -> tuple[RemoteScore, ...]:
    raw = _read_url(
        _API_URL,
        _MAX_MANIFEST_BYTES,
        accept='application/vnd.github+json',
    )
    try:
        entries = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError('GitHub 返回的更新清单无法解析') from exc
    if not isinstance(entries, list):
        raise UpdateError('GitHub 返回的更新清单格式不正确')

    scores: list[RemoteScore] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get('type') != 'file':
            continue
        name = entry.get('name')
        if not isinstance(name, str) or Path(name).suffix.lower() not in _SCORE_SUFFIXES:
            continue
        if name in ('.', '..') or '/' in name or '\\' in name or Path(name).name != name:
            raise UpdateError('更新清单包含不安全的文件名，已取消更新')
        blob_sha = entry.get('sha')
        size = entry.get('size')
        if not isinstance(blob_sha, str) or not _GIT_SHA.fullmatch(blob_sha):
            raise UpdateError(f'更新清单中 {name} 的校验值无效')
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= _MAX_SCORE_BYTES:
            raise UpdateError(f'更新清单中 {name} 的文件大小无效')
        scores.append(RemoteScore(name=name, blob_sha=blob_sha.lower(), size=size))

    if not scores:
        raise UpdateError('GitHub 曲库中没有可更新的 TXT、MID 或 MIDI 文件')
    return tuple(sorted(scores, key=lambda score: score.name.casefold()))


class ScoreUpdater:
    def __init__(self, scores_dir: str | Path):
        self.scores_dir = Path(scores_dir)

    def managed_score_names(self) -> set[str]:
        return self._load_managed_scores()

    def check_for_updates(self) -> UpdateCheck:
        remote_scores = _fetch_remote_scores()
        managed_scores = self._load_managed_scores()
        updates: list[RemoteScore] = []
        local_conflicts: list[str] = []
        for remote in remote_scores:
            path = self.scores_dir / remote.name
            try:
                local_data = path.read_bytes()
            except FileNotFoundError:
                updates.append(remote)
                continue
            except OSError as exc:
                raise UpdateError(f'无法读取本地曲谱 {remote.name}：{exc}') from exc
            if remote.name not in managed_scores:
                local_conflicts.append(remote.name)
                continue
            if git_blob_sha(local_data) != remote.blob_sha:
                # Windows checkouts may convert LF to CRLF while Git stores normalized text.
                if (path.suffix.lower() == '.txt'
                        and git_blob_sha(local_data.replace(b'\r\n', b'\n')) == remote.blob_sha):
                    continue
                updates.append(remote)
        return UpdateCheck(
            updates=tuple(updates),
            total=len(remote_scores),
            local_conflicts=tuple(local_conflicts),
        )

    def install_updates(
        self,
        updates: tuple[RemoteScore, ...],
        progress: ProgressCallback | None = None,
    ) -> InstallResult:
        if not updates:
            return InstallResult(installed=(), backup_dir=None)
        if sum(update.size for update in updates) > _MAX_TOTAL_BYTES:
            raise UpdateError('本次曲库更新超过允许的总大小，已取消下载')
        managed_scores = self._load_managed_scores()
        updated_managed = managed_scores | {update.name for update in updates}
        manifest_changed = updated_managed != managed_scores

        try:
            self.scores_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise UpdateError(f'无法创建曲库目录：{exc}') from exc

        installed: list[Path] = []
        replaced: list[tuple[Path, Path | None]] = []
        backup_dir: Path | None = None
        try:
            with tempfile.TemporaryDirectory(prefix='.update-', dir=self.scores_dir) as staging_name:
                staging = Path(staging_name)
                for index, update in enumerate(updates, 1):
                    data = self._download(update)
                    staged_path = staging / update.name
                    staged_path.write_bytes(data)
                    try:
                        load_score(staged_path)
                    except Exception as exc:
                        raise UpdateError(f'下载的曲谱 {update.name} 无法解析：{exc}') from exc
                    if progress:
                        progress(index, len(updates), update.name)

                if manifest_changed:
                    (staging / _MANIFEST_NAME).write_text(
                        self._serialize_manifest(updated_managed), encoding='utf-8')

                for update in updates:
                    target = self.scores_dir / update.name
                    staged_path = staging / update.name
                    if update.name not in managed_scores:
                        data = staged_path.read_bytes()
                        try:
                            with target.open('xb') as output:
                                replaced.append((target, None))
                                output.write(data)
                        except FileExistsError as exc:
                            raise UpdateError(f'本地同名曲谱 {update.name} 已保留，已取消本次更新') from exc
                        installed.append(target)
                        continue

                    backup: Path | None = None
                    if target.exists():
                        if backup_dir is None:
                            backup_dir = self._new_backup_dir()
                        backup = backup_dir / update.name
                        shutil.copy2(target, backup)
                    os.replace(staged_path, target)
                    replaced.append((target, backup))
                    installed.append(target)

                if manifest_changed:
                    target = self.scores_dir / _MANIFEST_NAME
                    backup: Path | None = None
                    if target.exists():
                        if backup_dir is None:
                            backup_dir = self._new_backup_dir()
                        backup = backup_dir / _MANIFEST_NAME
                        shutil.copy2(target, backup)
                    os.replace(staging / _MANIFEST_NAME, target)
                    replaced.append((target, backup))
        except UpdateError:
            self._rollback(replaced)
            raise
        except OSError as exc:
            self._rollback(replaced)
            raise UpdateError(f'安装曲库更新失败：{exc}') from exc

        return InstallResult(installed=tuple(installed), backup_dir=backup_dir)

    def _load_managed_scores(self) -> set[str]:
        path = self.scores_dir / _MANIFEST_NAME
        try:
            raw = path.read_text(encoding='utf-8')
        except FileNotFoundError:
            return set()
        except (OSError, UnicodeDecodeError) as exc:
            raise UpdateError(f'无法读取 Git 曲目清单：{exc}') from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise UpdateError('Git 曲目清单格式损坏，已取消更新') from exc
        if (not isinstance(data, dict) or data.get('version') != 1
                or not isinstance(data.get('scores'), list)):
            raise UpdateError('Git 曲目清单格式不正确，已取消更新')

        managed: set[str] = set()
        for name in data['scores']:
            if (not isinstance(name, str) or Path(name).suffix.lower() not in _SCORE_SUFFIXES
                    or '/' in name or '\\' in name or Path(name).name != name):
                raise UpdateError('Git 曲目清单包含无效文件名，已取消更新')
            managed.add(name)
        return managed

    @staticmethod
    def _serialize_manifest(scores: set[str]) -> str:
        payload = {'version': 1, 'scores': sorted(scores, key=str.casefold)}
        return json.dumps(payload, ensure_ascii=False, indent=2) + '\n'

    def _download(self, update: RemoteScore) -> bytes:
        data = _read_url(f'{_RAW_ROOT}/{quote(update.name, safe="")}', _MAX_SCORE_BYTES)
        if len(data) != update.size:
            raise UpdateError(f'{update.name} 的下载大小与更新清单不一致')
        if git_blob_sha(data) != update.blob_sha:
            raise UpdateError(f'{update.name} 的校验值不匹配，已取消更新')
        return data

    def _new_backup_dir(self) -> Path:
        root = self.scores_dir / '.update-backups'
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        backup_dir = root / stamp
        backup_dir.mkdir()
        return backup_dir

    @staticmethod
    def _rollback(replaced: list[tuple[Path, Path | None]]) -> None:
        for target, backup in reversed(replaced):
            try:
                if backup is None:
                    target.unlink(missing_ok=True)
                else:
                    shutil.copy2(backup, target)
            except OSError:
                continue
