"""
Resolve league names to API-Football ids.

`API_FOOTBALL_LEAGUES` is a list of numeric ids, but nobody thinks in ids — they
think "Brazilian Serie A". Looking fifty of those up by hand through the API
docs is an afternoon; this does it in one request, because /leagues with no
filter returns every league the plan can see.

    manage.py find_leagues --top50
    manage.py find_leagues "Eredivisie" "Liga MX"
"""

import difflib

from django.core.management.base import BaseCommand, CommandError

from apps.fixtures.providers.api_football import ApiFootballClient, ApiFootballError

# The Opta "strongest leagues" list, as names to match against the provider's
# own naming. Kept here rather than in settings because it is a lookup input,
# not configuration — the output is what goes in the env file.
TOP_50 = [
    ("Premier League", "England"), ("La Liga", "Spain"), ("Bundesliga", "Germany"),
    ("Serie A", "Italy"), ("Ligue 1", "France"), ("Serie A", "Brazil"),
    ("Liga Profesional Argentina", "Argentina"), ("Jupiler Pro League", "Belgium"),
    ("Primeira Liga", "Portugal"), ("Championship", "England"),
    ("Ekstraklasa", "Poland"), ("Superliga", "Denmark"),
    ("Primera A", "Colombia"), ("J1 League", "Japan"), ("Eliteserien", "Norway"),
    ("Liga Pro", "Ecuador"), ("Super League", "Switzerland"), ("HNL", "Croatia"),
    ("Major League Soccer", "USA"), ("Primera Division", "Paraguay"),
    ("Liga MX", "Mexico"), ("Eredivisie", "Netherlands"), ("Segunda División", "Spain"),
    ("Czech Liga", "Czech-Republic"), ("Super Lig", "Turkey"),
    ("Allsvenskan", "Sweden"), ("Premier League", "Russia"), ("2. Bundesliga", "Germany"),
    ("Primera División", "Chile"), ("Super League 1", "Greece"),
    ("K League 1", "South-Korea"), ("Primera División", "Uruguay"),
    ("NB I", "Hungary"), ("Bundesliga", "Austria"), ("First Division", "Cyprus"),
    ("Serie B", "Italy"), ("Primera Nacional", "Argentina"), ("Liga I", "Romania"),
    ("Primera División", "Bolivia"), ("Ligue 2", "France"),
    ("Premiership", "Scotland"), ("Pro League", "Saudi-Arabia"),
    ("Division Profesional", "Bolivia"), ("Liga 1", "Peru"), ("Ligue 1", "Algeria"),
    ("Primera División", "Venezuela"), ("League One", "England"),
    ("Premier League", "Egypt"), ("Super Liga", "Slovakia"), ("A-League", "Australia"),
]

# Cups are excluded from --top50 because they have no table and thin rating
# history. But a midweek cup round is most of what is played that night, and a
# slate that goes empty on cup nights looks broken however defensible the reason.
MAJOR_CUPS = [
    ("UEFA Champions League", "World"), ("UEFA Europa League", "World"),
    ("UEFA Europa Conference League", "World"),
    ("FA Cup", "England"), ("League Cup", "England"),
    ("Copa del Rey", "Spain"), ("Coppa Italia", "Italy"),
    ("DFB Pokal", "Germany"), ("Coupe de France", "France"),
    ("Copa Libertadores", "World"), ("Copa Sudamericana", "World"),
    ("Copa Do Brasil", "Brazil"), ("Copa Argentina", "Argentina"),
]


class Command(BaseCommand):
    help = "Look up API-Football league ids by name, ready for API_FOOTBALL_LEAGUES"

    def add_arguments(self, parser):
        parser.add_argument("names", nargs="*", help="League names to search for.")
        parser.add_argument("--top50", action="store_true",
                            help="Resolve the Opta top-50 list built into this command.")
        parser.add_argument("--include-cups", action="store_true",
                            help="Add the major cup competitions. A midweek cup round "
                                 "is most of what is played that night.")
        parser.add_argument("--type", default="League",
                            help="League or Cup (default League) — Cup competitions "
                                 "have no table and thinner rating history.")

    def handle(self, *args, **options):
        wanted = TOP_50 if options["top50"] else [(n, None) for n in options["names"]]
        cups = MAJOR_CUPS if options["include_cups"] else []
        if not wanted and not cups:
            raise CommandError("Pass league names, --top50, or --include-cups.")

        self.stdout.write("Fetching the full league list (1 request)…")
        try:
            everything = ApiFootballClient().leagues()
        except ApiFootballError as exc:
            raise CommandError(str(exc)) from exc

        # With cups requested the type filter has to admit both, so matching is
        # done against everything and the type is checked per entry instead.
        allowed = {options["type"]} if options["type"] else set()
        if cups:
            allowed.add("Cup")

        index = {}
        for node in everything:
            league, country = node["league"], (node.get("country") or {})
            if allowed and league.get("type") not in allowed:
                continue
            index[(league["name"].lower(), (country.get("name") or "").lower())] = (
                league["id"], league["name"], country.get("name") or "",
            )

        self.stdout.write(f"  {len(index)} {options['type']} competitions available\n")
        self.stdout.write(f"  {'ID':<7}{'LEAGUE':<34}{'COUNTRY':<20}")
        self.stdout.write(f"  {'-' * 60}")

        found, missing = [], []
        for name, country in list(wanted) + list(cups):
            hit = self._match(index, name, country)
            if hit is None:
                missing.append(f"{name} ({country})" if country else name)
                continue
            league_id, real_name, real_country = hit
            found.append(league_id)
            self.stdout.write(f"  {league_id:<7}{real_name[:33]:<34}{real_country[:19]:<20}")

        self.stdout.write("")
        if missing:
            # Named rather than silently dropped: a league missing from the list
            # is one the plan cannot see or one the provider names differently,
            # and both are worth knowing before you trust the output.
            self.stdout.write(self.style.WARNING(
                f"  Not matched ({len(missing)}): {', '.join(missing)}"
            ))
            self.stdout.write(
                "  Search for them individually — the provider's naming often differs.\n"
            )

        self.stdout.write(self.style.SUCCESS(f"  Resolved {len(found)} leagues.\n"))
        self.stdout.write("  Paste into backend/.env:\n")
        self.stdout.write(f"  API_FOOTBALL_LEAGUES={','.join(str(i) for i in found)}\n")
        self._report_cost(len(found))

    @staticmethod
    def _match(index, name, country):
        key = (name.lower(), (country or "").lower())
        if key in index:
            return index[key]

        # Same country, near-enough name: providers write "Primera División" and
        # "Liga Profesional" for the same competition in different seasons.
        if country:
            same_country = {n: v for (n, c), v in index.items() if c == country.lower()}
            close = difflib.get_close_matches(name.lower(), same_country, n=1, cutoff=0.6)
            if close:
                return same_country[close[0]]
            return None

        names = {n: v for (n, _c), v in index.items()}
        close = difflib.get_close_matches(name.lower(), names, n=1, cutoff=0.7)
        return names[close[0]] if close else None

    def _report_cost(self, leagues: int) -> None:
        """Breadth is cheap in code and not free in requests or tokens."""
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("  What this costs per day"))
        fixtures_sync = leagues * 4 * 2          # (days_ahead+1) x syncs per day
        busy_fixtures = leagues * 2              # rough: 2 fixtures/league on a busy day
        odds = busy_fixtures * 2                 # odds + injuries per fixture
        self.stdout.write(f"    fixture sync      {fixtures_sync:>5} requests")
        self.stdout.write(f"    odds + injuries   {odds:>5} requests (busy day)")
        self.stdout.write(f"    total             {fixtures_sync + odds:>5} / 7,500 on PRO")
        self.stdout.write(f"    one-off backfill  {leagues * 3:>5} requests (3 seasons)")
        self.stdout.write("")
        self.stdout.write(f"    LLM write-ups     ~{busy_fixtures * 3} per day, "
                          f"about ${busy_fixtures * 3 * 0.0024:.2f} batched")
