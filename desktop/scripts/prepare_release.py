"""Prepare a complete desktop release and verify GitHub's uploaded asset hashes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil


FORMATS = {'.exe': 'windows-x64-setup.exe', '.msi': 'windows-x64.msi',
           '.deb': 'linux-amd64.deb', '.AppImage': 'linux-amd64.AppImage'}


def installer_set(directory):
    files = {}
    for extension in FORMATS:
        matches = list(Path(directory).rglob('*' + extension))
        if len(matches) != 1 or matches[0].is_symlink() or not matches[0].is_file() or matches[0].stat().st_size == 0:
            raise ValueError(f'Expected exactly one nonempty {extension} installer')
        files[extension] = matches[0]
    return files


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def prepare(artifacts, output):
    source = installer_set(artifacts)  # Validate both OS builds before creating a Release.
    version = json.loads((Path(__file__).parents[1] / 'package.json').read_text())['version']
    sha = os.environ['GITHUB_SHA']
    repository = os.environ['GITHUB_REPOSITORY']
    run_id = os.environ['GITHUB_RUN_ID']
    if not re.fullmatch(r'[a-f0-9]{40}', sha) or not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Invalid source commit or package version')
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', repository) or not run_id.isdigit():
        raise ValueError('Invalid repository or workflow run')
    output.mkdir(parents=True, exist_ok=True)
    assets = []
    for extension, path in source.items():
        destination = output / f'VirtOps-Desktop-{version}-{FORMATS[extension]}'
        shutil.copyfile(path, destination)
        assets.append({'name': destination.name, 'size': destination.stat().st_size, 'sha256': digest(destination)})
    sums = output / 'SHA256SUMS.txt'
    sums.write_text(''.join(f"{item['sha256']}  {item['name']}\n" for item in assets), encoding='utf-8')
    assets.append({'name': sums.name, 'size': sums.stat().st_size, 'sha256': digest(sums)})
    tag = f'desktop-v{version}-preview-{sha[:12]}'
    title = f'VirtOps Desktop {version} 预览版 ({sha[:8]})'
    (output / 'expected-assets.json').write_text(json.dumps(assets, indent=2), encoding='utf-8')
    notes = f'''Windows 用户优先下载 `windows-x64-setup.exe`；MSI 用于需要 MSI 安装的环境。
Linux 提供 Debian/Ubuntu 的 `.deb` 和 `.AppImage`。`SHA256SUMS.txt` 可校验下载完整性。

这是未签名的开发预览版。Windows 本机 ISO 初始化需要专业版/企业版及 Hyper-V，真实安装、首次启动和隔离尚未完成实机验收；Linux 本机目前管理已有 KVM 实例，未提供 ISO 构建向导。

ISO、基础镜像、VM 数据和凭据均不包含在安装包内，也不会上传至 GitHub。

源码提交：`{sha}`
[自动构建与测试](https://github.com/{repository}/actions/runs/{run_id})
'''
    (output / 'RELEASE_NOTES.md').write_text(notes, encoding='utf-8')
    with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8') as stream:
        stream.write(f'tag={tag}\ntitle={title}\n')
    print(f'Prepared {tag}: four installers and SHA256SUMS.txt')


def verify(output):
    expected = json.loads((output / 'expected-assets.json').read_text())
    release = json.loads((output / 'server-release.json').read_text())
    uploaded = {asset['name']: asset for asset in release['assets']}
    if set(uploaded) != {item['name'] for item in expected}:
        raise ValueError('Release asset set differs from the complete installer set')
    for item in expected:
        remote = uploaded[item['name']]
        if remote['state'] != 'uploaded' or remote['size'] != item['size'] or remote.get('digest') != 'sha256:' + item['sha256']:
            raise ValueError(f"Uploaded asset failed size/hash verification: {item['name']}")
    print('Verified all five uploaded assets by size and SHA-256')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'verify'])
    parser.add_argument('--artifacts-dir', type=Path)
    parser.add_argument('--output-dir', type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.action == 'prepare':
        if not arguments.artifacts_dir:
            parser.error('--artifacts-dir is required for prepare')
        prepare(arguments.artifacts_dir, arguments.output_dir)
    else:
        verify(arguments.output_dir)
