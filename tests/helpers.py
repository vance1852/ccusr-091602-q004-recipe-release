"""测试共享的环境构造器。"""

from datetime import datetime, timedelta, timezone

from app.catalog import default_catalog
from app.devices import Device, SimulatedDeviceGateway
from app.recipe import ParameterValue, RecipeDraft
from app.release import ReleaseManager
from app.review import ROLE_PROCESS_REVIEWER, ROLE_QUALITY_LEAD, ReviewRecord
from app.signing import KeyStore

NOW = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)
VALID_FROM = NOW
VALID_UNTIL = NOW + timedelta(days=30)

AUTHOR = "eng.li"
TECH_REVIEWER = "rev.wang"
QUALITY_LEAD = "qa.zhao"
RELEASE_BOT = "release.bot"

FULL_RANGES = {
    "mix.temperature": (0.0, 300.0),
    "mix.duration": (0.0, 480.0),
    "mix.pressure": (0.0, 16.0),
    "dose.mass": (0.0, 600.0),
    "stir.speed": (0.0, 2000.0),
    "cool.temperature": (-10.0, 100.0),
    "cool.duration": (0.0, 600.0),
    "fill.ratio": (0.0, 100.0),
}


def build_world():
    """构造目录、密钥库、4 台设备（line-a: MX-1/2, line-b: MX-3/4）与发布管理器。"""
    catalog = default_catalog()
    keystore = KeyStore()
    for signer in (AUTHOR, TECH_REVIEWER, QUALITY_LEAD, RELEASE_BOT):
        keystore.register(signer, f"key-of-{signer}".encode())
    devices = {
        f"MX-{i}": Device(
            device_id=f"MX-{i}",
            group="line-a" if i <= 2 else "line-b",
            products=frozenset({"RESIN-01"}),
            param_ranges=dict(FULL_RANGES),
            key=f"device-key-{i}".encode(),
        )
        for i in range(1, 5)
    }
    for device in devices.values():
        keystore.register(f"device:{device.device_id}", device.key)
    gateway = SimulatedDeviceGateway(devices, keystore)
    manager = ReleaseManager(catalog, devices, keystore, gateway)
    return catalog, keystore, devices, gateway, manager


def make_draft(version=1, mix_temp=180.0, cool_temp=40.0, **overrides):
    params = {
        "mix.temperature": ParameterValue(mix_temp, "Cel"),
        "mix.duration": ParameterValue(45.0, "min"),
        "mix.pressure": ParameterValue(6.0, "bar"),
        "dose.mass": ParameterValue(120.0, "kg"),
        "stir.speed": ParameterValue(150.0, "rpm"),
        "cool.temperature": ParameterValue(cool_temp, "Cel"),
        "cool.duration": ParameterValue(60.0, "min"),
        "fill.ratio": ParameterValue(80.0, "%"),
    }
    params.update(overrides)
    return RecipeDraft(
        recipe_id="RCP-RESIN-01",
        product_id="RESIN-01",
        version=version,
        author=AUTHOR,
        params=params,
        created_at=NOW,
    )


def approve_record(catalog, keystore, draft):
    """走完整审核链：提交 → 技术审核 → 质量会签。"""
    review = ReviewRecord(draft, catalog)
    review.submit(AUTHOR, NOW)
    review.approve(keystore, TECH_REVIEWER, ROLE_PROCESS_REVIEWER, NOW)
    review.approve(keystore, QUALITY_LEAD, ROLE_QUALITY_LEAD, NOW)
    return review


def freeze_and_deploy(manager, catalog, keystore, draft, groups=("line-a", "line-b")):
    """审核 + 冻结 + 全部批次下发，返回 (review, release)。"""
    review = approve_record(catalog, keystore, draft)
    release = manager.freeze(review, list(groups), RELEASE_BOT, VALID_FROM, VALID_UNTIL, NOW)
    manager.start(release.release_id)
    while release.state.value == "deploying":
        manager.deploy_next_batch(release.release_id, NOW)
    return review, release
