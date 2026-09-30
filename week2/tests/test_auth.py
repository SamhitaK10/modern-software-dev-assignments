import base64
import hashlib
import urllib.parse

import auth


def test_pkce_challenge_matches_verifier():
    verifier, challenge = auth.make_pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_authorize_url_uses_minimal_scopes_and_pkce():
    q = urllib.parse.parse_qs(urllib.parse.urlparse(auth.build_authorize_url("cid", "st", "chal")).query)
    assert q["scope"] == ["read,issues:create"]
    assert q["code_challenge_method"] == ["S256"] and q["state"] == ["st"]
    assert "write" not in q["scope"][0].split(",") and "admin" not in q["scope"][0]
