# wharf

Distribution hub for perish.code products. It sits above Plumb: it distributes
Plumb, so Plumb does not govern it.

- `main` is production. Merging is deploying; a bad change is reverted.
- Python under `src/wharf` holds atomic actions. Each action owns only its own
  inputs, outputs and preconditions, refuses early, and knows nothing about
  other actions or the order they run in.
- `.github/workflows` owns orchestration across artifact shapes: ordering,
  matrices, conditions. One workflow per distribution path.
- Distribution paths (`ship`, later `depot`) never import each other.
- Third-party Python dependencies are allowed only when locked to exact
  versions and hashes. GitHub actions are limited to official `actions/*`,
  pinned by commit SHA.
- The only gate is the local pre-commit hook: `git config core.hooksPath .githooks`.
