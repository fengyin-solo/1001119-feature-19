"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

授权柜（grant）相关的并发口径集中在这里：
- self._lock 是一把可重入锁，权限缓存的失效/重建与库存预占、额度扣减在同一把锁里完成，
  保证“权限缓存与库存预占原子更新”，并发领料不会超授权额度。
"""
from __future__ import annotations

import threading
from typing import Any

from app.grant_bootstrap import bootstrap
from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 在拷贝上挂授权柜的额外表与材料台账，不污染原始 SEED_ROWS。
        bootstrap(tables)
        self._tables = tables
        # 授权柜写操作的临界区：权限缓存、额度、库存预占都在这把锁内更新。
        self.lock = threading.RLock()

    @property
    def transaction(self) -> threading.RLock:
        """授权柜写事务使用的可重入锁（with store.transaction: ...）。"""
        return self.lock

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if str(row.get(field, "")) == str(value):
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
