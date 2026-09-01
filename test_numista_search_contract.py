#!/usr/bin/env python3
"""Offline contract regression for Numista catalogue search."""
import importlib
from unittest.mock import patch

b=importlib.import_module("numisvault_backend")


class FakeResponse:
    status_code=200
    text=""

    def json(self):
        return {"types":[{"id":123,"title":"Test coin"}]}


def run():
    captured={}

    def fake_get(url,params=None,timeout=15):
        captured.update(url=url,params=dict(params or {}),timeout=timeout)
        return FakeResponse(),None

    old_key=b.NUMISTA_API_KEY
    b.NUMISTA_API_KEY="configured-for-offline-test"
    try:
        with patch.object(b,"_numista_get_with_backoff",side_effect=fake_get):
            rows,err=b.numista_search("Greece 10 euro 2022",year=2022)
    finally:
        b.NUMISTA_API_KEY=old_key

    assert err is None,err
    assert captured["url"]=="https://api.numista.com/v3/types",captured
    assert captured["params"]["q"]=="Greece 10 euro",captured
    assert str(captured["params"]["year"])=="2022",captured
    assert rows and rows[0]["id"]==123,rows
    print("PASS Numista search uses official GET /v3/types contract")
    return 0


if __name__=="__main__":raise SystemExit(run())
