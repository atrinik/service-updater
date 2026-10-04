// Copyright (c) 2026 Atrinik contributors. MIT licensed.
import {execFileSync} from 'node:child_process';
import semanticRelease from 'semantic-release';
if (process.env.RELEASE_RECOVERY === 'true') {
  // A complete draft precedes the semantic-release tag. Recovery never edits tags.
  execFileSync('python3', ['.github/release/state.py', 'publish'], {stdio: 'inherit'});
} else {
  const result = await semanticRelease();
  if (result?.nextRelease?.version !== process.env.RELEASE_VERSION) {
    throw new Error('Expected semantic release was not published');
  }
}
