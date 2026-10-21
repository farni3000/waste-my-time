from __future__ import annotations

import time
import urllib.robotparser
from collections import defaultdict
from urllib.parse import urlsplit

import httpx

USER_AGENT = "waste-my-time/0.1 (personal local app; contact: local)"
_last_request: dict[str, float] = defaultdict(float)
_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower()


def _robots_for(url: str) -> urllib.robotparser.RobotFileParser | None:
    host = _host(url)
    if host in _robots:
        return _robots[host]
    parser = urllib.robotparser.RobotFileParser()
    robots_url = f"{urlsplit(url).scheme}://{host}/robots.txt"
    try:
        response = httpx.get(
            robots_url,
            timeout=10,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        if response.status_code < 400:
            parser.parse(response.text.splitlines())
        else:
            parser = None
    except Exception:
        parser = None
    _robots[host] = parser
    return parser


def can_fetch(url: str) -> bool:
    parser = _robots_for(url)
    if parser is None:
        return True
    try:
        return parser.can_fetch(USER_AGENT, url)
    except Exception:
        return True


def polite_delay(url: str, seconds: float = 1.0) -> None:
    host = _host(url)
    elapsed = time.monotonic() - _last_request[host]
    if elapsed < seconds:
        time.sleep(seconds - elapsed)
    _last_request[host] = time.monotonic()


def fetch_text(url: str, timeout: float = 20.0) -> str:
    if not can_fetch(url):
        raise PermissionError(f"robots.txt erlaubt keinen Abruf: {url}")
    polite_delay(url)
    response = httpx.get(
        url,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )
    response.raise_for_status()
    return response.text
