# Development workflow

How changes to hermes-linear go from idea to `main`. The rule: **a PR means
"tested live, ready to merge"**, not "first attempt at a fix".

## Branches

- `main`: what others install. Only reviewed, CI-green, live-tested work lands here.
- One branch per change (`fix-…`, `feat-…`, or the Linear branch name, e.g.
  `ret-93-webhook-exposure-docs`), each in its own worktree under `.worktrees/`.
- No `develop` branch.

## Live checkout

The gateway loads the plugin from one dedicated worktree:

```
~/git/hermes-linear/.worktrees/live        # detached HEAD, never a branch
~/.hermes/profiles/<profile>/plugins/linear -> ~/git/hermes-linear/.worktrees/live
```

The symlink never changes. Hermes keys the plugin's dependency environment on the
resolved plugin path; repointing it forces a re-stage, and a failed re-stage makes
Hermes auto-disable the plugin (`plugins.disabled_by_update` in `config.yaml`) so the
next restart comes up without Linear. If you ever must repoint it, run
`hermes -p <profile> plugins enable linear` right after and check `hermes linear status`
before restarting.

Deploying means moving the live worktree's HEAD:

```bash
scripts/deploy-live <branch|sha> [<branch|sha> ...]
```

- One ref: detaches the live worktree at it.
- Several refs: builds a throwaway local merge of them (e.g. a feature branch plus
  a fix) and detaches at that. Nothing is pushed; the branches stay separate.

Then restart the gateway (the operator does this). `hermes linear status` shows the
loaded commit.

## The loop

1. Branch from `main`, fix with tests (TDD), push the branch. CI runs on every
   branch push: `pytest` + `hermes plugins validate` against the pinned
   hermes-agent commit (required) and hermes-agent `main` (early warning).
2. `scripts/deploy-live <branch>`; restart; test it live in the Linear project
   **hermes linear test project**.
3. Works: open the PR, CI green, merge (merge commit, not squash, so dependent
   branches stay clean descendants). Doesn't: iterate on the same branch.
4. After a merge: `scripts/deploy-live main` (or the next stack); restart.

Linear issues for plugin work live in the **hermes-linear** project; dogfooding
tickets go in **hermes linear test project**.
