import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.app.config import Settings
from api.app.main import app

client = TestClient(app)

DEMO = "http://localhost:8080"  # the widget demo page, allowed by default


def preflight(origin):
    return client.options(
        "/chat",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )


def test_allowed_origin_gets_cors_headers():
    response = client.get("/health", headers={"Origin": DEMO})
    assert response.headers["access-control-allow-origin"] == DEMO
    assert "access-control-allow-credentials" not in response.headers


def test_preflight_from_allowed_origin_lets_the_widget_post_json():
    response = preflight(DEMO)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == DEMO
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "content-type" in response.headers["access-control-allow-headers"].lower()


def test_other_origins_get_no_cors_headers():
    response = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers
    assert preflight("https://evil.example").status_code == 400


def test_origins_setting_is_a_trimmed_comma_separated_list():
    s = Settings(allowed_origins=" https://www.desu.edu/ , http://localhost:8080,, ")
    assert s.cors_origins == ["https://www.desu.edu", "http://localhost:8080"]


@pytest.mark.parametrize("origins", ["*", "https://www.desu.edu, *", "https://*.desu.edu"])
def test_wildcard_origins_are_refused(origins):
    with pytest.raises(ValidationError, match="isn't allowed"):
        Settings(allowed_origins=origins)
