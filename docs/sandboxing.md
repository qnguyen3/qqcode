# Sandboxing & containerization

QQcode has **no built-in process sandbox**: tools run as subprocesses or file
operations with the current user's privileges. If you need a hard boundary
around untrusted code, repositories or unattended runs, put QQcode itself
inside an OS-level boundary. This document describes three patterns, from
strongest to lightest.

## Pattern 1 — run QQcode in a container

Run the CLI inside a container that only mounts the project directory. The
agent can break out of the container, not into your host account.

A ready-to-use image is provided at
[`examples/docker/Dockerfile.qqcode`](../examples/docker/Dockerfile.qqcode):

```bash
docker build -f examples/docker/Dockerfile.qqcode -t qqcode:latest .

docker run --rm -it \
  --network=none \
  -e MISTRAL_API_KEY \
  -v "$PWD:/workspace" \
  -w /workspace \
  qqcode:latest --no-trust -p "summarize this repository"
```

Notes:

- Add `--auto-approve` if the programmatic run must edit files; `-p` is
  read-only by default.
- `--no-trust` skips project `.qqcode/` resources (config, hooks, tools, MCP);
  add `--trust` only for repositories you audited.
- `--network=none` blocks exfiltration and MCP connections. The model API call
  also needs network access, so drop that flag (or use a proxy) when the
  provider is remote — the container still isolates the filesystem.
- Mount additional read-only data with `-v /path:/data:ro` rather than the
  whole home directory. Never mount the Docker socket.
- Output/files written by the container are owned by the container user; the
  sample image runs as UID 10001.

Resource limits apply on top:

```bash
docker run --rm -it --memory=2g --cpus=2 --pids-limit=256 ...
```

## Pattern 2 — separate workdir with least privilege

When a container is not available, isolate at the filesystem level:

1. Run QQcode from a dedicated, disposable directory (a fresh clone or a
   throwaway copy), never from `$HOME`.
2. Keep credentials only in `~/.qqcode/.env` / the global config, and enable
   the audit log so you can review every decision afterwards.
3. Restrict writes in the global config (see [security](security.md)):

```toml
[tools.write_file]
permission = "ask"
path_allowlist = ["/srv/work/**"]

[tools.edit]
permission = "ask"
path_allowlist = ["/srv/work/**"]

[tools.read_file]
path_denylist = ["/home/**/.ssh/**", "/home/**/.aws/**", "**/.env"]
```

4. Expose only the tools the task needs, e.g.
   `qqcode --tools read_file --tools grep -p "audit this code"`. In
   programmatic mode an explicit `--tools` list disables all other tools.
5. Run `--no-trust` so a hostile repository cannot load hooks or custom tools,
   and delete the project's `.qqcode/` directory if you do not need it.

This pattern bounds *what the agent can touch* but not *what the process can
do*; a bug in a tool still runs as your user.

## Pattern 3 — restricted user account

For long-running or shared automation, create a dedicated account with no
administrative rights and run QQcode there:

```bash
sudo useradd --create-home --shell /usr/sbin/nologin qqcode
sudo -u qqcode -H qqcode --no-trust -p "run the test suite" \
  --output text
```

Recommended hardening for that account:

- No `sudo`/`doas` membership, no SSH keys, no cloud credentials.
- Home directory holding only `~/.qqcode` (config, trust store, logs).
- Project checkout owned by the account but backed up elsewhere; run inside a
  ramdisk or disposable clone if the repository must stay pristine.
- Optional read-only bind mounts for reference material and a read-only root
  filesystem (`systemd` `ProtectSystem=strict`, `ProtectHome=yes`,
  `PrivateTmp=yes`, `NoNewPrivileges=yes`).
- Enable `[audit]` and `[telemetry] mode = "local"` for a local record of the
  run.

## Choosing a pattern

| Situation | Pattern |
|---|---|
| Untrusted repo, unknown tools, strongest boundary | 1. Container |
| Trusted repo, you want to limit blast radius | 2. Separate workdir + path allowlists |
| CI / scheduled job on a shared host | 3. Restricted user (optionally inside 1) |

Combining patterns is encouraged: a container running as a non-root user with
a read-only root filesystem and a single mounted workdir gives all three
layers. Remember that a sandbox is not a substitute for reviewing what you
ask the agent to run — use `--no-trust`, path allowlists, the audit log and
tool allowlists together.
