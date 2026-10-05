"""The public landing page at /."""


def test_home_page_is_public_and_explains_the_project(client):
    r = client.get("/", auth=None)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert "Aegis" in body and 'href="/docs"' in body and 'href="/health"' in body
    csp = r.headers["content-security-policy"]
    assert "script-src" not in csp and "default-src 'none'" in csp
    assert "nonce-" in csp and 'nonce="' in body


def test_home_page_is_not_in_the_api_schema(client):
    assert "/" not in client.get("/openapi.json").json()["paths"]
