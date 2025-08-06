#!/usr/bin/env python3
"""Download a video file using an existing authenticated browser session.

This script connects to a pre‑opened Chromium instance (address provided
via the ``CHROME_WS_ENDPOINT`` environment variable) in order to reuse the
session cookies already present in the user's browser. The script accepts a
single URL pointing either directly to a video file (e.g. ``.mp4``) or to a
Panopto page. In the latter case the Panopto "DeliveryInfo" endpoint is used
to retrieve the underlying MP4 stream before downloading it.

Example:
    $ export CHROME_WS_ENDPOINT="ws://localhost:9222/devtools/browser/..."
    $ python download.py --url https://univ.example.com/path/to/video.mp4

The file is saved in the current working directory using a name derived from
the URL or the Panopto session title.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import re
import urllib.parse

import requests
from pyppeteer import connect


VIDEO_EXTS = {
    ".mp4",
    ".mov",
    ".mkv",
    ".avi",
    ".ppsm",
    ".pptx",
}


def _sanitize(name: str) -> str:
    """Remove characters that are problematic for file systems."""
    cleaned = re.sub(r"[\\/\\#?%&{}<>:*|\"^~\[\]`]+", "", name).strip()
    return cleaned or "video.mp4"


async def _fetch_info(url: str) -> tuple[str, list[dict], str]:
    """Return (download_url, cookies, filename)."""

    ws_endpoint = os.environ.get("CHROME_WS_ENDPOINT")
    if not ws_endpoint:
        raise RuntimeError("CHROME_WS_ENDPOINT environment variable is required")

    browser = await connect(browserWSEndpoint=ws_endpoint)
    page = await browser.newPage()

    parsed = urllib.parse.urlparse(url)
    is_panopto = "panopto" in parsed.netloc.lower() or "panopto" in parsed.path.lower()

    if is_panopto:
        delivery = page.waitForResponse(lambda r: "DeliveryInfo.aspx" in r.url)
        await page.goto(url)
        response = await delivery
        info = await response.json()
        cookies = await page.cookies()
        await page.close()
        await browser.disconnect()

        mp4_url = info.get("Delivery", {}).get("PodcastStreams", [{}])[0].get("StreamUrl")
        session_name = info.get("Delivery", {}).get("SessionName", "lecture")
        filename = _sanitize(session_name) + ".mp4"
        return mp4_url, cookies, filename

    # Direct download case
    try:
        await page.goto(url)
    except Exception:
        pass  # Many resources trigger navigation aborts
    cookies = await page.cookies()
    await page.close()
    await browser.disconnect()

    ext = pathlib.Path(parsed.path).suffix
    filename = pathlib.Path(parsed.path).name or f"video{ext or '.mp4'}"
    if ext.lower() in VIDEO_EXTS and "forcedownload" not in parsed.query:
        query = urllib.parse.parse_qs(parsed.query)
        query["forcedownload"] = ["1"]
        parsed = parsed._replace(query=urllib.parse.urlencode(query, doseq=True))
        url = urllib.parse.urlunparse(parsed)

    return url, cookies, _sanitize(filename)


def _download(url: str, cookies: list[dict], filename: str) -> None:
    cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    headers = {"Cookie": cookie_header} if cookie_header else {}
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    with open(filename, "wb") as fh:
        fh.write(resp.content)
    print(f"[download] saved to {filename}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Download a video file")
    parser.add_argument("--url", required=True, help="Direct or Panopto video URL")
    args = parser.parse_args()

    download_url, cookies, filename = await _fetch_info(args.url)
    _download(download_url, cookies, filename)


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    asyncio.run(main())

