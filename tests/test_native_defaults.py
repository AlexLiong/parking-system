"""Direct native launch must not inherit the course demo configuration."""
import sys
import pytest
from tools.frozen_entry import main
from server.core import config
from tools import preflight


@pytest.mark.parametrize('explicit', [False, True])
def test_native_configuration_default_and_explicit_override(monkeypatch, tmp_path, explicit):
    app = tmp_path / 'parking-system'
    app.mkdir()
    (app / 'config.yaml').write_text('server:\n  demo_mode: true\n')
    (app / 'config.production.yaml').write_text('server:\n  demo_mode: false\n')
    override = tmp_path / 'custom.yaml'
    override.write_text('server:\n  demo_mode: true\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(app / 'parking'))
    monkeypatch.setattr(sys, 'argv', ['parking', '--check'])
    monkeypatch.setattr(config, '_config', None)
    monkeypatch.setattr(config, '_config_path', None)
    # Register the original environment before main creates a default itself.
    monkeypatch.setenv('PARKING_CONFIG', '')
    if explicit:
        monkeypatch.setenv('PARKING_CONFIG', str(override))
    else:
        monkeypatch.delenv('PARKING_CONFIG', raising=False)
    observed = []

    def inspect_configuration():
        observed.append(config.load_config(force=True).server.demo_mode)
        return {'demo_mode': observed[-1]}

    monkeypatch.setattr(preflight, 'check', inspect_configuration)
    main()
    assert observed == [explicit]
