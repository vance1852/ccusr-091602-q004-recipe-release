"""平台装配：把目录、密钥、网关、工作流、发布与授权组装在一起。"""

from __future__ import annotations

from typing import Callable

from .authorization import AUTH_KID, Authorizer
from .catalog import default_registry
from .content import ContentStore
from .crypto import KeyRing
from .devices import PLATFORM_KEY_ID, DeviceGateway, device_key_id
from .identity import default_directory
from .recipe import RecipeWorkflow, user_key_id
from .release import ReleaseManager


class Platform:
    def __init__(self, clock: Callable[[], float] | None = None,
                 register_devices: bool = True):
        self.clock = clock
        self.registry = default_registry()
        self.store = ContentStore()
        self.directory = default_directory()
        self.key_ring = KeyRing()
        self._bootstrap_keys()
        self.gateway = DeviceGateway(self.key_ring,
                                     clock or _default_clock())
        if register_devices:
            for device_id in self.registry.devices:
                self.gateway.register_device(device_id)
        self.recipes = RecipeWorkflow(
            self.registry.catalog, self.store, self.directory,
            self.key_ring, clock or _default_clock())
        self.releases = ReleaseManager(
            self.registry, self.store, self.key_ring, self.recipes,
            self.gateway, clock or _default_clock())
        self.authorizer = Authorizer(
            self.releases, self.key_ring, clock or _default_clock())

    def _bootstrap_keys(self) -> None:
        self.key_ring.register(PLATFORM_KEY_ID, b"platform-master-secret")
        self.key_ring.register(AUTH_KID, b"auth-service-secret")
        for user in self.directory.all():
            self.key_ring.register(user_key_id(user.user_id),
                                   f"secret-{user.user_id}".encode())
        for device_id in self.registry.devices:
            self.key_ring.register(device_key_id(device_id),
                                   f"dev-secret-{device_id}".encode())


def _default_clock():
    import time
    return time.time
