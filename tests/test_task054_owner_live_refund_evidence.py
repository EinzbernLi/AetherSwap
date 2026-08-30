"""Evidence-backed regression for TASK-054 OWNER-live BUFF refund shape."""

from app.auto_offer.adapters import BuffOrderLifecycle, PlatformResultStatus
from tests.test_task042_buff_lifecycle_read import (
    BuffStub,
    adapter,
    history_page,
    refunded_item,
    request,
)


def test_task054_owner_live_refund_shape_with_sent_offer_is_accepted():
    """Pin the sanitized refund variant accepted in #181 comment 5465619479."""

    stub = BuffStub(
        history_pages={
            1: history_page(page_num=1, total_page=2),
            2: history_page(
                page_num=2,
                total_page=2,
                items=[refunded_item(has_sent_offer=True)],
            ),
        }
    )

    result = adapter(stub).execute(request())

    assert result.status is PlatformResultStatus.SUCCESS
    assert result.detail == "refunded"
    assert result.evidence.lifecycle is BuffOrderLifecycle.REFUNDED
    assert result.evidence.page_num == 2
    assert stub.history_calls == [(1, "csgo"), (2, "csgo")]
    assert stub.steam_calls == 0
