import copy


SYSTEM_PROXY_CONFIG = {"buff": {"egress_mode": "system_proxy"}}


def _proxy_binding():
    from app.services.buff_egress import BuffEgressBinding

    return BuffEgressBinding(
        mode="system_proxy",
        fingerprint="b" * 64,
        _proxy_server="http://127.0.0.1:7890",
    )


def _install_state(monkeypatch, adoption, credentials):
    state = {"credentials": copy.deepcopy(credentials), "updates": []}
    binding = _proxy_binding()

    def get_credentials():
        return copy.deepcopy(state["credentials"])

    def update_credentials(
        cookies,
        user_agent=None,
        source=None,
        egress_mode=None,
        egress_fingerprint=None,
    ):
        state["updates"].append(
            {
                "cookies": cookies,
                "user_agent": user_agent,
                "source": source,
                "egress_mode": egress_mode,
                "egress_fingerprint": egress_fingerprint,
            }
        )
        updated = dict(state["credentials"])
        updated["cookies"] = cookies
        if user_agent:
            updated["user_agent"] = user_agent
        updated["egress_mode"] = egress_mode
        updated["egress_fingerprint"] = egress_fingerprint
        updated["generation"] = int(updated.get("generation", 0) or 0) + 1
        state["credentials"] = updated

    monkeypatch.setattr(adoption, "get_buff_credentials", get_credentials)
    monkeypatch.setattr(adoption, "update_buff_creds", update_credentials)
    monkeypatch.setattr(
        adoption,
        "buff_credential_replacement_block_reason",
        lambda: "",
    )
    monkeypatch.setattr(
        adoption,
        "load_app_config_validated",
        lambda: copy.deepcopy(SYSTEM_PROXY_CONFIG),
    )
    monkeypatch.setattr(adoption, "resolve_buff_egress", lambda _cfg: binding)
    return state, binding


def test_legacy_unbound_valid_session_adopts_rotated_cookie_once(monkeypatch):
    from app.services import buff_credential_adoption as adoption

    state, binding = _install_state(
        monkeypatch,
        adoption,
        {
            "cookies": "session=old; csrf_token=old",
            "user_agent": "UA-old",
            "generation": 7,
            "source": "playwright",
        },
    )

    class FakeClient:
        verify_calls = 0

        def __init__(self, cookies, **kwargs):
            assert cookies == "session=old; csrf_token=old"
            assert kwargs["user_agent"] == "UA-old"
            assert kwargs["credential_generation"] == 7
            assert kwargs["egress_binding"] is binding
            self.callback = kwargs["credentials_update_callback"]

        def verify_session(self):
            type(self).verify_calls += 1
            self.callback("session=rotated; csrf_token=rotated", "UA-new")
            return True

        def close(self):
            return None

    result = adoption.adopt_legacy_buff_credentials_for_current_egress(
        client_factory=FakeClient
    )

    assert result.ok is True
    assert result.status == "adopted"
    assert result.binding_mode == "system_proxy"
    assert result.generation_before == 7
    assert result.generation_after == 8
    assert FakeClient.verify_calls == 1
    assert len(state["updates"]) == 1
    update = state["updates"][0]
    assert update["cookies"] == "session=rotated; csrf_token=rotated"
    assert update["user_agent"] == "UA-new"
    assert update["source"] is None
    assert update["egress_mode"] == "system_proxy"
    assert update["egress_fingerprint"] == binding.fingerprint


def test_legacy_unbound_failed_validation_does_not_write(monkeypatch):
    from app.services import buff_credential_adoption as adoption

    state, _binding = _install_state(
        monkeypatch,
        adoption,
        {"cookies": "session=old", "generation": 11},
    )

    class FakeClient:
        def __init__(self, _cookies, **_kwargs):
            pass

        def verify_session(self):
            return False

        def close(self):
            pass

    result = adoption.adopt_legacy_buff_credentials_for_current_egress(
        client_factory=FakeClient
    )

    assert result.ok is False
    assert result.status == "not_verified"
    assert result.generation_before == 11
    assert state["updates"] == []
    assert state["credentials"]["generation"] == 11
    assert "egress_mode" not in state["credentials"]


def test_legacy_unbound_verification_exception_does_not_write(monkeypatch):
    from app.services import buff_credential_adoption as adoption
    from buff import BuffVerificationRequired

    state, _binding = _install_state(
        monkeypatch,
        adoption,
        {"cookies": "session=old", "generation": 3},
    )

    class FakeClient:
        def __init__(self, _cookies, **_kwargs):
            pass

        def verify_session(self):
            raise BuffVerificationRequired("challenge")

        def close(self):
            pass

    result = adoption.adopt_legacy_buff_credentials_for_current_egress(
        client_factory=FakeClient
    )

    assert result.ok is False
    assert result.status == "verification_required"
    assert state["updates"] == []


def test_existing_exact_bound_credentials_do_not_probe_or_write(monkeypatch):
    from app.services import buff_credential_adoption as adoption

    binding = _proxy_binding()
    state, _installed_binding = _install_state(
        monkeypatch,
        adoption,
        {
            "cookies": "session=existing",
            "generation": 4,
            "egress_mode": binding.mode,
            "egress_fingerprint": binding.fingerprint,
        },
    )

    class ForbiddenClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("already-bound credentials must not be probed")

    result = adoption.adopt_legacy_buff_credentials_for_current_egress(
        client_factory=ForbiddenClient
    )

    assert result.ok is True
    assert result.status == "already_bound"
    assert result.generation_before == result.generation_after == 4
    assert state["updates"] == []


def test_existing_mismatched_bound_credentials_require_reauth_without_probe(monkeypatch):
    from app.services import buff_credential_adoption as adoption

    state, _binding = _install_state(
        monkeypatch,
        adoption,
        {
            "cookies": "session=bound-elsewhere",
            "generation": 9,
            "egress_mode": "direct",
            "egress_fingerprint": "a" * 64,
        },
    )

    class ForbiddenClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("bound mismatch must fail before network")

    result = adoption.adopt_legacy_buff_credentials_for_current_egress(
        client_factory=ForbiddenClient
    )

    assert result.ok is False
    assert result.status == "reauth_required"
    assert state["updates"] == []


def test_legacy_adoption_fails_if_credential_changes_during_probe(monkeypatch):
    from app.services import buff_credential_adoption as adoption

    state, _binding = _install_state(
        monkeypatch,
        adoption,
        {"cookies": "session=old", "user_agent": "UA", "generation": 5},
    )

    class FakeClient:
        def __init__(self, _cookies, **_kwargs):
            pass

        def verify_session(self):
            state["credentials"]["generation"] = 6
            state["credentials"]["cookies"] = "session=other-writer"
            return True

        def close(self):
            pass

    result = adoption.adopt_legacy_buff_credentials_for_current_egress(
        client_factory=FakeClient
    )

    assert result.ok is False
    assert result.status == "credential_changed"
    assert state["updates"] == []


def test_legacy_adoption_respects_checkout_credential_freeze(monkeypatch):
    from app.services import buff_credential_adoption as adoption

    state, _binding = _install_state(
        monkeypatch,
        adoption,
        {"cookies": "session=old", "generation": 2},
    )
    monkeypatch.setattr(
        adoption,
        "buff_credential_replacement_block_reason",
        lambda: "checkout frozen",
    )

    class ForbiddenClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("frozen credentials must not reach network")

    result = adoption.adopt_legacy_buff_credentials_for_current_egress(
        client_factory=ForbiddenClient
    )

    assert result.ok is False
    assert result.status == "credential_frozen"
    assert state["updates"] == []


def test_legacy_adoption_empty_cookie_fails_before_network(monkeypatch):
    from app.services import buff_credential_adoption as adoption

    state, _binding = _install_state(
        monkeypatch,
        adoption,
        {"cookies": "", "generation": 1},
    )

    class ForbiddenClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("empty Cookie must not reach network")

    result = adoption.adopt_legacy_buff_credentials_for_current_egress(
        client_factory=ForbiddenClient
    )

    assert result.ok is False
    assert result.status == "expired"
    assert state["updates"] == []


def test_adoption_result_is_secret_free():
    from app.services.buff_credential_adoption import LegacyBuffCredentialAdoptionResult

    rendered = LegacyBuffCredentialAdoptionResult(
        True,
        "adopted",
        binding_mode="system_proxy",
        generation_before=7,
        generation_after=8,
    ).as_dict()

    assert set(rendered) == {
        "ok",
        "status",
        "message",
        "binding_mode",
        "generation_before",
        "generation_after",
    }
    assert "cookies" not in rendered
    assert "egress_fingerprint" not in rendered
