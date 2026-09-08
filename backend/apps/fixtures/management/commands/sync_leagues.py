"""
Bootstrap the League rows the rest of the ingest depends on.

`sync_fixtures` iterates leagues that already exist in the database — it never
creates them. So a correct API key with no leagues syncs nothing at all, and the
symptom is an empty slate rather than an error. This is the command that closes
that gap, and it normally runs once per season.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.fixtures.models import League
from apps.fixtures.tasks import DemoDataPresent, sync_leagues


class Command(BaseCommand):
    help = "Create or refresh League rows for the api-ids in API_FOOTBALL_LEAGUES"

    def add_arguments(self, parser):
        parser.add_argument(
            "--force", action="store_true",
            help="Sync even if seeded demo fixtures are present (they share real api-ids).",
        )

    def handle(self, *args, **options):
        configured = settings.API_FOOTBALL_LEAGUES
        self.stdout.write(
            f"Syncing {len(configured)} league(s): {', '.join(map(str, configured))} "
            f"— one request each."
        )

        try:
            synced = sync_leagues(force=options["force"])
        except DemoDataPresent as exc:
            raise CommandError(str(exc)) from exc
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write("")
        self.stdout.write(f"  {'ID':<7}{'LEAGUE':<26}{'COUNTRY':<16}{'SEASON':>7}")
        for league in League.objects.filter(api_id__in=configured).order_by("api_id"):
            self.stdout.write(
                f"  {league.api_id:<7}{league.name[:25]:<26}{league.country[:15]:<16}"
                f"{league.season:>7}"
            )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"{synced} league(s) ready."))
        self.stdout.write("Next: manage.py shell -c \"from apps.fixtures.tasks import "
                          "sync_fixtures; sync_fixtures()\"")
