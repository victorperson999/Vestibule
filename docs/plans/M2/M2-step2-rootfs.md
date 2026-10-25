# M2 step 2 — Rootfs: registry client, unpacker, `vestibule-mcp setup`

Status: **signed off by Victor 2026-10-24 (v2,** revised after the `skeptic` review, §7).
- **Victor decided 2026-10-24:**
  - S2-D1, the command line;
  - S2-D2, refuse what the pinned images don't use (amends contract §6);
  - S2-D9, where the overlap check lives (amends contract §7).
- The other decisions are marked *fixed*: one defensible answer each.

Contract: [`M2-native-warden.md`](M2-native-warden.md) N2, §6, §7 and §10 step 2. One commit.

**What this step delivers:** `vestibule-mcp setup` downloads M1's two digest-pinned images
straight from the registry (no Docker), checks every byte against the pin, and unpacks each
into a tree that step 3's warden will mount as the guest's `/`. **Nothing in the server uses
the trees yet:** native becomes reachable in step 6.

**Not in this step** (Karpathy "simplicity first"; each is a later step's job or not needed):
- any server-side use of the rootfs (steps 3 and 6), and the `native` / `container` backend
  values (step 6);
- the README (S2-D8);
- resuming partial downloads, retries, a blob cache, `--force`, pruning old trees, progress
  bars;
- tag resolution, private registries, zstd layers, whiteouts (S2-D2);
- removing the `seccomp` extra from `pyproject.toml` (step 5, with N4) and the `native`
  pytest marker (step 3, with the first native test).

## 1. What the investigation found (2026-10-24, before any code)

A throwaway stdlib script fetched both pinned images from Docker Hub the way `setup` will
(anonymous token, fetch by digest, sha256 check), then replayed every layer in order without
writing a tree to disk. The `skeptic` re-ran it and got the same results.

| Fact | python (`a6e34c59…`) | node (`c3de60bf…`) |
|---|---|---|
| Index | OCI index, 8 platforms + 8 attestation entries (`unknown/unknown`) | OCI index, 4 platforms + 4 attestation entries |
| `linux/amd64` manifest | OCI manifest, 4 layers | OCI manifest, 5 layers |
| arm64 platform entry | `linux/arm64` with `variant: v8` | same |
| Layer media type | `application/vnd.oci.image.layer.v1.tar+gzip` (all) | same (all) |
| Download | 43.3 MB, 2.8 s | 79.8 MB, 3.4 s |
| Unpacked (regular-file bytes) | 119.4 MB | 227.0 MB |
| Entries | 4,443 files, 709 dirs, 506 symlinks (186 absolute), 1 hardlink | 5,807 files, 1,046 dirs, 514 symlinks (40 absolute), 2 hardlinks |
| First entry of the first layer | `.` (the root itself, mode 0o755) | same |
| setuid / setgid / sticky entries | 15 (`su`, `passwd`, `mount`, `umount`, …; `tmp`, `var/tmp`, `run/lock`) | 16 (same, plus `var/local`) |
| Whiteouts (`.wh.*`, opaque) | **0** | **0** |
| Devices, FIFOs, xattrs, `.pyc` files | **0** | **0** |
| Entries whose parent path crosses a symlink | **0** | **0** |
| Entries whose parent doesn't exist yet | **0** | **0** |
| Path replaced across layers | only dir→dir (51, re-listed) and file→file (29) | only dir→dir (40) and file→file (37) |
| Hardlink targets | earlier regular files in the same tree | same |
| Case-only name collisions | 0 | 0 |
| `workspace/`, `sandbox/`, `etc/hosts` | **absent** (the spike created the first two by hand) | **absent** |
| Config `Env` | `PATH`, `LANG=C.UTF-8`, `GPG_KEY`, `PYTHON_VERSION`, `PYTHON_SHA256` | `PATH`, `NODE_VERSION`, `YARN_VERSION` |

Registry behaviour:
- **Auth:** an unauthenticated manifest GET answers 401 with
  `WWW-Authenticate: Bearer realm="https://auth.docker.io/token",service="registry.docker.io",scope="repository:library/<name>:pull"`.
  The token lasts **300 s**.
- **Blobs:** `307` to `production.cloudfront.docker.com`. With the token in an
  *unredirected* header, the CDN request carries no `Authorization` (contract §6). The
  `skeptic` confirmed that the planned test fails with a plain `add_header`, on 3.14 and 3.11.
- **Rate limit:** anonymous manifest pulls are limited to **100 per hour per IP** here
  (`ratelimit-limit: 100;w=3600`). `setup` uses 4 (index + manifest per image). A gate run
  uses about 8 (§4.3). Not a constraint, but a 429 must produce a clear message.

So the contract's open questions are settled: no zstd (the refusal stays for overrides),
about **123 MB to download and ~350 MB on disk** for both trees. That's the "~N MB" figure
in §5's no-rootfs message.

**Assumptions** (Karpathy "think before coding"; each is checked or made loud):
- Only the two pinned images, and overrides pinned the same way, must work. A re-pin may
  bring entry kinds these two don't have; S2-D2 makes that a loud failure, and the re-pin
  procedure now runs `setup` (S2-D2).
- The rootfs directory sits on a Linux filesystem the user owns (ext4 in WSL). Not detected
  (§5 edge cases). It **may be shared with unrelated files**: setup touches only its own
  names (S2-D5).
- Only a human runs `setup`. The server never downloads anything (D2).
- amd64 is the only architecture tested for real. arm64 is mapped (S2-D4), with an offline
  round-trip test, but unverified until a real arm64 host runs it.

## 2. Decisions

### S2-D1 (Victor) — `vestibule-mcp setup`; anything else still starts the server, with a warning

- **`vestibule-mcp`** with no arguments starts the MCP server, exactly as today. Every MCP
  client registration uses this form.
- **`vestibule-mcp setup`** runs the setup command and exits.
- **Any other arguments:** one line on stderr (`vestibule-mcp: ignoring unexpected
  arguments [...]; the only subcommand is "setup"`), then the server starts as today.
  - A client config with a stray argument keeps working; the line lands in the client's
    log.
  - A human who types `vestibule-mcp setp` sees the line right away in the terminal.
- **Setup reads its own settings:** it calls `Limits.from_env()` itself, not `server.LIMITS`.
  On a bad config the server falls back to the defaults and reports the error on every tool
  call (`server.py:47-56`, `:146-148`), but setup makes no tool call, so it would quietly run
  on the defaults. A `ConfigError` exits 1, listing every problem.
  - It starts by printing the resolved directory and both image references, so a mismatch
    with the server's settings is visible.
  - `docs/GETTING_STARTED.md` says that settings in the client's `env` block must also be
    set for setup (`VESTIBULE_ROOTFS_DIR=… vestibule-mcp setup`).
- **All setup output goes to stderr;** stdout stays empty. Golden rule 1 is about the server,
  but this keeps the rule trivially true for every entry point.
- **No flags.** `VESTIBULE_ROOTFS_DIR` and `VESTIBULE_IMAGE_*` already choose the directory
  and the images; deleting the directory is the "force".
- **Exit codes:** 0 when both trees are ready, already present, or on a non-Linux host
  (S2-D7); 1 on a config error or when any image failed (the other is still attempted).
- **Rejected:**
  - **Exit 2 on unknown arguments (draft v1).** The M1 follow-up already rejected refusing to
    start, because the client then only says "failed to connect".
  - Refusing only when stdin is a TTY: cleverer, and surprising.
  - A second console script (`vestibule-setup`): a second name to document and install.
  - Flags (`--force`, `--dir`, `--image`): the env vars already cover them.

### S2-D2 (Victor; amends contract §6) — Refuse what the pinned images don't use

The contract's §6 says the unpacker applies OCI whiteouts. The investigation found **none**
in either image, and no directory ever replaced by a file. Implementing them means
recursive deletion inside an attacker-shaped tree, exactly the code that's hardest to get
right, for a feature nothing uses.

- **Refused, failing that image's setup with the entry named:** whiteouts (`.wh.<name>` and
  `.wh..wh..opq`), a directory replaced by a non-directory, an entry whose parent doesn't
  exist, device nodes, FIFOs, and any other entry type.
- **Fail closed, never skip.** Skipping would quietly build a filesystem that differs from
  the image. A refused image leaves no tree behind.
- **The safety net is wired up:**
  - M1's re-pin procedure (`docs/plans/M1/M1-step6-pinning.md`, "Re-pin cost") and the
    comment above the pins in `config.py` gain one line: on Linux, run `vestibule-mcp setup`
    and the `network` tests.
  - The automated real-image tests cover **both** images (§4.3).

  So a re-pin that brings a whiteout or a zstd layer fails before it ships, with the entry
  named. That's the moment to add support, with its fixtures.
- **With this, the unpacker never deletes a directory tree.** The only recursive deletes in
  this step are setup's own temp dir and a stale tree of its own (S2-D5).
- **Rejected:**
  - Implement whiteouts now, as the contract's v1 said: speculative, and the riskiest code
    in the step.
  - Skip what we can't handle: produces a tree that isn't the pinned image.

### S2-D3 (fixed) — Verify, then parse

- **Every blob** is downloaded to a file in setup's temp dir. Its size and sha256 are checked
  against the descriptor that named it, and only then is it opened.
- **The index** is fetched by the pinned digest and capped at 4 MiB; the manifest and config
  are checked the same way, as bytes, before `json.loads`.
- So the JSON parser, `gzip` and `tarfile` only ever see bytes the pin vouches for. A body
  longer than its descriptor's size is cut off and refused.
- **Cost:** peak disk use is blobs plus tree, about 310 MB for node. The blobs are deleted
  after unpacking.
- **Rejected:** hashing while streaming into the unpacker. It parses unverified bytes, and
  only the discarded temp tree would protect against a bad stream.

### S2-D4 (fixed) — The registry client

- **References** must carry a digest: `[<host>/]<name>[:<tag>]@sha256:<64 hex>`.
  - **Short forms are normalised as Docker does:** a single component becomes
    `docker.io/library/<name>`; a first component without `.`, `:` or `localhost` gets a
    `docker.io/` prefix.
  - **A tag next to a digest is ignored;** the digest governs. That's how Docker treats it,
    and it matches M1's pin check (`select.py`), which counts these forms as pinned and
    tells users to "pin it as `<name>@sha256:<digest>`".
  - **A tag without a digest** refuses *that* image for native: "native needs a digest
    reference, like `docker.io/library/python@sha256:…`". A tag has nothing to verify the
    index against. The container backend is unaffected (it warns, as today).
- `docker.io` maps to `registry-1.docker.io`. **HTTPS only.**
- **Auth:** an anonymous bearer token from the 401 challenge's `realm`, `service` and
  `scope`. This works for any registry that allows anonymous pulls; private auth stays out
  (N2).
  - The token goes in an **unredirected** header, so the CDN never sees it (§1).
  - **A 401 re-authenticates once per request,** with no limit per run. Tokens last 300 s,
    and on a slow link one image's layers can span several token windows.
  - The token is never printed or logged.
- **Platform:** canonically `linux/<arch>`, with `x86_64` → `amd64` and `aarch64`/`arm64` →
  `arm64`.
  - An index entry matches on `os` and `architecture`; arm64's `variant: v8` is accepted and
    not stored.
  - Other machines are refused with the machine name. Attestation entries
    (`unknown/unknown`) are ignored.
  - A digest that names a single-platform manifest is accepted if its config says
    `linux/<arch>`.
- **Layers:** OCI or Docker gzip tar only. zstd is refused with the contract's message
  (it needs Python 3.14); any other media type is refused, named.
- **Network:** a 30 s socket timeout, `HTTPS_PROXY` honoured (urllib does that), no retries
  (re-running `setup` is the retry). A 429 says "registry rate limit; try again later".

### S2-D5 (fixed) — Cache layout and completion

```
<rootfs_dir>/                    VESTIBULE_ROOTFS_DIR, default ~/.vestibule/rootfs
  .vestibule-setup.lock          flock: one setup at a time
  <index-hex>-<arch>/            the tree, the guest's /. Named by the pinned digest and the
                                 arch, so a lookup needs no network and one home folder
                                 shared by two machines can hold both architectures
  <index-hex>-<arch>.json        the marker, beside the tree (spike A6), written last
  .vestibule-tmp-<random>/       setup's work dir (mkdtemp prefix): blobs/ and tree/
```

- **Setup touches only names it owns:** `<64 hex>-amd64` or `<64 hex>-arm64` (with or
  without `.json`), its lock file, and `.vestibule-tmp-*` dirs. Anything else in the
  directory is never read, moved or deleted. So pointing `VESTIBULE_ROOTFS_DIR` at a shared
  folder (`~/.cache`) can't delete anything (`skeptic` high finding).
- **The marker:** `{"format": 1, "ref", "index_digest", "manifest_digest", "platform",
  "env"}`, with `platform` in canonical `linux/<arch>` form. `env` is the image config's
  `Env`, which step 3 needs (N9). Nothing else.
- **`rootfs.find(rootfs_dir, ref)`** returns the tree only if:
  - the marker parses, has `format: 1`, and names this digest and this host's canonical
    platform;
  - the tree is a real directory, not a symlink.

  Anything else means "not present". `setup` uses it now; steps 3 and 6 will.
- **Order:**
  1. unpack into `.vestibule-tmp-…/tree`;
  2. add the mount points (S2-D10);
  3. `os.sync()`;
  4. rename the tree to `<index-hex>-<arch>`;
  5. write the marker to a temp file, `fsync` it, `os.replace` it into place, and `fsync`
     the directory.

  So a reader never sees a marker without a complete tree, and a VM crash or power loss
  right after setup can't leave a valid marker over files whose data never reached the disk
  (`skeptic` finding 13).
- **Under the lock,** `setup` first removes its own stale `.vestibule-tmp-*` dirs (nobody
  else can be using them), then any of its own trees without a valid marker, then fetches.
- **Already present** (`find()` succeeds) means no network at all.
- **Rejected:** keying the directory by the per-platform manifest digest. Step 6 would then
  need the registry to find a tree.

### S2-D6 (fixed) — Unpacker rules (contract §6, with S2-D2)

- **The root entry** (`.` or `./`) is recognised first and skipped (its mode isn't applied),
  before the name rules run. Both pinned images start with it.
- **Names:** a leading `./` and a trailing `/` are removed. Refused: absolute names, NUL, and
  empty, `.` or `..` components.
- **Parents** are resolved one component at a time from the tree's root directory fd with
  `O_NOFOLLOW | O_DIRECTORY`. A symlink or a missing component refuses the entry, so a link
  from an earlier entry is never followed.
- **Every filesystem call uses a resolved parent fd and a single-component name,** never a
  path string. An absolute path would make the kernel ignore `dir_fd`.
- **Per type:**

  | Entry | Existing path | Action | Mode written |
  |---|---|---|---|
  | dir | none | `mkdir` | `(mode & 0o777) \| 0o700` |
  | dir | dir | keep | same formula |
  | dir | non-dir | `unlink`, then `mkdir` | same formula |
  | file | none or non-dir | `unlink` if any; `O_CREAT \| O_EXCL \| O_NOFOLLOW`; copy | `mode & 0o555` |
  | symlink | none or non-dir | `unlink` if any; `symlink`, target verbatim | — |
  | hardlink | none or non-dir | `unlink` if any; then link (below) | the target's |
  | any non-dir | dir | **refused** (S2-D2) | — |

- **Hardlinks:** the `linkname` goes through the **same name rules and parent walk** as an
  entry name, so an absolute or `..` linkname is refused. The target's last component must
  `lstat` as a regular file. The link is made with
  `os.link(<leaf>, <leaf>, src_dir_fd=…, dst_dir_fd=…, follow_symlinks=False)` (`skeptic`
  finding 2: an absolute linkname would otherwise link a user-owned host file, like an SSH
  key, into every guest's `/`).
- **Modes:** files lose write bits and setuid/setgid/sticky. Directories keep owner `rwx`, so
  `rm -rf ~/.vestibule/rootfs` works without a `chmod`. Modes are set with `fchmod`, so the
  umask can't change them. The read-only mount (N5's check) is the real guard; this is
  hygiene (contract §6).
- **Ignored:** ownership (everything belongs to the user, as N6 needs), mtimes (no `.pyc`
  files to invalidate), and xattrs (none in the pins). `tarfile` handles PAX and GNU long
  names.
- **POSIX only:** `dir_fd`, `O_NOFOLLOW` and symlinks behave differently on Windows, and
  native is Linux-only anyway.

### S2-D7 (fixed) — Non-Linux hosts, and keeping mypy clean

- `setup` says native isolation is Linux-only, points to the README's `docker pull` lines for
  the container backend, and exits 0.
- **Linux-only code sits behind `sys.platform` checks that mypy understands.** The
  `skeptic` showed that a function-local `import fcntl` and `os.O_NOFOLLOW` still fail
  Windows mypy without one.
  - `unpack.py` is imported only on the Linux path.
  - The POSIX-only test modules skip at module level before importing it, so they skip on
    Windows instead of erroring at collection.

### S2-D8 (fixed) — Config, and what the docs say now

- **`config.py`:** a `rootfs_dir` field (`VESTIBULE_ROOTFS_DIR`, default
  `~/.vestibule/rootfs`) with a `rootfs_path` property, like `workspace_path`. Nothing else;
  the overlap check is S2-D9.
- **README unchanged until native is reachable** (step 6 or 7). Advertising a command whose
  result nothing uses yet would mislead users.
- **`docs/GETTING_STARTED.md`** (the developer guide) gets the command, the sizes, the env
  note from S2-D1, and "keep the rootfs directory on the Linux disk".

### S2-D9 (Victor; amends contract §7) — The overlap check refuses native, not the server

The contract's §7 makes "the workspace and the rootfs directory overlap" a `ConfigError`,
which blocks every tool. The `skeptic` found that this breaks working M1 setups.
- **The scenario:** a Windows, macOS or Linux container user with `VESTIBULE_WORKSPACE=~` or
  `~/.vestibule` (both contain the default rootfs directory) worked under M1. They would get
  every tool blocked, over a directory nothing uses, naming a setting the README doesn't
  list yet.
- **Instead:** the danger only exists where native runs. The check (both paths expanded and
  `realpath`ed, `normcase`d, neither equal nor one inside the other) is one function in
  `rootfs.py`:
  - **step 2:** `setup` refuses to unpack into an overlapping directory, naming both
    settings;
  - **step 6:** native selection refuses the same way. The container backend is still used,
    and the result says why native wasn't (golden rule 6).
- **Bonus:** no new filesystem access at server import time. `realpath` on a hung network
  workspace would otherwise delay startup.
- **Rejected:**
  - A `ConfigError` on every platform (the contract's text): it blocks working setups.
  - A `ConfigError` on Linux only: the same break for Linux container users, and from step 6
    it would block the container fallback too.

### S2-D10 (fixed) — Setup adds the mount points the warden needs

Neither image has `workspace/`, `sandbox/` or `etc/hosts` (§1). The spike created the first
two by hand after `docker export`. With an empty `/etc/hosts`, `localhost` doesn't resolve in
a network-less sandbox, while Docker writes `127.0.0.1 localhost` even under
`--network none` (`skeptic` experiment). Step 3 can't add these later: the tree is shared,
verified and mounted read-only.
- **Before the rename,** setup adds to the tree:
  - empty `workspace/` and `sandbox/` directories (mode 0o755);
  - `etc/hosts` with `127.0.0.1 localhost` and `::1 localhost ip6-localhost ip6-loopback`
    (mode 0o444).
- **If the image already has one of them as another type** (a file at `workspace`, a symlink
  at `etc/hosts`), setup refuses that image. An image's own regular `etc/hosts` is replaced,
  as Docker replaces it at run time.
- The oracle test (§4.3) lists these three as expected differences.

## 3. Changes

| File | Change |
|---|---|
| `src/vestibule/config.py` | `rootfs_dir`, `VESTIBULE_ROOTFS_DIR`, `rootfs_path` (S2-D8); the pin comment gains the re-pin line (S2-D2) |
| `src/vestibule/registry.py` | new: references, anonymous token auth, fetch by digest with size and sha256 checks, platform choice (S2-D3, S2-D4) |
| `src/vestibule/unpack.py` | new: the layer unpacker (S2-D2, S2-D6) |
| `src/vestibule/rootfs.py` | new: cache layout, marker, `find()`, the overlap check, mount points, the `setup` command (S2-D5, S2-D7, S2-D9, S2-D10) |
| `src/vestibule/server.py` | `run()` dispatches `setup` and warns about other arguments (S2-D1). Nothing else changes |
| `pyproject.toml` | the `network` pytest marker |
| `tests/test_registry.py`, `tests/test_unpack.py`, `tests/test_rootfs.py` | new (§4) |
| `tests/test_config.py` | `VESTIBULE_ROOTFS_DIR` |
| `docs/plans/M2/M2-native-warden.md` | amendments: N2 item 4 and §6 (whiteouts refused, S2-D2); §6 cache layout (S2-D5); §7 overlap check (S2-D9) |
| `docs/plans/M1/M1-step6-pinning.md` | "Re-pin cost" gains the Linux `setup` + `network` tests line (S2-D2) |
| `docs/GETTING_STARTED.md` | the setup command for developers (S2-D1, S2-D8) |
| `docs/BUILD_LOG.md`, `docs/HISTORY.md` | the step-2 entry; the sign-off bullet |

**Size check** (Karpathy: "if 200 lines could be 50, rewrite it"): roughly 150 lines each for
`registry.py` and `unpack.py`, and 170 for `rootfs.py`. If one grows well past that, the
design is wrong somewhere and goes back to the plan.

**Build order, each step with its check:**
1. `config.py` → `tests/test_config.py` passes on both platforms.
2. `registry.py` → the offline registry tests pass on both platforms.
3. `unpack.py` → the unpacker tests pass on Linux, and the attack fixtures leave the sentinel
   directory and the tree's parent untouched.
4. `rootfs.py` and `run()` → the cache and CLI tests pass; in WSL, `vestibule-mcp setup`
   makes both trees, and a second run says "already present" without touching the network.
5. The real-image tests (§4.3) → the Linux gate is green with zero skips, inside its 540 s
   budget (measured and recorded).
6. Docs → `tests/test_docs.py` passes.

## 4. Tests

No binary fixtures are committed: the tests build their tar layers in memory with
`tarfile`, so line endings can't change them.

### 4.1 All platforms, offline

- **Config:** `VESTIBULE_ROOTFS_DIR`'s default and an override.
- **References:**
  - accepted: the full form, `python@sha256:…`, `library/python@sha256:…`,
    `ghcr.io/x/y@sha256:…`, and `name:tag@sha256:…` (tag ignored), each normalised exactly;
  - refused, each with the fix in the message: a tag alone, a wrong digest length, `sha512`.
- **Platform choice:**
  - amd64 and arm64 (`variant: v8`) picked from a sample index; attestation entries ignored;
  - a missing platform lists the ones available;
  - a single-platform manifest is accepted or refused by its config;
  - an arm64 marker written and read back by `find()` (the canonical-key round trip).
- **Media types:** gzip accepted; zstd and an unknown type refused, with the message.
- **Verify, then parse:** a digest mismatch and an over-long body are refused before
  anything is parsed (the parser is replaced by one that fails the test if called).
- **Auth, against two local HTTP servers** (a registry and a "CDN") on `127.0.0.1`, through
  a test-only base-URL seam:
  - 401 → token → retry;
  - a blob `307` to the CDN, whose recorded request headers contain **no `Authorization`**;
  - a token that expires twice within one fetch is renewed each time, once per request;
  - a 429 gives the rate-limit message.
- **The overlap check (S2-D9):** both directions, equal paths, via `..` and `~`, through a
  symlink (POSIX); siblings are fine. The message names both settings.
- **Command line:**
  - no arguments → the server path (monkeypatched);
  - `setup` → the setup command, which reads `Limits.from_env()`; a bad setting exits 1,
    listing it;
  - other arguments → the stderr line, stdout empty, then the server path;
  - a non-Linux host → the message and exit 0.

### 4.2 POSIX only (Linux gate; on Windows these modules skip as POSIX-only)

Each attack fixture is unpacked inside a work dir that also holds a **sentinel directory**,
and has a **sentinel file** with a known inode and link count. Afterwards the sentinels, the
work dir's other contents and the tree's parent must be untouched, and the sentinel file's
`st_nlink` unchanged.
- **Single bad entries:** `../x`, `/etc/x`, `a/../../x`, a NUL in a name, a device node, a
  FIFO. Each is refused, named.
- **Multi-entry attacks (contract §6):**
  - symlink `a -> <sentinel dir>`, then `a/.bashrc`;
  - symlink `a -> ..`, then `a/x`. The refusal is asserted, and the tree's parent checked;
  - hardlink `h -> a/x` where `a` is a symlink;
  - hardlinks with an **absolute linkname**, a **`../` linkname**, and one aimed at the
    sentinel file (`skeptic` finding 2);
  - hardlinks to a directory, to a symlink, and to a missing path.
- **S2-D2 refusals:** a `.wh.` whiteout, an opaque whiteout, a directory replaced by a file,
  an entry with no parent.
- **Good layers:**
  - the real images' shape: a `.` root entry first;
  - absolute and relative symlink targets stored verbatim;
  - a hardlink shares the target's inode;
  - **a later layer writes a regular file at a hardlinked name,** and the other name keeps
    its content and inode;
  - file→file, symlink→file and file→dir replacement across layers;
  - a re-listed directory takes the new mode;
  - modes stripped under a umask of 022 (a `0o4755` file becomes `0o555`; a `0o1777` dir
    becomes `0o777`);
  - the finished tree can be deleted with `shutil.rmtree`;
  - a PAX long name.
- **Cache:**
  - the marker is written last: a crash between the rename and the marker leaves `find()`
    saying "absent", and the next setup replaces the tree;
  - setup's own stale `.vestibule-tmp-*` dir is removed;
  - **unrelated entries survive setup:** a sibling directory without a marker, a `.tmp-mine`
    dir, and a stray `x.json`;
  - an invalid or mismatched marker causes a refetch;
  - "already present" makes no registry call (the fetcher is replaced by one that fails);
  - a held lock gives "another setup is running" and exit 1;
  - `find()` refuses a tree directory that is a symlink;
  - the mount points (S2-D10) are present, and an image with a file at `workspace` is
    refused;
  - setup refuses an overlapping rootfs directory before writing anything.

### 4.3 The real images (Linux)

One session-scoped fetch of **both** images serves both tests. That's 4 registry pulls per
pytest session and about 8 per gate run (the 3.14 full run and the 3.11 `not docker` run),
well inside the limit.
- **`network`:**
  - setup's real path fetches both pinned images into `tmp_path`, and `find()` returns each;
  - python's `env` has `PATH` and `LANG=C.UTF-8`, and node's has `PATH`;
  - `usr/local/bin/python3.12` and `usr/local/bin/node` are regular files;
  - `usr/bin/su` has no setuid bit.
- **`docker` + `network`, the oracle test,** for **both** images: our tree is compared with
  `docker export` of the same digest (`docker create --pull never`, removed afterwards).
  - **Must match, for every entry:** the path, type, symlink target, file content (sha256)
    and hardlink pairing; and **our mode equals S2-D6's formula applied to Docker's mode**
    (`skeptic` finding 12a: checking a few known files would let a kept setuid bit through).
  - **Expected differences:** ownership; S2-D10's three entries; and the entries Docker adds
    to a container (`.dockerenv`, `etc/hostname`, `etc/resolv.conf`, mount points). That
    exact list is fixed from the first diff during the build and asserted exactly, so any
    other difference fails.
  - Docker's own unpacker is the reference; this is what proves ours builds the same
    filesystem.

The 3.11 Linux run (`-m "not docker"`) includes the `network` test, so the unpacker runs on
3.11 too.

### 4.4 Done when

- **Gates:** both platform gates green. On Linux, zero skips (so the `network` and oracle
  tests ran), inside the 540 s budget.
- **For real, in WSL:** `vestibule-mcp setup` makes both trees, and its time and sizes are
  recorded in the BUILD_LOG entry. A second run says "already present".
- **Review:** `critic` and `verifier` leave no open CONFIRMED high or medium findings.
- **Docs:** updated, including the contract and M1 re-pin amendments.

## 5. Edge cases

- **Disk full, or any error mid-unpack:** that image fails with the error, its temp dir is
  removed, and the other image is still attempted. Exit 1.
- **Ctrl-C:** the `finally` removes the temp dir, and the lock goes with the process.
  Anything left is removed by the next setup, under the lock.
- **Rootfs directory on `/mnt/c` (drvfs) or another non-POSIX filesystem:** unsupported,
  because symlinks, modes and case behave differently. Not detected;
  `docs/GETTING_STARTED.md` says to keep it on the Linux disk. (The pins have no case-only
  collisions, checked.)
- **Rootfs directory shared with other files** (`~/.cache`): works; setup touches only its
  own names (S2-D5).
- **A re-pin:** a new `<index-hex>-<arch>` tree is fetched; the old one stays until deleted.
  Pruning is out of scope.
- **`VESTIBULE_IMAGE_*` set to a tag without a digest:** native refuses that image with the
  fix (S2-D4); the container backend behaves as today.
- **Settings only in the client's `env` block:** setup prints the directory and references
  it used, and the docs say to set them for setup too (S2-D1). Step 6's no-rootfs message
  prints the exact command (§6).
- **A server running while setup runs** (step 6 on): `find()` only accepts a marker plus a
  tree, written in that order.
- **The rate limit:** 100 anonymous pulls an hour per IP, shared with Docker Desktop's own
  pulls. A 429 gives the message; nothing retries.
- **The workspace pointed into the rootfs through a symlink created later:** caught again at
  native selection (step 6), which re-runs the check on resolved paths.

## 6. Carried forward

- **Step 3:** the warden mounts `find()`'s tree read-only, mounts onto S2-D10's
  `workspace/` and `sandbox/`, and sets the guest environment from the marker's `env` plus
  N9's set.
- **Step 6:**
  - selection calls `find()` and S2-D9's overlap check;
  - the no-rootfs message uses §1's numbers (~123 MB download, ~350 MB on disk) and prints
    the exact command, with any non-default `VESTIBULE_ROOTFS_DIR` / `VESTIBULE_IMAGE_*` in
    front.
- **Step 7:** the README setup section and settings table; arm64 stays "untested" in the
  availability table unless a real host runs it.

## 7. Review record

`skeptic` reviewed draft v1 on 2026-10-24. Verdict: **"sound with amendments."** It re-ran
the investigation scripts and got the same §1 facts. Claims about existing code and external
behaviour went to `verifier` (results below the table).

| # | Sev. | Finding | Disposition |
|---|---|---|---|
| 1 | high | Stale-tree cleanup had no limit on which names it removes: `VESTIBULE_ROOTFS_DIR=~/.cache` would delete unrelated dirs | Accepted: setup touches only its own names (S2-D5); survival test (§4.2) |
| 2 | med | Hardlink `linkname` rules unstated; an absolute linkname ignores `dir_fd` and could link a user-owned host file (SSH key) into every guest | Accepted: linkname through the same rules and walk, leaf-only `os.link` (S2-D6); fixtures with `st_nlink` check |
| 3 | med | The tree lacks `workspace/`, `sandbox/` and a usable `etc/hosts`; step 3 would have to write into the shared cache | Accepted: S2-D10 |
| 4 | med | Where setup reads its settings was unspecified: the client's `env` block is invisible to a shell, and `server.LIMITS` falls back to defaults on a bad config | Accepted: `Limits.from_env()`, printed settings, docs note (S2-D1); step-6 message (§6) |
| 5 | med | The "fails at re-pin" safety net wasn't wired up, and node was never tested | Accepted: re-pin procedure and `config.py` comment amended; both images in §4.3 |
| 6 | low | The mypy claim in S2-D7 was wrong (function-local `import fcntl` still fails on Windows) | Accepted: `sys.platform` guards; module-level skips (S2-D7) |
| 7 | low | Refusing short refs contradicts M1's own "pin it as `<name>@sha256:…`" advice | Accepted: Docker-style normalisation; tag ignored next to a digest (S2-D4) |
| 8 | low | "Re-authenticate once" per run fails on slow links (300 s tokens) | Accepted: once per request (S2-D4); test |
| 9 | low | The platform key wasn't canonical (arm64 has `variant: v8`); one directory for two arches | Accepted: canonical `linux/<arch>`; directory `<hex>-<arch>`; arm64 round-trip test |
| 10 | low | An overlap `ConfigError` on every platform breaks working M1 setups (`VESTIBULE_WORKSPACE=~`), and adds filesystem access at import | Accepted, as a Victor decision: S2-D9 (setup and native selection refuse; no `ConfigError`) |
| 11 | low | Exit 2 on unknown arguments conflicts with the M1 follow-up's rejection of refusing to start | Accepted: warn on stderr and start (S2-D1) |
| 12 | low | Tests that could pass while wrong: oracle modes, the `.` root entry, `a -> ..` landing outside the sentinel, hardlink write-through | Accepted: all four added (§4.2, §4.3) |
| 13 | low | No fsync before the marker: a crash could leave a valid marker over truncated files | Accepted: `os.sync()` before the rename, fsynced marker (S2-D5) |

**`verifier` results: 9 confirmed, 0 refuted.**
- `docs/GETTING_STARTED.md:42-58` puts `VESTIBULE_*` in the client's `env` block (finding 4).
- `server.py:47-56` falls back to `Limits()`; the error still reaches every tool call
  (`:146-148`) (finding 4).
- `select.py:59`/`:529` counts `python@sha256:…` and `name:tag@sha256:…` as pinned; `:531-532`
  says "Pin it as `<name>@sha256:<digest>`" (finding 7).
- M1's re-pin procedure (`M1-step6-pinning.md:117-167`) is Docker-only (finding 5).
- `docs/HISTORY.md:434-437` rejected refusing to start (finding 11).
- Windows mypy fails on a function-local `fcntl` and `os.O_NOFOLLOW`; an early
  `sys.platform` guard makes it clean (finding 6).
- Neither image has `workspace`, `sandbox` or `etc/hosts` in any layer, checked against
  re-verified blobs (finding 3).
- The spike made `workspace/` and `sandbox/` by hand (`mkrootfs.sh`; `warden.py:144-145`);
  its empty `etc/hosts` makes `getaddrinfo('localhost')` fail in a network-less sandbox
  (finding 3).
- Both indexes list arm64 as `linux/arm64` with `variant: v8` (finding 9).
