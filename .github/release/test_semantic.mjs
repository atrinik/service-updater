// Copyright (c) 2026 Atrinik contributors. MIT licensed.
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {mkdtempSync, writeFileSync, readFileSync, existsSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {Writable} from 'node:stream';
import test from 'node:test';
import semanticRelease from 'semantic-release';

test('semantic-release owns initial version and tags only after draft preparation', async () => {
  const root = mkdtempSync(join(tmpdir(), 'service-updater-semantic-'));
  const cwd = join(root, 'source');
  const remote = join(root, 'remote.git');
  const env = {...process.env, GIT_AUTHOR_NAME: 'Release fixture', GIT_AUTHOR_EMAIL: 'fixture@example.invalid',
    GIT_COMMITTER_NAME: 'Release fixture', GIT_COMMITTER_EMAIL: 'fixture@example.invalid'};
  for (const key of Object.keys(env)) {
    if (key.startsWith('NODE_TEST_') || key.startsWith('GITHUB_') || key.startsWith('GH_')) delete env[key];
  }
  const git = (...args) => execFileSync('git', args, {cwd, env, encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe']}).trim();
  const sink = new Writable({write(_chunk, _encoding, callback) {callback();}});
  try {
    execFileSync('git', ['init', '--bare', '--initial-branch=main', remote], {env, stdio: 'pipe'});
    execFileSync('git', ['clone', remote, cwd], {env, stdio: 'pipe'});
    writeFileSync(join(cwd, 'fixture.txt'), 'fixture\n');
    git('add', 'fixture.txt');
    git('commit', '-m', 'feat: introduce service updater');
    git('push', 'origin', 'main');
    const plugin = join(root, 'fixture-plugin.mjs');
    writeFileSync(plugin, `
      import {execFileSync} from 'node:child_process';
      import {writeFileSync, existsSync} from 'node:fs';
      import {join} from 'node:path';
      export function prepare(_config, context) {
        const tags = execFileSync('git', ['tag', '--list', context.nextRelease.gitTag], {cwd: context.cwd, encoding: 'utf8'}).trim();
        if (tags) throw new Error('Tag existed before complete draft');
        writeFileSync(join(context.cwd, 'prepared'), context.nextRelease.version);
      }
      export function publish(_config, context) {
        if (!existsSync(join(context.cwd, 'prepared'))) throw new Error('Missing draft');
        const head = execFileSync('git', ['rev-parse', context.nextRelease.gitTag], {cwd: context.cwd, encoding: 'utf8'}).trim();
        if (head !== context.nextRelease.gitHead) throw new Error('Wrong tag source');
        writeFileSync(join(context.cwd, 'published'), context.nextRelease.version);
        return {name: 'Fixture release'};
      }
    `);
    const options = {ci: false, branches: ['main'], repositoryUrl: remote, tagFormat: 'v${version}',
      plugins: [[fileURLToPath(import.meta.resolve('@semantic-release/commit-analyzer')), {preset: 'conventionalcommits'}], plugin]};
    const environment = {cwd, env, stdout: sink, stderr: sink};
    const plan = await semanticRelease({...options, dryRun: true}, environment);
    assert.equal(plan.nextRelease.version, '1.0.0');
    assert.equal(git('tag', '--list'), '');
    assert.equal(existsSync(join(cwd, 'prepared')), false);
    const release = await semanticRelease(options, environment);
    assert.equal(release.nextRelease.version, '1.0.0');
    assert.equal(readFileSync(join(cwd, 'published'), 'utf8'), '1.0.0');
    assert.equal(git('tag', '--list'), 'v1.0.0');
    const repeated = await semanticRelease(options, environment);
    assert.equal(repeated, false, 'Recovery must run outside semantic-release once its tag exists');
  } finally {
    rmSync(root, {recursive: true, force: true});
  }
});
