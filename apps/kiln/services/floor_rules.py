"""灶台相位切换与探针业务规则（校验唯一数据源）。

探针合法性（0 < softPointC <= 120）与出胶资格（最新探针读数须 ≤ 95）
都从本模块取数与判定；抽屉表单、服务保存、模板时间线共用同一套
排序与判定，杜绝两处读数分叉。
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Prefetch

from apps.kiln.models import CookRun, FireHearth, SoftPointProbe

# 探针软化点合法区间（正数，上限 120℃）
SOFT_POINT_MIN_EXCLUSIVE = Decimal("0")
SOFT_POINT_MAX = Decimal("120")

# 出胶资格：合格探针读数上限
DRAWING_SOFT_POINT_MAX = Decimal("95")

# 非法软化点的唯一中文文案：表单与服务层两条写路径都复用它
INVALID_SOFT_POINT_MESSAGE = "软化点必须为大于 0 且不超过 120℃ 的正数。"

# 探针时间线统一排序（最近在前），时间线与「最新合格探针」共用
PROBE_ORDERING = ("-sampledAt", "-id")


def validate_soft_point(value) -> Decimal:
    """探针软化点唯一校验：正数且上限 120℃。

    表单 clean 与服务保存两条写路径都调这里，保证拒绝文案完全一致。
    """
    if value is None:
        raise ValidationError(INVALID_SOFT_POINT_MESSAGE)
    try:
        numeric = Decimal(str(value))
    except Exception:
        raise ValidationError(INVALID_SOFT_POINT_MESSAGE)
    if (
        not numeric.is_finite()
        or numeric <= SOFT_POINT_MIN_EXCLUSIVE
        or numeric > SOFT_POINT_MAX
    ):
        raise ValidationError(INVALID_SOFT_POINT_MESSAGE)
    return numeric


def open_run_for(hearth):
    """该灶当前进行中的值守（无则 None），直接复用模型方法，避免两处查询分叉。"""
    return hearth.open_run()


def run_probes(run):
    """某值守的探针时间线，统一按取样时间倒序（最近在前）。"""
    return list(run.probes.order_by(*PROBE_ORDERING))


def qualifying_probe_in(probes):
    """在一条「已按 PROBE_ORDERING 排好序」的时间线里找第一条合格探针。

    这是出胶资格的唯一谓词：无论探针列表来自实时查询还是看板预取缓存，
    都走这里，保证读数与时间线不分叉。
    """
    for probe in probes:
        if probe.softPointC is not None and probe.softPointC <= DRAWING_SOFT_POINT_MAX:
            return probe
    return None


def latest_qualifying_probe(run):
    """时间线上第一条满足出胶读数（softPointC ≤ 95）的探针。

    与 run_probes() 同一排序，保证「改相位读到的最新合格探针」
    与抽屉时间线不分叉。
    """
    if run is None:
        return None
    return qualifying_probe_in(run_probes(run))


def latest_valid_probe(run):
    """时间线上最新一条探针（无论是否达到出胶读数），供展示「最新读数」。"""
    probes = run_probes(run)
    return probes[0] if probes else None


def can_enter_drawing(hearth) -> bool:
    """出胶资格：存在进行中值守，且时间线上有读数 ≤ 95 的合格探针。"""
    return latest_qualifying_probe(open_run_for(hearth)) is not None


def assert_can_enter_drawing(hearth) -> None:
    """进入「出胶」相位前的资格断言，判定与 can_enter_drawing 同源。

    抛单消息 ValidationError（而非 {"phase": ...} 字典形式）：它同时被
    表单 clean_phase 与 change_hearth_phase 复用，字典形式在字段清洗器里
    会被 Django 当成多字段错误而抛 TypeError。
    """
    open_run = open_run_for(hearth)
    if open_run is None:
        raise ValidationError("无法进入出胶：该灶没有进行中的值守纪录。")

    if latest_qualifying_probe(open_run) is None:
        latest = latest_valid_probe(open_run)
        latest_hint = f"最新探针 {latest.softPointC}℃，" if latest else "尚无探针，"
        raise ValidationError(
            f"无法进入出胶：{latest_hint}"
            f"须先有软化点 ≤ {DRAWING_SOFT_POINT_MAX}℃ 的合格探针。"
        )


def record_probe(*, run, sampledAt, softPointC, samplerName):
    """探针写入的服务层统一入口：先过同一合法性校验，再落库。

    非表单写路径（管理脚本、批量导入等）走这里，与抽屉表单
    同一校验、同一中文拒绝文案。
    """
    valid_value = validate_soft_point(softPointC)
    return SoftPointProbe.objects.create(
        run=run,
        sampledAt=sampledAt,
        softPointC=valid_value,
        samplerName=samplerName,
    )


def change_hearth_phase(hearth, new_phase: str):
    """统一入口：改相位时校验出胶规则并保存。"""
    if new_phase == FireHearth.PHASE_DRAWING:
        assert_can_enter_drawing(hearth)

    hearth.phase = new_phase
    hearth.save(update_fields=["phase"])
    return hearth


def open_runs_with_probes_queryset():
    """看板预取进行中值守及其探针，时间线排序与 PROBE_ORDERING 一致。"""
    return (
        CookRun.objects.filter(closedAt__isnull=True)
        .select_related("resinLot")
        .prefetch_related(
            Prefetch(
                "probes",
                queryset=SoftPointProbe.objects.order_by(*PROBE_ORDERING),
            )
        )
    )
