# ghforge

A standalone GitHub-compatible git forge in pure Python 3 (standard library
only, one process, no git binary): git smart HTTP (upload-pack protocol v2,
receive-pack v0), the REST v3 API for repositories, issues, pull requests and
contents, the GraphQL v4 API, and an MCP endpoint — all served from its own
in-memory store.

Entry point: `main.py` binds an ephemeral port, registers it with the trial
environment, and serves until the suite signals done.

[![make.fun · github-core-clone-01](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/jusst01/ghforge/rank.svg)](https://make.fun/bounty/github-core-clone-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/jusst01/ghforge/reward.svg)](https://make.fun/bounty/github-core-clone-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)

MIT licensed.
