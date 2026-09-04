from pathlib import Path


def replace_once(path, old, new, label):
    p = Path(path)
    s = p.read_text(encoding='utf-8')
    count = s.count(old)
    if count != 1:
        raise SystemExit(f'{label}: count={count}')
    p.write_text(s.replace(old, new, 1), encoding='utf-8')

replace_once(
    'app/auto_offer/canary_authority.py',
    '''        elif target.action == "auto_offer_accept":
            if (
                permit.expected_is_our_offer is not False
                or permit.expected_counterparty_steam_id is None
            ):
                raise CanaryWriteBlockedError("canary_direction_mismatch")
''',
    '''        elif target.action == "auto_offer_accept":
            if (
                permit.expected_is_our_offer is not False
                or permit.expected_counterparty_steam_id is None
            ):
                raise CanaryWriteBlockedError("write_not_allowlisted")
''',
    'buyer permit accept rejection code',
)

p = Path('tests/test_auto_offer_canary_authority.py')
s = p.read_text(encoding='utf-8')
anchor = '''def test_canary_owner_session_does_not_gain_accept_authority(tmp_path):
    db_path = _host_db_with_goods(tmp_path / "host.db")
    authority = CanaryAuthority(
        _root=tmp_path / "authority",
        _host_db_path=db_path,
    )
    session = authority._arm_owner_session(_permit())
    with pytest.raises(CanaryWriteBlockedError, match="write_not_allowlisted"):
        with session.external_write_guard(
            _target(
                "auto_offer_accept",
                db_id=7,
                host_goods_id=73001,
            )
        ):
            pass
    session.release_keep_fence()
'''
addition = anchor + '''\n\ndef test_canary_seller_owner_session_gains_exact_accept_authority(tmp_path):
    db_path = _host_db_with_goods(tmp_path / "host.db")
    authority = CanaryAuthority(
        _root=tmp_path / "authority-seller",
        _host_db_path=db_path,
    )
    session = authority._arm_owner_session(
        _permit(expected_is_our_offer=False)
    )
    calls = []
    with session.external_write_guard(_target("auto_offer_accept")):
        calls.append("accept")
    assert calls == ["accept"]
    session.release_keep_fence()
'''
if s.count(anchor) != 1:
    raise SystemExit(f'seller accept test anchor count={s.count(anchor)}')
p.write_text(s.replace(anchor, addition, 1), encoding='utf-8')
print('TASK098 CI fix applied')
