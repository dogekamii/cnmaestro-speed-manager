from __future__ import annotations

from typing import Any

from operations_toolkit.core.modules import ModuleContext

from .adapters import DemoCnMaestroAdapter


class BulkSpeedChangesProvider:
    module_id = "cnmaestro.bulk_speed_changes"
    title = "Bulk Speed Changes"
    description = "Preview, canary, publish, verify, reconcile, and audit cnMaestro speed changes."

    def create_adapter(self, context: ModuleContext) -> DemoCnMaestroAdapter:
        if not context.demo:
            raise RuntimeError("live adapter requires an explicit authenticated connection")
        return DemoCnMaestroAdapter()

    def create_view(self, parent: Any, context: ModuleContext) -> Any:
        from .ui import BulkSpeedChangesView

        return BulkSpeedChangesView(
            parent, context, self.create_adapter(context) if context.demo else None
        )
