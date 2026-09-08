"""
Thin API-Football (v3) client.

The free/entry plans are rate-limited hard (requests/minute and /day), so every
call is funnelled through here where it can be counted, cached and backed off in
one place. Responses are stored raw on the model rows — re-deriving a feature
must never cost another request.
"""

import logging
from datetime import date

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)


class ApiFootballError(RuntimeError):
    pass


class ApiFootballClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key or settings.API_FOOTBALL_KEY
        self.base_url = (base_url or settings.API_FOOTBALL_BASE_URL).rstrip("/")
        if not self.api_key:
            raise ApiFootballError("API_FOOTBALL_KEY is not set")

    def _get(self, path: str, **params) -> list[dict]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {"x-apisports-key": self.api_key}
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        payload = resp.json()
        # API-Football returns 200 with an `errors` object rather than an HTTP
        # error for quota and parameter problems — surface those loudly.
        if payload.get("errors"):
            raise ApiFootballError(f"{path}: {payload['errors']}")
        return payload.get("response", [])

    # --- endpoints used by the sync tasks ---------------------------------

    def leagues(self, league_id: int | None = None):
        """
        League metadata including every season and its coverage flags.

        One request per league id. Called rarely — a league's identity and its
        current season change once a year — so this is the cheapest possible
        draw on a 100/day quota.
        """
        return self._get("leagues", **({"id": league_id} if league_id else {}))

    def fixtures_by_date(self, on: date, league_id: int | None = None, season: int | None = None):
        params = {"date": on.isoformat()}
        if league_id:
            params |= {"league": league_id, "season": season}
        return self._get("fixtures", **params)

    def fixture_by_id(self, fixture_id: int):
        return self._get("fixtures", id=fixture_id)

    def team_last_fixtures(self, team_id: int, last: int = 10):
        return self._get("fixtures", team=team_id, last=last)

    def head_to_head(self, home_id: int, away_id: int, last: int = 10):
        return self._get("fixtures/headtohead", h2h=f"{home_id}-{away_id}", last=last)

    def injuries(self, fixture_id: int):
        return self._get("injuries", fixture=fixture_id)

    def odds(self, fixture_id: int):
        return self._get("odds", fixture=fixture_id)

    def standings(self, league_id: int, season: int):
        return self._get("standings", league=league_id, season=season)
