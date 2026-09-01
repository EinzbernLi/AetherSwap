def test_pipeline_route_blocks_egress_reauth_before_start(monkeypatch):
    from app.routes import pipeline as route

    monkeypatch.setattr(
        route,
        "_buff_egress_start_blocker",
        lambda: {
            "code": "BUFF_EGRESS_REAUTH_REQUIRED",
            "message": "reauth",
        },
    )
    monkeypatch.setattr(
        route,
        "start_pipeline",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("pipeline must not start when egress mismatches")
        ),
    )

    response = route.api_pipeline_start(route.ConfigBody(config={}))

    assert response == {
        "ok": False,
        "code": "BUFF_EGRESS_REAUTH_REQUIRED",
        "error": "reauth",
    }


def test_pipeline_egress_blocker_accepts_legacy_direct_credentials(monkeypatch):
    from app import config_loader
    from app.routes import pipeline as route
    from app.services import buff_egress

    monkeypatch.setattr(
        config_loader,
        "load_app_config_validated",
        lambda: {"buff": {"egress_mode": "direct"}},
    )
    monkeypatch.setattr(
        config_loader,
        "get_buff_credentials",
        lambda: {"cookies": "session=legacy", "generation": 10},
    )
    monkeypatch.setattr(
        buff_egress,
        "resolve_buff_egress",
        lambda _cfg: buff_egress.direct_buff_egress_binding(),
    )

    assert route._buff_egress_start_blocker() == {}


def test_pipeline_egress_blocker_returns_sanitized_resolution_code(monkeypatch):
    from app import config_loader
    from app.routes import pipeline as route
    from app.services import buff_egress

    monkeypatch.setattr(
        config_loader,
        "load_app_config_validated",
        lambda: {"buff": {"egress_mode": "system_proxy"}},
    )
    monkeypatch.setattr(config_loader, "get_buff_credentials", lambda: {})
    monkeypatch.setattr(
        buff_egress,
        "resolve_buff_egress",
        lambda _cfg: (_ for _ in ()).throw(
            buff_egress.BuffEgressError("BUFF_EGRESS_SYSTEM_PROXY_UNAVAILABLE")
        ),
    )

    blocker = route._buff_egress_start_blocker()
    assert blocker["code"] == "BUFF_EGRESS_SYSTEM_PROXY_UNAVAILABLE"
    assert "proxy" not in blocker["message"].lower()
    assert "cookie" not in blocker["message"].lower()
