"""Registry client for `vestibule-mcp setup` (M2 step 2, docs/plans/M2/M2-step2-rootfs.md
S2-D3, S2-D4). Stdlib only.

Fetches a digest-pinned image straight from an OCI registry, without Docker: the index by
the pinned digest, the manifest for this host's platform, its config, and its layers. Every
response is checked against the digest that named it before anything parses it (S2-D3), so
the JSON parser here, and gzip and tarfile in the unpacker, only see bytes the pin vouches
for.

Auth is the anonymous bearer flow from the registry's 401 challenge. The token travels in an
unredirected header: blob GETs redirect to a CDN, which must never see it.
"""
from __future__ import annotations

import hashlib
import json
import platform
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

INDEX_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
MANIFEST_TYPES = (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
GZIP_LAYERS = (
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.docker.image.rootfs.diff.tar.gzip",
)
MAX_JSON_BYTES = 4 * 1024 * 1024
TIMEOUT_S = 30
_CHUNK = 1024 * 1024
_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_PATH = re.compile(r"[a-z0-9]+(?:[._-]+[a-z0-9]+)*(?:/[a-z0-9]+(?:[._-]+[a-z0-9]+)*)*")


class RegistryError(Exception):
    """This image can't be fetched; the message says why and, where it can, the fix."""


@dataclass(frozen=True)
class Ref:
    host: str    # as written, e.g. docker.io
    name: str    # repository path, e.g. library/python
    digest: str  # sha256:<64 hex>

    def __str__(self) -> str:
        return f"{self.host}/{self.name}@{self.digest}"


@dataclass(frozen=True)
class Image:
    manifest_digest: str
    env: list[str]                # the config's Env (N9 needs its PATH and LANG)
    layers: list[dict[str, Any]]  # gzip layer descriptors, in order


def parse_ref(ref: str) -> Ref:
    """Docker's reference forms, normalised as Docker does; a digest is required. A tag next
    to the digest is ignored, as Docker ignores it (and as M1's pin check counts it)."""
    name, _, digest = ref.partition("@")
    if not _DIGEST.fullmatch(digest):
        raise RegistryError(
            f"{ref!r} is not pinned: native needs a digest reference, like "
            "docker.io/library/python@sha256:<64 hex digits>"
        )
    first, slash, rest = name.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        host, path = first, rest
    else:
        host, path = "docker.io", name
    last = path.rsplit("/", 1)[-1]
    path = path[: len(path) - len(last)] + last.split(":", 1)[0]  # drop a tag
    if host == "docker.io" and "/" not in path:
        path = f"library/{path}"
    if not _PATH.fullmatch(path):
        raise RegistryError(f"{ref!r}: {path!r} is not a valid repository name")
    return Ref(host, path, digest)


def host_arch() -> str:
    """This machine as an OCI architecture, or a RegistryError naming it."""
    machine = platform.machine()
    try:
        return _ARCH[machine.lower()]
    except KeyError:
        raise RegistryError(
            f"no native rootfs for this machine ({machine}); only x86_64 and arm64"
        ) from None


def _api_base(host: str) -> str:  # the tests point this at local servers
    return "https://" + ("registry-1.docker.io" if host == "docker.io" else host)


class Registry:
    def __init__(self, ref: Ref) -> None:
        self.ref = ref
        self.base = f"{_api_base(ref.host)}/v2/{ref.name}"
        self.token: str | None = None

    def _open(self, path: str, accept: tuple[str, ...] = ()) -> Any:
        """GET one URL. A 401 renews the token and retries, once per request: a token
        lasts 300 s on Docker Hub, and one image's layers can outlast several."""
        for attempt in (1, 2):
            req = urllib.request.Request(self.base + path)
            if accept:
                req.add_header("Accept", ", ".join(accept))
            if self.token:
                req.add_unredirected_header("Authorization", f"Bearer {self.token}")
            try:
                return urllib.request.urlopen(req, timeout=TIMEOUT_S)
            except urllib.error.HTTPError as e:
                challenge = e.headers.get("WWW-Authenticate", "")
                e.close()
                if e.code == 401 and attempt == 1:
                    self.token = _anonymous_token(challenge, self.base.split(":", 1)[0])
                    continue
                raise RegistryError(_http_problem(e.code, self.ref)) from None
            except OSError as e:  # URLError, timeouts, resets
                raise RegistryError(f"{self.ref.host}: {getattr(e, 'reason', e)}") from None
        raise AssertionError("unreachable")

    def manifest(self, digest: str, accept: tuple[str, ...]) -> dict[str, Any]:
        with self._open(f"/manifests/{digest}", accept) as r:
            data = _read_capped(r, MAX_JSON_BYTES, digest)
        _check(data, digest)
        return _json(data, digest)

    def json_blob(self, desc: dict[str, Any]) -> dict[str, Any]:
        digest, size = _descriptor(desc, MAX_JSON_BYTES)
        with self._open(f"/blobs/{digest}") as r:
            data = _read_capped(r, size, digest)
        _check(data, digest)
        return _json(data, digest)

    def blob_to_file(self, desc: dict[str, Any], dest: Path) -> None:
        """Download one blob to `dest`; a RegistryError unless it matches its descriptor."""
        digest, size = _descriptor(desc, None)
        h = hashlib.sha256()
        got = 0
        with self._open(f"/blobs/{digest}") as r, open(dest, "wb") as f:
            while chunk := r.read(min(_CHUNK, size - got + 1)):
                got += len(chunk)
                if got > size:
                    raise RegistryError(f"{digest}: longer than its {size} bytes")
                h.update(chunk)
                f.write(chunk)
        if got != size or h.hexdigest() != digest[7:]:
            raise RegistryError(f"{digest}: the download does not match its digest")


def resolve(reg: Registry, arch: str) -> Image:
    """The index (or single manifest) the pin names, narrowed to linux/<arch>."""
    top = reg.manifest(reg.ref.digest, INDEX_TYPES + MANIFEST_TYPES)
    try:
        if "manifests" in top:
            matches = [d for d in top["manifests"]
                       if d.get("platform", {}).get("os") == "linux"
                       and d["platform"].get("architecture") == arch]
            if not matches:
                have = sorted({f"{p.get('os')}/{p.get('architecture')}"
                               for d in top["manifests"]
                               if (p := d.get("platform", {})).get("os") not in (None, "unknown")})
                raise RegistryError(
                    f"{reg.ref} has no linux/{arch} image (it has {', '.join(have)})"
                )
            manifest_digest, _ = _descriptor(matches[0], MAX_JSON_BYTES)
            man = reg.manifest(manifest_digest, MANIFEST_TYPES)
        else:
            manifest_digest, man = reg.ref.digest, top
        config = reg.json_blob(man["config"])
        if (config.get("os"), config.get("architecture")) != ("linux", arch):
            raise RegistryError(f"{reg.ref} is {config.get('os')}/{config.get('architecture')}, "
                                f"not linux/{arch}")
        layers = list(man["layers"])
        env = list(config.get("config", {}).get("Env") or [])
    except (KeyError, TypeError, AttributeError) as e:
        raise RegistryError(f"{reg.ref}: unexpected manifest shape ({e!r})") from None
    for layer in layers:
        media = layer.get("mediaType")
        if media in GZIP_LAYERS:
            continue
        if isinstance(media, str) and "zstd" in media:
            raise RegistryError(f"{reg.ref} has a zstd layer; it needs Python 3.14's "
                                "compression.zstd, and Vestibule supports 3.11+")
        raise RegistryError(f"{reg.ref}: unsupported layer type {media!r}")
    return Image(manifest_digest, env, layers)


def _anonymous_token(challenge: str, url_scheme: str) -> str:
    """A token from the challenge's realm, which must use the registry's own scheme (HTTPS):
    a `file:` realm would read a local file, and its "token" would go to the registry."""
    scheme, _, params = challenge.partition(" ")
    if scheme.lower() != "bearer":
        raise RegistryError(f"the registry wants {scheme or 'unknown'} auth; only anonymous "
                            "bearer tokens are supported")
    fields = dict(re.findall(r'(\w+)="([^"]*)"', params))
    realm = fields.pop("realm", "")
    if urllib.parse.urlsplit(realm).scheme != url_scheme:
        raise RegistryError(f"the registry's token realm {realm[:100]!r} is not {url_scheme}")
    url = realm + "?" + urllib.parse.urlencode(fields)
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as r:
            body = _json(_read_capped(r, MAX_JSON_BYTES, "token"), "token")
    except urllib.error.HTTPError as e:
        raise RegistryError(f"token request: HTTP {e.code}") from None
    except (OSError, ValueError) as e:
        raise RegistryError(f"token request: {getattr(e, 'reason', e)}") from None
    token = body.get("token") or body.get("access_token")
    if not isinstance(token, str) or not token:
        raise RegistryError("the registry's token response has no token")
    return token


def _http_problem(code: int, ref: Ref) -> str:
    if code == 429:
        return (f"{ref.host}: registry rate limit (anonymous pulls are counted per IP); "
                "try again later")
    return f"{ref.host}: HTTP {code}"


def _descriptor(desc: Any, cap: int | None) -> tuple[str, int]:
    digest, size = desc.get("digest"), desc.get("size")
    if not (isinstance(digest, str) and _DIGEST.fullmatch(digest)):
        raise RegistryError(f"bad digest in a descriptor: {digest!r}")
    if not isinstance(size, int) or size < 0 or (cap is not None and size > cap):
        raise RegistryError(f"{digest}: bad size {size!r}")
    return digest, size


def _read_capped(r: Any, cap: int, what: str) -> bytes:
    data: bytes = r.read(cap + 1)
    if len(data) > cap:
        raise RegistryError(f"{what}: larger than {cap} bytes")
    return data


def _check(data: bytes, digest: str) -> None:
    if hashlib.sha256(data).hexdigest() != digest[7:]:
        raise RegistryError(f"{digest}: the registry sent content that does not match it")


def _json(data: bytes, what: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except ValueError:
        raise RegistryError(f"{what}: not valid JSON") from None
    if not isinstance(value, dict):
        raise RegistryError(f"{what}: not a JSON object")
    return value
