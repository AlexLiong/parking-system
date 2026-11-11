"""The launcher must refuse a missing explicit scene instead of starting legacy geometry."""
import pytest
from tools.preflight import check

def test_explicit_missing_manifest_stops_startup(monkeypatch,tmp_path):
    monkeypatch.setenv('PARKING_LAYOUT_MANIFEST',str(tmp_path/'absent.json'))
    with pytest.raises(Exception,match='manifest|存在|找到'):
        check()
