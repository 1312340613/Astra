import agent.runtime.native_startup as startup


def test_windows_imports_serially(monkeypatch):
    calls = []
    monkeypatch.setattr(startup.sys, "platform", "win32")
    monkeypatch.setattr(startup.importlib, "import_module", calls.append)
    startup.prepare_native_dependencies()
    assert calls == ["numpy", "jiter"]


def test_mac_keeps_lazy_imports(monkeypatch):
    calls = []
    monkeypatch.setattr(startup.sys, "platform", "darwin")
    monkeypatch.setattr(startup.importlib, "import_module", calls.append)
    startup.prepare_native_dependencies()
    assert calls == []


def test_optional_numpy_missing_does_not_skip_jiter(monkeypatch):
    calls = []
    monkeypatch.setattr(startup.sys, "platform", "win32")

    def load(name):
        calls.append(name)
        if name == "numpy":
            raise ImportError("optional")

    monkeypatch.setattr(startup.importlib, "import_module", load)
    startup.prepare_native_dependencies()
    assert calls == ["numpy", "jiter"]
