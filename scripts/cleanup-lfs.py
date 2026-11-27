#!/usr/bin/env python3
"""Permanently delete unreferenced HF LFS blobs after a verified history squash.

Requires the expected dataset HEAD SHA from the completed squash operation.
Refuses other branches, tags, PR refs or conversion refs.
"""
import argparse
import os
import sys
from huggingface_hub import HfApi


def inspect_refs(api, repo):
    refs = api.list_repo_refs(repo, repo_type='dataset', include_pull_requests=True)
    branches = getattr(refs, 'branches', []) or []
    tags = getattr(refs, 'tags', []) or []
    pulls = getattr(refs, 'pull_requests', []) or []
    converts = getattr(refs, 'converts', []) or []
    if len(branches) != 1 or branches[0].name != 'main' or tags or pulls or converts:
        raise RuntimeError('Unexpected HF references; refusing LFS deletion')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--expected-revision', required=True)
    args = parser.parse_args()

    repo = os.environ['HF_REPO']
    token = os.environ['HF_TOKEN']
    api = HfApi(token=token)
    inspect_refs(api, repo)

    info = api.dataset_info(repo, revision='main', files_metadata=True)
    if not info.private or info.sha != args.expected_revision:
        raise RuntimeError('Dataset privacy or HEAD revision changed')
    if not any(s.rfilename == 'latest.json' for s in info.siblings):
        raise RuntimeError('Missing latest.json; refusing LFS deletion')

    active = {s.lfs.sha256 for s in info.siblings if s.lfs}
    if not active:
        raise RuntimeError('No active LFS objects detected; refusing deletion')
    objects = list(api.list_lfs_files(repo, repo_type='dataset'))
    orphans = [obj for obj in objects if obj.file_oid not in active]

    size = sum(obj.size for obj in orphans)
    print(f'[LFS] Active: {len(active)}; orphans: {len(orphans)}; recoverable: {size / 1e9:.3f} GB', flush=True)
    if not orphans:
        return

    # Recheck as close as practical to the destructive API call.
    inspect_refs(api, repo)
    latest = api.dataset_info(repo, revision='main', files_metadata=True)
    if not latest.private or latest.sha != info.sha:
        raise RuntimeError('HF revision changed during LFS audit')
    if {s.lfs.sha256 for s in latest.siblings if s.lfs} != active:
        raise RuntimeError('Active LFS references changed during audit')

    api.permanently_delete_lfs_files(
        repo, orphans, repo_type='dataset', rewrite_history=False
    )
    remaining = list(api.list_lfs_files(repo, repo_type='dataset'))
    remaining_orphans = [obj for obj in remaining if obj.file_oid not in active]
    if remaining_orphans:
        raise RuntimeError(f'{len(remaining_orphans)} orphan LFS objects still listed')
    if not active.issubset({obj.file_oid for obj in remaining}):
        raise RuntimeError('An active LFS object is missing after cleanup')
    print('[LFS] Orphan objects deleted; active objects preserved', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'[LFS] FAILED: {exc}', file=sys.stderr)
        raise
