"""Capture README screenshots from the running stack (n8n canvases, an execution, the approval e-mail).

    uv run python scripts/screenshots.py

Needs `make up`, at least one completed run of each demo, and an n8n owner account
(created on first visit to http://localhost:5678; the script uses N8N_EMAIL / N8N_PASSWORD).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parents[1] / "docs" / "img"
N8N = "http://localhost:5678"
EMAIL = os.environ.get("N8N_EMAIL", "admin@nordlys.example")
PASSWORD = os.environ.get("N8N_PASSWORD", "LocalAdmin123!")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        # Use a pre-installed Chromium if one is configured (e.g. CI images); else Playwright's own.
        exe = os.environ.get("CHROMIUM_PATH") or (
            "/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None
        )
        browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        page = browser.new_page(viewport={"width": 2000, "height": 1150}, device_scale_factor=1)
        page.goto(f"{N8N}/signin")
        page.fill("input[type=email], input[name=emailOrLdapLoginId], input[name=email]", EMAIL)
        page.fill("input[type=password]", PASSWORD)
        page.keyboard.press("Enter")
        page.wait_for_load_state("networkidle")
        # Workflow ids are deterministic (deploy/n8n/build_workflows.py), so no REST calls are needed.
        wf_dir = Path(__file__).resolve().parents[1] / "deploy" / "n8n" / "workflows"
        ids = {
            "A": json.loads((wf_dir / "access-request-approval.json").read_text())["id"],
            "B": json.loads((wf_dir / "api-registration.json").read_text())["id"],
        }
        for key, filename in (("A", "workflow-a-access-approval.png"), ("B", "workflow-b-api-registration.png")):
            page.goto(f"{N8N}/workflow/{ids[key]}")
            page.wait_for_selector(".vue-flow__node", timeout=30000)
            page.wait_for_timeout(1500)
            page.keyboard.press("Escape")  # dismiss n8n's "production checklist" popover if shown
            for close in page.locator("button[aria-label='Close'], .el-popover .el-icon-close").all():
                if close.is_visible():
                    close.click()
            page.keyboard.press("Shift+1")  # zoom to fit
            page.wait_for_timeout(800)
            page.screenshot(path=str(OUT / filename))
            print("saved", filename)

        # The executions tab opens on the most recent run of workflow A.
        page.goto(f"{N8N}/workflow/{ids['A']}/executions")
        page.wait_for_timeout(5000)
        page.screenshot(path=str(OUT / "workflow-a-execution.png"))
        print("saved workflow-a-execution.png")

        page.set_viewport_size({"width": 1100, "height": 650})
        for query, filename in (
            ("to:bob@nordlys.example", "approval-email.png"),
            ("to:alice@nordlys.example", "requester-notification.png"),
            ("to:api-governance@nordlys.example", "registration-review-email.png"),
        ):
            mails = httpx.get("http://localhost:8025/api/v1/search", params={"query": query}, timeout=10).json()[
                "messages"
            ]
            if mails:
                page.goto(f"http://localhost:8025/view/{mails[0]['ID']}")
                page.wait_for_timeout(1500)
                page.screenshot(path=str(OUT / filename))
                print("saved", filename)
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
