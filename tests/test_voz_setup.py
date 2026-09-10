from stt_cli import backends, config
from stt_cli.commands import models, setup


def test_automatic_setup_does_not_select_opt_in_voz():
    assert setup._choose([("voz", backends.Availability(True, "native"))], assume_yes=True) is None


def test_explicit_voz_setup_picks_matching_model_before_saving(monkeypatch):
    saved, installed = [], []
    monkeypatch.setattr(setup, "_check_ffmpeg", lambda: True)
    monkeypatch.setattr(backends, "survey", lambda: [])
    monkeypatch.setattr(setup, "_report_llm", lambda: None)
    monkeypatch.setattr(config, "save_setting", lambda *pair: saved.append(pair))

    def install(engine, model):
        assert not saved, "do not persist a configuration before its model is ready"
        installed.append((engine, model))
        return True

    monkeypatch.setattr(setup, "_install_model", install)
    assert setup.run(["--backend", "voz"]) == 0
    assert installed == [("voz", "voz")]
    assert saved == [("backend", "voz"), ("model", "voz")]


def test_models_pull_leaves_cache_space_checks_to_engine(monkeypatch):
    prepared = []

    class Backend:
        name = "voz"

        async def ensure_model(self, model):
            prepared.append(model)

    monkeypatch.setattr(backends, "resolve", lambda name: Backend())

    def wrong_volume(*args, **kwargs):
        raise AssertionError("the models command does not know the SDK's cache volume")

    monkeypatch.setattr(models.resources, "require_space", wrong_volume)
    monkeypatch.setattr(models.resources, "check_memory", lambda *args, **kwargs: [])
    assert models._pull("voz", "voz") == 0
    assert prepared == ["voz"]
