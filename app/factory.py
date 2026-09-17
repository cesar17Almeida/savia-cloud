"""Flask application factory and composition root.

The ONLY place concrete adapters are chosen: it wires the driven adapters (TTN,
Open-Meteo, LSTM, SQL repositories over PostgreSQL) into the application services
and hands them to the HTTP layer. Domain and application code depend on ports (interfaces) only.
"""
from flask import Flask, redirect
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from config import Settings

from .adapters.inference.lstm import LstmInference
from .adapters.openmeteo.client import OpenMeteoForecast
from .adapters.repository.db import make_sessionmaker
from .adapters.repository.sql import (
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
    DEFAULT_ADMIN_PASSWORD,
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

# Operator account of the web panel.
ADMIN_USER = "admin"


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or Settings.from_env()
    app = Flask(__name__)
    app.config["SETTINGS"] = settings
    app.secret_key = settings.secret_key
    # Panel cookie: never sent on cross-site requests (CSRF) nor, behind TLS, in clear.
    app.config.update(SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=settings.cookie_secure)

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
                         lambda h, p: check_password_hash(h, p),
                         allow_registration=settings.allow_registration),
        stations=StationService(stations),
        ingest_uplink=IngestUplinkService(stations, readings, ul_log,
                                          settings.default_lat, settings.default_lon,
                                          ttn=ttn, downlinks=dl_log),
        signal_query=SignalQueryService(stations),
        schedule_downlink=ScheduleDownlinkService(stations, forecast_src, ttn, dl_log,
                                                  settings.default_lat, settings.default_lon),
        config_downlink=ConfigDownlinkService(ttn, dl_log),
        run_inference=run_inference,
        daily_cron=DailyCronService(stations, run_inference, settings.daily_hour,
                                    forecasts=forecasts, log=app.logger.warning),
        panel=PanelService(stations, readings, forecasts, ul_log, dl_log, ttn),
    )

    # Seed the operator; without ADMIN_PASSWORD it must change the default first.
    if users.get_by_email(ADMIN_USER) is None:
        try:
            users.add(ADMIN_USER, generate_password_hash(
                settings.admin_password or DEFAULT_ADMIN_PASSWORD))
        except IntegrityError:
            pass   # another gunicorn worker seeded it first

    app.register_blueprint(http_bp)
    app.register_blueprint(web_bp)

    @app.get("/")
    def _root():
        return redirect("/home/")

    return app
