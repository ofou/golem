# Golem

Golem is an agent that works inside one repository. When a task needs an operation it does not have, it writes a Python tool, proves that tool in a sandbox, and installs that exact version. Later sessions in the same repository call the installed tool.

What a tool may read, import, and spend is fixed by `authority.json`. Golem prints that file's sha256 before and after every run and does not modify it. Whole-line `//` comments are part of the hashed bytes. Installed tools accumulate. The licence stays the same.

## Requirements

- Python 3.13 or later
- Docker. Generated code runs only in the sandbox.
- An OpenRouter account. Golem spends credits on that account.

## Install

```bash
uv tool install git+https://github.com/ofou/golem
golem login
```

`pip install` from the same URL works the same way. The wheel includes the default licence, `authority.json`.

To run from a container:

```bash
docker build -t golem:local https://github.com/ofou/golem.git#main
scripts/golem-docker login
scripts/golem-docker PATH run "TASK"
```

The image is `python:3.12-slim` and the entrypoint is `python -m golem`. `scripts/golem-docker` mounts the repository and the host Docker socket, so sandbox containers run beside Golem. That socket gives the Golem container control of the host Docker. `GOLEM_IMAGE` defaults to `golem:local`.

## Commands

```bash
golem login [--port N] [--no-browser]
golem --repo PATH run "TASK" [--attach FILE ...]
golem --repo PATH registry
golem --repo PATH verify [--attach FILE ...]
golem --repo PATH rollback NAME VERSION
golem licence
golem credits
```

`--repo` and `--licence` go before the subcommand. `--repo` defaults to the current directory. If `--licence` is omitted, Golem uses `<repo>/.golem/authority.json` when that file exists, otherwise the packaged licence. A `--licence` path that does not exist is an error.

`login` uses OpenRouter's OAuth PKCE flow and stores the key at `${XDG_CONFIG_HOME:-~/.config}/golem/openrouter.key` with mode `0600`. The key is not printed. `OPENROUTER_API_KEY`, when set, takes precedence. Set a spending limit on the key at [openrouter.ai](https://openrouter.ai); the login flow has no limit field. Golem's per-task cap still applies. `--headless` prints a code to paste. The Docker wrapper uses that mode.

`verify` re-runs every installed tool's tests in the sandbox and checks the files against the receipt. It does not need an API key. `--attach` supplies files the tests read under `/inputs`. A matching install prints `OK`. An edited file fails with `CHANGED since they were tested`.

A run writes its answer to `.golem/runs/<run_id>/result.md`.

## How a tool is made

A session starts with four kernel tools: `list_files`, `read_file`, `make_tool`, and `install_tool`. Every other tool the agent calls, it built.

1. **Proposal.** `make_tool` requires a `task_quote` copied from the task. After whitespace is collapsed and case is folded, the quote must be at least 8 characters. Attempts are counted on that quote. A new name does not reset the count. An installed name is refused unless the proposal revises it.
2. **Licence.** A request for access, an import, a subprocess, an environment read, or a write that the licence does not grant is refused. The lint names the request. The sandbox enforces it.
3. **Advisor.** `typesafe/jev-1.13`, called through OpenRouter's Decisions API, may send a proposal back once. It does not approve an install. If the advisor is unavailable, the build continues.
4. **Blind tests.** A tester model writes `test_blind.py` from the interface, the task, and the repository. It does not see the implementation. On failure, the builder receives the test name and the exception type.
5. **Sandbox.** Golem runs the builder's tests, the blind tests, and the same tests against two stubs that must fail them. The builder's suite must contain at least 3 tests, and at least 80% of the tests must fail on both stubs.
6. **Install.** `install_tool` checks that the files still hash to what was tested, writes `.golem/registry/NAME/VERSION/`, and points `.golem/registry/active.json` at that version. The first version of a name is `0.1.0`. Each later version bumps the minor number.
7. **Next session.** A new session starts only after an install, while sessions and budget remain. It loads installed tools from disk and calls them in the sandbox.

The sandbox is:

```text
docker run --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user 65534:65534
```

Memory, CPU, process, and time limits come from the licence. The host environment is not forwarded. The only writable path is a `/tmp` tmpfs. Repository-read tools also see a read-only snapshot at `/repo`. Registry-read tools also see the registry at `/registry`.

## Tool contract

A tool is a standard-library Python function, `run(args: dict) -> dict`, with JSON schemas for its input and output. It runs once per call and keeps no state. Output is a typed result or a unified diff. A tool does not apply a patch, and it does not receive network access, credentials, or a shell.

| Access | What it can read |
| --- | --- |
| Pure | Its arguments, and files attached to the task at `/inputs` |
| Repository-read | Also a read-only snapshot of the repository at `/repo` |
| Registry-read | Also the registry at `/registry`: manifests, receipts, and usage. No tool code. |

The repository snapshot omits `.git`, `.golem`, virtualenvs, dependency and build directories, secret filenames, symlinks, and files larger than 1 MB. `.env.example`, `.env.sample`, `.env.template`, and `.env.dist` are kept.

## Limits

From `authority.json`, enforced by the kernel:

| Cap | Value |
| --- | --- |
| Spend per task | $0.20 |
| Sessions per task | 3 |
| Steps per session | 18 |
| `make_tool` calls per task | 4 |
| Attempts per gap | 3 |
| Sandbox runs per task | 60 |
| One sandbox run | 120 s, 512 MB, 1 CPU, 128 processes |

The spend cap is checked between model steps. Models are set in the same file. The builder and the tester are both `deepseek/deepseek-v4.1-flash`, so receipts mark `independent_tester` false. Both run at low reasoning effort (`builder_reasoning` and `tester_reasoning`); the model's default is high. The advisor is `typesafe/jev-1.13`.

Answers and tools come from language models and can be wrong. A tool that passes its own tests, its blind tests, and the stub check agrees with those tests; it is not proven correct, and an answer is not checked at all. Read an answer or a proposed diff before acting on it. Under Golem's own licence, tools use only the standard library and run without network access; another licence can change both.

## Data and privacy

Golem sends the task, the file paths it lists, the files it reads, tool output, the names and descriptions of installed tools, and the handoff between sessions to OpenRouter and the model providers OpenRouter routes to, under your key. Proposals also go to OpenRouter's Decisions API for the advisor. In a comment-triggered run, the comment is the task. An attached file reaches a model only when Golem reads it or a tool returns it. What OpenRouter and its providers keep, and where, is set by OpenRouter's [privacy policy](https://openrouter.ai/privacy) and [terms](https://openrouter.ai/terms).

On GitHub Actions, GitHub keeps the run logs, the summary, the artifact, and the registry cache under the [GitHub Privacy Statement](https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement). On a public repository they are public. Golem sends nothing to its developer and has no telemetry.

## GitHub Actions

### In your repository

Any repository, public or private, can run Golem through the reusable workflow [`run.yml`](.github/workflows/run.yml) on GitHub-hosted Ubuntu runners.

1. Add the repository secret `OPENROUTER_API_KEY`. Give the key its own credit limit.
2. Copy [`examples/golem.yml`](examples/golem.yml) to `.github/workflows/golem.yml` on the default branch.

```bash
gh secret set OPENROUTER_API_KEY
mkdir -p .github/workflows && curl -fsSL https://raw.githubusercontent.com/ofou/golem/v1/examples/golem.yml -o .github/workflows/golem.yml
```

Start a task from the Actions tab, with `gh workflow run golem.yml -f task="..."`, or with a `/golem <task>` comment on an issue or pull request. A comment gets the answer as a reply. Every run writes it to the run summary.

| Job | Holds | Permissions | Does |
| --- | --- | --- | --- |
| `gate` | no secret | `contents: read`, `issues: write` | Decides who may spend the key, reads the task from the comment, pins a pull request's head commit, reacts with 👀 |
| `run` | `OPENROUTER_API_KEY` | `contents: read`, `actions: read` | Restores the registry, runs the task, saves a new snapshot, uploads the run as an artifact |
| `verify` | no secret | `contents: read` | Re-runs every installed tool's tests in the sandbox on a runner that never had the key |
| `reply` | no secret | `contents: read`, `issues: write` | Answers the comment |

The job with the key cannot push, comment, or open a pull request. Golem's code is checked out at the commit the caller pinned (`job.workflow_sha`). Every action is pinned to a full commit SHA.

- **Who can start it.** `workflow_dispatch` needs write access. A comment starts Golem only when its author has write or admin permission, read from the API by a job with no secret. Other comments, edited comments, and bots are ignored. The `allowed-users` input narrows the set further.
- **Pull requests.** On a pull request comment, Golem reads the head commit pinned by the gate and names it in the reply. The pull request's `.golem/` is removed, and the licence never comes from it. Tools built while reading a fork's code are not saved.
- **Registry.** Installed tools live in the repository's Actions cache. A run restores the newest snapshot (`golem-registry-v1-*`) and saves a new one when the registry changed. Runs that can change it queue one at a time. A snapshot that nobody restores for 7 days is evicted. Anyone who can open a pull request can read the base branch's caches, and a manifest quotes its task. The `run` job sets `cache-mode: write`, because comment-triggered runs get a read-only cache by default. A calling job that sets `cache-mode: read` stops the workflow from starting.
- **Licence.** Golem's packaged `authority.json` by default. `with: licence: path/to/authority.json` uses a file from the calling repository, read at the commit the workflow runs at.
- **Public repositories.** Logs, summaries, and the run artifact (registry, events, answer, candidates) are public. The key is not printed, and the artifact step drops any file that contains it.
- **Pinning.** `@v1` follows the newest 1.x release. For a fixed version, use a full commit SHA.
- **Limits.** The reusable workflow cannot use your self-hosted runners. GitHub Enterprise Server is not supported. Organizations that restrict actions must allow `ofou/golem@*` and `ofou/golem/.github/workflows/run.yml@*`, plus GitHub's own actions.

### As a step

The composite action [`action.yml`](action.yml) does the same in one step of your own job. It needs a Linux runner with Docker, not a `container:` job. It has no gate of its own, so use the reusable workflow for triggers that anyone can start, such as `issue_comment`.

```yaml
    permissions:
      contents: read
      actions: read
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
        with:
          persist-credentials: false
      - uses: ofou/golem@v1
        with:
          task: ${{ inputs.task }}
          openrouter-api-key: ${{ secrets.OPENROUTER_API_KEY }}
          attach: build.log
```

Inputs: `command` (`run` or `verify`), `task`, `openrouter-api-key`, `attach` (one path per line), `path`, `licence`, `registry` (`read-write`, `read-only`, or `off`), `artifact`, and `github-token`. Outputs: `exit-code`, `run-id`, `answer-file`, `installed`, `spent`, `registry`, and `artifact-id`.

### On another public repository

[`.github/workflows/golem.yml`](.github/workflows/golem.yml) runs a task against another public repository from this one. Add the repository secret `OPENROUTER_API_KEY`, then:

```bash
gh workflow run golem.yml -f target=OWNER/REPO -f ref=SHA -f task="..."
```

An optional `task2` runs as a new process on the same registry. A second job holds no secret and re-runs every installed tool's tests. Only accounts with write access can start the workflow.

### Releasing

1. Merge to `main`. The Marketplace listing shows the default branch's README.
2. Turn on two-factor authentication for the publishing account.
3. Open `action.yml` on GitHub and choose **Draft a release**. Tick **Publish this Action to the GitHub Marketplace**. If the box is disabled, accept the GitHub Marketplace Developer Agreement from the link beside it.
4. Wait for **Everything looks good!**. Choose **Agent apps** as the primary category and **AI Assisted** as the other one.
5. Tag `vX.Y.Z` and publish the release. [`release.yml`](.github/workflows/release.yml) then moves the tag `vX` to that commit, when the release is published as a full release or a pre-release is promoted to one.

Tick the Marketplace box on every release that should be listed. Keep the file name `action.yml`, because renaming it hides earlier versions on the listing. Keep the name "Run Golem"; the plain name "Golem" is a GitHub user's login.

## Development

```bash
uv sync --no-editable
python -m unittest discover -s tests
```

[`.github/workflows/tests.yml`](.github/workflows/tests.yml) runs on Python 3.13: it checks `golem licence` against the sha256 of `authority.json`, then runs ruff, pyright, and pytest. Sandbox tests need Docker and `python:3.12-slim`.

## Support

Report bugs, wrong or harmful answers, and questions in [issues](https://github.com/ofou/golem/issues). Report security problems privately, as [SECURITY.md](SECURITY.md) describes.

## License

Golem is released under the [MIT License](LICENSE). That is the software licence. `authority.json`, which this README calls the licence, is the policy file that bounds what tools may do.
