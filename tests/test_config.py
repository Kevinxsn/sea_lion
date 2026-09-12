from sea_lion import config as C


def test_env_substitution_with_defaults(monkeypatch):
    monkeypatch.delenv("SEA_LION_LOCAL_LLM_URL", raising=False)
    monkeypatch.setenv("SEA_LION_LOCAL_LLM_MODEL", "custom-model")
    cfg = C.load(mode="shadow")
    assert cfg.ai.cheap.base_url == "http://127.0.0.1:8324/v1"     # default kicks in
    assert cfg.ai.cheap.model == "custom-model"                       # env wins when set
    assert C._sub_env("${NOPE_X:-fallback}/v1") == "fallback/v1"
    assert C._sub_env("${NOPE_Y}") == ""


def test_paper_and_live_use_separate_databases(tmp_path):
    cfg = C.load(mode="paper"); cfg.runtime_dir = str(tmp_path)
    live = C.load(mode="live"); live.runtime_dir = str(tmp_path)
    assert cfg.db_path != live.db_path
