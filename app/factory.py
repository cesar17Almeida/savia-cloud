"""Flask application factory and composition root.

The ONLY place concrete adapters are chosen: it wires the driven adapters (TTN,
Open-Meteo, LSTM, SQLite repositories) into the application services and hands them
to the HTTP layer. Domain and application code depend on ports (interfaces) only.
"""
from flask import Flask, redirect
from werkzeug.security import check_password_hash, generate_password_hash

from config import Settings

from .adapters.inference.lstm import LstmInference
from .adapters.openmeteo.client import OpenMeteoForecast
from .adapters.repository.db import make_sessionmaker
from .adapters.repository.sqlite import (
    SqlDownlinkLogRepository,
    SqlForecastRepository,
    SqlReadingRepository,
    SqlSessionRepository,
    SqlStationRepository,
    SqlUplinkLogRepository,
    SqlUserRepository,
)
from .adapters.ttn.client import TtnHttpClient
from .application.services import (
    AuthService,
    ConfigDownlinkService,
    DailyCronService,
    IngestUplinkService,
    PanelService,
    RunCloudInferenceService,
    ScheduleDownlinkService,
    Services,
    SignalQueryService,
    StationService,
)
from .interfaces.http.routes import bp as http_bp
from .interfaces.web.routes import bp as web_bp

# Default operator credentials for the web panel; change via /ui/password.
ADMIN_USER = "admin"
ADMIN_DEFAULT_PASSWORD = "admin"


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or Settings.from_env()
    app = Flask(__name__)
    app.config["SETTINGS"] = settings
    app.secret_key = settings.secret_key

    # Persistence.
    sm = make_sessionmaker(settings.db_url)
    users = SqlUserRepository(sm)
    sessions = SqlSessionRepository(sm)
    stations = SqlStationRepository(sm)
    readings = SqlReadingRepository(sm)
    forecasts = SqlForecastRepository(sm)
    dl_log = SqlDownlinkLogRepository(sm)
    ul_log = SqlUplinkLogRepository(sm)

    # Outbound adapters.
    ttn = TtnHttpClient(settings)
    forecast_src = OpenMeteoForecast(settings)
    infer = LstmInference(settings.model_path)

    run_inference = RunCloudInferenceService(readings, forecasts, forecast_src, infer, ttn, dl_log)
    app.config["SERVICES"] = Services(
        auth=AuthService(users, sessions, generate_password_hash,
                         lambda h, p: check_password_hash(h, p)),
        stations=StationService(stations),
        ingest_uplink=IngestUplinkService(stations, readings, ul_log,
                                          settings.default_lat, settings.default_lon),
        signal_query=SignalQueryService(stations),
        schedule_downlink=ScheduleDownlinkService(stations, forecast_src, ttn, dl_log,
                                                  settings.default_lat, settings.default_lon),
        config_downlink=ConfigDownlinkService(ttn, dl_log),
        run_inference=run_inference,
        daily_cron=DailyCronService(stations, run_inference, settings.daily_hour),
        panel=PanelService(stations, readings, forecasts, ul_log, dl_log),
    )

    # Seed the default operator account for the web panel.
    if users.get_by_email(ADMIN_USER) is None:
        users.add(ADMIN_USER, generate_password_hash(ADMIN_DEFAULT_PASSWORD))

    app.register_blueprint(http_bp)
    app.register_blueprint(web_bp)

    @app.get("/")
    def _root():
        return redirect("/ui/")

    return app
