"""Application errors. The HTTP layer maps each to a status code; the core stays
framework-free."""
from __future__ import annotations


class AppError(Exception):
    """Base for expected, mappable application errors."""
    status = 400


class EmailTaken(AppError):
    status = 409


class InvalidCredentials(AppError):
    status = 401


class Unauthorized(AppError):
    status = 401


class Forbidden(AppError):
    status = 403


class NotFound(AppError):
    status = 404


class StationClaimed(AppError):
    status = 409


class InsufficientData(AppError):
    status = 422
