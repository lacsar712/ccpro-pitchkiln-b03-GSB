from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Prefetch
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from .forms import OpenCookRunForm, PhaseChangeForm, ResinLotForm, SoftPointProbeForm
from .models import FireHearth, ResinLot
from .services.floor_rules import (
    DRAWING_SOFT_POINT_MAX,
    change_hearth_phase,
    open_run_for,
    open_runs_with_probes_queryset,
    qualifying_probe_in,
    record_probe,
    run_probes,
)


def _wants_htmx(request):
    return request.headers.get("HX-Request") == "true"


def _hearths_for_board():
    return FireHearth.objects.prefetch_related(
        Prefetch(
            "runs",
            queryset=open_runs_with_probes_queryset(),
            to_attr="open_runs_cache",
        )
    ).order_by("lane", "tag")


def _board_context(active_phase=None):
    """看板上下文。

    图例计数与瓦片来自同一份 hearths；传 active_phase 时只保留该相位，
    保证「图例出胶计数」与「出胶过滤瓦片」按同一口径复算对齐。
    """
    hearths = list(_hearths_for_board())
    for h in hearths:
        # 预取探针已按 PROBE_ORDERING 排序；直接用同一谓词复算，零额外查询
        if h.open_runs_cache:
            run = h.open_runs_cache[0]
            h.board_latest_qualifying = qualifying_probe_in(run.probes.all())
        else:
            h.board_latest_qualifying = None

    legend_totals = {
        key: sum(1 for h in hearths if h.phase == key)
        for key, _label in FireHearth.PHASE_CHOICES
    }
    if active_phase:
        tiles = [h for h in hearths if h.phase == active_phase]
    else:
        tiles = hearths
    lanes = {}
    for h in tiles:
        lanes.setdefault(h.lane, []).append(h)
    phase_legend = [
        (key, label, legend_totals.get(key, 0))
        for key, label in FireHearth.PHASE_CHOICES
    ]
    return {
        "hearths": hearths,
        "tiles": tiles,
        "lanes": sorted(lanes.items()),
        "phase_legend": phase_legend,
        "active_phase": active_phase,
        "drawing_max": DRAWING_SOFT_POINT_MAX,
    }


def _drawer_context(hearth):
    open_run = open_run_for(hearth)
    probes = run_probes(open_run) if open_run else []
    # 合格探针直接沿刚取出的同一条时间线复算，读数不分叉、不重复查询
    latest_qualifying = qualifying_probe_in(probes)
    return {
        "hearth": hearth,
        "open_run": open_run,
        "probes": probes,
        "latest_qualifying": latest_qualifying,
        "drawing_max": DRAWING_SOFT_POINT_MAX,
        "phase_form": PhaseChangeForm(hearth=hearth),
        "probe_form": SoftPointProbeForm() if open_run else None,
        "open_run_form": OpenCookRunForm(hearth=hearth) if open_run is None else None,
    }


PHASE_KEYS = {key for key, _label in FireHearth.PHASE_CHOICES}


def _active_phase(request):
    phase = request.GET.get("phase")
    return phase if phase in PHASE_KEYS else None


@login_required
def home(request):
    active_phase = _active_phase(request)
    ctx = _board_context(active_phase)
    drawer_pk = request.GET.get("hearth")
    if drawer_pk:
        try:
            hearth = FireHearth.objects.get(pk=drawer_pk)
            ctx.update(_drawer_context(hearth))
            ctx["drawer_open"] = True
        except (FireHearth.DoesNotExist, ValueError):
            ctx["drawer_open"] = False
    else:
        ctx["drawer_open"] = False
    return render(request, "floor/board.html", ctx)


@login_required
def floor_grid_partial(request):
    active_phase = _active_phase(request)
    html = render_to_string(
        "floor/_board_section.html", _board_context(active_phase), request=request
    )
    return HttpResponse(html)


@login_required
def hearth_drawer(request, pk):
    hearth = get_object_or_404(FireHearth, pk=pk)
    ctx = _drawer_context(hearth)
    if _wants_htmx(request):
        return render(request, "floor/_drawer.html", ctx)
    return redirect(f"/?hearth={pk}")


@login_required
@require_POST
def change_phase(request, pk):
    hearth = get_object_or_404(FireHearth, pk=pk)
    form = PhaseChangeForm(request.POST, hearth=hearth)
    if form.is_valid():
        try:
            change_hearth_phase(hearth, form.cleaned_data["phase"])
            messages.success(request, f"灶牌 {hearth.tag} 相位已更新")
        except ValidationError as exc:
            msg = (
                exc.message_dict.get("phase") if hasattr(exc, "message_dict") else None
            )
            messages.error(request, msg[0] if msg else str(exc))
    else:
        err = form.errors.get("phase")
        messages.error(request, err[0] if err else "相位切换失败")

    if _wants_htmx(request):
        hearth.refresh_from_db()
        resp = render(request, "floor/_drawer.html", _drawer_context(hearth))
        resp["HX-Trigger"] = "floor-refresh"
        return resp
    return redirect(f"/?hearth={pk}")


@login_required
@require_POST
def add_probe(request, pk):
    hearth = get_object_or_404(FireHearth, pk=pk)
    open_run = open_run_for(hearth)
    if open_run is None:
        messages.error(request, "没有进行中的值守，无法登记探针")
        if _wants_htmx(request):
            resp = render(request, "floor/_drawer.html", _drawer_context(hearth))
            resp["HX-Trigger"] = "floor-refresh"
            return resp
        return redirect(f"/?hearth={pk}")

    form = SoftPointProbeForm(request.POST)
    if form.is_valid():
        try:
            # 即使表单已 clean，保存仍走服务层 record_probe——两条写路径
            # 强制经过同一个 validate_soft_point，拒绝文案只有一个来源。
            probe = record_probe(
                run=open_run,
                sampledAt=form.cleaned_data["sampledAt"],
                softPointC=form.cleaned_data["softPointC"],
                samplerName=form.cleaned_data["samplerName"],
            )
            messages.success(request, f"已登记探针 {probe.softPointC}℃")
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
    else:
        # 软点字段错误文案即服务层 INVALID_SOFT_POINT_MESSAGE，原样透传
        soft_point_errors = form.errors.get("softPointC")
        messages.error(
            request,
            soft_point_errors[0] if soft_point_errors else "探针登记失败，请检查输入",
        )

    if _wants_htmx(request):
        resp = render(request, "floor/_drawer.html", _drawer_context(hearth))
        resp["HX-Trigger"] = "floor-refresh"
        return resp
    return redirect(f"/?hearth={pk}")


@login_required
@require_POST
def open_run(request, pk):
    hearth = get_object_or_404(FireHearth, pk=pk)
    form = OpenCookRunForm(request.POST, hearth=hearth)
    if form.is_valid():
        run = form.save(commit=False)
        run.hearth = hearth
        run.save()
        if hearth.phase == FireHearth.PHASE_COLD:
            hearth.phase = FireHearth.PHASE_CHARGING
            hearth.save(update_fields=["phase"])
        messages.success(request, "新值守已开灶")
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
            break

    if _wants_htmx(request):
        hearth.refresh_from_db()
        resp = render(request, "floor/_drawer.html", _drawer_context(hearth))
        resp["HX-Trigger"] = "floor-refresh"
        return resp
    return redirect(f"/?hearth={pk}")


@login_required
@require_POST
def close_run(request, pk):
    hearth = get_object_or_404(FireHearth, pk=pk)
    open_run = hearth.open_run()
    if open_run is None:
        messages.error(request, "没有进行中的值守可收灶")
    else:
        open_run.closedAt = timezone.now()
        open_run.save(update_fields=["closedAt"])
        hearth.phase = FireHearth.PHASE_COLD
        hearth.save(update_fields=["phase"])
        messages.success(request, "值守已收灶，灶台回冷灶")

    if _wants_htmx(request):
        hearth.refresh_from_db()
        resp = render(request, "floor/_drawer.html", _drawer_context(hearth))
        resp["HX-Trigger"] = "floor-refresh"
        return resp
    return redirect(f"/?hearth={pk}")


@login_required
@require_http_methods(["GET", "POST"])
def resin_lot_feed(request):
    if request.method == "POST":
        form = ResinLotForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "来脂批已登记")
            return redirect("resin_lot_feed")
    else:
        form = ResinLotForm(
            initial={
                "receivedAt": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
            }
        )

    lots = ResinLot.objects.all()[:40]
    return render(request, "resin/feed.html", {"lots": lots, "form": form})
