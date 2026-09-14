"""Exercise build metadata generation without compiling, signing or installing."""
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path


def test_build_manifest_records_build_inputs_before_signing(tmp_path):
    script = (Path(__file__).resolve().parents[1] / 'scripts/build_macos_computer_helper.sh').read_text()
    marker = "<<'BUILD_INFO_PY'\n"
    assert marker in script
    code = script.split(marker, 1)[1].split('\nBUILD_INFO_PY', 1)[0]
    assert script.index(marker) < script.index('codesign --force')
    source = tmp_path / 'repo'
    source.mkdir()
    subprocess.run(['git', 'init', '-q', str(source)], check=True)
    subprocess.run(['git', '-C', str(source), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-qm', 'fixture'], check=True)
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    registry = tmp_path / 'registry.json'
    registry.write_text('{"cells": []}')
    output = tmp_path / 'build-info.json'
    def generate():
        subprocess.run([sys.executable, '-', str(source), str(registry), str(output)], input=code, text=True, check=True)
        return json.loads(output.read_text())
    first = generate()
    assert first['helper_git_revision'] == revision
    assert first['helper_source_dirty'] is False
    assert first['compatibility_registry_sha256'] == hashlib.sha256(registry.read_bytes()).hexdigest()
    assert re.fullmatch(r'[0-9a-f-]{36}', first['build_id'])
    (source / 'uncommitted.swift').write_text('private source content')
    second = generate()
    assert second['helper_source_dirty'] is True
    assert first['build_id'] != second['build_id']
    assert 'private source content' not in output.read_text()
