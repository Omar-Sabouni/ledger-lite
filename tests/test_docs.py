from importlib.resources import files

from app.main import app, redoc_docs, swagger_docs, swagger_ui_redirect


def test_documentation_routes_serve_generated_interfaces_and_live_schema() -> None:
    swagger = swagger_docs()
    redoc = redoc_docs()
    schema = app.openapi()
    stylesheet = files("app").joinpath("static", "ledger-docs.css")

    assert swagger.status_code == 200
    assert "SwaggerUIBundle" in swagger.body.decode("utf-8")
    assert redoc.status_code == 200
    assert '<redoc spec-url="/openapi.json">' in redoc.body.decode("utf-8")
    assert schema["openapi"]
    assert stylesheet.is_file()


def test_swagger_uses_the_shared_safety_notice_and_format_switcher() -> None:
    response = swagger_docs()
    html = response.body.decode("utf-8")

    assert response.status_code == 200
    assert "LedgerLite · API reference" in html
    assert 'class="docs-toolbar"' in html
    assert "Local ledger console — no authentication or ownership checks." in html
    assert 'id="api-reference"' in html
    assert 'href="/docs" aria-current="page">Swagger UI' in html
    assert 'href="/redoc"' in html
    assert 'href="/openapi.json"' in html
    assert 'lang="en"' in html
    assert '"tryItOutEnabled": true' in html
    assert 'href="/static/ledger-docs.css"' in html


def test_redoc_has_the_same_context_and_uses_the_live_schema() -> None:
    response = redoc_docs()
    html = response.body.decode("utf-8")

    assert response.status_code == 200
    assert 'class="docs-page docs-page--redoc"' in html
    assert 'class="docs-toolbar"' in html
    assert 'href="/redoc" aria-current="page">ReDoc' in html
    assert 'spec-url="/openapi.json"' in html
    assert 'href="/static/ledger-docs.css"' in html


def test_openapi_makes_security_idempotency_and_conflicts_explicit() -> None:
    schema = app.openapi()
    paths = schema["paths"]
    transfer = paths["/api/v1/transfers"]["post"]
    deposit = paths["/api/v1/accounts/{account_id}/deposits"]["post"]
    reversal = paths["/api/v1/transactions/{transaction_id}/reversals"]["post"]

    assert "no authentication" in schema["info"]["description"]
    assert "securitySchemes" not in schema["components"]

    account_create = paths["/api/v1/accounts"]["post"]
    for operation in (account_create, deposit, transfer, reversal):
        idempotency = next(
            parameter
            for parameter in operation["parameters"]
            if parameter["name"] == "Idempotency-Key"
        )
        assert idempotency["in"] == "header"
        assert idempotency["required"] is True
        assert "Exact retries replay" in idempotency["description"]
        assert idempotency["schema"]["maxLength"] == 255
        assert (
            operation["responses"]["201"]["headers"]["Idempotent-Replayed"]["schema"][
                "type"
            ]
            == "boolean"
        )
        assert "409" in operation["responses"]
        problem = operation["responses"]["409"]["content"]
        assert set(problem) == {"application/problem+json"}
        assert problem["application/problem+json"]["schema"]["title"] == (
            "ProblemResponse"
        )
        assert set(operation["responses"]["422"]["content"]) == {
            "application/problem+json"
        }
        assert set(operation["responses"]["503"]["content"]) == {
            "application/problem+json"
        }

    assert "404" in transfer["responses"]
    assert "404" in deposit["responses"]


def test_openapi_preserves_real_methods_paths_and_decimal_string_contract() -> None:
    schema = app.openapi()
    paths = schema["paths"]

    assert {
        "/health",
        "/livez",
        "/readyz",
        "/api/v1/overview",
        "/api/v1/capabilities",
        "/api/v1/accounts",
        "/api/v1/accounts/{account_id}/deposits",
        "/api/v1/transfers",
        "/api/v1/accounts/{account_id}/statement",
        "/api/v1/transactions",
        "/api/v1/reconciliation/runs",
        "/api/v1/events/stream",
    }.issubset(paths)
    assert set(paths["/api/v1/accounts"]) == {"get", "post"}
    assert set(paths["/api/v1/transfers"]) == {"post"}
    assert set(paths["/api/v1/accounts/{account_id}/statement"]) == {"get"}
    assert "/transfers" not in paths
    assert "/accounts" not in paths

    deposit_amount = schema["components"]["schemas"]["DepositCreate"]["properties"][
        "amount"
    ]
    assert deposit_amount["type"] == "string"
    assert deposit_amount["examples"] == ["25.00"]
    assert "409" in paths["/api/v1/transfers"]["post"]["responses"]
    assert "503" in paths["/health"]["get"]["responses"]
    assert set(
        paths["/api/v1/events/stream"]["get"]["responses"]["200"]["content"]
    ) == {"text/event-stream"}


def test_oauth_redirect_and_documentation_routes_remain_available() -> None:
    routes = {
        route.path
        for route in app.routes
        if isinstance(getattr(route, "path", None), str)
    }
    oauth_redirect = swagger_ui_redirect()

    assert {
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/openapi.json",
        "/static",
    }.issubset(routes)
    assert oauth_redirect.status_code == 200
