from typing import Any

from supabase import Client, create_client

from app.config import settings


class SupabaseRepository:
    def __init__(self) -> None:
        self.client: Client | None = None
        if settings.supabase_url and settings.supabase_service_role_key:
            self.client = create_client(settings.supabase_url, settings.supabase_service_role_key)

    def insert(self, table: str, payload: dict[str, Any]) -> None:
        if not self.client:
            return
        self.client.table(table).insert(payload).execute()

    def insert_many(self, table: str, payload: list[dict[str, Any]]) -> None:
        if not self.client or not payload:
            return
        self.client.table(table).insert(payload).execute()

    def select(
        self,
        table: str,
        columns: str = "*",
        filters: dict[str, Any] | None = None,
        order_by: str | None = None,
        desc: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if not self.client:
            return []

        query = self.client.table(table).select(columns)
        if filters:
            for key, value in filters.items():
                query = query.eq(key, value)
        if order_by:
            query = query.order(order_by, desc=desc)
        if limit:
            query = query.limit(limit)
        response = query.execute()
        return response.data or []

    def update(self, table: str, values: dict[str, Any], filters: dict[str, Any]) -> None:
        if not self.client:
            return
        query = self.client.table(table).update(values)
        for key, value in filters.items():
            query = query.eq(key, value)
        query.execute()

    def delete(self, table: str, filters: dict[str, Any]) -> None:
        if not self.client:
            return
        query = self.client.table(table).delete()
        for key, value in filters.items():
            query = query.eq(key, value)
        query.execute()


repo = SupabaseRepository()
