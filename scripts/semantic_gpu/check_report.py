"""Browser integration checks for the generated local report (no live API)."""

import argparse
import hashlib
import json
from pathlib import Path
import time

from playwright.sync_api import sync_playwright


def check(path, chrome):
    out = path.parent
    errors, network, checks = [], [], []
    started = time.perf_counter()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chrome, headless=True,
                                    args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 1440, "height": 1100}, device_scale_factor=1)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: network.append(request.url) if request.url.startswith(("http:", "https:")) else None)
        page.goto(path.resolve().as_uri(), wait_until="load")
        page.wait_for_function("window.reportDiagnostics !== undefined")
        initial = page.evaluate("reportDiagnostics()")
        assert initial["units"] == initial["visible"] == 5128
        assert initial["documents"] == 3621 and initial["calls"] == 69 and initial["issues"] == 21
        checks.append("full corpus, 5128 aligned units, 3621 documents, 69 calls, 21 issues")
        page.screenshot(path=str(out / "desktop-map.png"), full_page=True)

        page.select_option("#source", "moex_main")
        assert 0 < page.evaluate("reportDiagnostics().visible") < 5128
        assert page.evaluate("S.visible.every(i => doc(i).source === 'moex_main')")
        page.fill("#search", "no-such-news-xyz-895421")
        page.wait_for_timeout(250)
        assert page.evaluate("reportDiagnostics().visible") == 0
        assert "не найдены" in page.locator("#groupList").inner_text()
        page.click("#reset")
        page.fill("#dateFrom", "2026-10-20")
        page.dispatch_event("#dateFrom", "change")
        page.fill("#dateTo", "2026-10-22")
        page.dispatch_event("#dateTo", "change")
        assert page.evaluate("S.visible.every(i => date(i) >= '2026-10-20' && date(i) <= '2026-10-22')")
        assert 0 < page.evaluate("reportDiagnostics().visible") < 5128
        page.click("#reset")
        checks.append("source/text/date filters, no-results state, reset")

        coords = page.evaluate("D.coordinates.tsne[0]")
        page.select_option("#variant", "semantic_time")
        assert page.evaluate("reportDiagnostics().groups") == 1865
        assert page.evaluate("D.coordinates.tsne[0]") == coords
        page.select_option("#projection", "pca")
        assert "11,7%" in page.locator("#projectionMetrics").inner_text()
        page.select_option("#projection", "tsne")
        page.select_option("#variant", "semantic")
        checks.append("semantic/time variants change memberships, not embedding coordinates; PCA/t-SNE switch")

        # Click a nonoverlapped point on the actual canvas, including selection logic.
        point = page.evaluate("""(() => {for (const [x,y,i] of screen) {
          if (x>50&&x<width-50&&y>60&&y<height-60&&i!==S.selected&&nearestScreen(x,y)===i)
            return {x,y,i};} return null;})()""")
        assert point
        page.locator("#plot").click(position={"x": point["x"], "y": point["y"]})
        assert page.evaluate("reportDiagnostics().selected") == point["i"]
        page.click("#projectionHelp")
        assert page.locator("#modal").is_visible()
        assert "Trustworthiness" in page.locator("#modalBody").inner_text()
        page.click("#closeModal")
        checks.append("canvas hit detection, point inspector, projection explanation")

        page.click('[data-tab="quality"]')
        assert page.locator(".issue").count() == 21
        page.select_option("#issuePriority", "P0")
        assert page.locator(".issue").count() == 1
        page.select_option("#issuePriority", "")
        page.locator("#issue-Q01 > summary").click()
        assert "Гипотеза причины" in page.locator("#issue-Q01").text_content()
        page.screenshot(path=str(out / "desktop-quality.png"), full_page=True)
        page.select_option("#reviewFilter", "missing_link")
        assert page.locator(".review-card").count() == 2
        page.select_option("#reviewFilter", "")
        # Click the temporal failure example through its visible case button.
        page.locator('#issue-G01 > summary').click()
        page.locator('#issue-G01 button').first.click()
        assert page.locator("#space").is_visible() and page.locator("#caseNote").is_visible()
        assert page.evaluate("reportDiagnostics().visible") == 3
        assert page.evaluate("new Set(S.visible.map(i=>units[i].groups.semantic_time)).size") == 3
        checks.append("all issues, P0 filter, review labels; mortgage group splits into three with time")

        page.click('[data-tab="api"]')
        assert page.locator("#callTable tr").count() == 69
        assert page.locator("#skippedGroups button").count() == 5
        page.select_option("#callFilter", "length")
        assert page.locator("#callTable tr").count() == 8
        page.select_option("#callFilter", "error")
        assert page.locator("#callTable tr").count() == 2
        page.locator("#callTable button").first.click()
        assert page.locator("#modal").is_visible()
        assert "не получен" in page.locator("#modalBody").inner_text()
        page.click("#closeModal")
        page.select_option("#callFilter", "")
        page.locator("#summaryExamples button").first.click()
        assert page.locator("#space").is_visible()
        ref = page.locator("#inspector .ref").first
        assert ref.count()
        target_prefix = ref.inner_text()
        ref.click()
        assert page.evaluate("units[S.selected].unit_id").startswith(target_prefix)
        assert page.evaluate("S.visibleSet.has(S.selected)")
        page.click('[data-tab="api"]')
        page.screenshot(path=str(out / "desktop-api.png"), full_page=True)
        checks.append("69 attempts, eight output-limit failures, two transport failures; summary citation opens exact fragment")

        page.click('[data-tab="resources"]')
        assert page.locator(".resource-svg polyline").count() == 2
        for phase in ["full-complete", "full-chosen", "api-compact-parallel"]:
            page.select_option("#resourcePhase", phase)
            assert page.locator(".resource-svg polyline").count() == 2
        page.screenshot(path=str(out / "desktop-resources.png"), full_page=True)
        page.click('[data-tab="method"]')
        assert len(page.locator("#fullReport").inner_text()) > 10000
        page.click('[data-tab="space"]')
        page.click("#reset")
        with page.expect_download() as download:
            page.click("#export")
        export = json.loads(Path(download.value.path()).read_text())
        assert len(export["units"]) == 5128
        assert "text" not in export["units"][0]
        checks.append("resource series, full historical protocol, filtered selection export")

        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(200)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(out / "mobile-map.png"), full_page=True)
        page.click('[data-tab="quality"]')
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.locator("#issue-Q01").evaluate("el => el.open = true")
        page.screenshot(path=str(out / "mobile-quality.png"), full_page=True)
        checks.append("390px mobile viewport: no page overflow, map and issues usable")
        # Synthetic untrusted source title: rendering must remain text, never HTML.
        page.evaluate("""(() => { const i=S.selected,d=doc(i),old=d.title;
          d.title='<img src=x onerror="window.__xss=true">';renderInspector();
          window.__unsafeImages=document.querySelectorAll('#inspector img').length;
          d.title=old;renderInspector(); })()""")
        assert page.evaluate("!window.__xss && window.__unsafeImages === 0")
        assert not network and not errors
        checks.append("untrusted source title escaped; zero HTTP requests, zero browser errors")
        version = browser.version
        browser.close()
    result = {"status": "passed", "browser": version, "seconds": time.perf_counter()-started,
              "tested_html_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
              "check_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "checks": checks, "page_errors": errors, "http_requests": network}
    (out / "browser-check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path("data/processed/event_pipeline/interactive-report-20261023/index.html"))
    parser.add_argument("--chrome", default="/usr/bin/google-chrome")
    args = parser.parse_args()
    check(args.report, args.chrome)
