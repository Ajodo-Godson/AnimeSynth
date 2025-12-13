"""Download MIDI files from animezen.net/midis.

It walks the series list at https://animezen.net/midis, opens each series page,
and downloads the .mid/.midi files it finds into one folder per series.

Edit the constants near the top (OUT_DIR, DRY_RUN, etc.) if you want to change
where files go or how fast it runs.
"""

from __future__ import annotations
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable


BASE_URL = "https://animezen.net"
MIDIS_INDEX_URL = f"{BASE_URL}/midis"

# --- simple config (edit these if you want different behavior) ---
OUT_DIR = Path("Input/downloaded_midis")
DRY_RUN = False
RATE_LIMIT_S = 0.4
JITTER_S = 0.2
TIMEOUT_S = 30.0
RETRIES = 2
BACKOFF_S = 1.0
USER_AGENT = "MelodyVAE-MidiScraper/1.0 (+https://animezen.net)"


def _slugify(text: str, max_len: int = 120) -> str:
	text = text.strip().lower()
	text = re.sub(r"\s+", "-", text)
	text = re.sub(r"[^a-z0-9\-_.()]+", "-", text)
	text = re.sub(r"-+", "-", text).strip("-")
	return text[:max_len] or "untitled"


def _safe_filename_from_url(url: str) -> str:
	"""Derive a safe local filename from a MIDI download URL."""
	parsed = urllib.parse.urlparse(url)
	basename = os.path.basename(parsed.path)
	basename = urllib.parse.unquote(basename)
	# Keep extension if present
	root, ext = os.path.splitext(basename)
	ext = ext if ext else ".mid"
	safe_root = _slugify(root)
	return f"{safe_root}{ext.lower()}"


def _request(url: str, user_agent: str, timeout_s: float) -> urllib.request.Request:
	url = normalize_url(url)
	return urllib.request.Request(
		url,
		headers={
			"User-Agent": user_agent,
			"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
		},
		method="GET",
	)


def normalize_url(url: str) -> str:
	"""Return a safe, fully-qualified URL suitable for urllib.

	Some pages include hrefs like "/midis/Ah! My Goddess - Opening.mid" (with
	spaces, apostrophes, etc.). Browsers auto-encode these; urllib requires them
	to be percent-encoded.
	"""
	url = urllib.parse.urljoin(BASE_URL, url)
	parts = urllib.parse.urlsplit(url)
	# Encode path, query, and fragment while keeping URL separators intact.
	safe_path = urllib.parse.quote(urllib.parse.unquote(parts.path), safe="/()!.,;:@-_")
	safe_query = urllib.parse.quote_plus(urllib.parse.unquote_plus(parts.query), safe="=&")
	safe_fragment = urllib.parse.quote(urllib.parse.unquote(parts.fragment), safe="")
	return urllib.parse.urlunsplit((parts.scheme, parts.netloc, safe_path, safe_query, safe_fragment))


def fetch_text(url: str, *, user_agent: str, timeout_s: float) -> str:
	req = _request(url, user_agent, timeout_s)
	with urllib.request.urlopen(req, timeout=timeout_s) as resp:
		data = resp.read()
		# Let Python try to detect via headers; fallback to utf-8.
		encoding = resp.headers.get_content_charset() or "utf-8"
		return data.decode(encoding, errors="replace")


def _sleep_polite(min_delay_s: float, jitter_s: float) -> None:
	if min_delay_s <= 0 and jitter_s <= 0:
		return
	time.sleep(max(0.0, min_delay_s + random.uniform(0.0, max(0.0, jitter_s))))


def parse_categories(index_html: str) -> list[dict[str, str]]:
	"""Parse series/category links from the /midis index page."""
	# The index page contains many links like: href="/midis/evangelion"
	# We avoid matching the direct-download .mid links on series pages.
	hrefs = set(re.findall(r'href=["\']([^"\']+)["\']', index_html, flags=re.IGNORECASE))
	categories: list[dict[str, str]] = []
	for href in sorted(hrefs):
		if not href.startswith("/midis/"):
			continue
		# Skip the index itself and direct MIDI files.
		if href == "/midis":
			continue
		if re.search(r"\.(mid|midi)(\?|$)", href, flags=re.IGNORECASE):
			continue
		url = urllib.parse.urljoin(BASE_URL, href)
		# Start with a readable name from the path; we later prefer the page <h1>.
		name = urllib.parse.unquote(href.split("/midis/", 1)[1]).replace("-", " ").strip() or href
		categories.append({"name": name, "url": url})
	return categories


def parse_midis(category_html: str, *, category_name: str) -> list[dict[str, str]]:
	"""Parse MIDI download links from a series page."""
	# Links look like: https://animezen.net/midis/Evangelion%20-%20Angel%20Attack.mid
	hrefs = re.findall(r'href=["\']([^"\']+)["\']', category_html, flags=re.IGNORECASE)
	midis: list[dict[str, str]] = []
	for href in hrefs:
		if not re.search(r"\.(mid|midi)(\?|$)", href, flags=re.IGNORECASE):
			continue
		url = normalize_url(href)
		title = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(url).path))
		midis.append({"category_name": category_name, "title": title, "url": url})

	# Deduplicate while preserving order.
	seen: set[str] = set()
	unique: list[dict[str, str]] = []
	for m in midis:
		if m["url"] in seen:
			continue
		seen.add(m["url"])
		unique.append(m)
	return unique


def download_file(
	url: str,
	dest_path: Path,
	*,
	user_agent: str,
	timeout_s: float,
	retries: int,
	backoff_s: float,
	dry_run: bool,
) -> tuple[bool, str]:
	"""Download url to dest_path.

	Returns: (downloaded, message)
	"""
	if dest_path.exists():
		return False, "exists"
	if dry_run:
		return False, "dry-run"

	dest_path.parent.mkdir(parents=True, exist_ok=True)
	tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")

	last_err: Exception | None = None
	for attempt in range(retries + 1):
		try:
			req = _request(url, user_agent, timeout_s)
			with urllib.request.urlopen(req, timeout=timeout_s) as resp:
				with tmp_path.open("wb") as out:
					while True:
						chunk = resp.read(1024 * 64)
						if not chunk:
							break
						out.write(chunk)
			tmp_path.replace(dest_path)
			return True, "downloaded"
		except Exception as e:  # noqa: BLE001
			last_err = e
			# best-effort cleanup
			try:
				if tmp_path.exists():
					tmp_path.unlink()
			except Exception:
				pass
			if attempt < retries:
				time.sleep(backoff_s * (2**attempt))
				continue
			break

	return False, f"error: {last_err}"


def iter_selected(items: list, limit: int | None) -> Iterable:
	if limit is None or limit < 0:
		return items
	return items[:limit]



def main() -> int:
	OUT_DIR.mkdir(parents=True, exist_ok=True)

	print(f"Fetching index: {MIDIS_INDEX_URL}")
	index_html = fetch_text(MIDIS_INDEX_URL, user_agent=USER_AGENT, timeout_s=TIMEOUT_S)
	categories = parse_categories(index_html)

	if not categories:
		print("No categories found; site markup may have changed.", file=sys.stderr)
		return 2

	total_downloaded = 0
	total_seen = 0
	for cat in categories:
		_sleep_polite(RATE_LIMIT_S, JITTER_S)
		print(f"\n== Category: {cat['name']} ==")
		cat_html = fetch_text(cat["url"], user_agent=USER_AGENT, timeout_s=TIMEOUT_S)

		# Prefer a cleaner category name from the <h1> title if present.
		m = re.search(r"<h1[^>]*>(.*?)</h1>", cat_html, flags=re.IGNORECASE | re.DOTALL)
		cat_display = cat["name"]
		if m:
			cat_display = re.sub(r"<[^>]+>", "", m.group(1)).strip() or cat_display

		midi_links = parse_midis(cat_html, category_name=cat_display)
		print(f"Found {len(midi_links)} MIDI links")

		cat_folder = OUT_DIR / _slugify(cat_display)
		cat_folder.mkdir(parents=True, exist_ok=True)

		for midi in midi_links:
			total_seen += 1
			filename = _safe_filename_from_url(midi["url"])
			dest = cat_folder / filename

			downloaded, msg = download_file(
				midi["url"],
				dest,
				user_agent=USER_AGENT,
				timeout_s=TIMEOUT_S,
				retries=RETRIES,
				backoff_s=BACKOFF_S,
				dry_run=DRY_RUN,
			)
			if downloaded:
				total_downloaded += 1
			print(f"- {midi['title']} -> {dest} [{msg}]")
			_sleep_polite(RATE_LIMIT_S, JITTER_S)

	print(f"\nDone. Seen: {total_seen}, downloaded: {total_downloaded}, out: {OUT_DIR}")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
