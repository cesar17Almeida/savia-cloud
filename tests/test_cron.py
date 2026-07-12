"""DailyCronService local-hour gating + the InMemoryStationRepository (kept for
DB-free tests). No interpreter needed: RunCloudInferenceService is faked."""
from app.adapters.repository.memory import InMemoryStationRepository
from app.application.services import DailyCronService
from app.domain.models import Station

NOW = 1782000000   # UTC hour 0


class _FakeRun:
    def __init__(self):
        self.ran = []

    def run(self, station, now_s):
        self.ran.append(station.dev_eui)


def _repo_with_stations():
    repo = InMemoryStationRepository()
    repo.save(Station(dev_eui="LOCAL2H", mode="forward", utc_offset_min=120))   # local hour 2
    repo.save(Station(dev_eui="UTC0", mode="forward", utc_offset_min=0))        # local hour 0
    repo.save(Station(dev_eui="LOCALONLY", mode="local", utc_offset_min=120))   # not forward
    return repo


def test_cron_runs_only_stations_at_their_local_daily_hour():
    repo = _repo_with_stations()
    fake = _FakeRun()
    cron = DailyCronService(repo, fake, daily_hour=2)
    done = cron.run(NOW)
    assert done == ["LOCAL2H"]        # only the +2h station is at local hour 2


def test_cron_force_runs_all_forward_stations():
    repo = _repo_with_stations()
    fake = _FakeRun()
    cron = DailyCronService(repo, fake, daily_hour=9)
    done = cron.run(NOW, force=True)
    assert set(done) == {"LOCAL2H", "UTC0"}   # both forward stations, "local" excluded


def test_memory_repo_list_by_mode():
    repo = _repo_with_stations()
    assert {s.dev_eui for s in repo.list_by_mode("forward")} == {"LOCAL2H", "UTC0"}
    assert [s.dev_eui for s in repo.list_by_mode("local")] == ["LOCALONLY"]
