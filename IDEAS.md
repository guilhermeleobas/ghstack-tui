# Ideas

Scratch pad. One idea per heading. Status: `[ ]` todo, `[~]` in progress, `[x]` done.
Drop notes, links, open questions inline. Squash or split entries as they evolve.

Every new task should be executed in a new worktree

---

## [x] Semantic diff

Diff that understands code structure, not just lines.

Open questions:
- Scope: replace `d` (full-screen `DiffModal`)
- Granularity: function/class moves, rename detection, signature changes, no-op
  reformatting collapse?
- Engine: `difftastic` / `delta --side-by-side`? Choose the easiest one to integrate
- Cross-file: detect a function moved between files in the PR? hum... yes(?)
- Output: render inline in `DiffModal` (Rich Text) or open `difftastic` in a
  subprocess + `self.suspend()` like `v` does today? Use Diffmodal
- Install difftastic from conda-forge with pixi


## [x] Auto update

Add an option to pixi to automatically update ghstack-tui with git pull origin main


## [x] Add pi / claude as a pixi dependency

Add pixi / claude as pixi dependency. Or find a way to install them

`nodejs` added to `[dependencies]`. Run `pixi run install-agents` to install
`@earendil-works/pi-coding-agent` and `@anthropic-ai/claude-code` via npm into the pixi env.

## [x] remove the vim diff view

I don't use it. Just remove it