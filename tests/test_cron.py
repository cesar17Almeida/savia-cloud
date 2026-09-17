"""DailyCronService local-hour gating + the InMemoryStationRepository (kept for
DB-free tests). No interpreter needed: RunCloudInferenceService is faked."""
from app.adapters.repository.memory import InMemoryStationRepository
from app.application.errors import InsufficientData
from app.application.services import DailyCronService
from app.domain.models import ForecastRun, Station

NOW = 1782000000   # UTC hour 0


class _Forecasts:
    """Latest stored run per station (the only query the cron needs)."""

    def __init__(self):
        self.last = {}

    def latest_run(self, dev_eui):
        ts = self.last.get(dev_eui)
        return ForecastRun(dev_eui, ts, [0.3] * 24) if ts is not None else None


class _FakeRun:
    def __init__(self, forecasts=None, fail=None):
        self.ran = []
        self._forecasts = forecasts
        self._fail = fail or {}

    def run(self, station, now_s):
        if station.dev_eui in self._fail:
            raise self._fail[station.dev_eui]
        self.ran.append(station.dev_eui)
        if self._forecasts is not None:
            self._forecasts.last[station.dev_eui] = now_s


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
    report = cron.run(NOW)
    assert report.ran == ["LOCAL2H"]  # only the +2h station is at local hour 2


def test_cron_force_runs_all_forward_stations():
    repo = _repo_with_stations()
    fake = _FakeRun()
    cron = DailyCronService(repo, fake, daily_hour=9)
    report = cron.run(NOW, force=True)
    assert set(report.ran) == {"LOCAL2H", "UTC0"}   # both forward, "local" excluded


def test_memory_repo_list_by_mode():
    repo = _repo_with_stations()
    assert {s.dev_eui for s in repo.list_by_mode("forward")} == {"LOCAL2H", "UTC0"}
    assert [s.dev_eui for s in repo.list_by_mode("local")] == ["LOCALONLY"]


def test_one_failing_station_does_not_stop_the_rest():
    logged = []
    fake = _FakeRun(fail={"LOCAL2H": RuntimeError("Open-Meteo down")})
    cron = DailyCronService(_repo_with_stations(), fake, daily_hour=9, log=logged.append)
    report = cron.run(NOW, force=True)
    assert report.ran == ["UTC0"]
    assert "Open-Meteo down" in report.failed["LOCAL2H"]
    assert logged and "LOCAL2H" in logged[0]


def test_insufficient_data_is_a_skip_not_a_failure():
    fake = _FakeRun(fail={"UTC0": InsufficientData("only 3 real soil hours")})
    report = DailyCronService(_repo_with_stations(), fake, daily_hour=0).run(NOW)
    assert report.ran == [] and not report.failed
    assert "3 real soil hours" in report.skipped["UTC0"]


def test_a_repeated_tick_in_the_same_slot_does_not_rerun():
    """A retried or duplicated cron call must not push a second TIME_TA."""
    forecasts = _Forecasts()
    fake = _FakeRun(forecasts)
    cron = DailyCronService(_repo_with_stations(), fake, daily_hour=2, forecasts=forecasts)
    assert cron.run(NOW).ran == ["LOCAL2H"]
    again = cron.run(NOW + 600)                       # same local hour
    assert again.ran == [] and "LOCAL2H" in again.skipped
    assert cron.run(NOW + 86400).ran == ["LOCAL2H"]   # next day runs again
    assert "LOCAL2H" in cron.run(NOW + 86400 + 60, force=True).ran
