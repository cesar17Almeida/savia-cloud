"""Flask application factory and composition root.

The ONLY place concrete adapters are chosen: it wires the driven adapters (TTN,
Open-Meteo, repository) into the application services and hands them to the HTTP
layer. Domain and application code depend on ports (interfaces) only.
"""
from flask import Flask

from config import Settings

from .adapters.openmeteo.client import OpenMeteoForecast
from .adapters.repository.memory import InMemoryStationRepository
from .adapters.ttn.client import TtnHttpClient
from .application.services import Services
from .interfaces.http.routes import bp as http_bp


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or Settings.from_env()
    app = Flask(__name__)
    app.config["SETTINGS"] = settings

    repo = InMemoryStationRepository()
    ttn = TtnHttpClient(settings)
    forecast = OpenMeteoForecast(settings)
    app.config["SERVICES"] = Services.build(
        repo=repo,
        ttn=ttn,
        forecast=forecast,
        default_lat=settings.default_lat,
        default_lon=settings.default_lon,
    )

    app.register_blueprint(http_bp)
    return app
