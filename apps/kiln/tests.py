"""探针合法性 / 出胶资格 / 图例对齐 / 种子 的回归测试。"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from .forms import SoftPointProbeForm
from .models import CookRun, FireHearth, ResinLot
from .services.floor_rules import (
    INVALID_SOFT_POINT_MESSAGE,
    can_enter_drawing,
    change_hearth_phase,
    latest_qualifying_probe,
    open_run_for,
    record_probe,
    run_probes,
    validate_soft_point,
)

User = get_user_model()


def _make_lot(code="脂-测试-001"):
    return ResinLot.objects.create(
        lotCode=code,
        originPlace="松脂坳",
        arrivalKg=Decimal("100.00"),
        receivedAt=timezone.now(),
    )


def _make_open_run(hearth, lot=None, target=Decimal("90.00")):
    return CookRun.objects.create(
        hearth=hearth,
        resinLot=lot or _make_lot(f"脂-测试-{hearth.pk}"),
        openedAt=timezone.now(),
        closedAt=None,
        targetSoftPointC=target,
    )


class SoftPointValidationTests(TestCase):
    def test_boundary_values(self):
        for value in ("0.01", "95", "120", "120.00"):
            self.assertEqual(validate_soft_point(value), Decimal(value))

    def test_rejects_non_positive_or_over_120(self):
        for value in ("0", "-5", "120.01", "200", None):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError) as ctx:
                    validate_soft_point(value)
                self.assertEqual(ctx.exception.messages, [INVALID_SOFT_POINT_MESSAGE])

    def test_form_and_service_share_one_message(self):
        hearth = FireHearth.objects.create(lane=1, tag="测试-甲", resinGrade="一级脂")
        run = _make_open_run(hearth)
        now = timezone.now()

        # 写路径一：抽屉表单
        for bad in ("0", "-3", "120.01", "150"):
            form = SoftPointProbeForm(
                data={
                    "sampledAt": now.strftime("%Y-%m-%dT%H:%M"),
                    "softPointC": bad,
                    "samplerName": "测试员",
                }
            )
            self.assertFalse(form.is_valid(), bad)
            self.assertEqual(
                form.errors["softPointC"], [INVALID_SOFT_POINT_MESSAGE], bad
            )

        # 写路径二：服务层 record_probe
        for bad in (Decimal("0"), Decimal("-3"), Decimal("120.01")):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError) as ctx:
                    record_probe(
                        run=run,
                        sampledAt=now,
                        softPointC=bad,
                        samplerName="测试员",
                    )
                self.assertEqual(
                    ctx.exception.messages, [INVALID_SOFT_POINT_MESSAGE]
                )
        self.assertEqual(run.probes.count(), 0)

    def test_boundary_probes_persist_on_both_paths(self):
        hearth = FireHearth.objects.create(lane=1, tag="测试-乙", resinGrade="一级脂")
        run = _make_open_run(hearth)
        now = timezone.now()

        record_probe(
            run=run, sampledAt=now, softPointC=Decimal("120.00"), samplerName="甲"
        )
        form = SoftPointProbeForm(
            data={
                "sampledAt": now.strftime("%Y-%m-%dT%H:%M"),
                "softPointC": "0.01",
                "samplerName": "乙",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        record_probe(
            run=run,
            sampledAt=form.cleaned_data["sampledAt"],
            softPointC=form.cleaned_data["softPointC"],
            samplerName=form.cleaned_data["samplerName"],
        )
        self.assertEqual(run.probes.count(), 2)


class EligibilityAndTimelineTests(TestCase):
    def setUp(self):
        self.hearth = FireHearth.objects.create(
            lane=1, tag="测试-灶", resinGrade="一级脂", phase=FireHearth.PHASE_HOLDING
        )
        self.run = _make_open_run(self.hearth)
        self.now = timezone.now()

    def _probe(self, hours_ago, value, who="测试员"):
        return record_probe(
            run=self.run,
            sampledAt=self.now - timezone.timedelta(hours=hours_ago),
            softPointC=Decimal(value),
            samplerName=who,
        )

    def test_write_immediately_reflected_in_eligibility(self):
        self.assertFalse(can_enter_drawing(self.hearth))
        with self.assertRaises(ValidationError):
            change_hearth_phase(self.hearth, FireHearth.PHASE_DRAWING)

        self._probe(0, "94.00")
        # 写入后立刻可进入出胶，无需任何额外刷新
        self.assertTrue(can_enter_drawing(self.hearth))
        change_hearth_phase(self.hearth, FireHearth.PHASE_DRAWING)
        self.hearth.refresh_from_db()
        self.assertEqual(self.hearth.phase, FireHearth.PHASE_DRAWING)

    def test_latest_qualifying_reads_same_order_as_timeline(self):
        p_old_hot = self._probe(3, "99.00")
        p_qual = self._probe(2, "90.00")
        p_new_hot = self._probe(1, "98.00")

        timeline = run_probes(self.run)
        # 时间线最近在前
        self.assertEqual(list(timeline), [p_new_hot, p_qual, p_old_hot])
        # 「最新合格探针」沿同一时间线取第一条合格者，不能另算一套
        self.assertEqual(latest_qualifying_probe(self.run), p_qual)
        self.assertTrue(can_enter_drawing(self.hearth))

    def test_95_boundary_qualifies(self):
        self._probe(1, "95.00")
        self.assertTrue(can_enter_drawing(self.hearth))

        run2 = _make_open_run(
            FireHearth.objects.create(lane=2, tag="测试-灶2", resinGrade="一级脂")
        )
        record_probe(
            run=run2,
            sampledAt=self.now,
            softPointC=Decimal("95.01"),
            samplerName="边界",
        )
        self.assertIsNone(latest_qualifying_probe(run2))

    def test_no_open_run_is_not_eligible(self):
        cold = FireHearth.objects.create(lane=9, tag="空灶", resinGrade="特级脂")
        self.assertIsNone(open_run_for(cold))
        self.assertFalse(can_enter_drawing(cold))
        with self.assertRaises(ValidationError):
            change_hearth_phase(cold, FireHearth.PHASE_DRAWING)


class ProbeViewPathTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("tester", password="123456")
        self.client.force_login(self.user)
        self.hearth = FireHearth.objects.create(
            lane=1, tag="视图-灶", resinGrade="一级脂", phase=FireHearth.PHASE_HOLDING
        )
        self.run = _make_open_run(self.hearth)

    def test_invalid_probe_post_rejected_with_same_message(self):
        resp = self.client.post(
            f"/hearth/{self.hearth.pk}/probe/",
            {
                "sampledAt": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
                "softPointC": "0",
                "samplerName": "视图测试",
            },
            follow=True,
        )
        messages = [str(m) for m in resp.context["messages"]]
        self.assertIn(INVALID_SOFT_POINT_MESSAGE, messages)
        self.assertEqual(self.run.probes.count(), 0)

    def test_valid_probe_post_then_drawing(self):
        self.client.post(
            f"/hearth/{self.hearth.pk}/probe/",
            {
                "sampledAt": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
                "softPointC": "92.50",
                "samplerName": "视图测试",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(self.run.probes.count(), 1)
        # 紧接着改相位即可出胶
        self.assertTrue(can_enter_drawing(self.hearth))
        resp = self.client.post(
            f"/hearth/{self.hearth.pk}/phase/",
            {"phase": FireHearth.PHASE_DRAWING},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        self.hearth.refresh_from_db()
        self.assertEqual(self.hearth.phase, FireHearth.PHASE_DRAWING)


class BoardLegendFilterTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("tester2", password="123456")
        self.client.force_login(self.user)
        lot = _make_lot()
        # 两灶出胶、一灶保温
        for tag, phase in (
            ("灶-出1", FireHearth.PHASE_DRAWING),
            ("灶-出2", FireHearth.PHASE_DRAWING),
            ("灶-保", FireHearth.PHASE_HOLDING),
        ):
            h = FireHearth.objects.create(lane=1, tag=tag, resinGrade="一级脂", phase=phase)
            run = _make_open_run(h, lot=lot)
            record_probe(
                run=run,
                sampledAt=timezone.now(),
                softPointC=Decimal("90.00"),
                samplerName="测试",
            )

    def test_legend_drawing_count_equals_filtered_tiles(self):
        resp = self.client.get("/?phase=drawing")
        ctx = resp.context
        legend = {key: count for key, _label, count in ctx["phase_legend"]}

        # 图例出胶计数只含相位已是出胶的灶
        self.assertEqual(legend["drawing"], 2)
        self.assertEqual(legend["holding"], 1)

        # 出胶过滤瓦片与计数按同一份 hearth 列表复算
        tiles = ctx["tiles"]
        self.assertEqual(len(tiles), legend["drawing"])
        self.assertTrue(all(h.phase == FireHearth.PHASE_DRAWING for h in tiles))

        # 部分模板同样能复算（图例 + 网格一起返回）
        partial = self.client.get("/floor/grid/?phase=drawing", HTTP_HX_REQUEST="true")
        self.assertEqual(partial.status_code, 200)
        self.assertContains(partial, "灶-出1")
        self.assertContains(partial, "灶-出2")
        self.assertNotContains(partial, "灶-保")

    def test_unknown_phase_param_is_ignored(self):
        resp = self.client.get("/?phase=bogus")
        self.assertIsNone(resp.context["active_phase"])
        self.assertEqual(len(resp.context["tiles"]), 3)


class SeedDataTests(TestCase):
    def test_exactly_one_open_run_lacks_qualifying_probe(self):
        call_command("seed_data")
        lacking = []
        for hearth in FireHearth.objects.all():
            run = open_run_for(hearth)
            if run is not None and latest_qualifying_probe(run) is None:
                lacking.append(hearth.tag)
        self.assertEqual(lacking, ["坳火-乙"])

        # 已出胶的灶必然具备合格探针（资格与相位不自相矛盾）
        for hearth in FireHearth.objects.filter(phase=FireHearth.PHASE_DRAWING):
            self.assertIsNotNone(latest_qualifying_probe(open_run_for(hearth)))

        # 种子探针全部落在合法区间
        from .models import SoftPointProbe

        for probe in SoftPointProbe.objects.all():
            self.assertGreater(probe.softPointC, Decimal("0"))
            self.assertLessEqual(probe.softPointC, Decimal("120"))

    def test_seed_is_idempotent(self):
        call_command("seed_data")
        call_command("seed_data")
        self.assertEqual(FireHearth.objects.count(), 5)
