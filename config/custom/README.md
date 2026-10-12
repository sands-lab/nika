# config/custom

Your customized versions of the `config/*.example.yaml` templates for testing and staging,
such as credentials, endpoints, and local paths. Everything here except this README is
gitignored, so it never reaches the public repository.

Untracked files are not checked out into new git worktrees. To get them there, enable the
repository hook once per clone (`./scripts/install.sh` does this for you):

```shell
git config core.hooksPath scripts/git-hooks
```

`git worktree add` then copies the main checkout's `config/custom/` files into the new
worktree, without overwriting files that already exist. Each worktree gets its own copy, so
edits in one worktree do not affect the others.
