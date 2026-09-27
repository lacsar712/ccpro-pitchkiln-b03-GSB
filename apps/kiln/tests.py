"""探针双路径同源校验与出胶资格的回归测试。"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import SoftPointProbeForm
from .models import CookRun, FireHearth, ResinLot, SoftPointProbe
from .seed import ensure_seed_data
from .services.floor_rules import (
    SOFT_POINT_INVALID_MSG,
    assert_can_enter_drawing,
    change_hearth_phase,
    latest_qualified_probe,
    qualified_probes,
    register_probe,
)

ILLEGAL_VALUES = ["0", "-1", "-0.01", "120.01", "121", "999"]
LEGAL_VALUES = ["0.01", "1", "95", "95.00", "120", "120.00"]


def _now_str():
    return timezone.localtime().strftime("%Y-%m-%dT%H:%M")


def _probe_data(soft_point, **extra):
    data = {
        "sampledAt": _now_str(),
        "softPointC": soft_point,
        "samplerName": "值守测试",
    }
    data.update(extra)
    return data


class HearthFixtureMixin:
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("tester", "t@example.com", "pw123456")
        self.hearth = FireHearth.objects.create(
            lane=9, tag="测试灶", resinGrade="特级脂",
            phase=FireHearth.PHASE_HOLDING,
        )
        self.lot = ResinLot.objects.create(
            lotCode="脂-测试-0001",
            originPlace="松脂坳",
            arrivalKg=Decimal("100.00"),
            receivedAt=timezone.now(),
        )
        self.run = CookRun.objects.create(
            hearth=self.hearth,
            resinLot=self.lot,
            openedAt=timezone.now() - timezone.timedelta(hours=2),
            targetSoftPointC=Decimal("90.00"),
        )


class SoftPointDualPathTests(HearthFixtureMixin, TestCase):
    """软化点合法性：正数且 ≤ 120℃，两条写路径同一中文文案。"""

    def test_form_rejects_illegal_values(self):
        for raw in ILLEGAL_VALUES:
            form = SoftPointProbeForm(data=_probe_data(raw))
            self.assertFalse(form.is_valid(), raw)
            self.assertEqual(
                form.errors["softPointC"][0], SOFT_POINT_INVALID_MSG, raw
            )

    def test_form_accepts_boundary_values(self):
        for raw in LEGAL_VALUES:
            form = SoftPointProbeForm(data=_probe_data(raw))
            self.assertTrue(form.is_valid(), f"{raw}: {form.errors}")

    def test_service_rejects_with_identical_message(self):
        for raw in ILLEGAL_VALUES:
            with self.assertRaises(ValidationError) as cm:
                register_probe(
                    self.run,
                    sampledAt=timezone.now(),
                    softPointC=Decimal(raw),
                    samplerName="值守测试",
                )
            self.assertEqual(cm.exception.messages, [SOFT_POINT_INVALID_MSG], raw)
        self.assertEqual(SoftPointProbe.objects.count(), 0)

    def test_service_accepts_boundary_values(self):
        for raw in LEGAL_VALUES:
            register_probe(
                self.run,
                sampledAt=timezone.now(),
                softPointC=Decimal(raw),
                samplerName="值守测试",
            )
        self.assertEqual(SoftPointProbe.objects.count(), len(LEGAL_VALUES))

    def test_form_and_service_messages_are_identical(self):
        form = SoftPointProbeForm(data=_probe_data("130"))
        self.assertFalse(form.is_valid())
        with self.assertRaises(ValidationError) as cm:
            register_probe(
                self.run,
                sampledAt=timezone.now(),
                softPointC=Decimal("130"),
                samplerName="值守测试",
            )
        self.assertEqual(form.errors["softPointC"][0], cm.exception.messages[0])

    def test_validate_rejects_non_finite_and_blank(self):
        from apps.kiln.services.floor_rules import validate_soft_point

        for bad in (None, "", "NaN", "Infinity", "-Infinity", "abc"):
            with self.assertRaises(ValidationError) as cm:
                validate_soft_point(bad)
            self.assertEqual(cm.exception.messages, [SOFT_POINT_INVALID_MSG], bad)

    def test_model_full_clean_uses_same_rule(self):
        probe = SoftPointProbe(
            run=self.run,
            sampledAt=timezone.now(),
            softPointC=Decimal("130"),
            samplerName="值守测试",
        )
        with self.assertRaises(ValidationError) as cm:
            probe.full_clean()
        self.assertEqual(
            cm.exception.message_dict["softPointC"], [SOFT_POINT_INVALID_MSG]
        )

    def test_view_post_rejects_illegal_with_same_message(self):
        self.client.force_login(self.user)
        url = reverse("add_probe", args=[self.hearth.pk])
        resp = self.client.post(url, _probe_data("121"), follow=True)
        self.assertContains(resp, SOFT_POINT_INVALID_MSG)
        self.assertEqual(SoftPointProbe.objects.count(), 0)

    def test_view_post_accepts_and_saves(self):
        self.client.force_login(self.user)
        url = reverse("add_probe", args=[self.hearth.pk])
        resp = self.client.post(url, _probe_data("95"), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(SoftPointProbe.objects.count(), 1)
        self.assertEqual(SoftPointProbe.objects.get().softPointC, Decimal("95"))


class DrawingEligibilityTests(HearthFixtureMixin, TestCase):
    """出胶资格与探针合法性同源，写入后立刻反映，与时间线不分叉。"""

    def test_no_open_run_rejected(self):
        self.run.closedAt = timezone.now()
        self.run.save(update_fields=["closedAt"])
        with self.assertRaises(ValidationError):
            assert_can_enter_drawing(self.hearth)

    def test_no_qualified_probe_rejected(self):
        register_probe(
            self.run, sampledAt=timezone.now(),
            softPointC=Decimal("96"), samplerName="甲",
        )
        with self.assertRaises(ValidationError) as cm:
            assert_can_enter_drawing(self.hearth)
        self.assertIn("95", str(cm.exception.message_dict["phase"][0]))

    def test_illegal_probe_never_grants_eligibility(self):
        # 绕过写路径直接落库的非法探针（如历史脏数据）不得算作合格。
        SoftPointProbe.objects.create(
            run=self.run, sampledAt=timezone.now(),
            softPointC=Decimal("-5"), samplerName="脏数据",
        )
        probe = self.run.probes.get()
        self.assertFalse(probe.drawing_qualified)
        self.assertFalse(qualified_probes(self.run).exists())
        with self.assertRaises(ValidationError):
            assert_can_enter_drawing(self.hearth)

    def test_write_reflects_immediately(self):
        with self.assertRaises(ValidationError):
            assert_can_enter_drawing(self.hearth)
        probe = register_probe(
            self.run, sampledAt=timezone.now(),
            softPointC=Decimal("95"), samplerName="甲",
        )
        # 写入后无需任何额外动作即具备出胶资格。
        assert_can_enter_drawing(self.hearth)
        self.assertEqual(latest_qualified_probe(self.run).pk, probe.pk)

    def test_latest_qualified_matches_drawer_timeline(self):
        now = timezone.now()
        register_probe(
            self.run, sampledAt=now - timezone.timedelta(hours=3),
            softPointC=Decimal("94"), samplerName="甲",
        )
        register_probe(
            self.run, sampledAt=now - timezone.timedelta(hours=1),
            softPointC=Decimal("100"), samplerName="乙",
        )
        mid = register_probe(
            self.run, sampledAt=now - timezone.timedelta(hours=2),
            softPointC=Decimal("93"), samplerName="丙",
        )
        # 抽屉时间线（与 _drawer_context 同序）里第一个 ok 项即最新合格探针。
        timeline = list(self.run.probes.order_by("-sampledAt", "-id"))
        first_ok = next(p for p in timeline if p.drawing_qualified)
        self.assertEqual(first_ok.pk, mid.pk)
        self.assertEqual(latest_qualified_probe(self.run).pk, first_ok.pk)
        self.assertEqual(
            {p.pk for p in timeline if p.drawing_qualified},
            {p.pk for p in qualified_probes(self.run)},
        )

    def test_change_phase_view_enforces_and_then_reflects(self):
        self.client.force_login(self.user)
        url = reverse("change_phase", args=[self.hearth.pk])
        resp = self.client.post(url, {"phase": "drawing"}, follow=True)
        self.assertContains(resp, "无法进入出胶")
        self.hearth.refresh_from_db()
        self.assertNotEqual(self.hearth.phase, FireHearth.PHASE_DRAWING)

        register_probe(
            self.run, sampledAt=timezone.now(),
            softPointC=Decimal("93"), samplerName="甲",
        )
        resp = self.client.post(url, {"phase": "drawing"}, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.hearth.refresh_from_db()
        self.assertEqual(self.hearth.phase, FireHearth.PHASE_DRAWING)


class BoardLegendTests(TestCase):
    """图例出胶计数只含相位已是出胶的灶，并与瓦片复算对齐。"""

    def setUp(self):
        ensure_seed_data()
        self.user = get_user_model().objects.get(username="worker")

    def _legend_and_tiles(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse("home"))
        self.assertEqual(resp.status_code, 200)
        legend = {
            key: count for key, _label, count in resp.context["phase_legend"]
        }
        content = resp.content.decode()
        tiles = {
            key: content.count(f"hearth-tile phase-{key}")
            for key, _label in FireHearth.PHASE_CHOICES
        }
        return legend, tiles

    def test_legend_counts_match_phase_and_tiles(self):
        legend, tiles = self._legend_and_tiles()
        for key, _label in FireHearth.PHASE_CHOICES:
            db_count = FireHearth.objects.filter(phase=key).count()
            self.assertEqual(legend[key], db_count, key)
            self.assertEqual(legend[key], tiles[key], key)

    def test_legend_reflects_drawing_change_immediately(self):
        hearth = FireHearth.objects.get(tag="坳火-甲")
        self.assertFalse(qualified_probes(hearth.open_run()).exists())
        before, _ = self._legend_and_tiles()

        register_probe(
            hearth.open_run(), sampledAt=timezone.now(),
            softPointC=Decimal("92"), samplerName="值守测试",
        )
        change_hearth_phase(hearth, FireHearth.PHASE_DRAWING)

        legend, tiles = self._legend_and_tiles()
        self.assertEqual(legend["drawing"], before["drawing"] + 1)
        self.assertEqual(legend["drawing"], tiles["drawing"])
        self.assertEqual(
            legend["drawing"],
            FireHearth.objects.filter(phase=FireHearth.PHASE_DRAWING).count(),
        )


class SeedDataTests(TestCase):
    """种子数据：含一灶缺合格探针，且所有探针读数合法。"""

    def test_seed_has_hearth_without_qualified_probe(self):
        ensure_seed_data()
        lacking = [
            h.tag
            for h in FireHearth.objects.all()
            if h.open_run() is not None
            and not qualified_probes(h.open_run()).exists()
        ]
        self.assertIn("坳火-甲", lacking)
        self.assertGreaterEqual(len(lacking), 1)

    def test_seed_probes_are_all_legal(self):
        ensure_seed_data()
        for probe in SoftPointProbe.objects.all():
            self.assertGreater(probe.softPointC, Decimal("0"))
            self.assertLessEqual(probe.softPointC, Decimal("120"))

    def test_seed_drawing_hearth_has_qualified_probe(self):
        ensure_seed_data()
        hearth = FireHearth.objects.get(tag="坑火-西一")
        self.assertEqual(hearth.phase, FireHearth.PHASE_DRAWING)
        self.assertTrue(qualified_probes(hearth.open_run()).exists())
