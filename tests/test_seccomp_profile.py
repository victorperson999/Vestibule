"""The vendored seccomp profile's pin (M2 step 5, S5-D9). Runs on every platform — it only
reads bytes — so either gate catches a silent re-vendor (M1's image re-pin discipline). The
file is moby/profiles@6fe7deb1b9fb7c0397a4593480d7d22b9ee8caef, Apache-2.0.
"""
import hashlib
from pathlib import Path

PROFILE = Path(__file__).resolve().parents[1] / "src" / "vestibule" / "backends" \
    / "seccomp-profile.json"


def test_the_profile_matches_its_pin():
    data = PROFILE.read_bytes()
    assert len(data) == 17867
    assert hashlib.sha256(data).hexdigest() == \
        "6416b47770785a41ac59073cdc77d9fe98517df2799dc83ef207e622de3053f6"
