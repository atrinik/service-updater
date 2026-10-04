// Copyright (c) 2026 Atrinik contributors. MIT licensed.
import {appendFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
import semanticRelease from 'semantic-release';

const pending = JSON.parse(execFileSync('python3', ['.github/release/state.py', 'pending'], {encoding: 'utf8'}));
let version = pending.version;
if (!pending.tagged) {
  const result = await semanticRelease({dryRun: true});
  const planned = result?.nextRelease?.version;
  if (version && version !== planned) throw new Error('Pending draft differs from semantic-release version');
  version = planned;
}
if (version && !/^\d+\.\d+\.\d+$/.test(version)) throw new Error('Invalid semantic version');
appendFileSync(process.env.GITHUB_OUTPUT, `version=${version || ''}\nrecovery=${pending.tagged}\n`);
