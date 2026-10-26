"""Registry client (M2 step 2, docs/plans/M2/M2-step2-rootfs.md §4.1). Offline and on every
platform: two local HTTP servers stand in for a registry and the CDN its blobs redirect to.
"""
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

import vestibule.registry as registry
from vestibule.registry import GZIP_LAYERS, INDEX_TYPES, MANIFEST_TYPES, RegistryError, parse_ref

HEX = "a" * 64


@pytest.mark.parametrize("ref, host, name", [
    (f"docker.io/library/python@sha256:{HEX}", "docker.io", "library/python"),
    (f"python@sha256:{HEX}", "docker.io", "library/python"),
    (f"library/python@sha256:{HEX}", "docker.io", "library/python"),
    (f"docker.io/python@sha256:{HEX}", "docker.io", "library/python"),
    (f"someuser/img@sha256:{HEX}", "docker.io", "someuser/img"),
    (f"python:3.12-slim@sha256:{HEX}", "docker.io", "library/python"),  # tag ignored
    (f"ghcr.io/x/y:1.0@sha256:{HEX}", "ghcr.io", "x/y"),
    (f"localhost:5000/team/img@sha256:{HEX}", "localhost:5000", "team/img"),
])
def test_references_are_normalised_like_docker(ref, host, name):
    assert parse_ref(ref) == registry.Ref(host, name, f"sha256:{HEX}")


@pytest.mark.parametrize("ref", [
    "python:3.12-slim",                 # a tag alone has nothing to verify the index against
    f"python@sha256:{HEX[:-1]}",
    f"python@sha512:{'a' * 128}",
    f"python@sha256:{HEX.upper()}",
])
def test_unpinned_references_are_refused_with_the_fix(ref):
    with pytest.raises(RegistryError, match="native needs a digest reference, like docker.io"):
        parse_ref(ref)


@pytest.mark.parametrize("ref", [f"docker.io/../etc@sha256:{HEX}", f"ghcr.io/a?b@sha256:{HEX}"])
def test_a_bad_repository_name_is_refused(ref):
    with pytest.raises(RegistryError, match="not a valid repository name"):
        parse_ref(ref)


@pytest.mark.parametrize("machine, arch", [
    ("x86_64", "amd64"), ("AMD64", "amd64"), ("aarch64", "arm64"), ("arm64", "arm64"),
])
def test_host_arch(monkeypatch, machine, arch):
    monkeypatch.setattr(registry.platform, "machine", lambda: machine)
    assert registry.host_arch() == arch


def test_other_machines_are_refused_by_name(monkeypatch):
    monkeypatch.setattr(registry.platform, "machine", lambda: "riscv64")
    with pytest.raises(RegistryError, match=r"\(riscv64\)"):
        registry.host_arch()


class FakeRegistry:
    """Objects served by digest, behind the anonymous-token flow. Blob GETs redirect to a
    second server (the "CDN"), which records the headers it receives."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.wrong: dict[str, bytes] = {}  # digest -> bytes served instead (a lying registry)
        self.status: int | None = None     # forced status for every manifest/blob request
        self.expire_every = 0              # >0: the token goes stale after this many uses
        self.accept_tokens = True
        self.tokens_issued = 0
        self.authorised = 0
        self.registry_auth: list[str | None] = []
        self.cdn_headers: list[dict[str, str]] = []
        self._valid: str | None = None
        self.reg_url = self.cdn_url = ""

    def add(self, data: bytes, media: str = "application/octet-stream") -> dict:
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        self.objects[digest] = data
        return {"mediaType": media, "digest": digest, "size": len(data)}

    def add_json(self, obj: dict, media: str) -> dict:
        return self.add(json.dumps(obj).encode(), media)

    def body(self, digest: str) -> bytes:
        return self.wrong.get(digest, self.objects[digest])


def _handler(fake: FakeRegistry, cdn: bool) -> type[BaseHTTPRequestHandler]:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the test output clean
            pass

        def _send(self, code: int, body: bytes = b"", headers: dict | None = None) -> None:
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            if cdn:
                self.cdn_record()
                return self._send(200, fake.body(path.rsplit("/", 1)[-1]))
            if path == "/token":
                fake.tokens_issued += 1
                fake._valid = f"tok-{fake.tokens_issued}"
                return self._send(200, json.dumps({"token": fake._valid}).encode())
            auth = self.headers.get("Authorization")
            fake.registry_auth.append(auth)
            if not fake.accept_tokens or fake._valid is None or auth != f"Bearer {fake._valid}":
                realm = f"{fake.reg_url}/token"
                return self._send(401, headers={"WWW-Authenticate":
                    f'Bearer realm="{realm}",service="fake",scope="repository:x:pull"'})
            fake.authorised += 1
            if fake.expire_every and fake.authorised % fake.expire_every == 0:
                fake._valid = None
            if fake.status:
                return self._send(fake.status)
            kind, digest = path.split("/")[-2:]
            if digest not in fake.objects:
                return self._send(404)
            if kind == "blobs":
                return self._send(307, headers={"Location": f"{fake.cdn_url}/cdn/{digest}"})
            return self._send(200, fake.body(digest))

        def cdn_record(self):
            fake.cdn_headers.append({k.lower(): v for k, v in self.headers.items()})

    return H


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")  # never route the local servers via a proxy
    f = FakeRegistry()
    servers = [ThreadingHTTPServer(("127.0.0.1", 0), _handler(f, cdn)) for cdn in (False, True)]
    for s in servers:
        # A short poll: shutdown() waits for it, twice per test.
        threading.Thread(target=s.serve_forever, args=(0.02,), daemon=True).start()
    f.reg_url, f.cdn_url = (f"http://127.0.0.1:{s.server_address[1]}" for s in servers)
    monkeypatch.setattr(registry, "_api_base", lambda host: f.reg_url)
    yield f
    for s in servers:
        s.shutdown()
        s.server_close()


def _image(fake: FakeRegistry, arch: str = "amd64", layer_media: str = GZIP_LAYERS[0]):
    """A one-layer image for `arch`; returns (index descriptor, manifest descriptor, layer)."""
    config = fake.add_json({"os": "linux", "architecture": arch,
                            "config": {"Env": ["PATH=/usr/bin", "LANG=C.UTF-8"]}},
                           "application/vnd.oci.image.config.v1+json")
    layer = fake.add(b"layer bytes", layer_media)
    man = fake.add_json({"schemaVersion": 2, "mediaType": MANIFEST_TYPES[0],
                         "config": config, "layers": [layer]}, MANIFEST_TYPES[0])
    return man, layer


def _index(fake: FakeRegistry, *entries: tuple[dict, dict]) -> str:
    index = fake.add_json({"schemaVersion": 2, "mediaType": INDEX_TYPES[0], "manifests": [
        {**desc, "platform": platform} for desc, platform in entries
    ]}, INDEX_TYPES[0])
    return f"docker.io/library/test@{index['digest']}"


def _two_arch_index(fake: FakeRegistry) -> str:
    amd, _ = _image(fake, "amd64")
    arm, _ = _image(fake, "arm64")
    attestation = fake.add_json({"schemaVersion": 2}, MANIFEST_TYPES[0])
    return _index(fake, (amd, {"os": "linux", "architecture": "amd64"}),
                  (arm, {"os": "linux", "architecture": "arm64", "variant": "v8"}),
                  (attestation, {"os": "unknown", "architecture": "unknown"}))


@pytest.mark.parametrize("arch", ["amd64", "arm64"])
def test_resolve_picks_this_platform(fake, arch):
    ref = _two_arch_index(fake)
    image = registry.resolve(registry.Registry(parse_ref(ref)), arch)
    assert image.env == ["PATH=/usr/bin", "LANG=C.UTF-8"]
    assert len(image.layers) == 1
    man = json.loads(fake.objects[image.manifest_digest])
    assert json.loads(fake.objects[man["config"]["digest"]])["architecture"] == arch


def test_a_missing_platform_lists_the_available_ones(fake):
    ref = _two_arch_index(fake)
    with pytest.raises(RegistryError, match=r"no linux/s390x image \(it has linux/amd64, "
                                            r"linux/arm64\)"):
        registry.resolve(registry.Registry(parse_ref(ref)), "s390x")


def test_a_single_platform_manifest_is_judged_by_its_config(fake):
    man, _ = _image(fake, "amd64")
    reg = registry.Registry(parse_ref(f"docker.io/library/test@{man['digest']}"))
    assert registry.resolve(reg, "amd64").manifest_digest == man["digest"]
    with pytest.raises(RegistryError, match="is linux/amd64, not linux/arm64"):
        registry.resolve(reg, "arm64")


@pytest.mark.parametrize("media, message", [
    ("application/vnd.oci.image.layer.v1.tar+zstd", "zstd layer; it needs Python 3.14"),
    ("application/vnd.oci.image.layer.v1.tar", "unsupported layer type"),
])
def test_non_gzip_layers_are_refused(fake, media, message):
    man, _ = _image(fake, layer_media=media)
    ref = _index(fake, (man, {"os": "linux", "architecture": "amd64"}))
    with pytest.raises(RegistryError, match=message):
        registry.resolve(registry.Registry(parse_ref(ref)), "amd64")


@pytest.mark.parametrize("which", ["index", "config"])  # manifest() and json_blob()
def test_json_that_does_not_match_its_digest_is_never_parsed(fake, monkeypatch, which):
    ref = _two_arch_index(fake)
    digest = ref.split("@")[1]
    if which == "config":
        amd64 = json.loads(fake.objects[digest])["manifests"][0]["digest"]
        digest = json.loads(fake.objects[amd64])["config"]["digest"]
    lie = fake.wrong[digest] = b'{"manifests": []}'  # shorter than either real document
    real_loads = json.loads

    def loads(data, *args, **kwargs):  # the token response is parsed; the lie must not be
        assert data != lie, "unverified bytes reached the JSON parser"
        return real_loads(data, *args, **kwargs)

    monkeypatch.setattr(registry.json, "loads", loads)
    with pytest.raises(RegistryError, match="does not match"):
        registry.resolve(registry.Registry(parse_ref(ref)), "amd64")


def test_blob_download_is_checked_against_its_descriptor(fake, tmp_path):
    ref = _two_arch_index(fake)
    reg = registry.Registry(parse_ref(ref))
    layer = registry.resolve(reg, "amd64").layers[0]
    reg.blob_to_file(layer, tmp_path / "ok")
    assert (tmp_path / "ok").read_bytes() == b"layer bytes"

    fake.wrong[layer["digest"]] = b"layer bytes and more"  # longer than its size
    with pytest.raises(RegistryError, match="longer than its 11 bytes"):
        reg.blob_to_file(layer, tmp_path / "long")
    fake.wrong[layer["digest"]] = b"LAYER BYTES"  # same size, other content
    with pytest.raises(RegistryError, match="does not match its digest"):
        reg.blob_to_file(layer, tmp_path / "bad")


def test_the_token_never_reaches_the_cdn(fake, tmp_path):
    ref = _two_arch_index(fake)
    reg = registry.Registry(parse_ref(ref))
    reg.blob_to_file(registry.resolve(reg, "amd64").layers[0], tmp_path / "blob")
    assert fake.cdn_headers, "the blob was not served through the redirect"
    assert all("authorization" not in h for h in fake.cdn_headers)
    assert "Bearer tok-1" in fake.registry_auth  # ...while the registry did get it


def test_an_expired_token_is_renewed_once_per_request(fake, tmp_path):
    fake.expire_every = 1  # every token goes stale after one use
    ref = _two_arch_index(fake)
    reg = registry.Registry(parse_ref(ref))
    image = registry.resolve(reg, "amd64")   # index, manifest, config
    reg.blob_to_file(image.layers[0], tmp_path / "blob")  # + the layer
    assert fake.authorised == 4
    assert fake.tokens_issued == 4  # renewed for each request; no limit per run


def test_a_second_401_for_the_same_request_fails(fake):
    fake.accept_tokens = False
    ref = _two_arch_index(fake)
    with pytest.raises(RegistryError, match="HTTP 401"):
        registry.resolve(registry.Registry(parse_ref(ref)), "amd64")
    assert fake.tokens_issued == 1


@pytest.mark.parametrize("realm", ["file:///home/u/secret.json", "http://auth.example/token"])
def test_a_token_realm_must_use_the_registrys_scheme(realm):
    # A file: realm would read a local file and send its "token" to the registry.
    with pytest.raises(RegistryError, match="token realm .* is not https"):
        registry._anonymous_token(f'Bearer realm="{realm}",service="x"', "https")


def test_the_realm_check_gets_the_registrys_real_scheme(monkeypatch):
    # The fake registry is plain http, so this pins the wiring for the real (https) base.
    seen = []

    def urlopen(req, timeout):
        raise registry.urllib.error.HTTPError(
            req.full_url, 401, "Unauthorized",
            {"WWW-Authenticate": 'Bearer realm="https://auth.example/token"'}, None)

    def token(challenge, url_scheme):
        seen.append(url_scheme)
        raise RegistryError("stop here")

    monkeypatch.setattr(registry.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(registry, "_anonymous_token", token)
    reg = registry.Registry(parse_ref(f"docker.io/library/python@sha256:{HEX}"))
    assert reg.base.startswith("https://registry-1.docker.io/")
    with pytest.raises(RegistryError, match="stop here"):
        reg.manifest(f"sha256:{HEX}", INDEX_TYPES)
    assert seen == ["https"]


def test_a_rate_limit_says_so(fake):
    fake.status = 429
    ref = _two_arch_index(fake)
    with pytest.raises(RegistryError, match="rate limit .* try again later"):
        registry.resolve(registry.Registry(parse_ref(ref)), "amd64")
