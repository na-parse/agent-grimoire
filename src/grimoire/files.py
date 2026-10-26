'''On-disk file store: content-addressed blobs plus upload staging.

Blobs live at ``<root>/<sha[:2]>/<sha256>``. Uploads stream into ``<root>/tmp`` on
the same filesystem, so finishing one is an atomic rename and identical content is
stored once. File details (ids, names, uploaders, expiry) live in the database; the
store only knows hashes. Disk writes run on the event loop thread, like database
access: chunks are small and writes land in the page cache.
'''

from __future__ import annotations

import hashlib
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

# Uploads are refused if they would leave the filesystem with less free space
FREE_FLOOR = 0.01

# =============================================================================
# File Store
# =============================================================================


class FileStore:
    '''Blob storage and upload staging under a single root directory.'''

    def __init__(self, root: Path) -> None:
        self.root = root
        self.tmp = root / 'tmp'
        self.tmp.mkdir(parents=True, exist_ok=True)

    def blob_path(self, sha256: str) -> Path:
        '''Where the blob with this hash is stored.'''
        return self.root / sha256[:2] / sha256

    def free(self) -> int:
        '''Bytes that can still be written before the filesystem hits the free floor.'''
        usage = shutil.disk_usage(self.root)
        return max(0, int(usage.free - usage.total * FREE_FLOOR))

    def has_room(self, size: int) -> bool:
        '''True if ``size`` more bytes keep the filesystem above the free floor.'''
        return size <= self.free()

    async def receive(
        self, chunks: AsyncIterator[bytes], file_id: str, limit: int
    ) -> tuple[int, str]:
        '''Stream an upload into staging; return its size and sha256.

        The staged file is removed if the upload fails.

        Raises:
            OverflowError: more than ``limit`` bytes arrived.
        '''
        temp = self.tmp / file_id
        digest = hashlib.sha256()
        size = 0
        try:
            with temp.open('wb') as f:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > limit:
                        raise OverflowError(f'upload exceeds {limit} bytes')
                    digest.update(chunk)
                    f.write(chunk)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        return size, digest.hexdigest()

    def commit(self, file_id: str, sha256: str) -> None:
        '''Move a staged upload into place (dropped if the blob already exists).'''
        temp, blob = self.tmp / file_id, self.blob_path(sha256)
        if blob.exists():
            temp.unlink()
            return
        blob.parent.mkdir(exist_ok=True)
        temp.replace(blob)

    def clear_staging(self) -> None:
        '''Delete all staged uploads (only safe when no upload is running).'''
        for temp in self.tmp.iterdir():
            temp.unlink(missing_ok=True)

    def remove_orphans(self, keep: set[str]) -> int:
        '''Delete blobs whose hash is not in ``keep``; return how many.'''
        orphans = [b for b in self.root.glob('??/*') if b.name not in keep]
        for blob in orphans:
            blob.unlink(missing_ok=True)
        return len(orphans)
