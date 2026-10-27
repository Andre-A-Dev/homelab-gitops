"""Test environment — set before app.config is imported, since config fails
fast on missing values. No real device is ever contacted: upstream clients are
monkeypatched per test."""

import os
import tempfile

_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ.update({
    "PRUSA_HOST": "printer.invalid",
    "PRUSA_PASSWORD": "test",
    "PLUG_HOST": "plug.invalid",
    "CLIMATE_GATEWAY_HOST": "gateway.invalid",
    "CLIMATE_SENSOR_MAC": "00:00:00:00:00:00",
    "GALATEA_API_TOKEN": "test-token",
    "DB_PATH": _DB.name,
})
