// Copyright (c) 2026 Atrinik contributors. MIT licensed.
import {execFileSync} from 'node:child_process';
import {writeFileSync} from 'node:fs';

export async function prepare(_config, context) {
  if (context.nextRelease.version !== process.env.RELEASE_VERSION) {
    throw new Error('Semantic version changed after candidate validation');
  }
  writeFileSync('dist/release-notes.md', context.nextRelease.notes);
  execFileSync('python3', ['.github/release/state.py', 'prepare'], {stdio: 'inherit'});
}

export async function publish() {
  execFileSync('python3', ['.github/release/state.py', 'publish'], {stdio: 'inherit'});
  return {name: 'GitHub release', url: `https://github.com/atrinik/service-updater/releases/tag/v${process.env.RELEASE_VERSION}`};
}
