#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = ROOT / "infra" / "supabase" / "migrations" / "20260218_smartbrain_init.sql"
WORKFLOWS_DIR = ROOT / "workflows" / "n8n"
WORKFLOW_FILES = [
    "01_smartbrain_data_ingestion.json",
    "02_smartbrain_decision_cycle.json",
    "03_smartbrain_daily_retraining.json",
    "04_smartbrain_hourly_tree_update.json",
    "05_smartbrain_rl_replay_30m.json",
]


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        print(f"Missing required env var: {name}", file=sys.stderr)
        sys.exit(1)
    return value


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def run_cmd(cmd: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def run_sql_psycopg(db_url: str, sql: str) -> bool:
    try:
        import psycopg  # type: ignore
    except Exception:
        return False

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    return True


def run_sql_cli(db_url: str, sql: str) -> bool:
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8") as tmp:
        tmp.write(sql)
        tmp_path = tmp.name

    try:
        if shutil.which("supabase"):
            cmd = ["supabase", "db", "query", "--db-url", db_url, "--file", tmp_path]
            code, out, err = run_cmd(cmd)
            if code == 0:
                if out:
                    print(out)
                return True
            print(f"[WARN] supabase CLI SQL failed: {err or out}")

        if shutil.which("psql"):
            cmd = ["psql", db_url, "-v", "ON_ERROR_STOP=1", "-f", tmp_path]
            code, out, err = run_cmd(cmd)
            if code == 0:
                if out:
                    print(out)
                return True
            print(f"[WARN] psql SQL failed: {err or out}")

        return False
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def run_sql(db_url: str, sql: str, label: str) -> None:
    if run_sql_psycopg(db_url, sql):
        print(f"[OK] {label} via psycopg")
        return

    if run_sql_cli(db_url, sql):
        print(f"[OK] {label} via CLI")
        return

    print(
        f"[ERROR] Could not execute SQL for '{label}'. Install psycopg or ensure supabase/psql CLI is available.",
        file=sys.stderr,
    )
    sys.exit(1)


def apply_migration(db_url: str) -> None:
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    run_sql(db_url, sql, "Supabase migration")


def escape_sql(value: str) -> str:
    return value.replace("'", "''")


def build_settings_sql() -> str:
    enabled = env_bool("SMARTBRAIN_INIT_ENABLED", True)
    paper_mode = env_bool("SMARTBRAIN_INIT_PAPER_MODE", True)
    risk_level = os.getenv("SMARTBRAIN_INIT_RISK_LEVEL", "medium").strip().lower() or "medium"
    max_leverage = int(os.getenv("SMARTBRAIN_INIT_MAX_LEVERAGE", "3"))
    max_portfolio_risk = float(os.getenv("SMARTBRAIN_INIT_MAX_PORTFOLIO_RISK", "0.02"))
    max_drawdown_circuit = float(os.getenv("SMARTBRAIN_INIT_MAX_DRAWDOWN_CIRCUIT", "0.08"))
    emergency_stop = env_bool("SMARTBRAIN_INIT_EMERGENCY_STOP", False)

    assets_raw = os.getenv("SMARTBRAIN_INIT_ASSETS", "BTC,ETH")
    assets = [a.strip().upper() for a in assets_raw.split(",") if a.strip()]
    if not assets:
        assets = ["BTC", "ETH"]

    assets_literal = "{" + ",".join(assets) + "}"

    return f"""
with latest as (
  select id
  from smartbrain_settings
  order by updated_at desc nulls last
  limit 1
),
updated as (
  update smartbrain_settings
  set
    enabled = {str(enabled).lower()},
    paper_mode = {str(paper_mode).lower()},
    risk_level = '{escape_sql(risk_level)}',
    max_leverage = {max_leverage},
    max_portfolio_risk = {max_portfolio_risk},
    max_drawdown_circuit = {max_drawdown_circuit},
    assets_whitelist = '{escape_sql(assets_literal)}'::text[],
    emergency_stop = {str(emergency_stop).lower()},
    updated_at = now()
  where id in (select id from latest)
  returning id
)
insert into smartbrain_settings (
  enabled,
  paper_mode,
  risk_level,
  max_leverage,
  max_portfolio_risk,
  max_drawdown_circuit,
  assets_whitelist,
  emergency_stop,
  updated_at
)
select
  {str(enabled).lower()},
  {str(paper_mode).lower()},
  '{escape_sql(risk_level)}',
  {max_leverage},
  {max_portfolio_risk},
  {max_drawdown_circuit},
  '{escape_sql(assets_literal)}'::text[],
  {str(emergency_stop).lower()},
  now()
where not exists (select 1 from updated);
"""


def init_settings(db_url: str) -> None:
    if not env_bool("SMARTBRAIN_INIT_SETTINGS", True):
        print("[SKIP] SMARTBRAIN_INIT_SETTINGS=false")
        return

    sql = build_settings_sql()
    run_sql(db_url, sql, "SmartBrain settings initialization")


def n8n_request(
    base_url: str,
    api_key: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
) -> Any:
    url = f"{base_url}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"

    payload = None
    headers = {
        "X-N8N-API-KEY": api_key,
        "Content-Type": "application/json",
    }
    if body is not None:
        payload = json.dumps(body).encode("utf-8")

    req = Request(url=url, data=payload, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except HTTPError as e:
        text = e.read().decode("utf-8") if e.fp else ""
        print(f"[ERROR] n8n {method} {path} failed: {e.code} {text}", file=sys.stderr)
        raise
    except URLError as e:
        print(f"[ERROR] n8n {method} {path} failed: {e}", file=sys.stderr)
        raise


def get_existing_workflow_id(base_url: str, api_key: str, name: str) -> str | None:
    payload = n8n_request(base_url, api_key, "GET", "/workflows", query={"limit": 250})
    items = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return None
    for item in items:
        if isinstance(item, dict) and item.get("name") == name:
            wid = item.get("id")
            if wid is not None:
                return str(wid)
    return None


def upsert_workflow(base_url: str, api_key: str, workflow_path: Path, activate: bool) -> None:
    payload = json.loads(workflow_path.read_text(encoding="utf-8"))
    name = payload.get("name", workflow_path.name)

    body = {
        "name": payload.get("name"),
        "nodes": payload.get("nodes", []),
        "connections": payload.get("connections", {}),
        "settings": payload.get("settings", {}),
        "pinData": payload.get("pinData", {}),
        "active": False,
    }

    workflow_id = get_existing_workflow_id(base_url, api_key, name)
    if workflow_id:
        n8n_request(base_url, api_key, "PATCH", f"/workflows/{workflow_id}", body=body)
        print(f"[OK] Updated workflow: {name} ({workflow_id})")
    else:
        created = n8n_request(base_url, api_key, "POST", "/workflows", body=body)
        workflow_id = str(created.get("id"))
        print(f"[OK] Created workflow: {name} ({workflow_id})")

    if activate:
        try:
            n8n_request(base_url, api_key, "PATCH", f"/workflows/{workflow_id}", body={"active": True})
            print(f"[OK] Activated workflow: {name}")
        except Exception:
            n8n_request(base_url, api_key, "POST", f"/workflows/{workflow_id}/activate")
            print(f"[OK] Activated workflow via fallback endpoint: {name}")


def sync_workflows(n8n_base_url: str, n8n_api_key: str, activate: bool) -> None:
    base_url = n8n_base_url.rstrip("/") + "/api/v1"
    for file_name in WORKFLOW_FILES:
        workflow_path = WORKFLOWS_DIR / file_name
        if not workflow_path.exists():
            print(f"[WARN] Workflow file missing: {workflow_path}")
            continue
        upsert_workflow(base_url, n8n_api_key, workflow_path, activate=activate)


def verify_smartbrain_service() -> None:
    service_url = os.getenv("SMARTBRAIN_SERVICE_URL", "").strip()
    if not service_url:
        return

    url = service_url.rstrip("/") + "/health"
    req = Request(url=url, method="GET")
    with urlopen(req, timeout=15) as response:
        raw = response.read().decode("utf-8")
        payload = json.loads(raw) if raw else {}
        status = payload.get("status")
        if status != "ok":
            print(f"[WARN] SmartBrain health unexpected payload: {payload}")
        else:
            print(f"[OK] SmartBrain service healthy: {url}")


def main() -> None:
    db_url = require_env("SMARTBRAIN_SUPABASE_DB_URL")
    n8n_base_url = require_env("SMARTBRAIN_N8N_BASE_URL")
    n8n_api_key = require_env("SMARTBRAIN_N8N_API_KEY")
    activate_flag = env_bool("SMARTBRAIN_N8N_ACTIVATE", True)

    apply_migration(db_url)
    init_settings(db_url)
    sync_workflows(n8n_base_url, n8n_api_key, activate=activate_flag)
    verify_smartbrain_service()
    print("[DONE] SmartBrain bootstrap completed")


if __name__ == "__main__":
    main()
