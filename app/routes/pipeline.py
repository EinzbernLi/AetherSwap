"""Pipeline start/stop routes."""
from fastapi import APIRouter
from pydantic import BaseModel
from app.pipeline import get_pipeline_start_blocker, start_pipeline
from app.state import request_stop, set_status, log
router = APIRouter()
class ConfigBody(BaseModel):
    config: dict
    acknowledge_buff_reconciliation: bool = False
    buff_reconciliation_intent_id: str = ""


def _buff_egress_start_blocker() -> dict:
    """Fail locally before pipeline creation when BUFF route identity is unsafe."""

    from app.config_loader import get_buff_credentials, load_app_config_validated
    from app.services.buff_egress import (
        BuffEgressError,
        BuffEgressReauthRequired,
        resolve_buff_egress,
        validate_buff_credential_binding,
    )

    try:
        binding = resolve_buff_egress(load_app_config_validated())
        validate_buff_credential_binding(get_buff_credentials() or {}, binding)
        return {}
    except BuffEgressReauthRequired as exc:
        return {
            "code": exc.code,
            "message": "BUFF 网络出口与当前登录凭据不一致，请先重新完成 BUFF 登录/安全验证",
        }
    except BuffEgressError as exc:
        return {
            "code": exc.code,
            "message": "BUFF 网络出口当前不可用，请检查直连/系统代理设置后重试",
        }
    except Exception:
        return {
            "code": "BUFF_EGRESS_UNAVAILABLE",
            "message": "无法安全确认 BUFF 网络出口，已阻止启动流水线",
        }


@router.post("/api/pipeline/start")
def api_pipeline_start(body: ConfigBody):
    egress_blocker = _buff_egress_start_blocker()
    if egress_blocker:
        return {
            "ok": False,
            "code": egress_blocker.get("code"),
            "error": egress_blocker.get("message"),
        }
    blocker = get_pipeline_start_blocker()
    # Let start_pipeline perform its one bounded exact payment-failure
    # reconciliation attempt.  It returns the same blocker when evidence is
    # absent or unsafe, preserving the manual acknowledgement fallback.
    if blocker and blocker.get("code") != "BUFF_RECONCILIATION_REQUIRED":
        return {
            "ok": False,
            "code": blocker.get("code"),
            "error": blocker.get("message"),
        }
    if not start_pipeline(
        body.config,
        acknowledge_buff_reconciliation=body.acknowledge_buff_reconciliation,
        buff_reconciliation_intent_id=body.buff_reconciliation_intent_id,
    ):
        egress_blocker = _buff_egress_start_blocker()
        if egress_blocker:
            return {
                "ok": False,
                "code": egress_blocker.get("code"),
                "error": egress_blocker.get("message"),
            }
        blocker = get_pipeline_start_blocker()
        if blocker:
            return {
                "ok": False,
                "reconciliation_required": (
                    blocker.get("code") == "BUFF_RECONCILIATION_REQUIRED"
                ),
                "code": blocker.get("code"),
                "error": blocker.get("message"),
                "checkout": blocker.get("checkout") or {},
            }
        log("买入流水线已在运行，忽略重复启动请求", level="warn", category="pipeline")
        return {"ok": False, "already_running": True, "error": "买入流水线已在运行，请勿重复启动"}
    return {"ok": True}


@router.get("/api/pipeline/buff_checkout_guard")
def api_buff_checkout_guard():
    blocker = get_pipeline_start_blocker()
    return {
        "reconciliation_required": (
            blocker.get("code") == "BUFF_RECONCILIATION_REQUIRED"
        ),
        "checkout": blocker.get("checkout") or {},
    }
@router.post("/api/pipeline/stop")
def api_pipeline_stop():
    request_stop()
    log("接收到停止运行指令，正在终止任务...", level="warn", category="system")
    set_status("stopped", "正在停止并清理...")
    return {"ok": True}
