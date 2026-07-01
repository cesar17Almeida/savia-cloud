# The `app` package. Kept import-light on purpose: create_app lives in
# app/factory.py so importing domain/adapters (e.g. for tests) does not pull in
# Flask. See app/factory.py for the composition root.
