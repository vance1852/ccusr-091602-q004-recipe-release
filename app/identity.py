"""人员身份与角色目录。"""

from __future__ import annotations

from dataclasses import dataclass

ROLE_ENGINEER = "engineer"    # 工艺工程师：编辑/提交
ROLE_REVIEWER = "reviewer"    # 审核人：技术复核
ROLE_QUALITY = "quality"      # 质量负责人：高风险会签
ROLE_ADMIN = "admin"          # 平台管理员：发布/灰度操作
ROLES = (ROLE_ENGINEER, ROLE_REVIEWER, ROLE_QUALITY, ROLE_ADMIN)


@dataclass(frozen=True)
class User:
    user_id: str
    name: str
    roles: tuple[str, ...]

    def has(self, role: str) -> bool:
        return role in self.roles


class Directory:
    def __init__(self, users: dict[str, User] | None = None):
        self._users = dict(users or {})

    def add(self, user: User) -> None:
        self._users[user.user_id] = user

    def get(self, user_id: str) -> User:
        if user_id not in self._users:
            raise KeyError(f"未知用户: {user_id}")
        return self._users[user_id]

    def all(self) -> list[User]:
        return list(self._users.values())


def default_directory() -> Directory:
    return Directory({
        "u_gongyi": User("u_gongyi", "李工（工艺工程师）",
                         (ROLE_ENGINEER,)),
        "u_zhang": User("u_zhang", "张工（审核人）",
                        (ROLE_REVIEWER, ROLE_ENGINEER)),
        "u_wang": User("u_wang", "王工（资深审核人）", (ROLE_REVIEWER,)),
        "u_quality": User("u_quality", "赵质（质量负责人）",
                          (ROLE_QUALITY,)),
        "u_admin": User("u_admin", "平台管理员", (ROLE_ADMIN,)),
    })
