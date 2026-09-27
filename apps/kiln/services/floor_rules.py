"""灶台相位切换与软化点探针的业务规则（唯一事实来源）。

探针合法性与出胶资格同源：
- 合法性：``validate_soft_point`` —— 软化点须为正数且 ≤ 120℃，
  抽屉表单（``SoftPointProbeForm``）与服务层保存（``register_probe``）
  两条写路径都走这一个校验，中文拒绝文案一致。
- 出胶资格：``qualified_probes`` —— 合法且 ≤ 95℃ 的探针才算合格；
  相位切换（``assert_can_enter_drawing``）与抽屉时间线着色
  （``is_drawing_qualified``）都从这里取数，不会分叉。
"""
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError

# 软化点合法区间：正数（不含 0）且不超过 120℃。
SOFT_POINT_MIN = Decimal("0")
SOFT_POINT_MAX = Decimal("120")

# 出胶资格阈值：进行中值守须至少一条合格探针 ≤ 95℃。
DRAWING_SOFT_POINT_MAX = Decimal("95")

# 两条写路径共用的中文拒绝文案。
SOFT_POINT_INVALID_MSG = "软化点须为正数且不超过 120℃。"


def validate_soft_point(value) -> None:
    """软化点合法性唯一校验：正数且 ≤ 120℃，非法即抛 ValidationError。"""
    try:
        v = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError(SOFT_POINT_INVALID_MSG)
    if not v.is_finite() or v <= SOFT_POINT_MIN or v > SOFT_POINT_MAX:
        raise ValidationError(SOFT_POINT_INVALID_MSG)


def is_drawing_qualified(probe) -> bool:
    """单条探针是否计入出胶资格（与 ``qualified_probes`` 同一判定）。"""
    value = probe.softPointC
    return value is not None and SOFT_POINT_MIN < value <= DRAWING_SOFT_POINT_MAX


def qualified_probes(run):
    """出胶合格探针集：合法且 ≤ 95℃，排序与抽屉时间线一致。"""
    return run.probes.filter(
        softPointC__gt=SOFT_POINT_MIN,
        softPointC__lte=DRAWING_SOFT_POINT_MAX,
    ).order_by("-sampledAt", "-id")


def latest_qualified_probe(run):
    """最新一条合格探针；与抽屉时间线里第一个 ok 项同源。"""
    return qualified_probes(run).first()


def assert_can_enter_drawing(hearth) -> None:
    """
    进入「出胶」相位前：当前未收灶的 CookRun 须至少有一条
    合格探针（合法且 softPointC ≤ 95℃）。
    """
    open_run = hearth.open_run()
    if open_run is None:
        raise ValidationError(
            {"phase": "无法进入出胶：该灶没有进行中的值守纪录。"}
        )

    ok = qualified_probes(open_run).exists()
    if not ok:
        raise ValidationError(
            {
                "phase": (
                    "无法进入出胶：进行中值守尚无软化点探针 "
                    f"≤ {DRAWING_SOFT_POINT_MAX}℃。"
                )
            }
        )


def change_hearth_phase(hearth, new_phase: str):
    """统一入口：改相位时校验出胶规则并保存。"""
    from apps.kiln.models import FireHearth

    if new_phase == FireHearth.PHASE_DRAWING:
        assert_can_enter_drawing(hearth)

    hearth.phase = new_phase
    hearth.save(update_fields=["phase"])
    return hearth


def register_probe(run, *, sampledAt, softPointC, samplerName):
    """服务层写路径：登记软化点探针。

    与抽屉表单共用 ``validate_soft_point``，非法值以同一中文文案拒绝；
    保存成功后出胶资格（``qualified_probes``）立刻可读到该探针。
    """
    from apps.kiln.models import SoftPointProbe

    if run.closedAt is not None:
        raise ValidationError("该值守已收灶，不能登记探针。")
    validate_soft_point(softPointC)
    probe = SoftPointProbe(
        run=run,
        sampledAt=sampledAt,
        softPointC=softPointC,
        samplerName=samplerName,
    )
    probe.save()
    return probe
