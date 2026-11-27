#!/usr/bin/env python3
"""Offline QCOW2 layer consolidation for one AuroFlow private HF dataset.

Runs only when no AuroFlow VM workflow can be active: both workflows must
share the `windows-golden` concurrency group in the instance repository.
"""

import json
import os
import shutil
import sys
import uuid
from pathlib import Path

from huggingface_hub import CommitOperationAdd, CommitOperationDelete

import persistence as p

MIN_LAYERS = 2
MIN_GAIN = 0.0


def main():
    if not os.environ.get('HF_TOKEN'):
        print('[COMPACT] No Hugging Face token, skipping')
        return

    metadata = p.api.repo_info(p.repo, **p.opts)
    if not metadata.private:
        raise RuntimeError('Persistence dataset must be private')

    revision = metadata.sha
    paths = set(p.api.list_repo_files(p.repo, revision=revision, **p.opts))
    if 'latest.json' not in paths:
        print('[COMPACT] No persistent layers yet')
        return

    manifest = json.loads(p.download('latest.json', revision).read_text())
    chain = list(manifest['chain'])  # Snapshot! Never mutate before finding old files.
    if len(chain) < MIN_LAYERS:
        print(f'[COMPACT] {len(chain)} layer(s), nothing to consolidate')
        return

    # Confirm a clean, unchanged base and every layer before any conversion.
    p.restore()
    session = json.loads((p.root / 'session.json').read_text())
    if session['revision'] != revision or session['state'] != manifest:
        raise RuntimeError('Dataset changed during verification')

    previous_size = sum(
        (p.layers / item['id'] / 'disk.qcow2').stat().st_size
        for item in chain
    )
    if previous_size == 0:
        raise RuntimeError('Invalid empty layer chain')

    source = p.layers / chain[-1]['id'] / 'disk.qcow2'
    free_bytes = shutil.disk_usage(p.root).free
    needed = previous_size + 1024**3
    if free_bytes < needed:
        raise RuntimeError('Not enough runner disk space for safe consolidation')

    new_id = uuid.uuid4().hex
    output = p.layers / new_id
    output.mkdir(parents=True, exist_ok=False)
    target = output / 'disk.qcow2'
    backing = os.path.relpath(p.base, output)

    print(f'[COMPACT] Consolidating {len(chain)} layers', flush=True)

    # Work exclusively on copies. Never modify the saved HF layers or base.
    work = p.root / 'compact-work'
    work.mkdir(parents=True, exist_ok=False)

    copies = []
    parent = p.base

    for index, item in enumerate(chain):
        original = p.layers / item['id'] / 'disk.qcow2'
        copied = work / f'{index}.qcow2'
        shutil.copyfile(original, copied)

        # Only metadata is changed. The copied backing chain is equivalent.
        relative = os.path.relpath(parent, work)
        p.run('qemu-img', 'rebase', '-u', '-f', 'qcow2',
              '-F', 'qcow2', '-b', relative, str(copied))

        copies.append(copied)
        parent = copied

    # Commit from newest to oldest, stopping at the first overlay.
    # The read-only Golden Image is never a commit target.
    for index in range(len(copies) - 1, 0, -1):
        print(f'[COMPACT] Merging layer {index + 1}/{len(copies)}',
              flush=True)
        p.run('qemu-img', 'commit', '-f', 'qcow2',
              str(copies[index]))

    # The first copied overlay now contains the complete accumulated diff.
    p.run('qemu-img', 'check', '-f', 'qcow2', str(copies[0]))
    p.run('qemu-img', 'compare', '-f', 'qcow2', '-F', 'qcow2',
          str(source), str(copies[0]))

    # Compress only the delta relative to the unchanged Golden Image.
    p.run('qemu-img', 'convert', '-f', 'qcow2', '-O', 'qcow2',
          '-c', '-o', 'compression_type=zstd',
          '-B', backing, '-F', 'qcow2',
          str(copies[0]), str(target))

    p.run('qemu-img', 'check', '-f', 'qcow2', str(target))
    p.run('qemu-img', 'compare', '-f', 'qcow2', '-F', 'qcow2',
          str(source), str(target))
    if p.info(target).get('backing-filename') != backing:
        raise RuntimeError('Unexpected consolidated backing filename')

    new_size = target.stat().st_size
    gain = 1 - new_size / previous_size
    print(f'[COMPACT] Existing layers: {previous_size / 2**20:.1f} MiB')
    print(f'[COMPACT] Consolidated:    {new_size / 2**20:.1f} MiB')
    print(f'[COMPACT] Gain: {gain:.1%}')
    if gain < MIN_GAIN:
        print('[COMPACT] Below 10% threshold; keeping original chain')
        return

    # restore() has copied the latest layer's UEFI/TPM state into root.
    shutil.copyfile(p.nvram, output / 'OVMF_VARS.fd')
    shutil.copyfile(p.tpm, output / 'tpm2-00.permall')
    names = ('disk.qcow2', 'OVMF_VARS.fd', 'tpm2-00.permall')
    hashes = {name: p.sha(output / name) for name in names}
    p.write(output / 'manifest.json', hashes)

    new_state = {
        **manifest,
        'chain': [{'id': new_id, 'sha256': hashes['disk.qcow2']}],
    }
    p.write(p.root / 'latest-compacted.json', new_state)

    operations = [
        CommitOperationAdd(
            path_in_repo=f'layers/{new_id}/{name}',
            path_or_fileobj=str(output / name),
        ) for name in (*names, 'manifest.json')
    ]
    operations.append(CommitOperationAdd(
        path_in_repo='latest.json',
        path_or_fileobj=str(p.root / 'latest-compacted.json'),
    ))

    # Delete only paths belonging to the ORIGINAL layer IDs. They are now
    # replaced atomically with latest.json, never in an earlier commit.
    old_prefixes = tuple(f"layers/{item['id']}/" for item in chain)
    old_files = [name for name in sorted(paths)
                 if name.startswith(old_prefixes)]
    for name in old_files:
        operations.append(CommitOperationDelete(path_in_repo=name))

    if os.environ.get('COMPACT_DRY_RUN') == '1':
        print('[COMPACT] Dry run: all local checks passed; remote dataset unchanged')
        return

    # Server rejects the commit if another workflow saved data meanwhile.
    print('[COMPACT] Publishing one atomic revision...', flush=True)
    result = p.api.create_commit(
        repo_id=p.repo,
        operations=operations,
        commit_message=f'Consolidate {len(chain)} AuroFlow layers',
        parent_commit=revision,
        **p.opts,
    )

    final_revision = p.api.repo_info(p.repo, **p.opts).sha
    if final_revision != result.oid:
        raise RuntimeError('Dataset HEAD changed after compact commit')
    remote_state = json.loads(p.download('latest.json', final_revision).read_text())
    if remote_state != new_state:
        raise RuntimeError('Remote latest.json mismatch')
    final_paths = set(p.api.list_repo_files(p.repo,
                                           revision=final_revision, **p.opts))
    for name in (*names, 'manifest.json'):
        if f'layers/{new_id}/{name}' not in final_paths:
            raise RuntimeError(f'Missing remote compacted file: {name}')
    if any(path in final_paths for path in old_files):
        raise RuntimeError('Obsolete layer files still present at HEAD')
    print('[COMPACT] Success: active layer chain consolidated', flush=True)
    print('[COMPACT] Historical HF revisions are retained; this is not history squashing')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'[COMPACT] FAILED: {exc}', file=sys.stderr)
        raise
