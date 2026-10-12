"""Find and launch a real Chromium browser before a long acceptance build."""
import argparse
import json
import os
from pathlib import Path
import shutil

from playwright.sync_api import sync_playwright


def choose_executable(playwright_path, supplied=None):
    configured = supplied or os.environ.get('CAVE_CHROMIUM_EXECUTABLE')
    if configured:
        candidate = shutil.which(configured) or os.path.abspath(configured)
        if not Path(candidate).is_file() or not os.access(candidate, os.X_OK):
            raise ValueError('The supplied Chromium executable is missing or not executable')
        # Keep launcher symlinks intact: resolving /snap/bin/chromium to snap
        # changes argv[0] and prevents its Chromium launcher from working.
        return candidate, 'supplied executable'
    if Path(playwright_path).is_file() and os.access(playwright_path, os.X_OK):
        return playwright_path, 'shared Playwright browser cache'
    for name in ('chromium-browser', 'chromium', 'google-chrome'):
        candidate = shutil.which(name)
        if candidate:
            return candidate, 'installed system browser'
    raise ValueError('No Chromium browser is available. Install it on a supported '
                     'Playwright host, supply --chromium EXECUTABLE, or use the '
                     'documented local-browser handoff instead.')


def check(executable=None, output=None):
    with sync_playwright() as playwright:
        selected, origin = choose_executable(playwright.chromium.executable_path, executable)
        browser = playwright.chromium.launch(executable_path=selected, headless=True,
                                             args=['--no-sandbox', '--disable-dev-shm-usage'])
        try:
            result = {'passed': True, 'executable': selected,
                      'browser_version': browser.version, 'selection': origin,
                      'browser_is_shared_host_dependency': True}
        finally:
            browser.close()
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chromium', help='An existing Chromium/Chrome executable or launcher')
    parser.add_argument('--output', type=Path)
    arguments = parser.parse_args()
    check(arguments.chromium, arguments.output)
